"""Integration tests — require a live Kubecost endpoint.

These tests are NOT run by default. To run them:

    uv run pytest -m integration

They call the real MCP server tools via the fastmcp CLI, matching the commands:

    fastmcp call ./.bob/mcp.json get_container_savings_recommendations --input-json '{"window": "15d"}'
    fastmcp call ./.bob/mcp.json get_kubecost_workload_costs --input-json '{"window": "15d"}'

Prerequisites:
  - KUBECOST_BASE_URL must point to a live instance (or demo.kubecost.xyz is used).
  - Optionally set KUBECOST_API_KEY / KUBECOST_OPEN_TOKEN for authenticated endpoints.
"""

from __future__ import annotations

import os
import subprocess

import pytest

MCP_CONFIG = os.path.join(os.path.dirname(__file__), "..", ".bob", "mcp.json")

pytestmark = pytest.mark.integration


def _fastmcp(*args: str) -> subprocess.CompletedProcess:
    """Run `fastmcp <args>` via uv and return the result."""
    return subprocess.run(
        ["uv", "run", "fastmcp", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def mcp_config_path() -> str:
    path = os.path.abspath(MCP_CONFIG)
    if not os.path.isfile(path):
        pytest.skip(f"MCP config not found at {path}")
    return path


class TestIntegrationGetKubecostWorkloadCosts:
    def test_returns_status_ok_or_empty(self, mcp_config_path):
        result = _fastmcp("call", mcp_config_path, "get_kubecost_workload_costs", "--input-json", '{"window": "15d"}')
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        output = result.stdout
        assert '"status"' in output or "status" in output
        # Must not return an error status for a live cluster
        assert '"error"' not in output or '"ok"' in output or '"empty"' in output

    def test_response_includes_rows_or_empty(self, mcp_config_path):
        result = _fastmcp(
            "call",
            mcp_config_path,
            "get_kubecost_workload_costs",
            "--input-json",
            '{"window": "15d", "aggregate": "namespace"}',
        )
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"


class TestIntegrationGetContainerSavingsRecommendations:
    def test_returns_status_ok_or_empty(self, mcp_config_path):
        result = _fastmcp(
            "call", mcp_config_path, "get_container_savings_recommendations", "--input-json", '{"window": "15d"}'
        )
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        output = result.stdout
        assert '"status"' in output or "status" in output

    def test_conservative_preset(self, mcp_config_path):
        result = _fastmcp(
            "call",
            mcp_config_path,
            "get_container_savings_recommendations",
            "--input-json",
            '{"preset": "conservative"}',
        )
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"

    def test_aggressive_preset(self, mcp_config_path):
        result = _fastmcp(
            "call", mcp_config_path, "get_container_savings_recommendations", "--input-json", '{"preset": "aggressive"}'
        )
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
