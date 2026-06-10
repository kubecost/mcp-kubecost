"""Behavior tests for Kubecost MCP tools (mocked API — no live Kubecost required)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from tests.fixtures import SAMPLE_SAVINGS_RESPONSE

from mcp_kubecost.client import KubecostClientError


@pytest.mark.asyncio
async def test_kubecost_list_windows_returns_clarification(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        result = await client.call_tool("kubecost_list_windows", {})

    assert result.is_error is False
    assert len(result.content) == 1
    assert "TIME WINDOW REQUIRED" in result.content[0].text


@pytest.mark.asyncio
async def test_get_kubecost_workload_costs_requires_window(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        result = await client.call_tool("get_kubecost_workload_costs", {}, raise_on_error=False)

    assert result.is_error is True
    assert "TIME WINDOW REQUIRED" in result.content[0].text


@pytest.mark.asyncio
async def test_get_kubecost_workload_costs_success(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_kubecost_workload_costs",
            {"window": "7d", "aggregate": "cluster,namespace"},
        )

    assert result.is_error is False
    mock_kubecost_get.assert_awaited_once()
    assert result.structured_content is not None
    assert result.structured_content["summary"]["total_cost"] == pytest.approx(18.8)
    assert result.structured_content["summary"]["total_rows"] == 1
    assert "download_url" in result.structured_content
    assert "full_data" in result.structured_content
    assert result.content[0].text.startswith("[PRESENTATION RULES")


@pytest.mark.asyncio
async def test_get_kubecost_workload_costs_empty_response(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    mock_kubecost_get.return_value = {"data": []}

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_kubecost_workload_costs",
            {"window": "7d"},
            raise_on_error=False,
        )

    assert result.is_error is False
    assert "No allocation data returned" in result.content[0].text
    assert result.structured_content["total_cost"] == 0.0


@pytest.mark.asyncio
async def test_get_kubecost_workload_costs_api_error(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    mock_kubecost_get.side_effect = KubecostClientError(
        status_code=401,
        message="Unauthorized",
        url="https://example.com/model/allocation",
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_kubecost_workload_costs",
            {"window": "7d"},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "authentication_failed" in result.content[0].text


@pytest.mark.asyncio
async def test_get_container_savings_recommendations_success(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    mock_kubecost_get.return_value = SAMPLE_SAVINGS_RESPONSE

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_container_savings_recommendations",
            {"preset": "balanced", "window": "15d"},
        )

    assert result.is_error is False
    mock_kubecost_get.assert_awaited_once()
    assert result.structured_content is not None
    assert result.structured_content["summary"]["total_monthly_savings"] == pytest.approx(42.5)
    assert result.structured_content["summary"]["container_count"] == 1
    assert "download_url" in result.structured_content


@pytest.mark.asyncio
async def test_get_container_savings_recommendations_empty(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    mock_kubecost_get.return_value = {"TotalMonthlySavings": 0.0, "Count": 0, "Recommendations": []}

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_container_savings_recommendations",
            {"preset": "balanced"},
        )

    assert result.is_error is False
    assert "No savings recommendations returned" in result.content[0].text


@pytest.mark.asyncio
async def test_get_container_savings_recommendations_api_error(
    mcp_server: FastMCP,
    mock_kubecost_get: AsyncMock,
) -> None:
    mock_kubecost_get.side_effect = KubecostClientError(
        status_code=403,
        message="Forbidden",
        url="https://example.com/model/savings/requestSizingV2",
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_container_savings_recommendations",
            {"preset": "balanced"},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "permission_denied" in result.content[0].text


@pytest.mark.asyncio
async def test_unknown_tool_raises(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("nonexistent_tool", {})
