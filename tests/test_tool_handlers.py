"""Tests for the MCP tool handlers (kubecost_list_windows, get_kubecost_workload_costs,
get_container_savings_recommendations).

HTTP calls are intercepted with pytest-httpx so no real Kubecost endpoint is needed.
The FastMCP `tool.run()` returns a `ToolResult` with `.structured_content` (dict).
"""

from __future__ import annotations

import os
import re

import pytest
from fastmcp import FastMCP
from pytest_httpx import HTTPXMock

from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools

BASE_URL = os.environ.get("KUBECOST_BASE_URL", "https://demo.kubecost.xyz")
ALLOCATION_PATH = "/model/allocation"
SAVINGS_PATH = "/model/savings/requestSizingV2"


# ── shared helpers ────────────────────────────────────────────────────────────


def _allocation_url() -> re.Pattern:
    """Match any request to the allocation path, regardless of query params."""
    return re.compile(re.escape(f"{BASE_URL}{ALLOCATION_PATH}"))


def _savings_url() -> re.Pattern:
    """Match any request to the savings path, regardless of query params."""
    return re.compile(re.escape(f"{BASE_URL}{SAVINGS_PATH}"))


def _sc(result) -> dict:
    """Return structured_content from a ToolResult."""
    return result.structured_content


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mcp_app() -> FastMCP:
    """Shared FastMCP instance with Kubecost tools registered."""
    app = FastMCP("test-kubecost")
    register_kubecost_tools(app)
    return app


# ── kubecost_list_windows (no HTTP) ──────────────────────────────────────────


class TestKubecostListWindows:
    @pytest.mark.asyncio
    async def test_returns_ok_status(self, mcp_app):
        tool = await mcp_app.get_tool("kubecost_list_windows")
        result = await tool.run({})
        assert not result.is_error
        assert _sc(result)["status"] == "ok"

    @pytest.mark.asyncio
    async def test_windows_list_non_empty(self, mcp_app):
        tool = await mcp_app.get_tool("kubecost_list_windows")
        result = await tool.run({})
        assert len(_sc(result)["windows"]) > 0

    @pytest.mark.asyncio
    async def test_note_present(self, mcp_app):
        tool = await mcp_app.get_tool("kubecost_list_windows")
        result = await tool.run({})
        assert "RFC3339" in _sc(result)["note"]


# ── get_kubecost_workload_costs ───────────────────────────────────────────────


class TestGetKubecostWorkloadCosts:
    @pytest.mark.asyncio
    async def test_no_window_returns_error(self, mcp_app):
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": None})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, allocation_response_one_ns):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=allocation_response_one_ns,
        )
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d", "aggregate": "cluster,namespace"})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["total_cost"] > 0

    @pytest.mark.asyncio
    async def test_dimensions_in_response(self, httpx_mock: HTTPXMock, mcp_app, allocation_response_one_ns):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=allocation_response_one_ns,
        )
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d", "aggregate": "cluster,namespace"})
        dims = _sc(result)["dimensions"]
        assert "cluster" in dims
        assert "namespace" in dims

    @pytest.mark.asyncio
    async def test_empty_data_returns_empty_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json={"data": []},
        )
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d"})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            status_code=500,
        )
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d"})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_top_n_truncation(self, httpx_mock: HTTPXMock, mcp_app, allocation_response_multi_ns):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=allocation_response_multi_ns,
        )
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d", "top_n": 1})
        sc = _sc(result)
        assert sc["truncated"] is True
        assert len(sc["rows"]) == 1


# ── get_container_savings_recommendations ────────────────────────────────────


class TestGetContainerSavingsRecommendations:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["total_monthly_savings"] > 0

    @pytest.mark.asyncio
    async def test_rows_present(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        sc = _sc(result)
        assert len(sc["rows"]) == 2

    @pytest.mark.asyncio
    async def test_empty_recommendations(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json={"TotalMonthlySavings": 0.0, "Count": 0, "Recommendations": []},
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_401_returns_error_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            status_code=401,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_preset_conservative_uses_30d_window(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"preset": "conservative"})
        assert _sc(result)["window"] == "30d"

    @pytest.mark.asyncio
    async def test_min_monthly_savings_filter_to_empty(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "min_monthly_savings": 9999.0})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_summary_aggregate_namespace(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "summary_aggregate": "namespace"})
        sc = _sc(result)
        assert sc["summary_aggregate"] == "namespace"
        # Two distinct namespaces in fixture (default, monitoring)
        assert len(sc["summary"]) == 2

    @pytest.mark.asyncio
    async def test_interpretation_field_present(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        assert "interpretation" in _sc(result)
        assert len(_sc(result)["interpretation"]) > 0

    @pytest.mark.asyncio
    async def test_parameters_echo_in_response(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "q_cpu": 0.9})
        params = _sc(result)["parameters"]
        assert params["q_cpu"] == 0.9
        assert params["window"] == "15d"


# ── get_abandoned_workloads ───────────────────────────────────────────────────

ABANDONED_PATH = "/model/savings/abandonedWorkloads"


def _abandoned_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{ABANDONED_PATH}"))


class TestGetAbandonedWorkloads:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, abandoned_workloads_api_response):
        httpx_mock.add_response(
            method="GET",
            url=_abandoned_url(),
            json=abandoned_workloads_api_response,
        )
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["workload_count"] == 2
        assert sc["total_monthly_savings"] == pytest.approx(50.5)

    @pytest.mark.asyncio
    async def test_rows_sorted_by_savings_desc(self, httpx_mock: HTTPXMock, mcp_app, abandoned_workloads_api_response):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), json=abandoned_workloads_api_response)
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({})
        rows = _sc(result)["rows"]
        # FastMCP serialises Pydantic models by alias
        savings = [r["monthlySavings"] for r in rows]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), json=[])
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), status_code=500)
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_cluster_filter_echoed_in_response(
        self, httpx_mock: HTTPXMock, mcp_app, abandoned_workloads_api_response
    ):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), json=abandoned_workloads_api_response)
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({"cluster": "my-cluster"})
        assert _sc(result)["cluster_filter"] == "my-cluster"

    @pytest.mark.asyncio
    async def test_parameters_echoed(self, httpx_mock: HTTPXMock, mcp_app, abandoned_workloads_api_response):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), json=abandoned_workloads_api_response)
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        result = await tool.run({"days": 7, "threshold": 1000})
        sc = _sc(result)
        assert sc["days"] == 7
        assert sc["threshold_bytes_per_second"] == 1000

    @pytest.mark.asyncio
    async def test_truncated_flag_when_at_limit(self, httpx_mock: HTTPXMock, mcp_app, abandoned_workloads_api_response):
        httpx_mock.add_response(method="GET", url=_abandoned_url(), json=abandoned_workloads_api_response)
        tool = await mcp_app.get_tool("get_abandoned_workloads")
        # limit=2 and response has exactly 2 rows → truncated=True
        result = await tool.run({"limit": 2})
        assert _sc(result)["truncated"] is True
