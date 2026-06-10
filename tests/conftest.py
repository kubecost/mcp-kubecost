"""Shared pytest fixtures."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from tests.fixtures import SAMPLE_ALLOCATION_RESPONSE

from mcp_kubecost.skills import register_all_skills
from mcp_kubecost.tools.kubecost_tools import register_kubecost_csv_tools


@pytest.fixture
def mcp_server() -> FastMCP:
    """Build an isolated MCP server matching production registration."""
    server = FastMCP(name="mcp-kubecost-test", version="0.0.0-test")
    register_kubecost_csv_tools(server)
    register_all_skills(server)
    return server


@pytest.fixture
def mock_kubecost_get(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the Kubecost HTTP client used by tool handlers."""
    mock = AsyncMock(return_value=SAMPLE_ALLOCATION_RESPONSE)
    monkeypatch.setattr("mcp_kubecost.tools.kubecost_tools.get", mock)
    return mock
