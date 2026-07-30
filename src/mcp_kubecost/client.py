"""HTTP client for the Kubecost V3 API."""

import logging
from typing import Any

import httpx

from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)


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
                suggested_action="Verify that KUBECOST_API_KEY is correctly configured.",
                context={"status_code": self.status_code},
            )
        elif self.status_code == 403:
            return ToolError(
                code=ErrorCode.PERMISSION_DENIED,
                message=f"Permission denied for this resource: {self.redacted_url}",
                retryable=False,
                suggested_action="Check that the API key has the required permissions for this endpoint.",
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


def _get_auth() -> tuple[str, str] | None:
    """Return basic auth tuple if API key is configured."""
    api_key = get_settings().KUBECOST_API_KEY
    if api_key:
        logger.debug("Using KUBECOST_API_KEY authentication")
        return (api_key, "")
    return None


def wrap_list(data: list, key: str) -> dict[str, Any]:
    """Wrap a bare API list response in a dict so MCP structured content is valid.

    The MCP framework requires tool structured content to be a dict or None.
    Some Kubecost endpoints (e.g. /internal/tag_mappings) return a top-level
    JSON array. Call this helper to wrap before returning from a tool handler:

        raw = await get("/internal/tag_mappings")
        return wrap_list(raw, "tag_mappings")

    Args:
        data: The list returned by the API.
        key:  A descriptive dict key (e.g. "tag_mappings", "views").

    Returns:
        {"<key>": data}
    """
    return {key: data}


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
    """
    base_url = _get_base_url()
    url = f"{base_url}{path}"
    auth = _get_auth()

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            params=params,
            auth=auth,
            headers={"User-Agent": "Kubecost-MCPServer"},
        )

    if response.status_code >= 400:
        raise KubecostClientError(
            status_code=response.status_code,
            message=response.text,
            url=url,
            path=path,
        )

    try:
        return response.json()
    except Exception as exc:
        full_url = str(response.url)
        logger.error(
            "Failed to parse JSON response from %s (status=%s, body=%r): %s",
            full_url,
            response.status_code,
            response.text[:500],
            exc,
        )
        raise


async def post(path: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
    """Make a POST request to the Kubecost API.

    Authentication is optional. If no credentials are configured, the request
    will be made without authentication headers.

    Args:
        path: API path relative to the base URL (e.g., "/rightsizing/aws/recommendations/ec2/snooze").
        json: Optional JSON body payload.
        params: Optional query parameters.

    Returns:
        Parsed JSON response.

    Raises:
        KubecostClientError: If the API returns a non-2xx status.
    """
    base_url = _get_base_url()
    url = f"{base_url}{path}"
    auth = _get_auth()

    if not auth:
        raise ValueError("No authentication configured. Set KUBECOST_API_KEY environment variable.")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            json=json,
            params=params,
            auth=auth,
            headers={"User-Agent": "Kubecost-MCPServer"},
        )

    if response.status_code >= 400:
        raise KubecostClientError(
            status_code=response.status_code,
            message=response.text,
            url=url,
            path=path,
        )

    return response.json()
