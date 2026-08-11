"""Tests for HTTP-mode detection and rich-logging defaults."""

from __future__ import annotations

import os

import fastmcp

from mcp_kubecost.config.settings import apply_http_rich_logging, get_settings, is_http_mode


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
