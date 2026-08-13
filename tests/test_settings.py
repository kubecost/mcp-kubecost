"""Tests for HTTP-mode detection and rich-logging defaults."""

from __future__ import annotations

import os

import fastmcp

from mcp_kubecost.config.settings import apply_http_rich_logging, get_settings, is_http_mode
from mcp_kubecost.errors import ConfigError


class TestIsHttpMode:
    def test_stdio_argv_is_not_http(self):
        assert is_http_mode(["mcp-kubecost"]) is False
        assert is_http_mode(["fastmcp", "run", "fastmcp.json"]) is False

    def test_fastmcp_http_config_is_http(self):
        assert is_http_mode(["fastmcp", "run", "fastmcp-http.json", "--skip-env"]) is True

    def test_transport_flag_is_http(self):
        assert is_http_mode(["fastmcp", "run", "server.py", "--transport", "http"]) is True
        assert is_http_mode(["fastmcp", "run", "server.py", "--transport=http"]) is True
        assert is_http_mode(["fastmcp", "run", "server.py", "-t", "sse"]) is True
        assert is_http_mode(["fastmcp", "run", "server.py", "--transport", "streamable-http"]) is True

    def test_stdio_transport_flag_is_not_http(self):
        assert is_http_mode(["fastmcp", "run", "server.py", "--transport", "stdio"]) is False

    def test_env_transport(self, monkeypatch):
        monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
        assert is_http_mode(["pytest"]) is True
        monkeypatch.setenv("FASTMCP_TRANSPORT", "stdio")
        assert is_http_mode(["pytest"]) is False


class TestApplyHttpRichLogging:
    def test_http_disables_fastmcp_rich_logging(self, monkeypatch):
        monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
        previous = fastmcp.settings.enable_rich_logging
        try:
            apply_http_rich_logging()
            assert os.environ["FASTMCP_ENABLE_RICH_LOGGING"] == "false"
            assert fastmcp.settings.enable_rich_logging is False
        finally:
            fastmcp.settings.enable_rich_logging = previous

    def test_stdio_leaves_fastmcp_setting_alone(self, monkeypatch):
        monkeypatch.delenv("FASTMCP_TRANSPORT", raising=False)
        monkeypatch.delenv("FASTMCP_ENABLE_RICH_LOGGING", raising=False)
        monkeypatch.setattr("sys.argv", ["pytest"])
        before = fastmcp.settings.enable_rich_logging
        apply_http_rich_logging()
        assert fastmcp.settings.enable_rich_logging is before


class TestEnableRichLoggingSetting:
    def test_http_defaults_false(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
        get_settings.cache_clear()
        try:
            assert get_settings().enable_rich_logging is False
        finally:
            get_settings.cache_clear()

    def test_stdio_respects_env(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.delenv("FASTMCP_TRANSPORT", raising=False)
        monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "false")
        monkeypatch.setattr("sys.argv", ["pytest"])
        get_settings.cache_clear()
        try:
            assert get_settings().enable_rich_logging is False
        finally:
            get_settings.cache_clear()


class TestRequireClientApiKeySetting:
    def test_defaults_off(self, monkeypatch):
        """Off by default — an unset flag must not change existing behavior."""
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.delenv("REQUIRE_CLIENT_API_KEY", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().require_client_api_key is False
        finally:
            get_settings.cache_clear()

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("REQUIRE_CLIENT_API_KEY", "true")
        get_settings.cache_clear()
        try:
            assert get_settings().require_client_api_key is True
        finally:
            get_settings.cache_clear()

    def test_api_key_is_redacted_in_logs(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("KUBECOST_API_KEY", "super-secret")
        get_settings.cache_clear()
        try:
            assert get_settings().to_loggable_dict()["KUBECOST_API_KEY"] == "***"
        finally:
            get_settings.cache_clear()


class TestOidcRedirectPathSetting:
    def test_defaults_to_fastmcp_callback(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.delenv("OIDC_REDIRECT_PATH", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_redirect_path == "/auth/callback"
        finally:
            get_settings.cache_clear()

    def test_reads_env_and_normalizes(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("OIDC_REDIRECT_PATH", "auth-mcp/")
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_redirect_path == "/auth-mcp"
        finally:
            get_settings.cache_clear()

    def test_rejects_url(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("OIDC_REDIRECT_PATH", "https://mcp.example/auth-mcp")
        get_settings.cache_clear()
        try:
            try:
                get_settings()
            except ConfigError as exc:
                assert "OIDC_REDIRECT_PATH" in str(exc)
            else:
                raise AssertionError("expected ConfigError")
        finally:
            get_settings.cache_clear()


class TestOidcVerifyIdTokenSetting:
    def test_defaults_off(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.delenv("OIDC_VERIFY_ID_TOKEN", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_verify_id_token is False
        finally:
            get_settings.cache_clear()

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("OIDC_VERIFY_ID_TOKEN", "true")
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_verify_id_token is True
        finally:
            get_settings.cache_clear()
