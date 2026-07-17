"""Tests for tool-layer parsing helpers in kubecost_tools.py."""

from __future__ import annotations

import pytest

import mcp_kubecost.tools.kubecost_tools as ktools

_aggregate_by_dimensions = ktools._aggregate_by_dimensions
_format_date = ktools._format_date
_format_number = ktools._format_number
# _parse_allocation_response is module-level (not nested), so direct import works.
_parse_allocation_response = ktools._parse_allocation_response
aggregate_savings_by = ktools.aggregate_savings_by
compute_savings_notes = ktools.compute_savings_notes
parse_request_sizing_response = ktools.parse_request_sizing_response

# ---------------------------------------------------------------------------
# _format_number
# ---------------------------------------------------------------------------


class TestFormatNumber:
    def test_whole_float_returns_int(self):
        assert _format_number(5.0) == 5
        assert isinstance(_format_number(5.0), int)

    def test_fractional_rounds_to_two_dp(self):
        assert _format_number(5.123456) == 5.12

    def test_zero(self):
        assert _format_number(0.0) == 0

    def test_negative_whole(self):
        assert _format_number(-3.0) == -3


# ---------------------------------------------------------------------------
# _format_date
# ---------------------------------------------------------------------------


class TestFormatDate:
    def test_iso_zulu_string(self):
        assert _format_date("2024-01-15T00:00:00Z") == "2024-01-15"

    def test_iso_offset_string(self):
        assert _format_date("2024-06-01T12:00:00+00:00") == "2024-06-01"

    def test_empty_string_returns_empty(self):
        assert _format_date("") == ""

    def test_invalid_string_returned_as_is(self):
        assert _format_date("not-a-date") == "not-a-date"


# ---------------------------------------------------------------------------
# _parse_allocation_response
# ---------------------------------------------------------------------------


class TestParseAllocationResponse:
    def test_empty_data_returns_empty(self):
        dims, rows = _parse_allocation_response({"data": []})
        assert dims == []
        assert rows == []

    def test_missing_data_key(self):
        dims, rows = _parse_allocation_response({})
        assert dims == []
        assert rows == []

    def test_single_entry_dimensions(self, allocation_response_one_ns):
        dims, rows = _parse_allocation_response(allocation_response_one_ns)
        assert "cluster" in dims
        assert "namespace" in dims
        assert len(rows) == 1

    def test_cost_fields_present_in_row(self, allocation_response_one_ns):
        _, rows = _parse_allocation_response(allocation_response_one_ns)
        assert rows[0]["totalCost"] == 16
        assert rows[0]["cpuCost"] == 10.5

    def test_window_start_extracted(self, allocation_response_one_ns):
        _, rows = _parse_allocation_response(allocation_response_one_ns)
        assert rows[0]["window_start"] == "2024-01-01"

    def test_multi_entry_returns_all_rows(self, allocation_response_multi_ns):
        _, rows = _parse_allocation_response(allocation_response_multi_ns)
        assert len(rows) == 2

    def test_dim_fallback_when_no_properties(self):
        resp = {
            "data": [
                {
                    "x": {
                        "name": "cluster-one/ns-a",
                        "properties": {},
                        "window": {"start": "2024-01-01T00:00:00Z"},
                        "totalCost": 5.0,
                        "cpuCost": 0.0,
                        "cpuCostIdle": 0.0,
                        "ramCost": 0.0,
                        "ramCostIdle": 0.0,
                        "networkCost": 0.0,
                        "pvCost": 0.0,
                        "gpuCost": 0.0,
                        "gpuCostIdle": 0.0,
                        "loadBalancerCost": 0.0,
                        "sharedCost": 0.0,
                        "totalEfficiency": 0.0,
                    }
                }
            ]
        }
        dims, rows = _parse_allocation_response(resp)
        assert dims[0].startswith("dim_")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# _aggregate_by_dimensions
# ---------------------------------------------------------------------------


class TestAggregateByDimensions:
    def test_sums_cost_across_duplicate_keys(self, allocation_response_multi_ns):
        _, rows = _parse_allocation_response(allocation_response_multi_ns)
        # Force both rows to same namespace so they collapse
        for r in rows:
            r["namespace"] = "merged"
        aggregated = _aggregate_by_dimensions(rows, ["namespace"])
        assert len(aggregated) == 1
        assert aggregated[0]["totalCost"] == 38.2  # 31.0 + 7.2

    def test_sorted_by_total_cost_descending(self, allocation_response_multi_ns):
        _, rows = _parse_allocation_response(allocation_response_multi_ns)
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        # ns-a has higher cost than ns-b
        assert float(aggregated[0]["totalCost"]) >= float(aggregated[1]["totalCost"])

    def test_idle_pct_computed(self, allocation_response_one_ns):
        _, rows = _parse_allocation_response(allocation_response_one_ns)
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert "cpuIdlePct" in aggregated[0]
        assert aggregated[0]["cpuIdlePct"].endswith("%")

    def test_zero_cpu_cost_idle_pct(self):
        rows = [
            {
                "cluster": "c",
                "namespace": "n",
                "cpuCost": 0.0,
                "cpuCostIdle": 0.0,
                "ramCost": 1.0,
                "ramCostIdle": 0.0,
                "gpuCost": 0.0,
                "gpuCostIdle": 0.0,
                "networkCost": 0.0,
                "pvCost": 0.0,
                "loadBalancerCost": 0.0,
                "sharedCost": 0.0,
                "totalCost": 1.0,
                "totalEfficiency": 0.0,
            }
        ]
        agg = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert agg[0]["cpuIdlePct"] == "0%"


# ---------------------------------------------------------------------------
# compute_savings_notes
# ---------------------------------------------------------------------------


class TestComputeSavingsNotes:
    def test_note_when_recommended_less_than_max(self):
        row = {"Recommended_memory": 100.0, "MaxUsage_memory": 200.0}
        notes = compute_savings_notes(row)
        assert "memRecommendationLessThanMax" in notes

    def test_no_note_when_recommended_above_max(self):
        row = {"Recommended_memory": 300.0, "MaxUsage_memory": 200.0}
        notes = compute_savings_notes(row)
        assert notes == ""

    def test_missing_fields_default_to_zero(self):
        notes = compute_savings_notes({})
        # 0 < 0 is False, so no note
        assert notes == ""


# ---------------------------------------------------------------------------
# parse_request_sizing_response
# ---------------------------------------------------------------------------


class TestParseRequestSizingResponse:
    def test_totals_extracted(self, savings_api_response):
        total, count, rows = parse_request_sizing_response(savings_api_response)
        assert total == 45.50
        assert count == 2

    def test_two_rows_returned(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        assert len(rows) == 2

    def test_rows_sorted_by_savings_desc(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        assert float(rows[0]["monthlySavings_total"]) >= float(rows[1]["monthlySavings_total"])

    def test_metadata_fields_present(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        row = rows[0]
        assert row["containerName"] == "api"
        assert row["namespace"] == "default"
        assert row["clusterID"] == "cluster-one"

    def test_nested_fields_flattened(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        row = rows[0]
        assert row["monthlySavings_cpu"] == 20
        assert row["Recommended_cpu"] == 200
        assert row["currentEfficiency_cpu"] == 0.3

    def test_empty_recommendations(self):
        total, count, rows = parse_request_sizing_response(
            {"TotalMonthlySavings": 0.0, "Count": 0, "Recommendations": []}
        )
        assert total == 0.0
        assert count == 0
        assert rows == []

    def test_missing_keys_gracefully_handled(self):
        # Missing keys → defaults applied; no crash
        resp = {}
        total, count, rows = parse_request_sizing_response(resp)
        assert total == 0.0
        assert count == 0
        assert rows == []


# ---------------------------------------------------------------------------
# aggregate_savings_by
# ---------------------------------------------------------------------------


class TestAggregateSavingsBy:
    def test_groups_by_container_name(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        aggregated = aggregate_savings_by(rows, "containerName")
        assert len(aggregated) == 2  # two distinct container names

    def test_groups_by_namespace(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        aggregated = aggregate_savings_by(rows, "namespace")
        namespaces = {r["namespace"] for r in aggregated}
        assert namespaces == {"default", "monitoring"}

    def test_sorted_desc(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        aggregated = aggregate_savings_by(rows, "containerName")
        totals = [float(r["monthlySavings_total"]) for r in aggregated]
        assert totals == sorted(totals, reverse=True)

    def test_container_count_correct(self, savings_api_response):
        _, _, rows = parse_request_sizing_response(savings_api_response)
        # Force both rows to same namespace
        for r in rows:
            r["namespace"] = "same"
        aggregated = aggregate_savings_by(rows, "namespace")
        assert aggregated[0]["container_count"] == 2

    def test_notes_combined(self):
        rows = [
            {"containerName": "app", "monthlySavings_total": 10.0, "notes": "note-a"},
            {"containerName": "app", "monthlySavings_total": 5.0, "notes": "note-b"},
        ]
        aggregated = aggregate_savings_by(rows, "containerName")
        assert "note-a" in aggregated[0]["notes"]
        assert "note-b" in aggregated[0]["notes"]


# ---------------------------------------------------------------------------
# _parse_abandoned_workloads_response
# ---------------------------------------------------------------------------

_parse_abandoned_workloads_response = ktools._parse_abandoned_workloads_response


class TestParseAbandonedWorkloadsResponse:
    def test_empty_list_returns_empty(self):
        assert _parse_abandoned_workloads_response([]) == []

    def test_row_count_matches_input(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        assert len(rows) == 2

    def test_sorted_by_monthly_savings_desc(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        assert rows[0]["monthlySavings"] >= rows[1]["monthlySavings"]

    def test_metadata_fields_present(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        row = rows[0]  # highest savings
        assert row["pod"] == "idle-worker-abc"
        assert row["namespace"] == "batch"
        assert row["clusterId"] == "cluster-one"

    def test_owner_flattened(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        row = rows[0]
        assert row["owner_name"] == "idle-worker"
        assert row["owner_kind"] == "deployment"

    def test_no_owner_defaults_to_empty(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        unmanaged = next(r for r in rows if r["pod"] == "stale-job-xyz")
        assert unmanaged["owner_name"] == ""
        assert unmanaged["owner_kind"] == ""

    def test_allocation_flattened(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        row = rows[0]
        assert row["allocated_cpu_cores"] == pytest.approx(0.5)
        assert row["allocated_ram_bytes"] == pytest.approx(536870912.0)

    def test_network_fields_present(self, abandoned_workloads_api_response):
        rows = _parse_abandoned_workloads_response(abandoned_workloads_api_response)
        row = rows[0]
        assert row["ingressBytesPerSecond"] == 0.0
        assert row["egressBytesPerSecond"] == 0.0

    def test_missing_fields_default_gracefully(self):
        rows = _parse_abandoned_workloads_response([{}])
        assert len(rows) == 1
        assert rows[0]["monthlySavings"] == 0.0
        assert rows[0]["pod"] == ""
