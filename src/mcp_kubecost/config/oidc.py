"""OIDC authentication provider factory.

Builds a FastMCP ``OIDCProxy`` from environment-backed settings when
``AUTH_MODE`` includes OIDC. Returns ``None`` when OIDC is not enabled,
letting the server start without auth on the MCP endpoint.

Access-token format is detected from the IdP token response: JWT access
tokens are verified as-is; opaque tokens fall back to the
``id_token``. There is no user-facing override.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.auth import decode_jwt_header
from key_value.aio.stores.memory import MemoryStore

from mcp_kubecost.config.settings import AuthMode, Settings, get_settings
from mcp_kubecost.errors import ConfigError

logger = logging.getLogger(__name__)

# Per-request: True when the current verification token is the OIDC id_token.
_verify_id_token: ContextVar[bool] = ContextVar("oidc_verify_id_token", default=False)
_logged_opaque_access_token = False


def looks_like_jwt(token: str) -> bool:
    """Return True when ``token`` is a three-part JWT with a JSON ``alg`` header."""
    if not token or token.count(".") != 2:
        return False
    try:
        header = decode_jwt_header(token)
    except (ValueError, KeyError, IndexError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header


class AdaptiveTokenVerifier(TokenVerifier):
    """Route verification to the access-token or id_token JWTVerifier."""

    def __init__(self, access_verifier: TokenVerifier, id_token_verifier: TokenVerifier) -> None:
        super().__init__(required_scopes=access_verifier.required_scopes)
        self._access_verifier = access_verifier
        self._id_token_verifier = id_token_verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        if _verify_id_token.get():
            return await self._id_token_verifier.verify_token(token)
        return await self._access_verifier.verify_token(token)


class AdaptiveOidcProxy(OIDCProxy):
    """OIDC proxy that verifies JWT access tokens, else the OIDC id_token."""

    def _install_adaptive_verifier(self) -> None:
        """Wrap FastMCP's access-token JWTVerifier with an id_token fallback."""
        access_verifier = self._token_validator
        id_token_verifier = JWTVerifier(
            jwks_uri=str(self.oidc_config.jwks_uri),
            issuer=str(self.oidc_config.issuer),
            audience=self._upstream_client_id,
            required_scopes=None,
        )
        self._token_validator = AdaptiveTokenVerifier(access_verifier, id_token_verifier)

    def _get_verification_token(self, upstream_token_set: UpstreamTokenSet) -> str | None:
        global _logged_opaque_access_token
        access_token = upstream_token_set.access_token
        if looks_like_jwt(access_token):
            _verify_id_token.set(False)
            return access_token

        _verify_id_token.set(True)
        if not _logged_opaque_access_token:
            logger.info("OIDC access token is not a JWT; verifying id_token instead")
            _logged_opaque_access_token = True

        id_token = upstream_token_set.raw_token_data.get("id_token")
        if id_token is None:
            logger.warning("OIDC access token is opaque but no id_token was in the token response")
        return id_token

    def _uses_alternate_verification(self) -> bool:
        return _verify_id_token.get()


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
        "OIDC enabled — issuer=%s, base_url=%s, redirect_path=%s",
        settings.oidc_issuer_url,
        settings.oidc_base_url,
        settings.oidc_redirect_path,
    )

    # FastMCP defaults to an encrypted FileTreeStore under
    # platformdirs.user_data_dir("fastmcp") — typically
    # ~/.local/share/fastmcp/oauth-proxy/<key>/. That mkdir fails on a
    # read-only root filesystem. Keep DCR/token state in process memory;
    # MCP clients re-register after a restart.
    #
    # Consent is "external" so Keycloak owns the login/consent UI. FastMCP's
    # built-in consent page is another HTML response that MCP clients try to
    # parse as OAuth JSON.
    try:
        proxy = AdaptiveOidcProxy(
            config_url=settings.oidc_issuer_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            audience=settings.oidc_audience,
            base_url=settings.oidc_base_url,
            redirect_path=settings.oidc_redirect_path,
            required_scopes=settings.oidc_required_scopes or None,
            client_storage=MemoryStore(),
            require_authorization_consent="external",
        )
        proxy._install_adaptive_verifier()
        return proxy
    except ConfigError:
        raise
    except Exception as exc:
        logger.debug("OIDC provider initialization failed", exc_info=True)
        raise ConfigError(
            "OIDC provider initialization failed "
            f"({type(exc).__name__}): {exc}. "
            "Most often this means OIDC_ISSUER_URL returned an HTML login page instead of "
            "JSON discovery metadata. If this MCP server shares a Kubecost frontend hostname, "
            "OAuth paths (/register, /authorize, /token, /.well-known/oauth-*) must reach this "
            "Service without Kubecost SSO in front — set config.oidc.exposeAuthRoutes."
        ) from exc
