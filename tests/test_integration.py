"""Integration tests — require a live kubecost api endpoint.

These tests are NOT run by default. To run them:

    uv run pytest -m integration

They call the real MCP server tools via the fastmcp CLI, matching the commands:

    fastmcp call ./.bob/mcp.json get_container_savings_recommendations --input-json '{"window": "15d"}'
    fastmcp call ./.bob/mcp.json get_kubecost_workload_costs --input-json '{"window": "15d"}'

Which instance is queried comes from the MCP config file, since the fastmcp CLI
starts the server with the env block declared there — not from the ambient
environment. By default that is the developer's own ``.bob/mcp.json`` (gitignored);
set ``MCP_KUBECOST_CONFIG`` to point somewhere else. To run against the public demo,
the same target CI uses:

    MCP_KUBECOST_CONFIG=tests/mcp-demo.json uv run pytest -m integration

Every test skips when the config file is missing, so a checkout without one is not
a failure.

Prerequisites:
  - The config's KUBECOST_BASE_URL must point to a live instance.
  - Optionally set KUBECOST_API_KEY there for authenticated endpoints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, timedelta
from typing import Any

import pytest

DEFAULT_MCP_CONFIG = os.path.join(os.path.dirname(__file__), "..", ".bob", "mcp.json")
MCP_CONFIG = os.environ.get("MCP_KUBECOST_CONFIG") or DEFAULT_MCP_CONFIG

pytestmark = pytest.mark.integration


def _fastmcp(*args: str) -> subprocess.CompletedProcess:
    """Run `fastmcp <args>` via uv and return the result."""
    return subprocess.run(
        ["uv", "run", "fastmcp", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _call_tool(config_path: str, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call a tool and return its parsed structured response.

    The CLI writes its logging to stderr, so stdout is the response body alone.
    """
    result = _fastmcp("call", config_path, tool, "--input-json", json.dumps(payload))
    assert result.returncode == 0, f"fastmcp exited {result.returncode} for {tool} {payload}:\n{result.stderr}"
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Could not parse {tool} output as JSON ({exc}).\nstdout was:\n{result.stdout[:500]}")


@pytest.fixture(scope="module")
def mcp_config_path() -> str:
    path = os.path.abspath(MCP_CONFIG)
    if not os.path.isfile(path):
        pytest.skip(f"MCP config not found at {path} (set MCP_KUBECOST_CONFIG to choose another)")
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


# ── allocation row counts per window ─────────────────────────────────────────

# A label no workload carries, so Kubecost emits exactly one '__unallocated__'
# entry per bucket. Row count then depends only on the window — not on how many
# namespaces, clusters or workloads this particular cluster happens to have.
# This test is critical
_NONEXISTENT_LABEL_AGGREGATE = "label:fake"

# (window expression, days it should resolve to)
_WINDOW_CASES = [("3d", 3), ("7d", 7)]


@pytest.fixture(scope="module")
def allocation_call(mcp_config_path):
    """Return a memoized caller for get_kubecost_workload_costs.

    Each (window, accumulate) pair costs a subprocess round-trip, so results are
    cached for the module rather than re-fetched per assertion.
    """
    cache: dict[tuple[str, bool], dict[str, Any]] = {}

    def _call(window: str, *, accumulate: bool) -> dict[str, Any]:
        key = (window, accumulate)
        if key not in cache:
            response = _call_tool(
                mcp_config_path,
                "get_kubecost_workload_costs",
                {
                    "window": window,
                    "accumulate": accumulate,
                    "aggregate": _NONEXISTENT_LABEL_AGGREGATE,
                    # Keep the row set complete: nothing filtered as trivial, nothing truncated.
                    "min_total_cost": 0.0,
                    "top_n": 100,
                },
            )
            status = response.get("status")
            if status == "empty":
                pytest.skip(f"No allocation data on this cluster for window {window}.")
            assert status == "ok", f"Expected ok for window={window} accumulate={accumulate}: {response.get('message')}"
            cache[key] = response
        return cache[key]

    return _call


class TestIntegrationAllocationRowCounts:
    """Row counts must track the queried window against a live Kubecost api.

    Regression cover for the bug where accumulate=false collapsed every daily
    bucket into a single row and reported the window as one day.

    Every daily assertion is anchored to the *accumulated* response rather than
    to the daily one's own resolved window. That matters: the bug corrupted both
    halves of the daily response together (1 row, 1 day), so comparing it only
    against itself would have passed vacuously. The accumulated response was
    correct throughout, which makes it the trustworthy reference.
    """

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_accumulated_returns_exactly_one_row(self, allocation_call, window, expected_days):
        response = allocation_call(window, accumulate=True)
        assert response["row_count"] == 1, (
            f"Expected 1 accumulated row for the nonexistent label {_NONEXISTENT_LABEL_AGGREGATE!r}; got "
            f"{response['row_count']}. A real 'fake' label on this cluster would produce more — change "
            f"_NONEXISTENT_LABEL_AGGREGATE."
        )
        assert len(response["rows"]) == 1
        assert response["truncated"] is False

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_daily_returns_one_row_per_day_of_the_window(self, allocation_call, window, expected_days):
        """The core invariant: N days of window produce N daily rows for a single key."""
        days = allocation_call(window, accumulate=True)["resolved_window"]["days"]
        response = allocation_call(window, accumulate=False)
        assert response["row_count"] == days, (
            f"Expected one row per day for window={window}: the accumulated call resolved to {days} day(s) "
            f"but the daily call returned {response['row_count']} row(s)."
        )
        assert len(response["rows"]) == response["row_count"]
        assert response["truncated"] is False

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_both_modes_resolve_the_same_window(self, allocation_call, window, expected_days):
        """Bucketing changes the row shape, never the range the server queried."""
        accumulated = allocation_call(window, accumulate=True)["resolved_window"]
        daily = allocation_call(window, accumulate=False)["resolved_window"]
        assert (daily["display_start"], daily["display_end"], daily["days"]) == (
            accumulated["display_start"],
            accumulated["display_end"],
            accumulated["days"],
        ), f"accumulate=false reported {daily['display']} but accumulate=true reported {accumulated['display']}"

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_resolved_window_matches_requested_days(self, allocation_call, window, expected_days):
        days = allocation_call(window, accumulate=True)["resolved_window"]["days"]
        if days < expected_days:
            pytest.skip(f"Cluster holds only {days} day(s) of history for window {window}.")
        assert days == expected_days

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_daily_rows_are_consecutive_single_days(self, allocation_call, window, expected_days):
        accumulated = allocation_call(window, accumulate=True)["resolved_window"]
        rows = allocation_call(window, accumulate=False)["rows"]

        assert all(r["window_start"] == r["window_end"] for r in rows), (
            f"Every daily row should cover a single day: {[(r['window_start'], r['window_end']) for r in rows]}"
        )

        starts = [r["window_start"] for r in rows]
        assert len(set(starts)) == len(starts), f"Daily rows must not repeat a date: {starts}"
        assert starts == sorted(starts), f"Daily rows should be in date order: {starts}"

        parsed = [date.fromisoformat(s) for s in starts]
        assert all(b - a == timedelta(days=1) for a, b in zip(parsed, parsed[1:], strict=False)), (
            f"Daily rows should cover consecutive days: {starts}"
        )
        # The series must cover the accumulated window end to end.
        assert starts[0] == accumulated["display_start"]
        assert rows[-1]["window_end"] == accumulated["display_end"]

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_accumulated_row_spans_the_whole_window(self, allocation_call, window, expected_days):
        """The single accumulated row carries the full window, window_end included."""
        response = allocation_call(window, accumulate=True)
        resolved = response["resolved_window"]
        row = response["rows"][0]
        assert row["window_start"] == resolved["display_start"]
        assert row["window_end"] == resolved["display_end"]

    @pytest.mark.parametrize(("window", "expected_days"), _WINDOW_CASES)
    def test_totals_agree_across_accumulate_modes(self, allocation_call, window, expected_days):
        """Splitting the window into daily buckets must not change the money."""
        accumulated = allocation_call(window, accumulate=True)["total_cost"]
        daily_response = allocation_call(window, accumulate=False)
        daily = daily_response["total_cost"]

        assert daily == pytest.approx(sum(r["totalCost"] for r in daily_response["rows"]), abs=0.05)
        # The two calls are seconds apart and the in-progress day keeps accruing,
        # so compare within a tolerance rather than exactly.
        assert daily == pytest.approx(accumulated, rel=0.01)


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


class TestIntegrationGetAbandonedWorkloads:
    def test_returns_status_ok_or_empty(self, mcp_config_path):
        result = _fastmcp("call", mcp_config_path, "get_abandoned_workloads", "--input-json", "{}")
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        output = result.stdout
        assert '"status"' in output or "status" in output
        assert '"error"' not in output or '"ok"' in output or '"empty"' in output

    def test_default_limit_returns_20(self, mcp_config_path):
        result = _fastmcp("call", mcp_config_path, "get_abandoned_workloads", "--input-json", "{}")
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        output = result.stdout
        # Default limit is 20 — message should reflect a bounded workload count
        if '"ok"' in output:
            assert re.search(r"Found \d+ abandoned workload", output), f"Expected workload count in output:\n{output}"


_RFC3339_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T00:00:00Z,\d{4}-\d{2}-\d{2}T00:00:00Z$")


class TestIntegrationGetKubecostCostComparison:
    def test_defaults_to_week_over_week(self, mcp_config_path):
        """Calling with no args should compute RFC3339 week-over-week defaults and return ok or empty."""
        result = _fastmcp("call", mcp_config_path, "get_kubecost_cost_comparison", "--input-json", "{}")
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        response = json.loads(result.stdout)
        assert response.get("status") in {"ok", "empty"}, f"Unexpected status: {response.get('status')}"
        # Defaults must be concrete RFC3339 date ranges, not a named alias
        assert _RFC3339_RANGE_RE.match(response.get("current_window", "")), (
            f"current_window should be an RFC3339 range, got: {response.get('current_window')}"
        )
        assert _RFC3339_RANGE_RE.match(response.get("baseline_window", "")), (
            f"baseline_window should be an RFC3339 range, got: {response.get('baseline_window')}"
        )
        # The two windows must be exactly 7 days each and contiguous
        cur_start, cur_end = response["current_window"].split(",")
        base_start, base_end = response["baseline_window"].split(",")
        assert base_end == cur_start, "baseline_window must end where current_window begins (contiguous weeks)"
        cur_days = (date.fromisoformat(cur_end[:10]) - date.fromisoformat(cur_start[:10])).days
        base_days = (date.fromisoformat(base_end[:10]) - date.fromisoformat(base_start[:10])).days
        assert cur_days == 7, f"current_window should span 7 days, got {cur_days}"
        assert base_days == 7, f"baseline_window should span 7 days, got {base_days}"

    def test_response_shape(self, mcp_config_path):
        """Response must include rows, row_count, and window echo fields."""
        result = _fastmcp("call", mcp_config_path, "get_kubecost_cost_comparison", "--input-json", "{}")
        assert result.returncode == 0, f"fastmcp exited {result.returncode}:\n{result.stderr}"
        response = json.loads(result.stdout)
        if response.get("status") == "empty":
            pytest.skip("No comparison data available on this cluster.")
        assert "rows" in response
        assert "row_count" in response
        assert response["row_count"] >= len(response["rows"])
        assert len(response["rows"]) <= 20
        assert response["truncated"] is (response["row_count"] > len(response["rows"]))
