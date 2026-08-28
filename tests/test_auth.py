"""Tests for X-API-KEY resolution: client header, env fallback, and the gate flag."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from mcp_kubecost.auth import (
    MissingClientApiKeyError,
    client_supplied_api_key,
    resolve_api_key,
)
from mcp_kubecost.config.settings import AuthMode, Settings

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
    auth_mode=AuthMode.NONE,
    oidc_issuer_url=None,
    oidc_client_id=None,
    oidc_client_secret=None,
    oidc_audience=None,
    oidc_base_url=None,
    oidc_redirect_path="/auth-mcp",
    oidc_required_scopes=["openid", "profile"],
)


def _settings(**overrides: Any) -> Settings:
    return Settings(**{**_SETTINGS, **overrides})


def _resolve(headers: dict[str, str], *, http_mode: bool = True, **overrides: Any) -> str | None:
    with (
        patch("mcp_kubecost.auth.get_http_headers", return_value=headers),
        patch("mcp_kubecost.auth.get_settings", return_value=_settings(**overrides)),
        patch("mcp_kubecost.auth.is_http_mode", return_value=http_mode),
    ):
        return resolve_api_key()


class TestClientSuppliedApiKey:
    def test_reads_lowercased_header(self):
        with patch("mcp_kubecost.auth.get_http_headers", return_value={"x-api-key": "abc"}):
            assert client_supplied_api_key() == "abc"

    def test_absent_header_is_none(self):
        with patch("mcp_kubecost.auth.get_http_headers", return_value={"user-agent": "x"}):
            assert client_supplied_api_key() is None

    def test_stdio_empty_headers_is_none(self):
        """get_http_headers() returns {} with no active HTTP request."""
        with patch("mcp_kubecost.auth.get_http_headers", return_value={}):
            assert client_supplied_api_key() is None

    def test_whitespace_only_header_is_none(self):
        with patch("mcp_kubecost.auth.get_http_headers", return_value={"x-api-key": "  "}):
            assert client_supplied_api_key() is None


class TestResolveApiKey:
    def test_header_wins_over_env(self):
        assert _resolve({"x-api-key": "caller"}, KUBECOST_API_KEY="env") == "caller"

    def test_env_used_when_no_header(self):
        assert _resolve({}, KUBECOST_API_KEY="env") == "env"

    def test_none_when_neither_configured(self):
        assert _resolve({}) is None

    def test_header_used_when_env_unset(self):
        assert _resolve({"x-api-key": "caller"}) == "caller"


class TestRequireClientApiKey:
    def test_http_without_header_raises(self):
        with pytest.raises(MissingClientApiKeyError):
            _resolve({}, require_client_api_key=True, KUBECOST_API_KEY="env")

    def test_http_with_header_passes(self):
        assert _resolve({"x-api-key": "caller"}, require_client_api_key=True) == "caller"

    def test_gate_rejects_before_env_fallback(self):
        """A configured env key must not satisfy a flag that demands a caller key."""
        with pytest.raises(MissingClientApiKeyError):
            _resolve({}, require_client_api_key=True, KUBECOST_API_KEY="env")

    def test_stdio_is_never_gated(self):
        """STDIO callers cannot send headers, so the flag must not apply there."""
        assert _resolve({}, http_mode=False, require_client_api_key=True, KUBECOST_API_KEY="env") == "env"

    def test_disabled_by_default_no_raise(self):
        assert _resolve({}) is None
