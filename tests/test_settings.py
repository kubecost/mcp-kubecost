"""Tests for HTTP-mode detection and rich-logging defaults."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap

import fastmcp
import pytest
from cryptography.fernet import Fernet

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
    def test_default_timeout_is_300(self, monkeypatch):
        monkeypatch.delenv("REQUEST_TIMEOUT_SECONDS", raising=False)
        assert _load_settings(monkeypatch).request_timeout_seconds == 300.0

    def test_rejects_non_positive_timeout(self, monkeypatch):
        for value in ("0", "-0.1"):
            with pytest.raises(ConfigError, match="REQUEST_TIMEOUT_SECONDS must be greater than 0"):
                _load_settings(monkeypatch, REQUEST_TIMEOUT_SECONDS=value)

    def test_tool_call_deadline(self, monkeypatch):
        assert _load_settings(monkeypatch, MCP_TOOL_CALL_TIMEOUT_SECONDS="42").tool_call_timeout_seconds == 42.0
        with pytest.raises(ConfigError, match="MCP_TOOL_CALL_TIMEOUT_SECONDS must be greater than 0"):
            _load_settings(monkeypatch, MCP_TOOL_CALL_TIMEOUT_SECONDS="0")

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


class TestDefaultViewIdSetting:
    def test_unset_means_no_view_scope(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_VIEW_ID", raising=False)
        assert _load_settings(monkeypatch).default_view_id is None

    def test_blank_is_treated_as_unset(self, monkeypatch):
        assert _load_settings(monkeypatch, DEFAULT_VIEW_ID="   ").default_view_id is None

    @pytest.mark.parametrize("value", ["0", "7", "1234"])
    def test_accepts_non_negative_integers(self, monkeypatch, value):
        assert _load_settings(monkeypatch, DEFAULT_VIEW_ID=value).default_view_id == value

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        assert _load_settings(monkeypatch, DEFAULT_VIEW_ID=" 12 ").default_view_id == "12"

    @pytest.mark.parametrize("value", ["-1", "abc", "1.5", "0x0", "1,2"])
    def test_rejects_non_integer_values(self, monkeypatch, value):
        with pytest.raises(ConfigError, match="DEFAULT_VIEW_ID must be a non-negative integer"):
            _load_settings(monkeypatch, DEFAULT_VIEW_ID=value)

    def test_reaches_the_tool_schema_default(self, monkeypatch):
        """`view_id` defaults are baked into the tool signatures at import time, so the
        env var has to be visible to a fresh interpreter to land in the schema."""
        environment = os.environ.copy()
        environment["KUBECOST_BASE_URL"] = "http://localhost:9090"
        environment["DEFAULT_VIEW_ID"] = "9"
        program = textwrap.dedent("""
            import asyncio, json
            from fastmcp import FastMCP
            from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools

            mcp = FastMCP("t")
            register_kubecost_tools(mcp)
            tools = asyncio.run(mcp.list_tools())
            print(
                json.dumps(
                    {
                        tool.name: tool.parameters["properties"]["view_id"].get("default")
                        for tool in tools
                        if "view_id" in tool.parameters["properties"]
                    }
                )
            )
            """)
        result = subprocess.run(
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        defaults = json.loads(result.stdout)
        assert defaults, "no tool exposed a view_id parameter"
        assert set(defaults.values()) == {"9"}


class TestRequestLimitSettings:
    def test_defaults(self, monkeypatch):
        settings = _load_settings(monkeypatch)
        assert settings.rate_limit_requests_per_second == 10.0
        assert settings.rate_limit_burst_capacity == 20
        assert settings.max_concurrent_tool_calls == 10

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("MCP_RATE_LIMIT_REQUESTS_PER_SECOND", "0"),
            ("MCP_RATE_LIMIT_BURST_CAPACITY", "0"),
            ("MCP_MAX_CONCURRENT_TOOL_CALLS", "-1"),
        ],
    )
    def test_rejects_non_positive_values(self, monkeypatch, name, value):
        with pytest.raises(ConfigError, match=f"{name} must be greater than 0"):
            _load_settings(monkeypatch, **{name: value})


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

    _OIDC_BASE = {
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER_URL": "https://idp.example/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
        "MCP_EXTERNAL_URL": "https://mcp.example",
    }

    def test_oidc_requires_external_url(self, monkeypatch):
        environment = {**self._OIDC_BASE}
        environment.pop("MCP_EXTERNAL_URL")
        with pytest.raises(ConfigError, match="MCP_EXTERNAL_URL"):
            _load_settings(monkeypatch, **environment)

    @pytest.mark.parametrize(
        "value",
        [
            "kubecost.example.com",
            "http://kubecost.example.com",
            "https://kubecost.example.com/path",
            "https://user@kubecost.example.com",
            "https://kubecost.example.com?query=yes",
            "https://kubecost.example.com#fragment",
        ],
    )
    def test_oidc_external_url_must_be_a_secure_origin(self, monkeypatch, value):
        with pytest.raises(ConfigError, match="MCP_EXTERNAL_URL"):
            _load_settings(monkeypatch, **{**self._OIDC_BASE, "MCP_EXTERNAL_URL": value})

    def test_oidc_external_url_drops_trailing_slash(self, monkeypatch):
        settings = _load_settings(monkeypatch, **{**self._OIDC_BASE, "MCP_EXTERNAL_URL": "https://mcp.example/"})
        assert settings.external_url == "https://mcp.example"

    @pytest.mark.parametrize("value", ["http://localhost:3030", "http://127.0.0.1:3030"])
    def test_oidc_external_url_allows_local_http(self, monkeypatch, value):
        settings = _load_settings(monkeypatch, **{**self._OIDC_BASE, "MCP_EXTERNAL_URL": value})
        assert settings.external_url == value

    def test_oidc_starts_without_durable_storage_secrets(self, monkeypatch):
        settings = _load_settings(monkeypatch, **self._OIDC_BASE)
        assert settings.oidc_jwt_signing_key is not None
        assert settings.oidc_storage_encryption_key is not None
        assert settings.oidc_ephemeral_keys is True

    def test_oidc_auto_generated_keys_have_correct_format(self, monkeypatch):
        settings = _load_settings(monkeypatch, **self._OIDC_BASE)
        # JWT signing key: hex string, minimum 32 chars
        assert settings.oidc_jwt_signing_key is not None
        assert len(settings.oidc_jwt_signing_key) >= 32
        # Storage encryption key: valid 44-char Fernet key
        assert settings.oidc_storage_encryption_key is not None
        assert len(settings.oidc_storage_encryption_key) == 44
        Fernet(settings.oidc_storage_encryption_key.encode())  # must not raise

    def test_oidc_auto_generated_keys_emit_warnings(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="mcp_kubecost.config.settings"):
            _load_settings(monkeypatch, **self._OIDC_BASE)
        messages = [r.message for r in caplog.records]
        assert any("OIDC_JWT_SIGNING_KEY" in m for m in messages)
        assert any("OIDC_STORAGE_ENCRYPTION_KEY" in m for m in messages)

    def test_oidc_auto_generated_keys_differ_on_reload(self, monkeypatch):
        s1 = _load_settings(monkeypatch, **self._OIDC_BASE)
        s2 = _load_settings(monkeypatch, **self._OIDC_BASE)
        assert s1.oidc_jwt_signing_key != s2.oidc_jwt_signing_key
        assert s1.oidc_storage_encryption_key != s2.oidc_storage_encryption_key

    def test_oidc_explicit_keys_are_not_ephemeral(self, monkeypatch):
        settings = _load_settings(
            monkeypatch,
            **self._OIDC_BASE,
            OIDC_JWT_SIGNING_KEY="j" * 32,
            OIDC_STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        )
        assert settings.oidc_ephemeral_keys is False

    def test_generated_jwt_key_alone_is_not_ephemeral(self, monkeypatch):
        """A generated signing key must not wipe durable storage.

        oidc_ephemeral_keys drives shutil.rmtree() of the storage directory in
        build_oidc_provider(). State on disk is encrypted with the Fernet key, not
        signed with the JWT key, so a persisted Fernet key means that state is still
        readable and must survive. Clients simply re-authorize against the new
        signing key.
        """
        settings = _load_settings(
            monkeypatch,
            **self._OIDC_BASE,
            OIDC_STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        )
        assert settings.oidc_jwt_signing_key is not None  # generated
        assert settings.oidc_ephemeral_keys is False

    def test_generated_storage_key_alone_is_ephemeral(self, monkeypatch):
        """A generated Fernet key leaves existing state impossible to decrypt, so it must wipe."""
        settings = _load_settings(
            monkeypatch,
            **self._OIDC_BASE,
            OIDC_JWT_SIGNING_KEY="j" * 32,
        )
        assert settings.oidc_jwt_signing_key == "j" * 32
        assert settings.oidc_ephemeral_keys is True

    def test_oidc_storage_secrets_are_redacted(self, monkeypatch):
        settings = _load_settings(
            monkeypatch,
            AUTH_MODE="oidc",
            OIDC_ISSUER_URL="https://idp.example/.well-known/openid-configuration",
            OIDC_CLIENT_ID="client",
            OIDC_CLIENT_SECRET="secret",
            MCP_EXTERNAL_URL="https://mcp.example",
            OIDC_JWT_SIGNING_KEY="j" * 32,
            OIDC_STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        )
        logged = settings.to_loggable_dict()
        assert logged["oidc_jwt_signing_key"] == "***"
        assert logged["oidc_storage_encryption_key"] == "***"

    def test_oidc_storage_path_is_absolute(self, monkeypatch):
        with pytest.raises(ConfigError, match="OIDC_STORAGE_PATH"):
            _load_settings(monkeypatch, OIDC_STORAGE_PATH="relative/oauth")

    def test_oidc_storage_path_rejects_parent_traversal(self, monkeypatch):
        with pytest.raises(ConfigError, match="must not contain"):
            _load_settings(monkeypatch, OIDC_STORAGE_PATH="/var/lib/mcp-kubecost/../../etc/oauth")

    @pytest.mark.parametrize("raw", ["/", "/var", "/var/", "//var//"])
    def test_oidc_storage_path_rejects_shallow_paths(self, monkeypatch, raw):
        """build_oidc_provider() rmtree()s this directory, so '/' or '/var' must not pass."""
        with pytest.raises(ConfigError, match="nested directory"):
            _load_settings(monkeypatch, OIDC_STORAGE_PATH=raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/var/lib/mcp-kubecost/oauth/", "/var/lib/mcp-kubecost/oauth"),
            ("/var/lib/mcp-kubecost/./oauth", "/var/lib/mcp-kubecost/oauth"),
            ("//var//lib//oauth", "/var/lib/oauth"),
        ],
    )
    def test_oidc_storage_path_is_normalized(self, monkeypatch, raw, expected):
        assert _load_settings(monkeypatch, OIDC_STORAGE_PATH=raw).oidc_storage_path == expected

    def test_oidc_storage_path_defaults_to_standard_state_directory(self, monkeypatch):
        monkeypatch.delenv("OIDC_STORAGE_PATH", raising=False)
        assert _load_settings(monkeypatch).oidc_storage_path == "/var/lib/mcp-kubecost/oauth"

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


class TestOidcAllowedClientRedirectUrisSetting:
    def test_unset_and_blank_values_default_to_open_posture(self, monkeypatch):
        monkeypatch.delenv("OIDC_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
        assert _load_settings(monkeypatch).oidc_allowed_client_redirect_uris is None
        assert (
            _load_settings(monkeypatch, OIDC_ALLOWED_CLIENT_REDIRECT_URIS="   ").oidc_allowed_client_redirect_uris
            is None
        )

    def test_parses_and_normalizes_json_patterns(self, monkeypatch):
        settings = _load_settings(
            monkeypatch,
            OIDC_ALLOWED_CLIENT_REDIRECT_URIS='[" http://localhost:* ", "https://client.example/callback"]',
        )
        assert settings.oidc_allowed_client_redirect_uris == [
            "http://localhost:*",
            "https://client.example/callback",
        ]
        assert settings.to_loggable_dict()["oidc_allowed_client_redirect_uris"] == [
            "http://localhost:*",
            "https://client.example/callback",
        ]

    def test_preserves_explicit_empty_list(self, monkeypatch):
        settings = _load_settings(monkeypatch, OIDC_ALLOWED_CLIENT_REDIRECT_URIS="[]")
        assert settings.oidc_allowed_client_redirect_uris == []

    @pytest.mark.parametrize(
        "value",
        [
            "not-json",
            '"https://client.example/callback"',
            '["https://client.example/callback", 1]',
            '[""]',
        ],
    )
    def test_rejects_invalid_values(self, monkeypatch, value):
        with pytest.raises(ConfigError, match="OIDC_ALLOWED_CLIENT_REDIRECT_URIS"):
            _load_settings(monkeypatch, OIDC_ALLOWED_CLIENT_REDIRECT_URIS=value)
