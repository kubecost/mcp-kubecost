"""Tests for client.py error mapping and USE_CAC_VIEWS param injection."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from mcp_kubecost.client import KubecostClientError, _build_params, get, kubecost_client_lifespan, post
from mcp_kubecost.config.settings import AuthMode, Settings
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
    require_client_api_key=False,
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


# ---------------------------------------------------------------------------
# Integration: the API key travels as an X-API-KEY header
# ---------------------------------------------------------------------------


def _auth_settings(**overrides: Any) -> Settings:
    """Settings for the auth path; overrides apply on top of the base dict."""
    merged: dict[str, Any] = {**_BASE_SETTINGS, "use_cac_views": False, **overrides}
    return Settings(**merged)


async def _get_with(headers: dict[str, str], settings: Settings) -> Any:
    """Run a GET with a stubbed inbound header set and stubbed settings."""
    with (
        patch("mcp_kubecost.auth.get_http_headers", return_value=headers),
        patch("mcp_kubecost.auth.get_settings", return_value=settings),
        patch("mcp_kubecost.client.get_settings", return_value=settings),
    ):
        return await get("/model/allocation")


def _stub_allocation(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost:9090/model/allocation"),
        json={"data": []},
    )


def _sent_headers(httpx_mock: HTTPXMock) -> httpx.Headers:
    """Headers of the request that actually went out."""
    request = httpx_mock.get_request()
    assert request is not None
    return request.headers


@pytest.mark.asyncio
async def test_env_api_key_is_sent_as_x_api_key_header(httpx_mock: HTTPXMock):
    """KUBECOST_API_KEY goes out as X-API-KEY, never as Basic auth."""
    _stub_allocation(httpx_mock)
    await _get_with({}, _auth_settings(KUBECOST_API_KEY="env-key"))

    headers = _sent_headers(httpx_mock)
    assert headers["X-API-KEY"] == "env-key"
    assert "authorization" not in headers


@pytest.mark.asyncio
async def test_client_header_overrides_env_api_key(httpx_mock: HTTPXMock):
    _stub_allocation(httpx_mock)
    await _get_with({"x-api-key": "caller-key"}, _auth_settings(KUBECOST_API_KEY="env-key"))

    assert _sent_headers(httpx_mock)["X-API-KEY"] == "caller-key"


@pytest.mark.asyncio
async def test_client_header_used_when_no_env_key(httpx_mock: HTTPXMock):
    _stub_allocation(httpx_mock)
    await _get_with({"x-api-key": "caller-key"}, _auth_settings())

    assert _sent_headers(httpx_mock)["X-API-KEY"] == "caller-key"


@pytest.mark.asyncio
async def test_no_key_anywhere_sends_no_auth_header(httpx_mock: HTTPXMock):
    """The unconfigured default: an unauthenticated request, as before."""
    _stub_allocation(httpx_mock)
    await _get_with({}, _auth_settings())

    headers = _sent_headers(httpx_mock)
    assert "x-api-key" not in headers
    assert "authorization" not in headers


@pytest.mark.asyncio
async def test_blank_client_header_falls_back_to_env_key(httpx_mock: HTTPXMock):
    _stub_allocation(httpx_mock)
    await _get_with({"x-api-key": "   "}, _auth_settings(KUBECOST_API_KEY="env-key"))

    assert _sent_headers(httpx_mock)["X-API-KEY"] == "env-key"


# ---------------------------------------------------------------------------
# post() requires a key from either source
# ---------------------------------------------------------------------------


async def _post_with(headers: dict[str, str], settings: Settings) -> Any:
    with (
        patch("mcp_kubecost.auth.get_http_headers", return_value=headers),
        patch("mcp_kubecost.auth.get_settings", return_value=settings),
        patch("mcp_kubecost.client.get_settings", return_value=settings),
    ):
        return await post("/model/snooze", json={"id": 1})


@pytest.mark.asyncio
async def test_post_accepts_client_supplied_key(httpx_mock: HTTPXMock):
    """A caller-supplied key satisfies post()'s auth requirement."""
    httpx_mock.add_response(method="POST", url=re.compile(r"http://localhost:9090/model/snooze"), json={"ok": True})
    await _post_with({"x-api-key": "caller-key"}, _auth_settings())

    assert _sent_headers(httpx_mock)["X-API-KEY"] == "caller-key"


@pytest.mark.asyncio
async def test_post_raises_when_no_key_available():
    """_common.py matches on this message prefix, so it must not drift."""
    with pytest.raises(ValueError, match="^No authentication configured"):
        await _post_with({}, _auth_settings())


# ---------------------------------------------------------------------------
# Timeout and retries from settings
# ---------------------------------------------------------------------------


def _allocation_url() -> re.Pattern[str]:
    return re.compile(r"http://localhost:9090/model/allocation")


@pytest.mark.asyncio
async def test_get_uses_configured_timeout(httpx_mock: HTTPXMock):
    _stub_allocation(httpx_mock)
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    with patch("mcp_kubecost.client.httpx.AsyncClient", side_effect=factory):
        await _get_with({}, _auth_settings(request_timeout_seconds=7.5))

    assert captured["timeout"] == 7.5


@pytest.mark.asyncio
async def test_get_retries_connect_error_then_succeeds(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})

    with patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await _get_with({}, _auth_settings(retry_count=2))

    assert result == {"data": []}
    assert len(httpx_mock.get_requests()) == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_retries_503_then_succeeds(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=503, text="unavailable")
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})

    with patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock):
        result = await _get_with({}, _auth_settings(retry_count=2))

    assert result == {"data": []}
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_get_does_not_retry_client_errors(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=400, text="bad request")

    with pytest.raises(KubecostClientError) as exc_info:
        await _get_with({}, _auth_settings(retry_count=2))

    assert exc_info.value.status_code == 400
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_exhausts_retries_on_503(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=503, text="unavailable")

    with (
        patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(KubecostClientError) as exc_info,
    ):
        await _get_with({}, _auth_settings(retry_count=2))

    assert exc_info.value.status_code == 503
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_get_retry_count_zero_does_not_retry(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))

    with pytest.raises(httpx.ConnectError):
        await _get_with({}, _auth_settings(retry_count=0))

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_reuses_one_client_across_retries(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=503)
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})
    real_client = httpx.AsyncClient
    clients_created = 0

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        nonlocal clients_created
        clients_created += 1
        return real_client(*args, **kwargs)

    with (
        patch("mcp_kubecost.client.httpx.AsyncClient", side_effect=factory),
        patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _get_with({}, _auth_settings(retry_count=2))

    assert clients_created == 1


@pytest.mark.asyncio
async def test_lifespan_reuses_client_across_calls_and_closes_it(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"call": 1})
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"call": 2})
    real_client = httpx.AsyncClient
    created: list[httpx.AsyncClient] = []

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    settings = _auth_settings()
    with (
        patch("mcp_kubecost.client.httpx.AsyncClient", side_effect=factory),
        patch("mcp_kubecost.client.get_settings", return_value=settings),
        patch("mcp_kubecost.auth.get_settings", return_value=settings),
        patch("mcp_kubecost.auth.get_http_headers", return_value={}),
    ):
        async with kubecost_client_lifespan(None):
            assert await get("/model/allocation") == {"call": 1}
            assert await get("/model/allocation") == {"call": 2}
            assert len(created) == 1
            assert created[0].is_closed is False

    assert created[0].is_closed is True


@pytest.mark.asyncio
async def test_post_does_not_retry_transient_status(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="POST", url=re.compile(r"http://localhost:9090/model/snooze"), status_code=503)

    with pytest.raises(KubecostClientError) as exc_info:
        await _post_with({}, _auth_settings(KUBECOST_API_KEY="env-key", retry_count=2))

    assert exc_info.value.status_code == 503
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_post_does_not_retry_transport_error(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))

    with pytest.raises(httpx.ConnectError):
        await _post_with({}, _auth_settings(KUBECOST_API_KEY="env-key", retry_count=2))

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_uses_full_jitter_backoff(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=503)
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})

    with (
        patch("mcp_kubecost.client.random.uniform", return_value=0.125) as uniform,
        patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        await _get_with({}, _auth_settings(retry_count=1))

    uniform.assert_called_once_with(0.0, 0.25)
    sleep.assert_awaited_once_with(0.125)


@pytest.mark.asyncio
async def test_get_honors_numeric_retry_after_with_cap(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=429, headers={"Retry-After": "90"})
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})

    with patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await _get_with({}, _auth_settings(retry_count=1))

    sleep.assert_awaited_once_with(30.0)


@pytest.mark.asyncio
async def test_get_honors_http_date_retry_after(httpx_mock: HTTPXMock):
    retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=10), usegmt=True)
    httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=503, headers={"Retry-After": retry_at})
    httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})

    with patch("mcp_kubecost.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await _get_with({}, _auth_settings(retry_count=1))

    sleep.assert_awaited_once()
    delay = sleep.await_args_list[0].args[0]
    assert 8.0 <= delay <= 10.0
