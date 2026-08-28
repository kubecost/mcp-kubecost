"""Tests for OIDC provider construction and access-token shape detection."""

from __future__ import annotations

import base64
import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from mcp_kubecost.config.oidc import (
    ALLOWED_CLIENT_REDIRECT_URIS,
    AdaptiveOidcProxy,
    AdaptiveTokenVerifier,
    create_oidc_provider,
    looks_like_jwt,
)
from mcp_kubecost.config.settings import AuthMode, Settings
from mcp_kubecost.errors import ConfigError

_SETTINGS: dict[str, Any] = dict(
    kubecost_base_url="http://localhost:9090",
    kubecost_api_base_path="/model",
    KUBECOST_API_KEY=None,
    require_client_api_key=False,
    use_cac_views=False,
    ssl_verify=True,
    request_timeout_seconds=15.0,
    retry_count=2,
    default_window="15d",
    log_level="INFO",
    rate_limit_requests_per_second=10.0,
    rate_limit_burst_capacity=20,
    max_concurrent_tool_calls=10,
    auth_mode=AuthMode.NONE,
    oidc_issuer_url=None,
    oidc_client_id=None,
    oidc_client_secret=None,
    oidc_audience=None,
    oidc_base_url=None,
    oidc_redirect_path="/auth-mcp",
    oidc_required_scopes=["openid", "profile"],
    oidc_storage_path="/tmp/mcp-kubecost-test-oauth",
    oidc_jwt_signing_key=None,
    oidc_storage_encryption_key=None,
)

_OIDC = dict(
    auth_mode=AuthMode.OIDC,
    oidc_issuer_url="https://idp.example/.well-known/openid-configuration",
    oidc_client_id="client",
    oidc_client_secret="secret",
    oidc_base_url="https://mcp.example",
    oidc_storage_path="/tmp/mcp-kubecost-test-oauth",
    oidc_jwt_signing_key="j" * 32,
    oidc_storage_encryption_key=Fernet.generate_key().decode(),
)


def _settings(**overrides: Any) -> Settings:
    return Settings(**{**_SETTINGS, **overrides})


def _b64url(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt() -> str:
    return f"{_b64url({'alg': 'RS256', 'typ': 'JWT'})}.{_b64url({'sub': 'user'})}.sig"


def _token_set(*, access: str, id_token: str | None = None) -> UpstreamTokenSet:
    raw: dict[str, Any] = {}
    if id_token is not None:
        raw["id_token"] = id_token
    return UpstreamTokenSet(
        upstream_token_id="u1",
        access_token=access,
        refresh_token=None,
        refresh_token_expires_at=None,
        expires_at=1_000_000_000.0,
        token_type="Bearer",
        scope="openid profile",
        client_id="mcp-client",
        created_at=0.0,
        raw_token_data=raw,
    )


def _uninitialized_proxy() -> AdaptiveOidcProxy:
    """Return an AdaptiveOidcProxy bypassing __init__ for unit-testing individual methods.

    Tests that call _get_verification_token must set _logged_opaque on the instance
    themselves (or accept AttributeError) because __init__ is skipped.
    The ContextVar _verify_id_token is task-local, so _uses_alternate_verification()
    reads whatever the current task's _get_verification_token call last wrote.
    """
    proxy = AdaptiveOidcProxy.__new__(AdaptiveOidcProxy)
    proxy._logged_opaque = False  # normally set by __init__
    return proxy


class TestCreateOidcProvider:
    def test_none_when_auth_disabled(self):
        assert create_oidc_provider(_settings()) is None

    def test_uses_encrypted_file_storage_and_remembered_consent(self, tmp_path):
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(_settings(**{**_OIDC, "oidc_storage_path": str(tmp_path)}))
        kwargs = proxy.call_args.kwargs
        assert isinstance(kwargs["client_storage"], FernetEncryptionWrapper)
        assert isinstance(kwargs["client_storage"].key_value, FileTreeStore)
        assert kwargs["jwt_signing_key"] == _OIDC["oidc_jwt_signing_key"]
        assert kwargs["require_authorization_consent"] == "remember"
        assert kwargs["redirect_path"] == "/auth-mcp"
        assert kwargs["allowed_client_redirect_uris"] == ALLOWED_CLIENT_REDIRECT_URIS
        assert "verify_id_token" not in kwargs
        # _install_adaptive_verifier is now called from AdaptiveOidcProxy.__init__,
        # not from create_oidc_provider, so no explicit call assertion needed here.

    def test_forwards_dedicated_host_redirect_path(self, tmp_path):
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(
                _settings(
                    **{
                        **_OIDC,
                        "oidc_redirect_path": "/auth/callback",
                        "oidc_storage_path": str(tmp_path),
                    }
                )
            )
        assert proxy.call_args.kwargs["redirect_path"] == "/auth/callback"

    async def test_encrypted_file_storage_survives_reopen(self, tmp_path):
        settings = _settings(**{**_OIDC, "oidc_storage_path": str(tmp_path)})
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(settings)

        storage = proxy.call_args.kwargs["client_storage"]
        value = {"access_token": "super-secret-token", "scope": "openid"}
        await storage.put("client-1", value, collection="oauth-clients")

        stored_bytes = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
        assert b"super-secret-token" not in stored_bytes

        reopened_file_store = FileTreeStore(
            data_directory=tmp_path,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(tmp_path),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(tmp_path),
        )
        encryption_key = settings.oidc_storage_encryption_key
        assert encryption_key is not None
        reopened_storage = FernetEncryptionWrapper(
            reopened_file_store,
            fernet=Fernet(encryption_key.encode()),
        )
        assert await reopened_storage.get("client-1", collection="oauth-clients") == value

    def test_api_key_mode_does_not_build_proxy(self):
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            assert create_oidc_provider(_settings(auth_mode=AuthMode.API_KEY)) is None
        proxy.assert_not_called()

    def test_init_failure_is_config_error_not_traceback(self, tmp_path):
        with patch(
            "mcp_kubecost.config.oidc.AdaptiveOidcProxy",
            side_effect=ValueError("expected value at line 1 column 1"),
        ):
            try:
                create_oidc_provider(_settings(**{**_OIDC, "oidc_storage_path": str(tmp_path)}))
            except ConfigError as exc:
                assert "OIDC provider initialization failed" in str(exc)
                assert "HTML" in str(exc)
            else:
                raise AssertionError("expected ConfigError")

    def test_invalid_storage_encryption_key_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="OIDC provider initialization failed"):
            create_oidc_provider(
                _settings(
                    **{
                        **_OIDC,
                        "oidc_storage_path": str(tmp_path),
                        "oidc_storage_encryption_key": "not-a-fernet-key",
                    }
                )
            )


class TestOidcProxyKwargConformance:
    """`test_uses_in_memory_client_storage` et al. patch AdaptiveOidcProxy wholesale, so a
    kwarg removed upstream would still pass those tests. Assert the kwargs we
    pass still exist on the real signature, so a FastMCP bump fails a test
    instead of failing at server startup.
    """

    def test_kwargs_still_exist_on_oidc_proxy(self):
        sig = inspect.signature(OIDCProxy.__init__)
        for kwarg in (
            "redirect_path",
            "client_storage",
            "jwt_signing_key",
            "require_authorization_consent",
            "audience",
            "allowed_client_redirect_uris",
        ):
            assert kwarg in sig.parameters, f"OIDCProxy.__init__ no longer accepts {kwarg!r}"

    def test_verification_hooks_still_exist(self):
        assert hasattr(OIDCProxy, "_get_verification_token")
        assert hasattr(OIDCProxy, "_uses_alternate_verification")

    def test_unknown_clients_use_fastmcp_default_rejection(self):
        assert AdaptiveOidcProxy.get_client is OIDCProxy.get_client


class TestLooksLikeJwt:
    def test_three_part_alg_header(self):
        assert looks_like_jwt(_jwt()) is True

    def test_opaque_string(self):
        assert looks_like_jwt("opaque-ibm-access-token") is False

    def test_empty(self):
        assert looks_like_jwt("") is False

    def test_two_parts(self):
        assert looks_like_jwt("only.two") is False

    def test_three_parts_without_json_header(self):
        assert looks_like_jwt("not-json.payload.sig") is False


class TestGetVerificationToken:
    def test_jwt_access_token_is_verified(self):
        proxy = _uninitialized_proxy()
        access = _jwt()
        chosen = proxy._get_verification_token(_token_set(access=access, id_token=_jwt()))
        assert chosen == access
        assert proxy._uses_alternate_verification() is False

    def test_opaque_access_token_uses_id_token(self):
        proxy = _uninitialized_proxy()
        id_token = _jwt()
        chosen = proxy._get_verification_token(_token_set(access="opaque-token", id_token=id_token))
        assert chosen == id_token
        assert proxy._uses_alternate_verification() is True

    def test_opaque_access_token_without_id_token_returns_none(self):
        proxy = _uninitialized_proxy()
        chosen = proxy._get_verification_token(_token_set(access="opaque-token"))
        assert chosen is None
        assert proxy._uses_alternate_verification() is True


class TestInstallAdaptiveVerifier:
    def test_wraps_access_token_verifier(self):
        proxy = _uninitialized_proxy()
        access = MagicMock()
        access.required_scopes = ["openid", "profile"]
        proxy._token_validator = access
        proxy._upstream_client_id = "client"
        proxy.oidc_config = MagicMock(jwks_uri="https://idp.example/jwks", issuer="https://idp.example")
        proxy._install_adaptive_verifier()
        assert isinstance(proxy._token_validator, AdaptiveTokenVerifier)


class TestAdaptiveTokenVerifier:
    # Routing is driven by the task-local ContextVar _verify_id_token.
    # _get_verification_token sets the ContextVar as a side effect, so we call it
    # on an uninitialized proxy before asserting the verifier's routing — both
    # the proxy call and verify_token run in the same asyncio task, so the
    # ContextVar value is visible to the verifier without any extra wiring.

    async def test_routes_jwt_access_to_access_verifier(self):
        access = MagicMock()
        access.required_scopes = ["openid", "profile"]
        access.verify_token = AsyncMock(return_value="access-ok")
        id_token = MagicMock()
        id_token.verify_token = AsyncMock(return_value="id-ok")
        verifier = AdaptiveTokenVerifier(access, id_token)

        proxy = _uninitialized_proxy()
        proxy._get_verification_token(_token_set(access=_jwt(), id_token=_jwt()))
        assert await verifier.verify_token("tok") == "access-ok"
        access.verify_token.assert_awaited_once_with("tok")
        id_token.verify_token.assert_not_awaited()

    async def test_routes_opaque_to_id_token_verifier(self):
        access = MagicMock()
        access.required_scopes = ["openid", "profile"]
        access.verify_token = AsyncMock(return_value="access-ok")
        id_token = MagicMock()
        id_token.verify_token = AsyncMock(return_value="id-ok")
        verifier = AdaptiveTokenVerifier(access, id_token)

        proxy = _uninitialized_proxy()
        proxy._get_verification_token(_token_set(access="opaque", id_token=_jwt()))
        assert await verifier.verify_token("tok") == "id-ok"
        id_token.verify_token.assert_awaited_once_with("tok")
        access.verify_token.assert_not_awaited()


class TestAllowedClientRedirectUris:
    def test_rejects_arbitrary_host(self):
        assert validate_redirect_uri("https://evil.example/x", ALLOWED_CLIENT_REDIRECT_URIS) is False

    def test_allows_localhost_and_claude(self):
        assert validate_redirect_uri("http://localhost:54321/callback", ALLOWED_CLIENT_REDIRECT_URIS) is True
        assert validate_redirect_uri("https://claude.ai/api/mcp/auth_callback", ALLOWED_CLIENT_REDIRECT_URIS) is True
