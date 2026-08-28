"""Tests for HTTP-mode detection and rich-logging defaults."""

from __future__ import annotations

import os
import subprocess
import sys

import fastmcp
import pytest

from mcp_kubecost.config.settings import AuthMode, _get_auth_mode, apply_http_rich_logging, get_settings, is_http_mode
from mcp_kubecost.errors import ConfigError


def _load_settings(monkeypatch, **environment: str):
    monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


class TestIsHttpMode:
    def test_stdio_argv_is_not_http(self):
        assert is_http_mode(["mcp-kubecost"]) is False
        assert is_http_mode(["fastmcp", "run", "config/fastmcp.json"]) is False

    def test_fastmcp_http_config_is_http(self):
        assert is_http_mode(["fastmcp", "run", "config/fastmcp-http.json", "--skip-env"]) is True

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


class TestRequestSettings:
    def test_rejects_non_positive_timeout(self, monkeypatch):
        for value in ("0", "-0.1"):
            with pytest.raises(ConfigError, match="REQUEST_TIMEOUT_SECONDS must be greater than 0"):
                _load_settings(monkeypatch, REQUEST_TIMEOUT_SECONDS=value)

    def test_rejects_negative_retry_count(self, monkeypatch):
        with pytest.raises(ConfigError, match="REQUEST_RETRY_COUNT must be 0 or greater"):
            _load_settings(monkeypatch, REQUEST_RETRY_COUNT="-1")

    def test_default_window_propagates_to_tool_default_constant(self, monkeypatch):
        environment = os.environ.copy()
        environment["KUBECOST_BASE_URL"] = "http://localhost:9090"
        environment["DEFAULT_WINDOW"] = "30d"
        result = subprocess.run(
            [sys.executable, "-c", "from mcp_kubecost.tools._common import DEFAULT_WINDOW; print(DEFAULT_WINDOW)"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert result.stdout.strip() == "30d"


class TestAuthModeSetting:
    def test_both_is_rejected(self, monkeypatch):
        """AUTH_MODE=both was removed; it must raise ConfigError with migration hint."""
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("AUTH_MODE", "both")
        get_settings.cache_clear()
        try:
            try:
                get_settings()
            except ConfigError as exc:
                msg = str(exc)
                assert "AUTH_MODE='both' was removed" in msg
                assert "REQUIRE_CLIENT_API_KEY=true" in msg
            else:
                raise AssertionError("expected ConfigError for AUTH_MODE=both")
        finally:
            get_settings.cache_clear()

    def test_valid_modes_accepted(self, monkeypatch):
        for mode, expected in (
            ("none", AuthMode.NONE),
            ("open", AuthMode.OPEN),
            ("oidc", AuthMode.OIDC),
            ("api_key", AuthMode.API_KEY),
        ):
            monkeypatch.setenv("AUTH_MODE", mode)
            assert _get_auth_mode() == expected

    def test_open_does_not_require_oidc_vars(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("AUTH_MODE", "open")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.auth_mode is AuthMode.OPEN
            assert settings.require_client_api_key is False
        finally:
            get_settings.cache_clear()

    def test_api_key_implies_require_client_api_key(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("AUTH_MODE", "api_key")
        monkeypatch.delenv("REQUIRE_CLIENT_API_KEY", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().require_client_api_key is True
        finally:
            get_settings.cache_clear()


class TestOidcRedirectPathSetting:
    def test_defaults_to_auth_mcp(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.delenv("OIDC_REDIRECT_PATH", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_redirect_path == "/auth-mcp"
        finally:
            get_settings.cache_clear()

    def test_reads_env_and_normalizes(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("OIDC_REDIRECT_PATH", "auth/callback/")
        get_settings.cache_clear()
        try:
            assert get_settings().oidc_redirect_path == "/auth/callback"
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

    def test_rejects_dot_dot(self, monkeypatch):
        monkeypatch.setenv("KUBECOST_BASE_URL", "http://localhost:9090")
        monkeypatch.setenv("OIDC_REDIRECT_PATH", "/../auth-mcp")
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
