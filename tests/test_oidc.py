"""Tests for OIDC provider construction."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

from fastmcp.server.auth.oidc_proxy import OIDCProxy
from key_value.aio.stores.memory import MemoryStore

from mcp_kubecost.config.oidc import create_oidc_provider
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
    show_banner=False,
    log_level="INFO",
    enable_rich_logging=True,
    auth_mode=AuthMode.NONE,
    oidc_issuer_url=None,
    oidc_client_id=None,
    oidc_client_secret=None,
    oidc_audience=None,
    oidc_base_url=None,
    oidc_redirect_path="/auth-mcp",
    oidc_verify_id_token=False,
    oidc_required_scopes=["openid", "profile"],
)

_OIDC = dict(
    auth_mode=AuthMode.OIDC,
    oidc_issuer_url="https://idp.example/.well-known/openid-configuration",
    oidc_client_id="client",
    oidc_client_secret="secret",
    oidc_base_url="https://mcp.example",
)


def _settings(**overrides: Any) -> Settings:
    return Settings(**{**_SETTINGS, **overrides})


class TestCreateOidcProvider:
    def test_none_when_auth_disabled(self):
        assert create_oidc_provider(_settings()) is None

    def test_uses_in_memory_client_storage(self):
        with patch("mcp_kubecost.config.oidc.OIDCProxy") as proxy:
            create_oidc_provider(_settings(**_OIDC))
        kwargs = proxy.call_args.kwargs
        assert isinstance(kwargs["client_storage"], MemoryStore)
        assert kwargs["require_authorization_consent"] == "external"
        assert kwargs["redirect_path"] == "/auth-mcp"
        assert kwargs["verify_id_token"] is False

    def test_forwards_verify_id_token(self):
        with patch("mcp_kubecost.config.oidc.OIDCProxy") as proxy:
            create_oidc_provider(_settings(**_OIDC, oidc_verify_id_token=True))
        assert proxy.call_args.kwargs["verify_id_token"] is True

    def test_forwards_dedicated_host_redirect_path(self):
        with patch("mcp_kubecost.config.oidc.OIDCProxy") as proxy:
            create_oidc_provider(_settings(**_OIDC, oidc_redirect_path="/auth/callback"))
        assert proxy.call_args.kwargs["redirect_path"] == "/auth/callback"

    def test_api_key_mode_does_not_build_proxy(self):
        with patch("mcp_kubecost.config.oidc.OIDCProxy") as proxy:
            assert create_oidc_provider(_settings(auth_mode=AuthMode.API_KEY)) is None
        proxy.assert_not_called()

    def test_init_failure_is_config_error_not_traceback(self):
        with patch("mcp_kubecost.config.oidc.OIDCProxy", side_effect=ValueError("expected value at line 1 column 1")):
            try:
                create_oidc_provider(_settings(**_OIDC))
            except ConfigError as exc:
                assert "OIDC provider initialization failed" in str(exc)
                assert "HTML" in str(exc)
            else:
                raise AssertionError("expected ConfigError")


class TestOidcProxyKwargConformance:
    """`test_uses_in_memory_client_storage` et al. patch OIDCProxy wholesale, so a
    kwarg removed upstream would still pass those tests. Assert the kwargs we
    pass still exist on the real signature, so a FastMCP bump fails a test
    instead of failing at server startup.
    """

    def test_kwargs_still_exist_on_oidc_proxy(self):
        sig = inspect.signature(OIDCProxy.__init__)
        for kwarg in (
            "redirect_path",
            "verify_id_token",
            "client_storage",
            "require_authorization_consent",
            "audience",
        ):
            assert kwarg in sig.parameters, f"OIDCProxy.__init__ no longer accepts {kwarg!r}"
