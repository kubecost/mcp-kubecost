"""Tests for OIDC provider construction."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

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
