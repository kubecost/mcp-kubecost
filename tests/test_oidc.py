"""Tests for OIDC provider construction and access-token shape detection."""

from __future__ import annotations

import base64
import inspect
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient, UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcp_kubecost.config.oidc import (
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
    oidc_allowed_client_redirect_uris=None,
    oidc_storage_path="/tmp/mcp-kubecost-test-oauth",
    oidc_jwt_signing_key=None,
    oidc_storage_encryption_key=None,
    oidc_ephemeral_keys=False,
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


def _dcr_proxy(
    *,
    dcr_client_id_key: str = "storage-key",
    allowed_redirect_uris: list[str] | None = None,
) -> AdaptiveOidcProxy:
    """Return a proxy wired for register_client only, bypassing __init__.

    A real __init__ fetches the IdP discovery document over the network. Only
    the attributes OAuthProxy.register_client touches are supplied here.
    """
    proxy = AdaptiveOidcProxy.__new__(AdaptiveOidcProxy)
    proxy._dcr_client_id_key = dcr_client_id_key.encode()
    proxy._allowed_client_redirect_uris = allowed_redirect_uris
    proxy._default_scope_str = "openid profile"
    proxy._cimd_manager = None  # get_client skips CIMD refresh
    proxy._client_store = PydanticAdapter[ProxyDCRClient](
        key_value=MemoryStore(),
        pydantic_model=ProxyDCRClient,
        default_collection="mcp-oauth-proxy-clients",
        raise_on_validation_error=True,
    )
    return proxy


class TestCreateOidcProvider:
    def test_none_when_auth_disabled(self):
        assert create_oidc_provider(_settings()) is None

    def test_installs_kubecost_page_branding(self, tmp_path):
        with (
            patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy"),
            patch("mcp_kubecost.config.oidc.install_oauth_page_branding") as install,
        ):
            create_oidc_provider(_settings(**{**_OIDC, "oidc_storage_path": str(tmp_path)}))
        install.assert_called_once()

    @pytest.mark.parametrize("auth_mode", [AuthMode.NONE, AuthMode.OPEN, AuthMode.API_KEY])
    def test_no_page_branding_without_oidc(self, auth_mode):
        """The other auth modes serve no browser-facing OAuth pages, so leave FastMCP alone."""
        with patch("mcp_kubecost.config.oidc.install_oauth_page_branding") as install:
            assert create_oidc_provider(_settings(auth_mode=auth_mode)) is None
        install.assert_not_called()

    def test_uses_encrypted_file_storage_and_remembered_consent(self, tmp_path):
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(_settings(**{**_OIDC, "oidc_storage_path": str(tmp_path)}))
        kwargs = proxy.call_args.kwargs
        assert isinstance(kwargs["client_storage"], FernetEncryptionWrapper)
        assert isinstance(kwargs["client_storage"].key_value, FileTreeStore)
        assert kwargs["jwt_signing_key"] == _OIDC["oidc_jwt_signing_key"]
        assert kwargs["require_authorization_consent"] == "remember"
        assert kwargs["redirect_path"] == "/auth-mcp"
        assert kwargs["allowed_client_redirect_uris"] is None
        assert kwargs["dcr_client_id_key"] == _OIDC["oidc_storage_encryption_key"]
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

    def test_forwards_restricted_client_redirects(self, tmp_path):
        redirects = ["http://localhost:*", "https://client.example/callback"]
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(
                _settings(
                    **{
                        **_OIDC,
                        "oidc_allowed_client_redirect_uris": redirects,
                        "oidc_storage_path": str(tmp_path),
                    }
                )
            )
        assert proxy.call_args.kwargs["allowed_client_redirect_uris"] == redirects

    def test_forwards_explicit_empty_client_redirect_list(self, tmp_path):
        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy") as proxy:
            create_oidc_provider(
                _settings(
                    **{
                        **_OIDC,
                        "oidc_allowed_client_redirect_uris": [],
                        "oidc_storage_path": str(tmp_path),
                    }
                )
            )
        assert proxy.call_args.kwargs["allowed_client_redirect_uris"] == []

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

    def test_ephemeral_keys_wipe_storage_on_startup(self, tmp_path):
        # Write a sentinel file simulating state from a previous run
        (tmp_path / "stale.json").write_text("old state")

        with patch("mcp_kubecost.config.oidc.AdaptiveOidcProxy"):
            create_oidc_provider(
                _settings(
                    **{
                        **_OIDC,
                        "oidc_storage_path": str(tmp_path),
                        "oidc_ephemeral_keys": True,
                    }
                )
            )

        assert not (tmp_path / "stale.json").exists()


class TestIdempotentClientRegistration:
    """A client that registers twice with the same metadata must get one identity.

    The SDK mints a fresh uuid4 per /register. When an MCP client opens two
    connections at once, both register against the same loopback callback port,
    and the authorization code minted for one is redeemed by the other — a
    client ID mismatch that 401s the token exchange after the browser has
    already reported success.
    """

    @staticmethod
    def _metadata(redirect_uri: str = "http://127.0.0.1:33418/callback") -> OAuthClientInformationFull:
        """Registration metadata as the SDK handler hands it to register_client."""
        return OAuthClientInformationFull(
            client_id=str(uuid4()),  # the uuid4 the SDK mints per /register call
            client_secret=None,
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="openid profile",
            token_endpoint_auth_method="none",
        )

    async def test_same_metadata_yields_one_client_id(self):
        proxy = _dcr_proxy()
        first, second = self._metadata(), self._metadata()

        await proxy.register_client(first)
        await proxy.register_client(second)

        # Mutated in place: RegistrationHandler returns this object as the 201 body,
        # so the client only sees the derived id because of this assignment.
        assert first.client_id is not None
        assert first.client_id == second.client_id
        assert await proxy.get_client(first.client_id) is not None

    async def test_differing_redirect_uris_yield_distinct_client_ids(self):
        proxy = _dcr_proxy()
        first = self._metadata("http://127.0.0.1:33418/callback")
        second = self._metadata("http://127.0.0.1:44444/callback")

        await proxy.register_client(first)
        await proxy.register_client(second)

        assert first.client_id != second.client_id

    async def test_distinct_storage_keys_yield_distinct_client_ids(self):
        """Ids are keyed per deployment, so they are not guessable from metadata alone."""
        first, second = self._metadata(), self._metadata()

        await _dcr_proxy(dcr_client_id_key="deployment-a").register_client(first)
        await _dcr_proxy(dcr_client_id_key="deployment-b").register_client(second)

        assert first.client_id != second.client_id

    async def test_redirect_validation_still_runs(self):
        """Deriving the id must not bypass super()'s redirect-URI validation."""
        proxy = _dcr_proxy(allowed_redirect_uris=["https://client.example/callback"])

        with pytest.raises(RegistrationError):
            await proxy.register_client(self._metadata("http://127.0.0.1:33418/callback"))

    async def test_derived_id_is_uuid_shaped(self):
        """Keeps the wire format identical to the SDK's uuid4()."""
        proxy = _dcr_proxy()
        info = self._metadata()

        await proxy.register_client(info)

        assert info.client_id is not None
        assert str(UUID(info.client_id)) == info.client_id

    async def test_registration_responses_carry_the_derived_id(self):
        """The whole fix rests on RegistrationHandler returning the object it passed us.

        Driving the real SDK handler proves the derived id reaches the client in
        the 201 body, not just the in-process model. A future SDK that copies
        client_info before responding would break the fix silently, so assert it
        here rather than trusting the in-place mutation.
        """
        proxy = _dcr_proxy()
        handler = RegistrationHandler(provider=proxy, options=ClientRegistrationOptions(enabled=True))
        body = {
            "redirect_uris": ["http://127.0.0.1:33418/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid profile",
            "token_endpoint_auth_method": "none",
        }

        # The two concurrent /register calls seen in mcp.log.
        issued = []
        for _ in range(2):
            request = MagicMock()
            request.json = AsyncMock(return_value=body)
            response = await handler.handle(request)
            assert response.status_code == 201
            issued.append(json.loads(bytes(response.body))["client_id"])

        assert issued[0] == issued[1]


class TestRejectedAuthorizationCodeIsLogged:
    async def test_warns_when_super_rejects_the_code(self, caplog):
        proxy = AdaptiveOidcProxy.__new__(AdaptiveOidcProxy)
        client = OAuthClientInformationFull(
            client_id="client-a",
            redirect_uris=[AnyUrl("http://127.0.0.1:33418/callback")],
        )

        with patch.object(OIDCProxy, "load_authorization_code", AsyncMock(return_value=None)):
            with caplog.at_level(logging.WARNING, logger="mcp_kubecost.config.oidc"):
                assert await proxy.load_authorization_code(client, "code-issued-to-someone-else") is None

        assert "Rejected authorization code for client client-a" in caplog.text

    async def test_silent_when_the_code_is_accepted(self, caplog):
        proxy = AdaptiveOidcProxy.__new__(AdaptiveOidcProxy)
        client = OAuthClientInformationFull(
            client_id="client-a",
            redirect_uris=[AnyUrl("http://127.0.0.1:33418/callback")],
        )
        code = MagicMock()

        with patch.object(OIDCProxy, "load_authorization_code", AsyncMock(return_value=code)):
            with caplog.at_level(logging.WARNING, logger="mcp_kubecost.config.oidc"):
                assert await proxy.load_authorization_code(client, "good-code") is code

        assert "Rejected authorization code" not in caplog.text


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

    def test_get_client_is_overridden_for_decryption_guard(self):
        # AdaptiveOidcProxy now owns get_client to catch DecryptionError and
        # return None (clean 401) instead of propagating an unhandled exception.
        assert AdaptiveOidcProxy.get_client is not OIDCProxy.get_client


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
    def test_open_posture_allows_ordinary_redirects_and_rejects_unsafe_schemes(self):
        assert validate_redirect_uri("https://unanticipated.example/callback", None) is True
        assert validate_redirect_uri("javascript:alert(1)", None) is False

    def test_restricted_posture_allows_configured_patterns_only(self):
        allowed = ["http://localhost:*", "https://client.example/callback"]
        assert validate_redirect_uri("http://localhost:54321/callback", allowed) is True
        assert validate_redirect_uri("https://client.example/callback", allowed) is True
        assert validate_redirect_uri("https://unanticipated.example/callback", allowed) is False

    def test_empty_restricted_list_rejects_client_redirects(self):
        assert validate_redirect_uri("https://client.example/callback", []) is False
