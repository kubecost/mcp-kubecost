"""OIDC authentication provider factory.

Builds a FastMCP ``OIDCProxy`` from environment-backed settings when
``AUTH_MODE`` includes OIDC. Returns ``None`` when OIDC is not enabled,
letting the server start without auth on the MCP endpoint.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth.oidc_proxy import OIDCProxy

from mcp_kubecost.config.settings import AuthMode, Settings, get_settings

logger = logging.getLogger(__name__)


def create_oidc_provider(settings: Settings | None = None) -> OIDCProxy | None:
    """Return a configured ``OIDCProxy`` if OIDC is enabled, else ``None``.

    Parameters
    ----------
    settings:
        Explicit settings instance; defaults to ``get_settings()`` when omitted.

    Returns
    -------
    OIDCProxy | None
        The provider to pass as ``auth=`` to ``FastMCP()``, or ``None`` to
        leave the endpoint unauthenticated.
    """
    if settings is None:
        settings = get_settings()

    if settings.auth_mode not in (AuthMode.OIDC, AuthMode.BOTH):
        return None

    # These are guaranteed non-None by Settings validation when OIDC is active.
    assert settings.oidc_issuer_url is not None
    assert settings.oidc_client_id is not None
    assert settings.oidc_client_secret is not None
    assert settings.oidc_base_url is not None

    logger.info(
        "OIDC enabled — issuer=%s, base_url=%s",
        settings.oidc_issuer_url,
        settings.oidc_base_url,
    )

    return OIDCProxy(
        config_url=settings.oidc_issuer_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        audience=settings.oidc_audience,
        base_url=settings.oidc_base_url,
        required_scopes=settings.oidc_required_scopes or None,
    )
