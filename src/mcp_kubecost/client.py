"""HTTP client for the Kubecost V3 API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_kubecost.auth import KUBECOST_API_KEY_HEADER, resolve_api_key
from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

# Retry these status codes; 4xx (except 429) fail immediately.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class KubecostClientError(Exception):
    """Raised when the Kubecost API returns an error."""

    def __init__(self, status_code: int, message: str, url: str, path: str):
        self.status_code = status_code
        self.message = message
        self.url = url
        self.path = path
        super().__init__(f"HTTP {status_code} from {url}: {message}")

    @property
    def redacted_url(self) -> str:
        """URL with the configured base URL replaced by a placeholder, for user-facing messages."""
        return f"https://YOUR_KUBECOST_URL{self.path}"

    def to_tool_error(self) -> ToolError:
        """Convert to a structured ToolError for LLM consumption."""
        if self.status_code == 401:
            return ToolError(
                code=ErrorCode.AUTHENTICATION_FAILED,
                message="Authentication failed. The API key is invalid or expired.",
                retryable=False,
                suggested_action=(
                    "Verify the X-API-KEY sent with the request, or that KUBECOST_API_KEY "
                    "is correctly configured on the server."
                ),
                context={"status_code": self.status_code},
            )
        elif self.status_code == 403:
            return ToolError(
                code=ErrorCode.PERMISSION_DENIED,
                message=f"Permission denied for this resource: {self.redacted_url}",
                retryable=False,
                suggested_action="When using Kubecost Enterprise with SSO enabled, see this guide: https://github.com/kubecost/mcp-kubecost/tree/main/docs/auth",
                context={"status_code": self.status_code, "url": self.redacted_url},
            )
        elif self.status_code == 404:
            return ToolError(
                code=ErrorCode.NOT_FOUND,
                message=f"Resource not found: {self.redacted_url}",
                retryable=False,
                suggested_action="Verify the resource ID or path is correct. Use a list tool to find valid IDs.",
                context={"status_code": self.status_code, "url": self.redacted_url},
            )
        elif self.status_code == 429:
            return ToolError(
                code=ErrorCode.RATE_LIMITED,
                message="Rate limit exceeded for the Kubecost API.",
                retryable=True,
                suggested_action="Wait 30 seconds and retry the request.",
                context={"status_code": self.status_code, "retry_after_seconds": 30},
            )
        elif self.status_code >= 500:
            return ToolError(
                code=ErrorCode.UPSTREAM_TIMEOUT,
                message=f"Kubecost API returned server error ({self.status_code}).",
                retryable=True,
                suggested_action="Retry the request after a brief delay.",
                context={"status_code": self.status_code, "retry_after_seconds": 10},
            )
        else:
            return ToolError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message=f"Unexpected error from Kubecost API: HTTP {self.status_code}",
                retryable=False,
                suggested_action="Review the error details and adjust the request parameters.",
                context={"status_code": self.status_code, "raw_message": self.message[:200]},
            )


def _get_base_url() -> str:
    """Return the Kubecost base URL from settings."""
    base_url = get_settings().kubecost_base_url
    logger.debug(f"Base URL: {base_url}")
    return base_url


def _build_headers() -> dict[str, str]:
    """Return outbound request headers, including the API key when one resolves.

    The key comes from the caller's ``X-API-KEY`` header when present, else
    from ``KUBECOST_API_KEY``. See :mod:`mcp_kubecost.auth`.
    """
    headers = {"User-Agent": "Kubecost-MCPServer"}
    api_key = resolve_api_key()
    if api_key:
        headers[KUBECOST_API_KEY_HEADER] = api_key
    return headers


def _build_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Merge caller-supplied params with server-wide query flags (e.g. viewId)."""
    merged: dict[str, Any] = dict(params) if params else {}
    if get_settings().use_cac_views:
        merged.setdefault("viewId", 0)
    return merged or None  # type: ignore[return-value]


def _max_attempts() -> int:
    """First try plus ``retry_count`` additional attempts (minimum 1)."""
    return max(1, get_settings().retry_count + 1)


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Send one HTTP request, retrying transport errors and retryable status codes."""
    settings = get_settings()
    url = f"{_get_base_url()}{path}"
    headers = _build_headers() if headers is None else headers
    attempts = _max_attempts()

    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                verify=settings.ssl_verify,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    params=_build_params(params),
                    headers=headers,
                    json=json,
                )
        except httpx.RequestError:
            if attempt >= attempts:
                raise
            logger.warning(
                "Kubecost %s %s transport error on attempt %s/%s; retrying",
                method,
                path,
                attempt,
                attempts,
            )
            continue

        if response.status_code >= 400:
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < attempts:
                logger.warning(
                    "Kubecost %s %s returned HTTP %s on attempt %s/%s; retrying",
                    method,
                    path,
                    response.status_code,
                    attempt,
                    attempts,
                )
                continue
            raise KubecostClientError(
                status_code=response.status_code,
                message=response.text,
                url=url,
                path=path,
            )

        try:
            return response.json()
        except Exception as exc:
            logger.error(
                "Failed to parse JSON response from %s (status=%s, body=%r): %s",
                str(response.url),
                response.status_code,
                response.text[:500],
                exc,
            )
            raise

    raise RuntimeError("unreachable: retry loop exited without return or raise")


async def get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Make a GET request to the Kubecost API.

    Authentication is optional. If no credentials are configured, the request
    will be made without authentication headers.

    Args:
        path: API path relative to the base URL (e.g., "/reporting/cost/run").
        params: Optional query parameters.

    Returns:
        Parsed JSON response.

    Raises:
        KubecostClientError: If the API returns a non-2xx status.
        httpx.RequestError: If the request fails after retries (timeout, connect, etc.).
    """
    return await _request("GET", path, params=params)


async def post(path: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
    """Make a POST request to the Kubecost API.

    Unlike :func:`get`, this **requires** a key from either source: a POST
    mutates state, so it is never sent unauthenticated. No tool calls this
    today — the MCP surface is read-only — but the guard stands for whenever
    a write tool is added.

    Args:
        path: API path relative to the base URL (e.g., "/rightsizing/aws/recommendations/ec2/snooze").
        json: Optional JSON body payload.
        params: Optional query parameters.

    Returns:
        Parsed JSON response.

    Raises:
        ValueError: If neither an X-API-KEY header nor KUBECOST_API_KEY supplies a key.
        KubecostClientError: If the API returns a non-2xx status.
        httpx.RequestError: If the request fails after retries (timeout, connect, etc.).
    """
    headers = _build_headers()

    if KUBECOST_API_KEY_HEADER not in headers:
        raise ValueError(
            "No authentication configured. Set the KUBECOST_API_KEY environment variable "
            f"or send an {KUBECOST_API_KEY_HEADER} header with the request."
        )

    return await _request("POST", path, params=params, json=json, headers=headers)
