"""Tests for tool-layer parsing helpers in kubecost_tools.py."""

from __future__ import annotations

import pytest

import mcp_kubecost.tools.kubecost_tools as ktools

_aggregate_by_dimensions = ktools._aggregate_by_dimensions
_format_date = ktools._format_date
_format_end_date = ktools._format_end_date
_format_number = ktools._format_number
# _parse_allocation_response is module-level (not nested), so direct import works.
_parse_allocation_response = ktools._parse_allocation_response
_dimension_columns_for_aggregate = ktools._dimension_columns_for_aggregate
_window_from_allocation = ktools._window_from_allocation
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
# _format_end_date
# ---------------------------------------------------------------------------


class TestFormatEndDate:
    """Kubecost's window end is exclusive; rows report the last day it covers."""

    def test_exclusive_midnight_end_steps_back_one_day(self):
        assert _format_end_date("2024-01-08T00:00:00Z") == "2024-01-07"

    def test_single_day_bucket_end_equals_its_start_day(self):
        assert _format_end_date("2024-01-02T00:00:00Z") == "2024-01-01"

    def test_mid_day_end_stays_on_that_day(self):
        # In-progress windows ('week'/'month') end at 'now', not midnight.
        assert _format_end_date("2024-01-08T13:45:00Z") == "2024-01-08"

    def test_empty_string_returns_empty(self):
        assert _format_end_date("") == ""

    def test_invalid_string_returned_as_is(self):
        assert _format_end_date("not-a-date") == "not-a-date"


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

    def test_window_end_extracted_as_inclusive_last_day(self, allocation_response_one_ns):
        # Fixture window is 2024-01-01 -> 2024-01-08 exclusive, i.e. through the 7th.
        _, rows = _parse_allocation_response(allocation_response_one_ns)
        assert rows[0]["window_end"] == "2024-01-07"

    def test_window_end_empty_when_api_omits_end(self):
        resp = {
            "data": [
                {
                    "ns-a": {
                        "name": "cluster-one/ns-a",
                        "properties": {"cluster": "cluster-one", "namespace": "ns-a"},
                        "window": {"start": "2024-01-01T00:00:00Z"},
                        "totalCost": 1.0,
                    }
                }
            ]
        }
        _, rows = _parse_allocation_response(resp)
        assert rows[0]["window_end"] == ""

    def test_daily_buckets_keep_their_own_windows(self, allocation_response_daily_buckets):
        _, rows = _parse_allocation_response(allocation_response_daily_buckets)
        assert len(rows) == 6
        assert {(r["window_start"], r["window_end"]) for r in rows} == {
            ("2024-01-01", "2024-01-01"),
            ("2024-01-02", "2024-01-02"),
            ("2024-01-03", "2024-01-03"),
        }

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


def _alloc(*entries: dict) -> dict:
    """Build a single-bucket allocation response from (name, properties) entries."""
    return {
        "data": [
            {
                e["name"]: {
                    "name": e["name"],
                    "properties": e.get("properties", {}),
                    "window": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-08T00:00:00Z"},
                    "totalCost": e.get("totalCost", 1.0),
                }
                for e in entries
            }
        ]
    }


class TestDimensionColumnsForAggregate:
    def test_single_dimension(self):
        assert _dimension_columns_for_aggregate("namespace") == [("namespace", None)]

    def test_multiple_dimensions_keep_order(self):
        assert _dimension_columns_for_aggregate("cluster,namespace") == [
            ("cluster", None),
            ("namespace", None),
        ]

    def test_whitespace_and_empties_ignored(self):
        assert _dimension_columns_for_aggregate(" cluster , , namespace ") == [
            ("cluster", None),
            ("namespace", None),
        ]

    def test_label_maps_to_nested_group(self):
        assert _dimension_columns_for_aggregate("label:app") == [("app", "labels")]

    def test_annotation_maps_to_nested_group(self):
        assert _dimension_columns_for_aggregate("annotation:team") == [("team", "annotations")]

    def test_empty_string(self):
        assert _dimension_columns_for_aggregate("") == []


class TestParseAllocationResponseWithAggregate:
    def test_single_dimension_named_from_aggregate(self):
        """The reported bug: aggregate='namespace' must not yield a 'dim_0' column."""
        resp = _alloc({"name": "kube-system", "properties": {}})
        dims, rows = _parse_allocation_response(resp, "namespace")
        assert dims == ["namespace"]
        assert rows[0]["namespace"] == "kube-system"

    def test_multi_dimension_named_from_aggregate(self):
        resp = _alloc({"name": "cluster-one/ns-a", "properties": {}})
        dims, rows = _parse_allocation_response(resp, "cluster,namespace")
        assert dims == ["cluster", "namespace"]
        assert rows[0]["cluster"] == "cluster-one"
        assert rows[0]["namespace"] == "ns-a"

    def test_properties_take_precedence_over_name(self):
        resp = _alloc({"name": "ignored", "properties": {"namespace": "ns-a"}})
        _, rows = _parse_allocation_response(resp, "namespace")
        assert rows[0]["namespace"] == "ns-a"

    def test_propertyless_idle_entry_keeps_its_name(self):
        """An __idle__ entry sorted first must not poison discovery or blank its own row."""
        resp = _alloc(
            {"name": "__idle__", "properties": {}},
            {"name": "ns-a", "properties": {"namespace": "ns-a"}},
        )
        dims, rows = _parse_allocation_response(resp, "namespace")
        assert dims == ["namespace"]
        assert [r["namespace"] for r in rows] == ["__idle__", "ns-a"]

    def test_label_aggregate_reads_nested_property(self):
        resp = _alloc({"name": "aggregator", "properties": {"labels": {"app": "aggregator"}}})
        dims, rows = _parse_allocation_response(resp, "label:app")
        assert dims == ["app"]
        assert rows[0]["app"] == "aggregator"

    def test_label_aggregate_falls_back_to_name_when_unallocated(self):
        resp = _alloc({"name": "__unallocated__", "properties": {}})
        _, rows = _parse_allocation_response(resp, "label:app")
        assert rows[0]["app"] == "__unallocated__"

    def test_name_with_wrong_part_count_does_not_shift_columns(self):
        """A name that doesn't split into one part per column must not misalign values."""
        resp = _alloc({"name": "only-one-part", "properties": {}})
        dims, rows = _parse_allocation_response(resp, "cluster,namespace")
        assert dims == ["cluster", "namespace"]
        assert rows[0] == {**rows[0], "cluster": "", "namespace": ""}

    def test_list_valued_property_is_joined(self):
        resp = _alloc({"name": "svc", "properties": {"services": ["svc-a", "svc-b"]}})
        _, rows = _parse_allocation_response(resp, "services")
        assert rows[0]["services"] == "svc-a|svc-b"


class TestParseAllocationResponseWithoutAggregate:
    def test_dimensions_unioned_across_entries(self):
        """Discovery must not depend on the first entry alone."""
        resp = _alloc(
            {"name": "__idle__", "properties": {}},
            {"name": "cluster-one/ns-a", "properties": {"cluster": "cluster-one", "namespace": "ns-a"}},
        )
        dims, rows = _parse_allocation_response(resp)
        assert dims == ["cluster", "namespace"]
        assert rows[1]["namespace"] == "ns-a"

    def test_partial_first_entry_does_not_drop_dimensions(self):
        resp = _alloc(
            {"name": "cluster-one/", "properties": {"cluster": "cluster-one"}},
            {"name": "cluster-one/ns-a", "properties": {"cluster": "cluster-one", "namespace": "ns-a"}},
        )
        dims, _ = _parse_allocation_response(resp)
        assert dims == ["cluster", "namespace"]


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

    def test_accumulated_response_keeps_one_row_per_key(self, allocation_response_multi_ns):
        """A single shared window must not change the accumulated grouping."""
        _, rows = _parse_allocation_response(allocation_response_multi_ns)
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert len(aggregated) == 2
        assert all(r["window_start"] == "2024-01-01" and r["window_end"] == "2024-01-07" for r in aggregated)

    def test_daily_buckets_are_not_collapsed(self, allocation_response_daily_buckets):
        """Each (dimension key, day) stays its own row — the documented daily breakdown."""
        _, rows = _parse_allocation_response(allocation_response_daily_buckets)
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert len(aggregated) == 6
        assert [(r["window_start"], r["namespace"], r["totalCost"]) for r in aggregated] == [
            ("2024-01-01", "ns-a", 10.0),
            ("2024-01-01", "ns-b", 2.0),
            ("2024-01-02", "ns-a", 20.0),
            ("2024-01-02", "ns-b", 4.0),
            ("2024-01-03", "ns-a", 30.0),
            ("2024-01-03", "ns-b", 6.0),
        ]

    def test_daily_breakdown_preserves_the_full_total(self, allocation_response_daily_buckets):
        _, rows = _parse_allocation_response(allocation_response_daily_buckets)
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert sum(float(r["totalCost"]) for r in aggregated) == 72.0  # (10+2)+(20+4)+(30+6)

    def test_rows_without_window_still_group_by_dimension(self):
        """Hand-built rows carrying no window keep collapsing on dimensions alone."""
        rows = [
            {"cluster": "c", "namespace": "n", "totalCost": 1.0, "cpuCost": 1.0},
            {"cluster": "c", "namespace": "n", "totalCost": 2.0, "cpuCost": 2.0},
        ]
        aggregated = _aggregate_by_dimensions(rows, ["cluster", "namespace"])
        assert len(aggregated) == 1
        assert aggregated[0]["totalCost"] == 3.0
        assert aggregated[0]["window_start"] == ""
        assert aggregated[0]["window_end"] == ""


# ---------------------------------------------------------------------------
# _window_from_allocation
# ---------------------------------------------------------------------------


class TestWindowFromAllocation:
    """The queried range spans every bucket, not just the first one."""

    def test_single_bucket_reports_its_window(self, allocation_response_one_ns):
        resolved = _window_from_allocation(allocation_response_one_ns, "7d")
        assert resolved is not None
        assert resolved.days == 7
        assert resolved.display_start == "2024-01-01"
        assert resolved.display_end == "2024-01-07"

    def test_daily_buckets_span_first_start_to_last_end(self, allocation_response_daily_buckets):
        """Regression: previously reported 1 day because only the first bucket was read."""
        resolved = _window_from_allocation(allocation_response_daily_buckets, "3d")
        assert resolved is not None
        assert resolved.days == 3
        assert resolved.display_start == "2024-01-01"
        assert resolved.display_end == "2024-01-03"
        assert resolved.source_expression == "3d"

    def test_buckets_out_of_order_still_span_correctly(self, allocation_response_daily_buckets):
        reversed_response = {"data": list(reversed(allocation_response_daily_buckets["data"]))}
        resolved = _window_from_allocation(reversed_response, "3d")
        assert resolved is not None
        assert resolved.days == 3
        assert resolved.display_start == "2024-01-01"

    def test_no_usable_window_returns_none(self):
        assert _window_from_allocation({"data": [{"ns-a": {"totalCost": 1.0}}]}, "7d") is None

    def test_empty_response_returns_none(self):
        assert _window_from_allocation({}, "7d") is None

    def test_unparseable_windows_are_skipped(self):
        resp = {
            "data": [
                {"ns-a": {"window": {"start": "nonsense", "end": "also-nonsense"}}},
                {"ns-a": {"window": {"start": "2024-01-02T00:00:00Z", "end": "2024-01-03T00:00:00Z"}}},
            ]
        }
        resolved = _window_from_allocation(resp, "2d")
        assert resolved is not None
        assert resolved.days == 1
        assert resolved.display_start == "2024-01-02"


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
