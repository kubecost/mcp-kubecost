"""Shared building blocks for FastMCP tools.

This module lifts the error-handling, response-validation, and input-safety
pattern that was originally scoped to ``cost_usage_reporting.py`` into one place
so every tool can share it. It implements the cross-cutting parts of the MCP
best practices:

* Rule #6  — typed, structured output via :class:`BaseToolResponse`.
* Rule #10 — :func:`safe_path_segment` keeps untrusted input out of URL paths.
* Rule #12 — :func:`raise_tool_error` / :func:`call_get_api` produce structured,
  actionable errors. Truncated Kubecost product messages are included for 4xx
  responses; HTML error pages are not.
* Rule #13 — :func:`summarize_exception` yields a low-PII error summary suitable
  for logging and for ``skipped`` entries in fan-out tools.
* Rule #16 — :class:`QueryStatus` lets a tool distinguish "no results" from
  "query failed" / "data unavailable".
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, NoReturn

import httpx
from fastmcp.exceptions import ToolError as McpToolError
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from mcp_kubecost.auth import MissingClientApiKeyError
from mcp_kubecost.client import KubecostClientError, get, post
from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

# Re-export so callers that need to catch McpToolError can import it from here
# rather than depending on fastmcp.exceptions directly.
__all__ = [
    "DEFAULT_WINDOW",
    "MIN_QUANTILE_WINDOW",
    "BaseToolResponse",
    "McpToolError",
    "QueryStatus",
    "ResolvedWindow",
    "call_get_api",
    "call_post_api",
    "extract_list",
    "format_tool_error",
    "mcp_error_response_fields",
    "normalize_window_order",
    "parse_api_timestamp",
    "parse_window_days",
    "raise_tool_error",
    "resolve_window",
    "resolved_window_from_api",
    "safe_path_segment",
    "summarize_exception",
    "to_api_window",
    "validate_response",
]

# Default query window used by all tools unless the caller overrides it.
DEFAULT_WINDOW: str = get_settings().default_window

# Quantile algorithms (quantileOfAverages, quantileOfMaxes) require enough
# data points for a meaningful distribution. Enforce at least this many days.
MIN_QUANTILE_WINDOW: str = "15d"

# Cap message size so a misbehaving upstream cannot blow up token usage.
_MAX_ERROR_MESSAGE_CHARS = 500

# Cap the text-content summary for the same reason. Roomier than an error line
# because a summary may carry warnings and a next step alongside ``message``.
_MAX_SUMMARY_CHARS = 1500

# Named windows with a fixed, unambiguous day count.
_NAMED_WINDOW_DAYS: dict[str, int] = {
    "1d": 1,
    "3d": 3,
    "7d": 7,
    "15d": 15,
    "30d": 30,
    "60d": 60,
    "90d": 90,
}


def parse_window_days(window: str) -> int | None:
    """Return the number of days represented by *window*, or ``None`` if unknown.

    Handles simple ``"Nd"`` patterns (e.g. ``"7d"``, ``"15d"``).  Returns
    ``None`` for calendar aliases (``"month"``, ``"week"``, etc.) and RFC3339
    ranges — these cannot be reliably compared without a reference timestamp and
    are assumed to satisfy any minimum-day requirement.
    """
    lower = window.strip().lower()
    if lower in _NAMED_WINDOW_DAYS:
        return _NAMED_WINDOW_DAYS[lower]
    # Support arbitrary "Nd" patterns not in the lookup table.
    if lower.endswith("d") and lower[:-1].isdigit():
        return int(lower[:-1])
    return None


class ResolvedWindow(BaseModel):
    """A window converted to concrete UTC boundaries for display and reasoning."""

    start_utc: datetime = Field(description="Inclusive UTC start timestamp of the window.")
    end_utc: datetime = Field(description="Exclusive UTC end timestamp of the window.")
    display_start: str = Field(description="Start date formatted as YYYY-MM-DD.")
    display_end: str = Field(description="End date formatted as YYYY-MM-DD.")
    display: str = Field(description="Human-readable date range and day count.")
    days: int = Field(description="Number of 24-hour days covered by the window.")
    is_partial: bool = Field(
        description=(
            "True when the period is still in progress; the day count reflects elapsed days only "
            "and today's costs are incomplete."
        )
    )
    source_expression: str = Field(description="Original window expression supplied by the caller.")


def _to_display_date(dt: datetime) -> str:
    """Format a timestamp as an ISO ``YYYY-MM-DD`` date."""
    return dt.strftime("%Y-%m-%d")


def _start_of_utc_day() -> datetime:
    """Return midnight UTC of the current day."""
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _window_result(
    start: datetime,
    end: datetime,
    source_expression: str,
    *,
    allow_partial_day: bool = False,
) -> ResolvedWindow:
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    span_seconds = (end - start).total_seconds()
    if span_seconds <= 0 or (not allow_partial_day and span_seconds % 86400):
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            f"Window '{source_expression}' must span a positive whole number of days.",
            retryable=False,
            suggested_action="Provide a supported named window or an RFC3339 range with whole-day UTC boundaries.",
        )
    display_start = _to_display_date(start)
    display_end = _to_display_date(end - timedelta(microseconds=1))
    # Count the calendar days the window touches rather than dividing the span,
    # so the day count can never disagree with the printed range and an
    # in-progress end (Kubecost returns 'now' for 'week'/'month') still counts
    # today as a day of data.
    days = ((end - timedelta(microseconds=1)).date() - start.date()).days + 1
    # Any window whose end extends past midnight today includes the in-progress
    # current day, so its cost data is not yet final.
    is_partial = end > _start_of_utc_day()
    unit = "day" if days == 1 else "days"
    qualifier = ", partial" if is_partial else ""
    span = display_start if days == 1 else f"{display_start} to {display_end}"
    display = f"{span} ({days} {unit}{qualifier})"
    return ResolvedWindow(
        start_utc=start,
        end_utc=end,
        display_start=display_start,
        display_end=display_end,
        display=display,
        days=days,
        is_partial=is_partial,
        source_expression=source_expression,
    )


def parse_api_timestamp(value: Any) -> datetime | None:
    """Parse an RFC3339 timestamp returned by Kubecost, or ``None`` if unusable.

    Accepts the ``Z`` suffix Kubecost uses and normalizes naive timestamps to UTC
    so callers can compare results from different responses directly.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def normalize_window_order(window: str) -> str:
    """Return an explicit two-timestamp window in chronological order.

    Only otherwise-parseable ``start,end`` expressions are changed. Named
    windows, malformed ranges, and equal boundaries pass through so their
    existing resolution or validation paths retain responsibility for them.
    """
    parts = [part.strip() for part in window.split(",")]
    if len(parts) != 2 or not all(parts):
        return window

    first = parse_api_timestamp(parts[0])
    second = parse_api_timestamp(parts[1])
    if first is None or second is None or first <= second:
        return window
    return f"{parts[1]},{parts[0]}"


def resolved_window_from_api(window: Any, source_expression: str) -> ResolvedWindow | None:
    """Build a ``ResolvedWindow`` from the window Kubecost echoed in its response.

    This is ground truth — it is the range the server actually queried — and is
    preferred over :func:`resolve_window`, which can only predict it. Returns
    ``None`` for anything unusable so callers can fall back to the prediction
    rather than losing the response.

    Kubecost ends in-progress windows ('week', 'month') at the current instant
    rather than a midnight boundary, so sub-day spans are accepted here.
    """
    if not isinstance(window, dict):
        return None
    raw_start, raw_end = window.get("start"), window.get("end")
    if not raw_start or not raw_end:
        return None
    start, end = parse_api_timestamp(raw_start), parse_api_timestamp(raw_end)
    if start is None or end is None:
        logger.warning("Kubecost returned an unparseable window: %r", window)
        return None
    if end <= start:
        logger.warning("Kubecost returned a non-positive window: %r", window)
        return None
    return _window_result(start, end, source_expression, allow_partial_day=True)


def resolve_window(window: str) -> ResolvedWindow:
    """Predict how Kubecost will resolve a window expression, without querying it.

    Use :func:`resolved_window_from_api` instead whenever a response is already
    in hand — this function can only approximate what the server will do.
    """
    source_expression = normalize_window_order((window or "").strip())
    lower = source_expression.lower()
    if not source_expression:
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            "A window expression is required.",
            retryable=False,
            suggested_action="Provide a named window such as '15d' or an RFC3339 start,end range.",
        )

    today = _start_of_utc_day()
    # Windows that include today run to the close of the current day. For
    # 'week'/'month' this also clamps a period-to-date so it never reports days
    # that have not happened yet.
    tomorrow = today + timedelta(days=1)
    if lower in _NAMED_WINDOW_DAYS or (lower.endswith("d") and lower[:-1].isdigit()):
        days = parse_window_days(lower)
        assert days is not None
        # Kubecost counts back N days *including* today, ending at the close of
        # the current day — verified against /model/allocation. These windows
        # therefore always include the in-progress day.
        return _window_result(tomorrow - timedelta(days=days), tomorrow, source_expression)
    if lower == "today":
        return _window_result(today, tomorrow, source_expression)
    if lower in {"week", "lastweek"}:
        # Kubecost always starts its weeks on Sunday. datetime.weekday() is
        # Monday=0..Sunday=6, so (weekday() + 1) % 7 is days since that Sunday.
        current_week_start = today - timedelta(days=(today.weekday() + 1) % 7)
        if lower == "week":
            return _window_result(current_week_start, tomorrow, source_expression)
        start = current_week_start - timedelta(days=7)
        return _window_result(start, current_week_start, source_expression)
    if lower in {"month", "lastmonth"}:
        current_month_start = today.replace(day=1)
        if lower == "month":
            return _window_result(current_month_start, tomorrow, source_expression)
        previous_day = current_month_start - timedelta(days=1)
        return _window_result(previous_day.replace(day=1), current_month_start, source_expression)
    if "," in source_expression:
        parts = [part.strip() for part in source_expression.split(",")]
        if len(parts) != 2 or not all(parts):
            message = f"Window '{source_expression}' is not a valid RFC3339 range. Expected 'start,end'."
        else:
            try:
                start, end = (datetime.fromisoformat(part.replace("Z", "+00:00")) for part in parts)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                if end <= start:
                    message = f"Window '{source_expression}' has an end timestamp that is not after the start."
                else:
                    return _window_result(start, end, source_expression)
            except ValueError:
                message = f"Window '{source_expression}' contains an invalid RFC3339 timestamp."
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            message,
            retryable=False,
            suggested_action="Provide a valid RFC3339 range such as '2026-07-01T00:00:00Z,2026-07-08T00:00:00Z'.",
        )
    raise_tool_error(
        ErrorCode.INVALID_INPUT,
        f"Window '{source_expression}' is not supported.",
        retryable=False,
        suggested_action="Use a named window such as '15d', 'lastmonth', or an RFC3339 start,end range.",
    )


# Calendar aliases that Kubecost resolves incorrectly server-side.
# These must be pre-resolved to explicit RFC3339 ranges before the API call.
_CALENDAR_ALIASES: frozenset[str] = frozenset({"lastmonth", "lastweek", "month", "week"})


def to_api_window(window: str) -> str:
    """Return a Kubecost-safe window string for use in API call parameters.

    Explicit RFC3339 ranges are put in chronological order. Calendar aliases
    (``"lastmonth"``, ``"lastweek"``, ``"month"``, ``"week"``) are pre-resolved
    to explicit RFC3339 date pairs because Kubecost's own server-side resolution
    of these aliases is broken (e.g. ``"lastmonth"`` becomes a single day).
    Other values, including rolling windows like ``"7d"``, are returned unchanged.
    """
    ordered_window = normalize_window_order(window)
    normalized = ordered_window.strip().lower()
    if normalized not in _CALENDAR_ALIASES:
        return ordered_window
    resolved = resolve_window(ordered_window)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return f"{resolved.start_utc.strftime(fmt)},{resolved.end_utc.strftime(fmt)}"


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


class CostRowStatus(StrEnum):
    """How a dimension's cost changed between the baseline and current window."""

    NEW = "new"
    """Absent from the baseline window and present in the current one."""
    REMOVED = "removed"
    """Present in the baseline window and gone from the current one."""
    UNCHANGED = "unchanged"
    """Cost is identical across both windows (including zero in both)."""
    CHANGED = "changed"
    """Present in both windows at a different cost."""


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


_STRUCTURED_CONTENT_POINTER = "(Full results are in this tool call's structuredContent.)"


def summarize_tool_response(payload: dict[str, Any]) -> str:
    """Render a :class:`BaseToolResponse` payload as a short text-content summary.

    FastMCP serializes a tool's whole return value into ``content[0].text`` *and*
    ``structuredContent``, which duplicates every row for no added information.
    ``TextContentSummaryMiddleware`` replaces the text half with this summary and
    leaves the full payload in ``structuredContent``.

    Built from the envelope alone, so it works for every tool without per-tool
    code: ``message`` is already authored as a human-readable summary, and
    ``recommended_action`` already carries the next step. Payload shapes differ
    between tools (and ``AllocationRow`` allows extra keys), so every field is
    read defensively.
    """
    parts: list[str] = []

    message = str(payload.get("message") or "").strip()
    if not message:
        # An empty message would leave a text-only client with nothing at all.
        message = f"Query status: {payload.get('status', 'unknown')}."
    parts.append(message)

    for key, label in (("warnings", "Warnings"), ("notes", "Notes")):
        values = payload.get(key)
        if isinstance(values, list):
            joined = " ".join(str(v).strip() for v in values if str(v).strip())
            if joined:
                parts.append(f"{label}: {joined}")

    if payload.get("truncated"):
        hint = "Results are truncated."
        next_offset = payload.get("next_offset")
        if next_offset is not None:
            hint += f" Pass offset={next_offset} to fetch the next page."
        parts.append(hint)

    action = str(payload.get("recommended_action") or "").strip()
    if action:
        parts.append(f"Next: {action}")

    parts.append(_STRUCTURED_CONTENT_POINTER)

    summary = "\n\n".join(parts)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 3] + "..."
    return summary


# ---------------------------------------------------------------------------
# Structured errors (rules #12, #13)
# ---------------------------------------------------------------------------


_DEFAULT_API_FAILURE_ACTION = "Check Kubecost connectivity and credentials, then retry."
_ACTION_MARKER = " Action: "


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


def mcp_error_response_fields(exc: BaseException) -> tuple[str, str]:
    """Split a formatted FastMCP ToolError into ``(message, recommended_action)``.

    Tools that catch ``McpToolError`` and return a typed error envelope should use
    this so the Kubecost-suggested next step lands in ``recommended_action``
    instead of being buried only in the formatted line.
    """
    text = str(exc)
    idx = text.rfind(_ACTION_MARKER)
    if idx == -1:
        return text, _DEFAULT_API_FAILURE_ACTION
    return text[:idx], text[idx + len(_ACTION_MARKER) :]


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
    if isinstance(exc, MissingClientApiKeyError):
        logger.warning("Kubecost API call rejected: no client-supplied API key")
        raise_tool_error(
            ErrorCode.AUTHENTICATION_FAILED,
            "This server requires each caller to supply its own Kubecost API key.",
            retryable=False,
            suggested_action="Send an X-API-KEY header with the MCP request.",
        )
    if isinstance(exc, ValueError) and "No authentication configured" in str(exc):
        logger.warning("Kubecost API call attempted without auth configuration")
        raise_tool_error(
            ErrorCode.CONFIGURATION_ERROR,
            str(exc),
            retryable=False,
            suggested_action="Set KUBECOST_API_KEY in the environment, or send an X-API-KEY header.",
        )
    if isinstance(exc, httpx.ConnectError):
        logger.warning("Kubecost API unreachable at %s: %s", path, exc)
        raise_tool_error(
            ErrorCode.DATA_UNAVAILABLE,
            "Could not connect to the Kubecost API. The service may be down or the base URL may be incorrect.",
            retryable=True,
            suggested_action="Verify KUBECOST_BASE_URL is reachable and the Kubecost service is running.",
        )
    if isinstance(exc, httpx.TimeoutException):
        logger.warning("Kubecost API request timed out at %s: %s", path, exc)
        raise_tool_error(
            ErrorCode.UPSTREAM_TIMEOUT,
            "The Kubecost API request timed out.",
            retryable=True,
            suggested_action="Retry the request. If timeouts persist, the cluster may be under load.",
        )
    logger.exception("Unexpected error calling Kubecost API path %s", path)
    raise_tool_error(
        ErrorCode.DATA_UNAVAILABLE,
        f"Unexpected error contacting the Kubecost API: {type(exc).__name__}",
        retryable=True,
        suggested_action="Retry the request. If the failure persists, check the Kubecost service logs.",
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
