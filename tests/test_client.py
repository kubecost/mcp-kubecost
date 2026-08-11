"""Tests for client.py error mapping and USE_CAC_VIEWS param injection."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest
from pytest_httpx import HTTPXMock

from mcp_kubecost.client import KubecostClientError, _build_params, get
from mcp_kubecost.config.settings import Settings
from mcp_kubecost.errors import ErrorCode


class TestKubecostClientErrorToToolError:
    def _make(self, status_code: int) -> KubecostClientError:
        return KubecostClientError(
            status_code=status_code, message="err", url="http://x/model/savings", path="/model/savings"
        )

    def test_401_authentication_failed(self):
        te = self._make(401).to_tool_error()
        assert te.code == ErrorCode.AUTHENTICATION_FAILED
        assert te.retryable is False

    def test_403_permission_denied(self):
        te = self._make(403).to_tool_error()
        assert te.code == ErrorCode.PERMISSION_DENIED
        assert te.retryable is False
        assert "http://x" not in te.message
        assert "/model/savings" in te.message

    def test_404_not_found(self):
        te = self._make(404).to_tool_error()
        assert te.code == ErrorCode.NOT_FOUND
        assert te.retryable is False
        assert "http://x" not in te.message
        assert "/model/savings" in te.message

    def test_429_rate_limited(self):
        te = self._make(429).to_tool_error()
        assert te.code == ErrorCode.RATE_LIMITED
        assert te.retryable is True
        assert te.context["retry_after_seconds"] == 30

    def test_500_server_error(self):
        te = self._make(500).to_tool_error()
        assert te.code == ErrorCode.UPSTREAM_TIMEOUT
        assert te.retryable is True

    def test_503_server_error(self):
        te = self._make(503).to_tool_error()
        assert te.code == ErrorCode.UPSTREAM_TIMEOUT

    def test_400_bad_request(self):
        te = self._make(400).to_tool_error()
        assert te.code == ErrorCode.DATA_UNAVAILABLE
        assert te.retryable is False

    def test_str_contains_status_and_url(self):
        exc = self._make(404)
        assert "404" in str(exc)
        assert "http://x" in str(exc)


# ---------------------------------------------------------------------------
# _build_params — USE_CAC_VIEWS injection
# ---------------------------------------------------------------------------

_BASE_SETTINGS: dict[str, Any] = dict(
    kubecost_base_url="http://localhost:9090",
    kubecost_api_base_path="/model",
    KUBECOST_API_KEY=None,
    ssl_verify=True,
    request_timeout_seconds=15.0,
    retry_count=2,
    default_window="15d",
    show_banner=False,
    log_level="INFO",
    enable_rich_logging=True,
)


def _settings(use_cac_views: bool) -> Settings:
    return Settings(**_BASE_SETTINGS, use_cac_views=use_cac_views)


class TestBuildParams:
    def test_cac_views_false_no_view_id_added(self):
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(False)):
            result = _build_params({"window": "7d"})
        assert "viewId" not in result

    def test_cac_views_false_none_input_returns_none(self):
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(False)):
            result = _build_params(None)
        assert result is None

    def test_cac_views_true_adds_view_id_zero(self):
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(True)):
            result = _build_params({"window": "7d"})
        assert result["viewId"] == 0
        assert result["window"] == "7d"

    def test_cac_views_true_none_input_adds_view_id_zero(self):
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(True)):
            result = _build_params(None)
        assert result == {"viewId": 0}

    def test_cac_views_true_does_not_override_existing_view_id(self):
        """Caller-provided viewId must not be overwritten."""
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(True)):
            result = _build_params({"viewId": 5})
        assert result["viewId"] == 5

    def test_cac_views_true_preserves_all_caller_params(self):
        caller = {"window": "30d", "aggregate": "namespace", "accumulate": True}
        with patch("mcp_kubecost.client.get_settings", return_value=_settings(True)):
            result = _build_params(caller)
        assert result["window"] == "30d"
        assert result["aggregate"] == "namespace"
        assert result["viewId"] == 0


# ---------------------------------------------------------------------------
# Integration: viewId appears on the outbound HTTP request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sends_view_id_when_cac_views_enabled(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost:9090/model/allocation"),
        json={"data": []},
    )
    with patch("mcp_kubecost.client.get_settings", return_value=_settings(True)):
        await get("/model/allocation", params={"window": "7d"})

    request = httpx_mock.get_request()
    assert request is not None
    assert "viewId=0" in str(request.url)


@pytest.mark.asyncio
async def test_get_omits_view_id_when_cac_views_disabled(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost:9090/model/allocation"),
        json={"data": []},
    )
    with patch("mcp_kubecost.client.get_settings", return_value=_settings(False)):
        await get("/model/allocation", params={"window": "7d"})

    request = httpx_mock.get_request()
    assert request is not None
    assert "viewId" not in str(request.url)
