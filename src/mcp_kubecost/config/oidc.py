"""OIDC authentication provider factory.

Builds a FastMCP ``OIDCProxy`` from environment-backed settings when
``AUTH_MODE=oidc``. Returns ``None`` when OIDC is not enabled, letting
the server start without auth on the MCP endpoint.

Access-token format is detected from the IdP token response: JWT access
tokens are verified as-is; opaque tokens fall back to the
``id_token``. There is no user-facing override.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.auth import decode_jwt_header
from key_value.aio.errors.wrappers import DecryptionError
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import AccessToken as SdkAccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from mcp_kubecost.config.settings import AuthMode, Settings, get_settings
from mcp_kubecost.errors import ConfigError

logger = logging.getLogger(__name__)

# Per-request: True when the current verification token is the OIDC id_token.
_verify_id_token: ContextVar[bool] = ContextVar("oidc_verify_id_token", default=False)


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
    """OIDC proxy that verifies JWT access tokens, else the OIDC id_token.

    Also makes Dynamic Client Registration idempotent — see ``register_client``.
    """

    def __init__(self, *, dcr_client_id_key: str, storage_dir: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._dcr_client_id_key: bytes = dcr_client_id_key.encode()
        self._storage_dir = storage_dir
        self._logged_opaque: bool = False
        self._install_adaptive_verifier()

    def _derive_client_id(self, client_info: OAuthClientInformationFull) -> str:
        """Return a stable client_id for a registration: same metadata, same id.

        The MCP SDK mints a fresh ``uuid4()`` per ``/register`` call with no
        dedupe. A client that opens two connections at once therefore ends up
        with two identities sharing one loopback callback port, and the
        authorization code minted for one is redeemed by the other — which
        FastMCP rejects as a client ID mismatch, leaving the browser showing
        success while the session never establishes.

        Keyed on the storage encryption key so ids are not guessable across
        deployments. When that key is ephemeral the storage is wiped at startup
        anyway, so ids have no need to survive the restart.
        """
        material = json.dumps(
            {
                "redirect_uris": sorted(str(uri) for uri in client_info.redirect_uris or []),
                "client_name": client_info.client_name or "",
                "grant_types": sorted(client_info.grant_types or []),
                "response_types": sorted(client_info.response_types or []),
                "scope": client_info.scope or "",
                "token_endpoint_auth_method": client_info.token_endpoint_auth_method or "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hmac.new(self._dcr_client_id_key, material.encode(), hashlib.sha256).hexdigest()
        # UUID shape keeps the wire format identical to the SDK's uuid4().
        return str(uuid.UUID(digest[:32]))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a client under an id derived from its metadata.

        Mutated in place because ``RegistrationHandler`` returns this same
        object as the 201 body, so the client sees the derived id too.
        Delegates to ``super()`` rather than short-circuiting on an existing
        entry, keeping redirect-URI validation on every registration.
        """
        client_info.client_id = self._derive_client_id(client_info)
        await super().register_client(client_info)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Wrap the parent with a DecryptionError guard.

        A key rotation (new OIDC_STORAGE_ENCRYPTION_KEY without wiping the storage
        directory) leaves ciphertext on disk that cannot be decrypted with the current
        key. Without this guard the error propagates as an unhandled exception inside
        FastMCP's request handlers. Returning None lets FastMCP respond with a clean
        401 instead of a 500 traceback.
        """
        try:
            return await super().get_client(client_id)
        except DecryptionError:
            logger.error(
                "Failed to decrypt stored OAuth client %r — the storage was likely written "
                "with a different OIDC_STORAGE_ENCRYPTION_KEY. "
                "Set OIDC_STORAGE_ENCRYPTION_KEY to the original key, or clear the "
                "OIDC storage directory (%s) and restart so clients re-register.",
                client_id,
                self._storage_dir,
            )
            return None

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Reject codes as FastMCP does, but say so at a level operators see.

        FastMCP logs the specific reason at DEBUG, so in production this failure
        otherwise reaches the operator as a bare 401 with no explanation.
        Also catches DecryptionError when the storage key has rotated.
        """
        try:
            result = await super().load_authorization_code(client, authorization_code)
        except DecryptionError:
            logger.error(
                "Failed to decrypt authorization code for client %s — the storage was likely "
                "written with a different OIDC_STORAGE_ENCRYPTION_KEY. "
                "Set OIDC_STORAGE_ENCRYPTION_KEY to the original key, or clear the "
                "OIDC storage directory and restart so clients re-register.",
                client.client_id,
            )
            return None
        if result is None:
            logger.warning(
                "Rejected authorization code for client %s — the code was not found, has "
                "expired, or was issued to a different client_id. Enable DEBUG logging on "
                "fastmcp.server.auth for the specific reason.",
                client.client_id,
            )
        return result

    async def load_access_token(self, token: str) -> SdkAccessToken | None:
        """Wrap the parent with a DecryptionError guard."""
        try:
            return await super().load_access_token(token)
        except DecryptionError:
            logger.error(
                "Failed to decrypt stored access token — the storage was likely written "
                "with a different OIDC_STORAGE_ENCRYPTION_KEY. "
                "Clients will need to re-authenticate."
            )
            return None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        """Wrap the parent with a DecryptionError guard."""
        try:
            return await super().load_refresh_token(client, refresh_token)
        except DecryptionError:
            logger.error(
                "Failed to decrypt stored refresh token for client %s — the storage was likely "
                "written with a different OIDC_STORAGE_ENCRYPTION_KEY. "
                "Clients will need to re-authenticate.",
                client.client_id,
            )
            return None

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
        # oidc_ephemeral_keys means the storage encryption key was auto-generated this
        # startup, so anything already on disk was encrypted with a key we no longer
        # have and every read would fail. Discard it rather than serve errors. An
        # auto-generated *signing* key does not land here — see get_settings().
        # _get_oidc_storage_path() guarantees this is a nested path, never '/' or '/var'.
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
            allowed_client_redirect_uris=settings.oidc_allowed_client_redirect_uris,
            client_storage=encrypted_storage,
            jwt_signing_key=settings.oidc_jwt_signing_key,
            require_authorization_consent="remember",
            dcr_client_id_key=settings.oidc_storage_encryption_key,
            storage_dir=storage_dir,
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
