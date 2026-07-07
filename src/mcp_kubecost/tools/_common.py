"""Shared building blocks for FastMCP tools.

This module lifts the error-handling, response-validation, and input-safety
pattern that was originally scoped to ``cost_usage_reporting.py`` into one place
so every tool can share it. It implements the cross-cutting parts of the MCP
best practices:

* Rule #6  — typed, structured output via :class:`BaseToolResponse`.
* Rule #10 — :func:`safe_path_segment` keeps untrusted input out of URL paths.
* Rule #12 — :func:`raise_tool_error` / :func:`call_get_api` produce structured,
  actionable errors and never leak raw upstream bodies.
* Rule #13 — :func:`summarize_exception` yields a low-PII error summary suitable
  for logging and for ``skipped`` entries in fan-out tools.
* Rule #16 — :class:`QueryStatus` lets a tool distinguish "no results" from
  "query failed" / "data unavailable".
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any, NoReturn

from fastmcp.exceptions import ToolError as McpToolError
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from mcp_kubecost.client import KubecostClientError, get, post
from mcp_kubecost.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

# Re-export so callers that need to catch McpToolError can import it from here
# rather than depending on fastmcp.exceptions directly.
__all__ = [
    "BaseToolResponse",
    "McpToolError",
    "QueryStatus",
    "call_get_api",
    "call_post_api",
    "extract_list",
    "format_tool_error",
    "raise_tool_error",
    "safe_path_segment",
    "summarize_exception",
    "validate_response",
]

# Cap message size so a misbehaving upstream cannot blow up token usage.
_MAX_ERROR_MESSAGE_CHARS = 500


# ---------------------------------------------------------------------------
# Structured response envelope (rules #6, #16)
# ---------------------------------------------------------------------------


class QueryStatus(StrEnum):
    """Outcome of a tool query, so the LLM never has to guess from an empty list."""

    OK = "ok"
    """Results were found and returned."""
    EMPTY = "empty"
    """The query succeeded but matched no records."""
    PARTIAL = "partial"
    """Some endpoints/sources succeeded and others failed (see ``skipped``)."""
    ERROR = "error"
    """The query could not be completed; see ``message`` / ``recommended_action``."""


class BaseToolResponse(BaseModel):
    """Common envelope every tool response inherits.

    Concrete responses add their own typed payload fields (e.g. ``policies``)
    alongside these so the LLM always gets a machine-readable status and, where
    relevant, a next step.
    """

    status: QueryStatus = Field(description="Outcome of the query: ok, empty, partial, or error.")
    message: str = Field(
        default="",
        description="Human/LLM-readable explanation of the status — especially "
        "useful when status is empty, partial, or error.",
    )
    recommended_action: str | None = Field(
        default=None,
        description="Suggested next step for the caller, when one applies "
        "(e.g. which tool to call next, or how to broaden the query).",
    )


# ---------------------------------------------------------------------------
# Structured errors (rules #12, #13)
# ---------------------------------------------------------------------------


def format_tool_error(err: ToolError) -> str:
    """Render a structured :class:`ToolError` as a single LLM-readable line.

    The MCP protocol carries a raised error's message as a plain string, so we
    format the structured fields deterministically rather than embedding JSON.
    """
    msg = (
        f"[{err.code.value}] {err.message} "
        f"(retryable={'true' if err.retryable else 'false'}) "
        f"Action: {err.suggested_action}"
    )
    if len(msg) > _MAX_ERROR_MESSAGE_CHARS:
        msg = msg[: _MAX_ERROR_MESSAGE_CHARS - 3] + "..."
    return msg


def raise_tool_error(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool,
    suggested_action: str,
) -> NoReturn:
    """Raise a FastMCP ``ToolError`` carrying a structured, actionable message.

    Using FastMCP's ``ToolError`` is the documented escape hatch that delivers
    our message to the LLM unmodified regardless of ``mask_error_details``.
    """
    err = ToolError(
        code=code,
        message=message,
        retryable=retryable,
        suggested_action=suggested_action,
    )
    raise McpToolError(format_tool_error(err))


def summarize_exception(exc: Exception) -> dict[str, Any]:
    """Map an exception to a low-PII ``{code, message, retryable}`` summary.

    Never returns the raw upstream HTTP body or ``str(exc)`` for unknown errors
    — that is the leak this replaces (rule #13). Used for ``skipped`` entries in
    fan-out tools where one endpoint failing should not abort the whole call.
    """
    if isinstance(exc, KubecostClientError):
        te = exc.to_tool_error()
        # Include the HTTP status (useful signal) but never the raw response body.
        return {
            "code": te.code.value,
            "message": f"HTTP {exc.status_code}: {te.message}",
            "retryable": te.retryable,
        }
    if isinstance(exc, ValueError):
        # ValueErrors in this codebase are our own validation messages, which are
        # safe to surface (they describe the constraint, not upstream data).
        return {
            "code": ErrorCode.INVALID_INPUT.value,
            "message": str(exc)[:200],
            "retryable": False,
        }
    return {
        "code": ErrorCode.DATA_UNAVAILABLE.value,
        "message": f"Unexpected error: {type(exc).__name__}.",
        "retryable": True,
    }


# ---------------------------------------------------------------------------
# API call wrappers (rules #12, #13)
# ---------------------------------------------------------------------------


def _handle_call_failure(exc: Exception, path: str) -> NoReturn:
    if isinstance(exc, KubecostClientError):
        te = exc.to_tool_error()
        logger.warning("Kubecost API error at %s: [%s]", path, te.code.value)
        raise McpToolError(format_tool_error(te)) from exc
    if isinstance(exc, ValueError) and "No authentication configured" in str(exc):
        logger.warning("Kubecost API call attempted without auth configuration")
        raise_tool_error(
            ErrorCode.CONFIGURATION_ERROR,
            str(exc),
            retryable=False,
            suggested_action=("Set KUBECOST_API_KEY or KUBECOST_OPEN_TOKEN in the environment."),
        )
    logger.exception("Unexpected error calling Kubecost API path %s", path)
    raise_tool_error(
        ErrorCode.DATA_UNAVAILABLE,
        f"Unexpected error contacting the Kubecost API: {type(exc).__name__}",
        retryable=True,
        suggested_action=(
            "Retry the request. If the failure persists, the upstream service may be returning a malformed response."
        ),
    )


async def call_get_api(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET wrapper that converts known failures into structured ``ToolError``s."""
    try:
        if params is None:
            return await get(path)
        return await get(path, params=params)
    except McpToolError:
        raise
    except Exception as exc:  # noqa: BLE001 — mapped to structured error below
        _handle_call_failure(exc, path)


async def call_post_api(
    path: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """POST wrapper that converts known failures into structured ``ToolError``s."""
    try:
        return await post(path, json=json, params=params)
    except McpToolError:
        raise
    except Exception as exc:  # noqa: BLE001 — mapped to structured error below
        _handle_call_failure(exc, path)


def validate_response(model: type[BaseModel], data: Any) -> Any:
    """Validate raw JSON against a response model, mapping failures to MCP errors.

    FastMCP re-raises ``pydantic.ValidationError`` as-is, so without this the LLM
    receives Pydantic's multi-line dump. Wrap every ``model_validate`` here.
    """
    try:
        return model.model_validate(data)
    except McpToolError:
        raise
    except PydanticValidationError:
        logger.warning("Response failed %s validation", model.__name__)
        raise_tool_error(
            ErrorCode.DATA_UNAVAILABLE,
            f"The API returned an unexpected response shape that does not match {model.__name__}.",
            retryable=False,
            suggested_action=(
                "Retry the request. If it persists, the API contract may have changed and the tool needs updating."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error validating response against %s", model.__name__)
        raise_tool_error(
            ErrorCode.DATA_UNAVAILABLE,
            f"Unexpected error parsing the API response: {type(exc).__name__}",
            retryable=True,
            suggested_action="Retry the request.",
        )


# ---------------------------------------------------------------------------
# Input safety (rule #10)
# ---------------------------------------------------------------------------


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def extract_list(data: Any, *extra_keys: str) -> list[dict[str, Any]]:
    """Normalize common API response shapes to a plain list of dicts.

    Tries, in order:
    1. ``data`` itself if it is already a list.
    2. ``data[key]`` for each caller-supplied ``extra_keys`` (domain-specific).
    3. ``data["result"]``, ``data["results"]``, ``data["data"]`` (generic fallbacks).

    Non-dict items inside the list are silently dropped. Returns ``[]`` if no
    list can be found.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in (*extra_keys, "result", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def safe_path_segment(value: str, field_name: str) -> str:
    """Validate a value before interpolating it into an upstream URL path.

    ``httpx`` does not percent-encode path segments built via f-strings, so a
    value like ``../../internal/secrets`` would traverse the API. Reject anything
    outside ``[A-Za-z0-9._-]`` or containing ``..`` (rule #10).
    """
    if not value or value != value.strip() or ".." in value or not _SAFE_SEGMENT.match(value):
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            f"Invalid {field_name}: must contain only letters, digits, '.', '_', "
            f"or '-' and must not contain path separators.",
            retryable=False,
            suggested_action=(f"Provide a valid {field_name}, e.g. an id returned by a list_* tool."),
        )
    return value
