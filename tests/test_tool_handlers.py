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
        httpx_mock.add_response(method="GET", url=_savings_overview_url(), status_code=500)
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
        httpx_mock.add_response(method="GET", url=_pv_sizing_url(), status_code=500)
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
        httpx_mock.add_response(method="GET", url=_local_disks_url(), status_code=500)
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
        httpx_mock.add_response(method="GET", url=_node_group_url(), status_code=500)
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
        httpx_mock.add_response(method="GET", url=_unclaimed_volumes_url(), status_code=500)
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
        httpx_mock.add_response(method="GET", url=_resource_quota_url(), status_code=500)
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


class TestComparisonWindowValidation:
    @pytest.mark.parametrize("window", ["7d", "15d", "30d", "1d"])
    def test_bare_relative_window_rejected(self, window):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(window, "lastweek")

    @pytest.mark.parametrize("window", ["today", "week", "month"])
    def test_relative_alias_rejected(self, window):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(window, "lastweek")

    def test_mismatched_aliases_rejected(self):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows("lastweek", "lastmonth")

    def test_mixed_alias_and_rfc3339_rejected(self):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows("lastweek", "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z")

    def test_unequal_duration_rfc3339_rejected(self):
        with pytest.raises(FastMcpToolError, match="invalid_input"):
            _validate_comparison_windows(
                "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
                "2020-01-01T00:00:00Z,2020-01-06T00:00:00Z",
            )

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
            _validate_comparison_windows("not-a-date,also-not-a-date", "lastweek")

    def test_lastweek_vs_lastweek_accepted(self):
        _validate_comparison_windows("lastweek", "lastweek")  # should not raise

    def test_lastmonth_vs_lastmonth_accepted(self):
        _validate_comparison_windows("lastmonth", "lastmonth")  # should not raise

    def test_equal_duration_rfc3339_ranges_accepted(self):
        # Both 7-day ranges, well in the past — should not raise
        _validate_comparison_windows(
            "2020-01-08T00:00:00Z,2020-01-15T00:00:00Z",
            "2020-01-01T00:00:00Z,2020-01-08T00:00:00Z",
        )


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
        assert row["is_new"] is True
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
        assert row["is_new"] is False

    def test_normal_increase(self):
        current = [{"namespace": "ns-a", "totalCost": 120.0}]
        baseline = [{"namespace": "ns-a", "totalCost": 100.0}]
        result = _diff_allocation_rows(current, baseline, ["namespace"])
        row = result[0]
        assert row["change"] == 20.0
        assert row["pct_change"] == 20.0
        assert row["is_new"] is False

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
        assert row["is_new"] is True

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
                "current_window": "lastweek",
                "baseline_window": "lastweek",
            }
        )
        assert _sc(result)["status"] == "empty"

    @pytest.mark.asyncio
    async def test_http_error_on_either_call(self, httpx_mock: HTTPXMock, mcp_app):
        httpx_mock.add_response(method="GET", url=_allocation_url(), status_code=500)
        tool = await mcp_app.get_tool("get_kubecost_cost_comparison")
        result = await tool.run(
            {
                "current_window": "lastweek",
                "baseline_window": "lastweek",
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
                    "baseline_window": "lastweek",
                }
            )
