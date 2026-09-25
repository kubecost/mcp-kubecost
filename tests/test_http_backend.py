"""Tests for the injectable HTTP backend seam in client.py.

An embedding application installs its own transport with
:func:`mcp_kubecost.client.set_http_backend` so that every Kubecost API call
runs through its authentication, base URL and tracing instead of this package's
``httpx`` client. These tests pin the three properties that seam has to hold:

* an installed backend receives the path, params and body the tools built;
* with no backend installed the built-in transport is used, unchanged;
* :func:`reset_http_backend` puts the built-in transport back.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from mcp_kubecost import client as kc_client
from mcp_kubecost.client import KubecostClientError, get, post, reset_http_backend, set_http_backend
from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools


def _sc(result) -> dict:  # noqa: ANN001 — untyped so the optional structured_content narrows
    """Return structured_content from a ToolResult."""
    return result.structured_content


class _RecordingBackend:
    """Capture every call the tools route through the seam."""

    def __init__(self, result: Any = None):
        self.result = result if result is not None else {"data": []}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, params))
        return self.result

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.post_calls.append((path, json, params))
        return self.result


# ---------------------------------------------------------------------------
# Installation and delegation
# ---------------------------------------------------------------------------


class TestInstalledBackendReceivesTheCall:
    @pytest.mark.asyncio
    async def test_get_delegates_path_and_params(self):
        backend = _RecordingBackend(result={"data": ["ok"]})
        set_http_backend(backend.get, backend.post)

        result = await get("/model/allocation", {"window": "7d"})

        assert result == {"data": ["ok"]}
        assert backend.get_calls == [("/model/allocation", {"window": "7d"})]

    @pytest.mark.asyncio
    async def test_get_without_params_passes_none(self):
        backend = _RecordingBackend()
        set_http_backend(backend.get, backend.post)

        await get("/model/savings")

        assert backend.get_calls == [("/model/savings", None)]

    @pytest.mark.asyncio
    async def test_post_delegates_path_body_and_params(self):
        backend = _RecordingBackend(result={"ok": True})
        set_http_backend(backend.get, backend.post)

        result = await post("/model/thing", json={"a": 1}, params={"b": "2"})

        assert result == {"ok": True}
        assert backend.post_calls == [("/model/thing", {"a": 1}, {"b": "2"})]

    @pytest.mark.asyncio
    async def test_post_bypasses_the_api_key_guard(self):
        """The built-in POST refuses to send unauthenticated. A backend owns its own auth."""
        backend = _RecordingBackend()
        set_http_backend(backend.get, backend.post)

        # No KUBECOST_API_KEY and no X-API-KEY header — the built-in path would raise ValueError.
        await post("/model/thing", json={"a": 1})

        assert len(backend.post_calls) == 1

    @pytest.mark.asyncio
    async def test_installed_backend_makes_no_http_request(self, httpx_mock: HTTPXMock):
        """pytest-httpx fails the test if an unmatched request escapes, so this asserts isolation."""
        backend = _RecordingBackend()
        set_http_backend(backend.get, backend.post)

        await get("/model/allocation", {"window": "7d"})

        assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# The default path is untouched
# ---------------------------------------------------------------------------


class TestDefaultTransportUnchanged:
    @pytest.mark.asyncio
    async def test_no_backend_uses_the_builtin_httpx_client(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"https?://[^/]+/model/allocation"),
            json={"data": ["from-httpx"]},
        )

        result = await get("/model/allocation", {"window": "7d"})

        assert result == {"data": ["from-httpx"]}
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_reset_restores_the_builtin_transport(self, httpx_mock: HTTPXMock):
        backend = _RecordingBackend()
        set_http_backend(backend.get, backend.post)
        await get("/model/allocation")
        assert len(backend.get_calls) == 1

        reset_http_backend()

        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"https?://[^/]+/model/allocation"),
            json={"data": ["from-httpx"]},
        )
        result = await get("/model/allocation")

        assert result == {"data": ["from-httpx"]}
        assert len(backend.get_calls) == 1, "backend must not receive the call after reset"

    def test_reset_is_safe_when_no_backend_is_installed(self):
        reset_http_backend()
        reset_http_backend()
        assert kc_client._backend_get is None
        assert kc_client._backend_post is None

    def test_autouse_fixture_leaves_no_backend_installed(self):
        """Guards the conftest fixture: a leaked backend would silently mute other tests."""
        assert kc_client._backend_get is None
        assert kc_client._backend_post is None


# ---------------------------------------------------------------------------
# End to end through a real tool
# ---------------------------------------------------------------------------


class TestBackendInterceptsRealToolCalls:
    @staticmethod
    def _app() -> FastMCP:
        app = FastMCP("backend-seam-check")
        register_kubecost_tools(app)
        return app

    @pytest.mark.asyncio
    async def test_savings_overview_flows_through_the_backend(self, savings_overview_api_response: dict):
        """`tools/_common` binds `get` at import time, so this proves the indirection
        inside `get` is what makes the seam work — rebinding the module attribute would not."""
        backend = _RecordingBackend(result=savings_overview_api_response)
        set_http_backend(backend.get, backend.post)

        async with Client(self._app()) as client:
            result = await client.call_tool("get_savings_overview", {})

        assert len(backend.get_calls) == 1
        path, _params = backend.get_calls[0]
        assert path == "/model/savings"
        assert _sc(result)["status"] == "ok"
        assert len(_sc(result)["categories"]) == 8

    @pytest.mark.asyncio
    async def test_workload_costs_flows_through_the_backend(self, allocation_response_one_ns: dict):
        backend = _RecordingBackend(result=allocation_response_one_ns)
        set_http_backend(backend.get, backend.post)

        async with Client(self._app()) as client:
            result = await client.call_tool("get_kubecost_workload_costs", {"window": "7d"})

        assert len(backend.get_calls) == 1
        path, params = backend.get_calls[0]
        assert path == "/model/allocation"
        assert params is not None
        assert params["aggregate"] == "cluster,namespace"
        assert _sc(result)["status"] == "ok"

    @pytest.mark.asyncio
    async def test_backend_error_maps_through_the_upstream_error_contract(self):
        """A backend raising KubecostClientError reuses `to_tool_error` unchanged."""

        async def failing_get(path: str, params: dict[str, Any] | None = None) -> Any:
            raise KubecostClientError(
                status_code=404,
                message="no such endpoint",
                url=f"https://upstream.example{path}",
                path=path,
            )

        backend = _RecordingBackend()
        set_http_backend(failing_get, backend.post)

        async with Client(self._app()) as client:
            result = await client.call_tool("get_savings_overview", {})

        assert _sc(result)["status"] == "error"
        assert "not_found" in _sc(result)["message"]

    @pytest.mark.asyncio
    async def test_backend_timeout_maps_to_upstream_timeout(self):
        """httpx exceptions from a backend still reach `_handle_call_failure`."""

        async def timing_out_get(path: str, params: dict[str, Any] | None = None) -> Any:
            raise httpx.TimeoutException("too slow")

        backend = _RecordingBackend()
        set_http_backend(timing_out_get, backend.post)

        async with Client(self._app()) as client:
            result = await client.call_tool("get_savings_overview", {})

        assert _sc(result)["status"] == "error"
        assert "upstream_timeout" in _sc(result)["message"]
