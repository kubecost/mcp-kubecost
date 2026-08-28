"""Tests for the MCP tool handlers (kubecost_list_windows, get_kubecost_workload_costs,
get_container_savings_recommendations).

HTTP calls are intercepted with pytest-httpx so no real Kubecost endpoint is needed.
The FastMCP `tool.run()` returns a `ToolResult` with `.structured_content` (dict).
"""

from __future__ import annotations

import os
import re
from datetime import UTC

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as FastMcpToolError
from pytest_httpx import HTTPXMock

from mcp_kubecost.tools.kubecost_tools import (
    _default_wow_windows,
    _diff_allocation_rows,
    _validate_comparison_windows,
    register_kubecost_tools,
)

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


def _stub_http_500(httpx_mock: HTTPXMock, url: re.Pattern) -> None:
    """Register a reusable 500 so client retries (default retry_count=2) still match."""
    httpx_mock.add_response(method="GET", url=url, status_code=500, is_reusable=True)


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

    @pytest.mark.asyncio
    async def test_every_option_carries_a_resolved_range(self, mcp_app):
        """Discovery doubles as window preview, so no option may resolve to null."""
        tool = await mcp_app.get_tool("kubecost_list_windows")
        result = await tool.run({})
        for option in _sc(result)["windows"]:
            resolved = option["resolved"]
            assert resolved is not None, f"{option['value']} failed to resolve"
            assert resolved["days"] >= 1
            assert resolved["source_expression"] == option["value"]
            assert resolved["display_start"] in resolved["display"]

    @pytest.mark.asyncio
    async def test_to_date_options_are_marked_partial(self, mcp_app):
        tool = await mcp_app.get_tool("kubecost_list_windows")
        result = await tool.run({})
        by_value = {o["value"]: o["resolved"] for o in _sc(result)["windows"]}
        assert by_value["month"]["is_partial"] is True
        assert by_value["week"]["is_partial"] is True
        # Kubecost's Nd windows run through the close of today, so they are
        # partial too; only the 'last*' aliases cover a completed period.
        assert by_value["30d"]["is_partial"] is True
        assert by_value["lastmonth"]["is_partial"] is False
        assert by_value["lastweek"]["is_partial"] is False


# ── removed tools ─────────────────────────────────────────────────────────────


class TestRemovedTools:
    @pytest.mark.asyncio
    async def test_standalone_resolve_window_tool_is_not_registered(self, mcp_app):
        """Window resolution is served by kubecost_list_windows and the
        resolved_window field on every windowed response, not its own tool."""
        assert "resolve_window" not in {tool.name for tool in await mcp_app.list_tools()}


# ── get_kubecost_workload_costs ───────────────────────────────────────────────


class TestGetKubecostWorkloadCosts:
    @pytest.mark.asyncio
    async def test_empty_string_window_returns_error(self, mcp_app):
        """Passing an explicit empty string should still return an error (guard kept for safety)."""
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": ""})
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
        _stub_http_500(httpx_mock, _allocation_url())
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

    @pytest.mark.asyncio
    async def test_rows_carry_window_end(self, httpx_mock: HTTPXMock, mcp_app, allocation_response_one_ns):
        httpx_mock.add_response(method="GET", url=_allocation_url(), json=allocation_response_one_ns)
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d", "min_total_cost": 0.0})
        sc = _sc(result)
        assert sc["rows"][0]["window_start"] == "2024-01-01"
        assert sc["rows"][0]["window_end"] == "2024-01-07"
        # The row boundary agrees with the resolved window it belongs to.
        assert sc["resolved_window"]["display_end"] == "2024-01-07"

    @pytest.mark.asyncio
    async def test_daily_buckets_report_the_full_span(
        self, httpx_mock: HTTPXMock, mcp_app, allocation_response_daily_buckets
    ):
        """Regression: accumulate=false reported 1 day and collapsed every day into one row."""
        httpx_mock.add_response(method="GET", url=_allocation_url(), json=allocation_response_daily_buckets)
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run(
            {"window": "3d", "aggregate": "cluster,namespace", "accumulate": False, "min_total_cost": 0.0}
        )
        sc = _sc(result)
        assert sc["resolved_window"]["days"] == 3
        assert sc["resolved_window"]["display_start"] == "2024-01-01"
        assert sc["resolved_window"]["display_end"] == "2024-01-03"
        assert sc["row_count"] == 6
        assert sc["total_cost"] == 72.0
        assert [r["window_start"] for r in sc["rows"]] == [
            "2024-01-01",
            "2024-01-01",
            "2024-01-02",
            "2024-01-02",
            "2024-01-03",
            "2024-01-03",
        ]
        assert all(r["window_start"] == r["window_end"] for r in sc["rows"])
        assert "Daily breakdown" in sc["message"]

    @pytest.mark.asyncio
    async def test_accumulated_response_is_unchanged(
        self, httpx_mock: HTTPXMock, mcp_app, allocation_response_multi_ns
    ):
        """One shared window must still yield one row per key, costliest first."""
        httpx_mock.add_response(method="GET", url=_allocation_url(), json=allocation_response_multi_ns)
        tool = await mcp_app.get_tool("get_kubecost_workload_costs")
        result = await tool.run({"window": "7d", "aggregate": "cluster,namespace", "min_total_cost": 0.0})
        sc = _sc(result)
        assert sc["row_count"] == 2
        assert sc["resolved_window"]["days"] == 7
        assert [r["totalCost"] for r in sc["rows"]] == [31.0, 7.2]
        assert "Daily breakdown" not in sc["message"]


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
    async def test_profile_high_availability_uses_30d_window(
        self, httpx_mock: HTTPXMock, mcp_app, savings_api_response
    ):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"profile": "high-availability"})
        assert _sc(result)["window"] == "30d"
        assert _sc(result)["parameters"]["target_cpu_utilization"] == 0.50
        request = httpx_mock.get_request()
        assert request is not None
        assert request.url.params["window"] == "30d"
        assert request.url.params["targetCPUUtilization"] == "0.5"

    @pytest.mark.asyncio
    async def test_profile_development_sends_higher_target_utilization(
        self, httpx_mock: HTTPXMock, mcp_app, savings_api_response
    ):
        httpx_mock.add_response(
            method="GET",
            url=_savings_url(),
            json=savings_api_response,
        )
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"profile": "development"})
        assert _sc(result)["parameters"]["target_cpu_utilization"] == 0.80
        assert _sc(result)["parameters"]["target_ram_utilization"] == 0.80
        assert _sc(result)["parameters"]["min_monthly_savings"] is None
        request = httpx_mock.get_request()
        assert request is not None
        assert request.url.params["targetCPUUtilization"] == "0.8"
        assert request.url.params["targetRAMUtilization"] == "0.8"

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
    async def test_default_keeps_undersized_negative_total_rows(self, httpx_mock: HTTPXMock, mcp_app):
        """Null min_monthly_savings returns undersized (negative total) recommendations."""
        payload = {
            "TotalMonthlySavings": 20.0,
            "Count": 2,
            "Recommendations": [
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "api",
                    "containerName": "api",
                    "monthlySavings": {"cpu": 25.0, "memory": 0.0, "total": 25.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 256.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.4, "total": 0.35},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 200.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 300.0},
                },
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "leaky",
                    "containerName": "leaky",
                    "monthlySavings": {"cpu": 5.0, "memory": -20.0, "total": -15.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 1024.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.9, "total": 0.6},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 900.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 1000.0},
                },
            ],
        }
        httpx_mock.add_response(method="GET", url=_savings_url(), json=payload)
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d"})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["parameters"]["min_monthly_savings"] is None
        names = {r["containerName"] for r in sc["rows"]}
        assert names == {"api", "leaky"}

    @pytest.mark.asyncio
    async def test_positive_min_monthly_savings_drops_undersized(self, httpx_mock: HTTPXMock, mcp_app):
        payload = {
            "TotalMonthlySavings": 20.0,
            "Count": 2,
            "Recommendations": [
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "api",
                    "containerName": "api",
                    "monthlySavings": {"cpu": 25.0, "memory": 0.0, "total": 25.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 256.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.4, "total": 0.35},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 200.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 300.0},
                },
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "leaky",
                    "containerName": "leaky",
                    "monthlySavings": {"cpu": 5.0, "memory": -20.0, "total": -15.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 1024.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.9, "total": 0.6},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 900.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 1000.0},
                },
            ],
        }
        httpx_mock.add_response(method="GET", url=_savings_url(), json=payload)
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "min_monthly_savings": 5.0})
        sc = _sc(result)
        assert sc["status"] == "ok"
        names = {r["containerName"] for r in sc["rows"]}
        assert names == {"api"}

    @pytest.mark.asyncio
    async def test_negative_min_monthly_savings_keeps_undersized_within_floor(self, httpx_mock: HTTPXMock, mcp_app):
        payload = {
            "TotalMonthlySavings": 20.0,
            "Count": 2,
            "Recommendations": [
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "api",
                    "containerName": "api",
                    "monthlySavings": {"cpu": 25.0, "memory": 0.0, "total": 25.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 256.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.4, "total": 0.35},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 200.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 300.0},
                },
                {
                    "clusterID": "c1",
                    "namespace": "default",
                    "controllerKind": "Deployment",
                    "controllerName": "leaky",
                    "containerName": "leaky",
                    "monthlySavings": {"cpu": 5.0, "memory": -20.0, "total": -15.0},
                    "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 1024.0},
                    "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                    "currentEfficiency": {"cpu": 0.3, "memory": 0.9, "total": 0.6},
                    "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 900.0},
                    "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 1000.0},
                },
            ],
        }
        httpx_mock.add_response(method="GET", url=_savings_url(), json=payload)
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "min_monthly_savings": -100.0})
        sc = _sc(result)
        assert sc["status"] == "ok"
        names = {r["containerName"] for r in sc["rows"]}
        assert names == {"api", "leaky"}

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("short_window", ["1d", "3d", "7d"])
    async def test_quantile_algorithm_rejects_short_window(self, mcp_app, short_window):
        """quantileOfAverages / quantileOfMaxes must raise ToolError for windows shorter than 15d."""
        from fastmcp.exceptions import ToolError as FastMcpToolError

        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            await tool.run({"window": short_window, "algorithm_cpu": "quantileOfAverages"})

    @pytest.mark.asyncio
    async def test_quantile_algorithm_accepts_15d_window(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        """15d is the minimum allowed window for quantile algorithms — must not error."""
        httpx_mock.add_response(method="GET", url=_savings_url(), json=savings_api_response)
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "15d", "algorithm_cpu": "quantileOfAverages"})
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_max_algorithm_accepts_short_window(self, httpx_mock: HTTPXMock, mcp_app, savings_api_response):
        """Non-quantile algorithms (max) should not be subject to the 15d minimum."""
        httpx_mock.add_response(method="GET", url=_savings_url(), json=savings_api_response)
        tool = await mcp_app.get_tool("get_container_savings_recommendations")
        result = await tool.run({"window": "7d", "algorithm_cpu": "max", "algorithm_ram": "max"})
        assert not result.is_error


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
        _stub_http_500(httpx_mock, _abandoned_url())
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


# ── get_savings_overview ─────────────────────────────────────────────────────

SAVINGS_OVERVIEW_PATH = "/model/savings"
PV_SIZING_PATH = "/model/savings/persistentVolumeSizing"
LOCAL_DISKS_PATH = "/model/savings/localLowDisks"
NODE_GROUP_PATH = "/model/savings/nodeGroupSizing/recommendations"
UNCLAIMED_VOLUMES_PATH = "/model/savings/unclaimedVolumes"
RESOURCE_QUOTA_PATH = "/model/savings/resourceQuotaSizing/recommendations"


def _savings_overview_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{SAVINGS_OVERVIEW_PATH}"))


def _pv_sizing_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{PV_SIZING_PATH}"))


def _local_disks_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{LOCAL_DISKS_PATH}"))


def _node_group_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{NODE_GROUP_PATH}"))


def _unclaimed_volumes_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{UNCLAIMED_VOLUMES_PATH}"))


def _resource_quota_url() -> re.Pattern:
    return re.compile(re.escape(f"{BASE_URL}{RESOURCE_QUOTA_PATH}"))


class TestGetSavingsOverview:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, savings_overview_api_response):
        httpx_mock.add_response(method="GET", url=_savings_overview_url(), json=savings_overview_api_response)
        tool = await mcp_app.get_tool("get_savings_overview")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["category_count"] == 8
        assert sc["total_savings_per_month"] > 0

    @pytest.mark.asyncio
    async def test_categories_sorted_desc(self, httpx_mock: HTTPXMock, mcp_app, savings_overview_api_response):
        httpx_mock.add_response(method="GET", url=_savings_overview_url(), json=savings_overview_api_response)
        tool = await mcp_app.get_tool("get_savings_overview")
        result = await tool.run({})
        savings = [c["savings_per_month"] for c in _sc(result)["categories"]]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_drill_down_tool_populated(self, httpx_mock: HTTPXMock, mcp_app, savings_overview_api_response):
        httpx_mock.add_response(method="GET", url=_savings_overview_url(), json=savings_overview_api_response)
        tool = await mcp_app.get_tool("get_savings_overview")
        result = await tool.run({})
        cats = {c["key"]: c for c in _sc(result)["categories"]}
        assert cats["nodeGroupSizing"]["drill_down_tool"] == "get_cluster_rightsizing_recommendations"
        assert cats["orphanedResources"]["drill_down_tool"] is None

    @pytest.mark.asyncio
    async def test_empty_data_returns_empty_status(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_savings_overview_url(), json={"code": 200, "data": {}})
        tool = await mcp_app.get_tool("get_savings_overview")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error_status(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _savings_overview_url())
        tool = await mcp_app.get_tool("get_savings_overview")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"


class TestGetPVSizingRecommendations:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, pv_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), json=pv_sizing_api_response)
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["row_count"] == 2
        assert sc["total_monthly_savings"] == pytest.approx(46.52)

    @pytest.mark.asyncio
    async def test_rows_sorted_desc(self, httpx_mock: HTTPXMock, mcp_app, pv_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), json=pv_sizing_api_response)
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({})
        savings = [r["savings_monthly"] for r in _sc(result)["rows"]]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_min_savings_filter(self, httpx_mock: HTTPXMock, mcp_app, pv_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), json=pv_sizing_api_response)
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({"min_monthly_savings": 20.0})
        sc = _sc(result)
        assert sc["row_count"] == 1
        assert sc["rows"][0]["savings_monthly"] > 20.0

    @pytest.mark.asyncio
    async def test_empty_recommendations(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), json={"recommendations": []})
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _pv_sizing_url())
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_truncated_flag(self, httpx_mock: HTTPXMock, mcp_app, pv_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), json=pv_sizing_api_response)
        tool = await mcp_app.get_tool("get_pv_sizing_recommendations")
        result = await tool.run({"top_n": 1})
        sc = _sc(result)
        assert sc["truncated"] is True
        assert len(sc["rows"]) == 1
        assert "showing top 1" in sc["message"]


class TestGetLocalDiskSavings:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, local_disks_api_response):
        httpx_mock.add_response(method="GET", url=_local_disks_url(), json=local_disks_api_response)
        tool = await mcp_app.get_tool("get_local_disk_savings")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["row_count"] == 2
        assert sc["total_monthly_savings"] == pytest.approx(18.98)

    @pytest.mark.asyncio
    async def test_rows_sorted_desc(self, httpx_mock: HTTPXMock, mcp_app, local_disks_api_response):
        httpx_mock.add_response(method="GET", url=_local_disks_url(), json=local_disks_api_response)
        tool = await mcp_app.get_tool("get_local_disk_savings")
        result = await tool.run({})
        savings = [r["savings_monthly"] for r in _sc(result)["rows"]]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_response(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_local_disks_url(), json={"unutilizedDisks": []})
        tool = await mcp_app.get_tool("get_local_disk_savings")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _local_disks_url())
        tool = await mcp_app.get_tool("get_local_disk_savings")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"


class TestGetClusterRightsizingRecommendations:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, node_group_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_node_group_url(), json=node_group_sizing_api_response)
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-demo-prod"})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["recommendation_count"] == 2
        assert sc["total_savings_per_month"] == pytest.approx(209.27)

    @pytest.mark.asyncio
    async def test_recommendations_sorted_desc(self, httpx_mock: HTTPXMock, mcp_app, node_group_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_node_group_url(), json=node_group_sizing_api_response)
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-demo-prod"})
        savings = [r["savings_per_month"] for r in _sc(result)["recommendations"]]
        assert savings == sorted(savings, reverse=True)

    @pytest.mark.asyncio
    async def test_change_instance_type_value_accepted(
        self, httpx_mock: HTTPXMock, mcp_app, node_group_sizing_api_response
    ):
        """ChangeInstanceType is an open-string recommendation value — must not raise."""
        httpx_mock.add_response(method="GET", url=_node_group_url(), json=node_group_sizing_api_response)
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-demo-prod"})
        rec_values = {r["recommendation"] for r in _sc(result)["recommendations"]}
        assert "ChangeInstanceType" in rec_values

    @pytest.mark.asyncio
    async def test_warnings_surfaced(self, httpx_mock: HTTPXMock, mcp_app, node_group_sizing_api_response):
        httpx_mock.add_response(method="GET", url=_node_group_url(), json=node_group_sizing_api_response)
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-demo-prod"})
        assert "warnings" in _sc(result)

    @pytest.mark.asyncio
    async def test_empty_recommendations(self, httpx_mock: HTTPXMock, mcp_app):
        empty = {"code": 200, "data": {"recommendations": [], "totalSavingsPerMonth": 0.0, "warnings": []}}
        httpx_mock.add_response(method="GET", url=_node_group_url(), json=empty)
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-missing"})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _node_group_url())
        tool = await mcp_app.get_tool("get_cluster_rightsizing_recommendations")
        result = await tool.run({"cluster": "kc-demo-prod"})
        assert _sc(result)["status"] == "error"


class TestGetUnclaimedVolumes:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, unclaimed_volumes_api_response):
        httpx_mock.add_response(method="GET", url=_unclaimed_volumes_url(), json=unclaimed_volumes_api_response)
        tool = await mcp_app.get_tool("get_unclaimed_volumes")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["row_count"] == 2
        assert sc["total_monthly_cost"] == pytest.approx(15.24)

    @pytest.mark.asyncio
    async def test_properties_present(self, httpx_mock: HTTPXMock, mcp_app, unclaimed_volumes_api_response):
        httpx_mock.add_response(method="GET", url=_unclaimed_volumes_url(), json=unclaimed_volumes_api_response)
        tool = await mcp_app.get_tool("get_unclaimed_volumes")
        result = await tool.run({})
        row = _sc(result)["rows"][0]
        assert "properties" in row
        assert row["properties"]["provider"] in {"GCP", "AWS"}

    @pytest.mark.asyncio
    async def test_empty_volumes(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_unclaimed_volumes_url(),
            json={"code": 200, "data": {"count": 0, "monthlyCost": 0.0, "volumes": []}},
        )
        tool = await mcp_app.get_tool("get_unclaimed_volumes")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _unclaimed_volumes_url())
        tool = await mcp_app.get_tool("get_unclaimed_volumes")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_truncated_flag(self, httpx_mock: HTTPXMock, mcp_app, unclaimed_volumes_api_response):
        httpx_mock.add_response(method="GET", url=_unclaimed_volumes_url(), json=unclaimed_volumes_api_response)
        tool = await mcp_app.get_tool("get_unclaimed_volumes")
        result = await tool.run({"top_n": 1})
        sc = _sc(result)
        assert sc["truncated"] is True
        assert len(sc["rows"]) == 1


class TestGetResourceQuotaRecommendations:
    @pytest.mark.asyncio
    async def test_success_response(self, httpx_mock: HTTPXMock, mcp_app, resource_quota_api_response):
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), json=resource_quota_api_response)
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["item_count"] == 2
        assert len(sc["recommendations"]) == 2

    @pytest.mark.asyncio
    async def test_resources_nested(self, httpx_mock: HTTPXMock, mcp_app, resource_quota_api_response):
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), json=resource_quota_api_response)
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        rec = _sc(result)["recommendations"][0]
        assert len(rec["resources"]) == 1
        assert rec["resources"][0]["resource_type"] == "requests.cpu"

    @pytest.mark.asyncio
    async def test_is_downsize_flag(self, httpx_mock: HTTPXMock, mcp_app, resource_quota_api_response):
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), json=resource_quota_api_response)
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        # Second recommendation has is_downsize=True
        rec_with_downsize = _sc(result)["recommendations"][1]
        assert rec_with_downsize["resources"][0]["is_downsize"] is True

    @pytest.mark.asyncio
    async def test_resolved_window_comes_from_the_api_echo(
        self, httpx_mock: HTTPXMock, mcp_app, resource_quota_api_response
    ):
        """The endpoint echoes the range it queried; that beats the client-side prediction."""
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), json=resource_quota_api_response)
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({"window": "7d"})
        resolved = _sc(result)["resolved_window"]
        assert resolved["display_start"] == "2026-07-10"
        assert resolved["display_end"] == "2026-07-16"
        assert resolved["days"] == 7

    @pytest.mark.asyncio
    async def test_empty_recommendations(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_resource_quota_url(),
            json={"code": 200, "data": {"itemCount": 0, "totalMonthlySavings": 0.0, "recommendations": []}},
        )
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_500_returns_error(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _resource_quota_url())
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_total_monthly_savings_can_be_zero(self, httpx_mock: HTTPXMock, mcp_app, resource_quota_api_response):
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), json=resource_quota_api_response)
        tool = await mcp_app.get_tool("get_resource_quota_recommendations")
        result = await tool.run({})
        # totalMonthlySavings is 0 in the fixture — that's expected for this correctness tool
        assert _sc(result)["total_monthly_savings"] == pytest.approx(0.0)


# ── _validate_comparison_windows (Task 1) ────────────────────────────────────


_RFC3339_BASELINE = "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z"


class TestComparisonWindowValidation:
    @pytest.mark.parametrize("window", ["7d", "15d", "30d", "1d"])
    def test_bare_relative_window_rejected(self, window):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(window, _RFC3339_BASELINE)

    @pytest.mark.parametrize("window", ["today", "week", "month", "lastweek", "lastmonth"])
    def test_named_alias_rejected(self, window):
        """All named aliases are rejected — use explicit RFC3339 ranges."""
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(window, _RFC3339_BASELINE)

    def test_alias_as_baseline_rejected(self):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(_RFC3339_BASELINE, "lastweek")

    def test_unequal_duration_rfc3339_accepted(self):
        current_days, baseline_days = _validate_comparison_windows(
            "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",  # 7 days
            "2020-01-01T00:00:00Z,2020-01-06T00:00:00Z",  # 5 days
        )
        assert current_days == 7
        assert baseline_days == 5

    def test_equal_duration_rfc3339_returns_day_counts(self):
        current_days, baseline_days = _validate_comparison_windows(
            "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
            "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
        )
        assert current_days == 7
        assert baseline_days == 7

    def test_rfc3339_range_including_today_rejected(self):
        from datetime import datetime, timedelta

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(
                f"2020-01-01T00:00:00Z,{tomorrow}",
                "2019-01-01T00:00:00Z,2019-01-08T00:00:00Z",
            )

    def test_malformed_rfc3339_rejected(self):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows("not-a-date,also-not-a-date", _RFC3339_BASELINE)


# ── _diff_allocation_rows (Task 2) ───────────────────────────────────────────


class TestDiffAllocationRows:
    def test_new_dimension_in_current_only(self):
        current = [{"namespace": "new-ns", "totalCost": 50.0}]
        baseline: list[dict] = []
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        assert len(result) == 1
        row = result[0]
        assert row["namespace"] == "new-ns"
        assert row["current_cost"] == 50.0
        assert row["baseline_cost"] == 0
        assert row["row_status"] == "new"
        assert row["pct_change"] is None

    def test_dimension_dropped_in_baseline_only(self):
        current: list[dict] = []
        baseline = [{"namespace": "gone-ns", "totalCost": 30.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        assert len(result) == 1
        row = result[0]
        assert row["namespace"] == "gone-ns"
        assert row["current_cost"] == 0
        assert row["baseline_cost"] == 30.0
        assert row["change"] == -30.0
        assert row["row_status"] == "removed"

    def test_normal_increase(self):
        current = [{"namespace": "ns-a", "totalCost": 120.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 100.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        row = result[0]
        assert row["change"] == 20.0
        assert row["pct_change"] == 20.0
        assert row["row_status"] == "changed"

    def test_normal_decrease(self):
        current = [{"namespace": "ns-a", "totalCost": 80.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 100.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        row = result[0]
        assert row["change"] == -20.0
        assert row["pct_change"] == -20.0

    def test_zero_baseline_division_handled(self):
        current = [{"namespace": "ns-a", "totalCost": 10.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 0.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        row = result[0]
        assert row["pct_change"] is None
        assert row["row_status"] == "new"

    def test_zero_in_both_windows_is_not_new(self):
        """A dimension that cost nothing in either window has not appeared."""
        current = [{"namespace": "ns-a", "totalCost": 0.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 0.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        row = result[0]
        assert row["row_status"] == "unchanged"
        assert row["pct_change"] is None
        assert row["normalized_pct_change"] is None

    def test_identical_nonzero_cost_is_unchanged(self):
        current = [{"namespace": "ns-a", "totalCost": 100.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 100.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        assert result[0]["row_status"] == "unchanged"
        assert result[0]["pct_change"] == 0.0

    def test_sorted_by_absolute_change_desc(self):
        current = [
            {"namespace": "small-change", "totalCost": 101.0},
            {"namespace": "big-drop", "totalCost": 10.0},
        ]
        baseline = [
            {"namespace": "small-change", "totalCost": 100.0},
            {"namespace": "big-drop", "totalCost": 200.0},
        ]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        assert result[0]["namespace"] == "big-drop"
        assert result[1]["namespace"] == "small-change"


class TestDiffAllocationRowsNormalization:
    """Per-day figures make windows of unequal length comparable."""

    def test_equal_length_windows_normalize_to_the_same_pct(self):
        current = [{"namespace": "ns-a", "totalCost": 120.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 100.0}]
        row = _diff_allocation_rows(current, baseline, ["namespace"], current_days=7, baseline_days=7)[0]
        assert row["normalized_pct_change"] == row["pct_change"] == 20.0
        # Per-day costs round to 2dp like every other cost field in the response.
        assert row["current_daily_cost"] == pytest.approx(120.0 / 7, abs=0.005)
        assert row["baseline_daily_cost"] == pytest.approx(100.0 / 7, abs=0.005)

    def test_longer_month_at_identical_daily_spend_is_flat(self):
        """31 days at $10/day vs 30 days at $10/day is a $10 rise but no real change."""
        current = [{"namespace": "ns-a", "totalCost": 310.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 300.0}]
        row = _diff_allocation_rows(current, baseline, ["namespace"], current_days=31, baseline_days=30)[0]
        assert row["change"] == 10.0
        assert row["pct_change"] == pytest.approx(3.33, abs=0.01)
        assert row["daily_change"] == 0
        assert row["normalized_pct_change"] == 0.0

    def test_shorter_period_hides_a_real_increase(self):
        """A 30-day period costing the same as a 31-day one is a genuine daily rise."""
        current = [{"namespace": "ns-a", "totalCost": 300.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 300.0}]
        row = _diff_allocation_rows(current, baseline, ["namespace"], current_days=30, baseline_days=31)[0]
        assert row["change"] == 0
        assert row["pct_change"] == 0.0
        assert row["normalized_pct_change"] == pytest.approx(3.33, abs=0.01)

    def test_zero_day_window_does_not_divide_by_zero(self):
        current = [{"namespace": "ns-a", "totalCost": 50.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 25.0}]
        row = _diff_allocation_rows(current, baseline, ["namespace"], current_days=0, baseline_days=0)[0]
        assert row["current_daily_cost"] == 0
        assert row["baseline_daily_cost"] == 0
        assert row["normalized_pct_change"] is None


# ── get_kubecost_cost_comparison (Task 3, end-to-end) ────────────────────────


def _comparison_allocation_response(ns_name: str, total_cost: float) -> dict:
    return {
        "data": [
            {
                f"cluster-one/{ns_name}": {
                    "name": f"cluster-one/{ns_name}",
                    "properties": {"cluster": "cluster-one", "namespace": ns_name},
                    "window": {"start": "2020-01-01T00:00:00Z", "end": "2020-01-08T00:00:00Z"},
                    "cpuCost": total_cost * 0.6,
                    "cpuCostIdle": 0.0,
                    "ramCost": total_cost * 0.4,
                    "ramCostIdle": 0.0,
                    "networkCost": 0.0,
                    "pvCost": 0.0,
                    "gpuCost": 0.0,
                    "gpuCostIdle": 0.0,
                    "loadBalancerCost": 0.0,
                    "sharedCost": 0.0,
                    "totalCost": total_cost,
                    "totalEfficiency": 0.5,
                }
            }
        ]
    }


class TestGetKubecostCostComparison:
    @pytest.mark.asyncio
    async def test_success_with_clear_top_mover(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=_comparison_allocation_response("ns-a", 200.0),
        )
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=_comparison_allocation_response("ns-a", 50.0),
        )
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
                "baseline_window": "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
                "aggregate": "namespace",
            }
        )
        sc = _sc(result)
        assert sc["status"] == "ok"
        assert sc["row_count"] == 1
        assert sc["rows"][0]["change"] == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_empty_both_windows(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})
        httpx_mock.add_response(method="GET", url=_allocation_url(), json={"data": []})
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
                "baseline_window": "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
            }
        )
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_error_on_either_call(self, httpx_mock: HTTPXMock, mcp_app):
        _stub_http_500(httpx_mock, _allocation_url())
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
                "baseline_window": "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
            }
        )
        assert _sc(result)["status"] == "error"

    @pytest.mark.asyncio
    async def test_validation_error_returns_tool_error_not_raw_exception(self, mcp_app):
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            await tool.run(
                {
                    "current_window": "7d",
                    "baseline_window": "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
                }
            )

    @pytest.mark.asyncio
    async def test_unequal_duration_produces_warning(self, httpx_mock: HTTPXMock, mcp_app):
        """Mismatched window lengths succeed but carry a warning in the response."""
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=_comparison_allocation_response("ns-a", 100.0),
        )
        httpx_mock.add_response(
            method="GET",
            url=_allocation_url(),
            json=_comparison_allocation_response("ns-a", 80.0),
        )
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",  # 7 days
                "baseline_window": "2020-01-01T00:00:00Z,2020-01-06T00:00:00Z",  # 5 days
            }
        )
        sc = _sc(result)
        assert sc["status"] == "ok"
        warnings = sc.get("warnings", [])
        assert any("different number of days" in w for w in warnings), (
            f"Expected a duration-mismatch warning, got warnings={warnings!r}"
        )
        assert any("7" in w and "5" in w for w in warnings), (
            f"Warning should mention the actual day counts (7 vs 5), got warnings={warnings!r}"
        )


class TestCostComparisonNotes:
    """The response explains its own idle and __unallocated__ semantics."""

    @staticmethod
    async def _run(httpx_mock: HTTPXMock, mcp_app, current: dict, baseline: dict) -> dict:
        httpx_mock.add_response(method="GET", url=_allocation_url(), json=current)
        httpx_mock.add_response(method="GET", url=_allocation_url(), json=baseline)
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
                "baseline_window": "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
                "aggregate": "namespace",
            }
        )
        return _sc(result)

    @pytest.mark.asyncio
    async def test_idle_sharing_is_always_explained(self, httpx_mock: HTTPXMock, mcp_app):
        sc = await self._run(
            httpx_mock,
            mcp_app,
            _comparison_allocation_response("ns-a", 200.0),
            _comparison_allocation_response("ns-a", 50.0),
        )
        notes = sc.get("notes", [])
        assert any("Idle" in n and "distributed" in n for n in notes), (
            f"Expected an idle-sharing note, got notes={notes!r}"
        )

    @pytest.mark.asyncio
    async def test_unallocated_note_absent_when_no_such_row(self, httpx_mock: HTTPXMock, mcp_app):
        sc = await self._run(
            httpx_mock,
            mcp_app,
            _comparison_allocation_response("ns-a", 200.0),
            _comparison_allocation_response("ns-a", 50.0),
        )
        assert not any("__unallocated__" in n for n in sc.get("notes", []))

    @pytest.mark.asyncio
    async def test_unallocated_note_names_dimension_and_amount(self, httpx_mock: HTTPXMock, mcp_app):
        sc = await self._run(
            httpx_mock,
            mcp_app,
            _comparison_allocation_response("__unallocated__", 125.33),
            _comparison_allocation_response("__unallocated__", 100.0),
        )
        notes = sc.get("notes", [])
        unallocated = [n for n in notes if "__unallocated__" in n]
        assert unallocated, f"Expected an __unallocated__ note, got notes={notes!r}"
        assert "'namespace'" in unallocated[0]
        assert "125.33" in unallocated[0]
        assert "100.00" in unallocated[0]


# ── _default_wow_windows ──────────────────────────────────────────────────────


class TestDefaultWowWindows:
    """Unit tests for the week-over-week default window calculator."""

    _RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T00:00:00Z$")

    def _parse(self, window: str):
        from datetime import date

        start, end = window.split(",")
        return date.fromisoformat(start[:10]), date.fromisoformat(end[:10])

    def test_returns_two_rfc3339_ranges(self):
        current, baseline = _default_wow_windows()
        for part in (*current.split(","), *baseline.split(",")):
            assert self._RFC3339_RE.match(part), f"Not an RFC3339 date-time: {part!r}"

    def test_each_window_spans_exactly_7_days(self):
        current, baseline = _default_wow_windows()
        cur_start, cur_end = self._parse(current)
        base_start, base_end = self._parse(baseline)
        assert (cur_end - cur_start).days == 7
        assert (base_end - base_start).days == 7

    def test_windows_are_contiguous(self):
        current, baseline = _default_wow_windows()
        cur_start, _ = self._parse(current)
        _, base_end = self._parse(baseline)
        assert base_end == cur_start, "baseline_window must end exactly where current_window begins"

    def test_current_window_ends_at_today_utc(self):
        """The exclusive end of current_window is today midnight — yesterday is the last full day covered.

        Compared against UTC, not local time: _default_wow_windows works in UTC, so
        `date.today()` here would fail whenever the local date differs from the UTC one.
        """
        from datetime import datetime

        current, _ = _default_wow_windows()
        _, cur_end = self._parse(current)
        today_utc = datetime.now(UTC).date()
        assert cur_end == today_utc, "current_window exclusive end must be today UTC (yesterday is the last full day)"

    def test_windows_pass_validation(self):
        """The computed defaults must survive _validate_comparison_windows without raising."""
        current, baseline = _default_wow_windows()
        _validate_comparison_windows(current, baseline)  # should not raise
