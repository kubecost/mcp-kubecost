"""OIDC authentication provider factory.

Builds a FastMCP ``OIDCProxy`` from environment-backed settings when
``AUTH_MODE=oidc``. Returns ``None`` when OIDC is not enabled, letting
the server start without auth on the MCP endpoint.

Access-token format is detected from the IdP token response: JWT access
tokens are verified as-is; opaque tokens fall back to the
``id_token``. There is no user-facing override.
"""

from __future__ import annotations

import json
import logging
import shutil
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.redirect_validation import DEFAULT_LOCALHOST_PATTERNS
from fastmcp.utilities.auth import decode_jwt_header
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from mcp_kubecost.config.settings import AuthMode, Settings, get_settings
from mcp_kubecost.errors import ConfigError

logger = logging.getLogger(__name__)

# Per-request: True when the current verification token is the OIDC id_token.
_verify_id_token: ContextVar[bool] = ContextVar("oidc_verify_id_token", default=False)

# MCP-client redirect allowlist (not the IdP callback). Without this, FastMCP
# leaves patterns as None and validate_redirect_uri accepts any ordinary URI.
ALLOWED_CLIENT_REDIRECT_URIS: list[str] = [
    *DEFAULT_LOCALHOST_PATTERNS,  # http://localhost:*, http://127.0.0.1:*
    "https://claude.ai/api/mcp/auth_callback",
]


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

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._logged_opaque: bool = False
        self._install_adaptive_verifier()

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
        access_token = upstream_token_set.access_token
        if looks_like_jwt(access_token):
            _verify_id_token.set(False)
            return access_token

        _verify_id_token.set(True)
        if not self._logged_opaque:
            logger.info("OIDC access token is not a JWT; verifying id_token instead")
            self._logged_opaque = True

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

    if settings.auth_mode != AuthMode.OIDC:
        return None

    # These are guaranteed non-None by Settings — either an explicit value was supplied
    # or a secure key was auto-generated at startup.
    assert settings.oidc_issuer_url is not None
    assert settings.oidc_client_id is not None
    assert settings.oidc_client_secret is not None
    assert settings.oidc_base_url is not None
    assert settings.oidc_jwt_signing_key is not None
    assert settings.oidc_storage_encryption_key is not None

    logger.info(
        "OIDC enabled — issuer=%s, base_url=%s, redirect_path=%s",
        settings.oidc_issuer_url,
        settings.oidc_base_url,
        settings.oidc_redirect_path,
    )

    try:
        storage_dir = Path(settings.oidc_storage_path)
        if settings.oidc_ephemeral_keys:
            shutil.rmtree(storage_dir, ignore_errors=True)
        storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_store = FileTreeStore(
            data_directory=storage_dir,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
        )
        encrypted_storage = FernetEncryptionWrapper(
            file_store,
            fernet=Fernet(settings.oidc_storage_encryption_key.encode()),
        )
        return AdaptiveOidcProxy(
            config_url=settings.oidc_issuer_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            audience=settings.oidc_audience,
            base_url=settings.oidc_base_url,
            redirect_path=settings.oidc_redirect_path,
            required_scopes=settings.oidc_required_scopes or None,
            allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECT_URIS,
            client_storage=encrypted_storage,
            jwt_signing_key=settings.oidc_jwt_signing_key,
            require_authorization_consent="remember",
        )
    except ConfigError:
        raise
    except Exception as exc:
        logger.debug("OIDC provider initialization failed", exc_info=True)
        raise ConfigError(
            "OIDC provider initialization failed "
            f"({type(exc).__name__}): {exc}. "
            "Most often this means OIDC_ISSUER_URL returned an HTML login page instead of "
            "JSON discovery metadata. If this MCP server shares a Kubecost frontend hostname, "
            "set a path-prefixed OIDC_BASE_URL (e.g. https://kubecost.example.com/mcp) and "
            "configure the frontend nginx to proxy OAuth paths to this Service, or give the "
            "MCP server its own dedicated hostname."
        ) from exc
