"""HTTP client for the Kubecost V3 API."""

import logging
import os
from typing import Any

import httpx

from mcp_kubecost.errors import ErrorCode, ToolError

# Base URL can be overridden for EU/APAC regions
DEFAULT_BASE_URL = "https://actions.demo.kubecost.cloud"

logger = logging.getLogger(__name__)


class KubecostClientError(Exception):
    """Raised when the Kubecost API returns an error."""

    def __init__(self, status_code: int, message: str, url: str):
        self.status_code = status_code
        self.message = message
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {message}")

    def to_tool_error(self) -> ToolError:
        """Convert to a structured ToolError for LLM consumption."""
        if self.status_code == 401:
            return ToolError(
                code=ErrorCode.AUTHENTICATION_FAILED,
                message="Authentication failed. The API key or token is invalid or expired.",
                retryable=False,
                suggested_action="Verify that Kubecost_API_KEY or Kubecost_OPEN_TOKEN is correctly configured.",
                context={"status_code": self.status_code},
            )
        elif self.status_code == 403:
            return ToolError(
                code=ErrorCode.PERMISSION_DENIED,
                message=f"Permission denied for this resource: {self.url}",
                retryable=False,
                suggested_action="Check that the API key has the required permissions for this endpoint.",
                context={"status_code": self.status_code, "url": self.url},
            )
        elif self.status_code == 404:
            return ToolError(
                code=ErrorCode.NOT_FOUND,
                message=f"Resource not found: {self.url}",
                retryable=False,
                suggested_action="Verify the resource ID or path is correct. Use a list tool to find valid IDs.",
                context={"status_code": self.status_code, "url": self.url},
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
    """Determine the base URL from environment configuration."""

    return os.environ.get("Kubecost_BASE_URL", DEFAULT_BASE_URL)


def _get_auth_headers() -> dict[str, str]:
    """Build authentication headers.

    Supports two auth modes:
    1. API Key (basic auth) — set Kubecost_API_KEY
    2. Apptio OpenToken — set Kubecost_OPEN_TOKEN and Kubecost_ENVIRONMENT_ID
    """
    headers: dict[str, str] = {"User-Agent": "Kubecost-MCPServer/demo"}
    open_token = os.environ.get("Kubecost_OPEN_TOKEN")
    if open_token:
        headers["apptio-opentoken"] = open_token
        env_id = os.environ.get("Kubecost_ENVIRONMENT_ID")
        if env_id:
            headers["apptio-current-environment"] = env_id
        logger.debug(f"Using Kubecost_OPEN_TOKEN authentication for environment {env_id}")
    return headers


def _get_auth() -> tuple[str, str] | None:
    """Return basic auth tuple if API key is configured."""
    api_key = os.environ.get("Kubecost_API_KEY")
    if api_key:
        logger.debug("Using Kubecost_API_KEY authentication")
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
    headers = _get_auth_headers()

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            params=params,
            auth=auth,
            headers=headers,
        )

    if response.status_code >= 400:
        raise KubecostClientError(
            status_code=response.status_code,
            message=response.text,
            url=url,
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
    headers = _get_auth_headers()

    if not auth and "apptio-opentoken" not in headers:
        raise ValueError(
            "No authentication configured. Set Kubecost_API_KEY or Kubecost_OPEN_TOKEN environment variable."
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            json=json,
            params=params,
            auth=auth,
            headers=headers,
        )

    if response.status_code >= 400:
        raise KubecostClientError(
            status_code=response.status_code,
            message=response.text,
            url=url,
        )

    return response.json()
