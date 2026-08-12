"""Resolution of the API key sent to Kubecost.

The key travels as an ``X-API-KEY`` request header. Two sources feed it, in
precedence order:

1. An ``X-API-KEY`` header on the incoming MCP request (HTTP transport only).
2. The ``KUBECOST_API_KEY`` environment variable.

The per-request header lets several MCP clients share one server while each
presents its own Kubecost credential; ``KUBECOST_API_KEY`` remains the static
fallback for single-tenant deployments. Neither is required — with no key at
all the request goes out unauthenticated.
"""

from __future__ import annotations

import logging

from fastmcp.server.dependencies import get_http_headers

from mcp_kubecost.config.settings import AuthMode, get_settings, is_http_mode

logger = logging.getLogger(__name__)

#: Incoming header carrying the caller's Kubecost key. FastMCP lowercases
#: header names, so match it lowercased.
CLIENT_API_KEY_HEADER = "x-api-key"

#: Header used on the outbound request to Kubecost.
KUBECOST_API_KEY_HEADER = "X-API-KEY"


class MissingClientApiKeyError(Exception):
    """REQUIRE_CLIENT_API_KEY is set and the request carried no X-API-KEY."""


def client_supplied_api_key() -> str | None:
    """Return the ``X-API-KEY`` sent by the MCP client on this request.

    ``get_http_headers()`` returns an empty dict when there is no active HTTP
    request, so this is safe on STDIO and returns ``None`` there.
    """
    return (get_http_headers().get(CLIENT_API_KEY_HEADER) or "").strip() or None


def resolve_api_key() -> str | None:
    """Return the key to send to Kubecost, or None to call unauthenticated.

    Raises:
        MissingClientApiKeyError: If ``REQUIRE_CLIENT_API_KEY`` is enabled and
            an HTTP client called without supplying a key. The check sits
            between the header and the environment fallback, so the flag means
            "the caller must present a key" rather than "a key must exist".
            Also raised when ``AUTH_MODE=both`` — the client must supply an
            ``X-API-KEY`` header alongside the OIDC token (presence check only,
            value is not validated).
    """
    client_key = client_supplied_api_key()
    if client_key:
        logger.debug("Using client-supplied %s header", KUBECOST_API_KEY_HEADER)
        return client_key

    settings = get_settings()

    # In "both" mode, OIDC handles identity but we still require the API key
    # header to be present on HTTP requests (value is forwarded as-is, no
    # validation). This check is intentionally separate from require_client_api_key
    # so operators can enable OIDC+API-key gating without the legacy flag.
    if settings.auth_mode == AuthMode.BOTH and is_http_mode():
        raise MissingClientApiKeyError(
            f"AUTH_MODE=both requires an {KUBECOST_API_KEY_HEADER} header alongside OIDC authentication."
        )

    # STDIO callers cannot send headers, so enforcing there would break the
    # transport outright.
    if settings.require_client_api_key and is_http_mode():
        raise MissingClientApiKeyError(
            f"REQUIRE_CLIENT_API_KEY is enabled but the request did not include an {KUBECOST_API_KEY_HEADER} header."
        )

    if settings.KUBECOST_API_KEY:
        logger.debug("Using KUBECOST_API_KEY from the environment")
    return settings.KUBECOST_API_KEY
