"""Tests for tools/_common.py utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError as McpToolError

from mcp_kubecost.client import KubecostClientError
from mcp_kubecost.errors import ErrorCode, ToolError
from mcp_kubecost.tools import _common
from mcp_kubecost.tools._common import (
    ResolvedWindow,
    extract_list,
    format_tool_error,
    mcp_error_response_fields,
    parse_api_timestamp,
    resolve_window,
    resolved_window_from_api,
    safe_path_segment,
    summarize_exception,
    summarize_tool_response,
)

# ---------------------------------------------------------------------------
# format_tool_error
# ---------------------------------------------------------------------------


class TestFormatToolError:
    def test_contains_code_and_message(self):
        err = ToolError(
            code=ErrorCode.NOT_FOUND,
            message="thing not found",
            retryable=False,
            suggested_action="check id",
        )
        output = format_tool_error(err)
        assert "not_found" in output
        assert "thing not found" in output
        assert "check id" in output

    def test_truncated_at_500_chars(self):
        err = ToolError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="x" * 600,
            retryable=True,
            suggested_action="retry",
        )
        output = format_tool_error(err)
        assert len(output) <= 500
        assert output.endswith("...")

    def test_retryable_flag_rendered(self):
        for retryable in (True, False):
            err = ToolError(
                code=ErrorCode.NOT_FOUND,
                message="m",
                retryable=retryable,
                suggested_action="a",
            )
            output = format_tool_error(err)
            expected = "true" if retryable else "false"
            assert f"retryable={expected}" in output


class TestMcpErrorResponseFields:
    def test_splits_action_from_formatted_error(self):
        err = ToolError(
            code=ErrorCode.INVALID_INPUT,
            message="Kubecost response: Enterprise feature: requested window of 30d",
            retryable=False,
            suggested_action="Retry with a shorter window.",
        )
        message, action = mcp_error_response_fields(McpToolError(format_tool_error(err)))
        assert "Enterprise feature" in message
        assert "Action:" not in message
        assert action == "Retry with a shorter window."

    def test_fallback_when_action_missing(self):
        message, action = mcp_error_response_fields(RuntimeError("boom"))
        assert message == "boom"
        assert "connectivity" in action


# ---------------------------------------------------------------------------
# safe_path_segment
# ---------------------------------------------------------------------------


class TestSafePathSegment:
    def test_valid_segment_returned(self):
        assert safe_path_segment("abc-123", "field") == "abc-123"

    def test_valid_with_dot(self):
        assert safe_path_segment("v1.2.3", "field") == "v1.2.3"

    def test_path_traversal_rejected(self):
        with pytest.raises(McpToolError):
            safe_path_segment("../../secret", "field")

    def test_slash_rejected(self):
        with pytest.raises(McpToolError):
            safe_path_segment("a/b", "field")

    def test_empty_string_rejected(self):
        with pytest.raises(McpToolError):
            safe_path_segment("", "field")

    def test_double_dot_alone_rejected(self):
        with pytest.raises(McpToolError):
            safe_path_segment("..", "field")

    def test_leading_space_rejected(self):
        with pytest.raises(McpToolError):
            safe_path_segment(" abc", "field")


# ---------------------------------------------------------------------------
# extract_list
# ---------------------------------------------------------------------------


class TestExtractList:
    def test_list_input_returned_directly(self):
        data = [{"a": 1}, {"b": 2}]
        assert extract_list(data) == data

    def test_non_dict_items_dropped(self):
        assert extract_list([1, "x", {"ok": True}]) == [{"ok": True}]

    def test_result_key(self):
        assert extract_list({"result": [{"x": 1}]}) == [{"x": 1}]

    def test_results_key(self):
        assert extract_list({"results": [{"x": 2}]}) == [{"x": 2}]

    def test_data_key(self):
        assert extract_list({"data": [{"x": 3}]}) == [{"x": 3}]

    def test_extra_key_takes_priority(self):
        data = {"items": [{"k": 1}], "results": [{"k": 2}]}
        assert extract_list(data, "items") == [{"k": 1}]

    def test_no_match_returns_empty(self):
        assert extract_list({"unknown": [{"x": 1}]}) == []

    def test_scalar_returns_empty(self):
        assert extract_list(42) == []


# ---------------------------------------------------------------------------
# summarize_exception
# ---------------------------------------------------------------------------


class TestSummarizeException:
    def test_kubecost_client_error_mapped(self):
        exc = KubecostClientError(status_code=401, message="unauth", url="http://x", path="/x")
        summary = summarize_exception(exc)
        assert summary["code"] == ErrorCode.AUTHENTICATION_FAILED.value
        assert "401" in summary["message"]
        assert summary["retryable"] is False

    def test_value_error_mapped(self):
        exc = ValueError("bad input value")
        summary = summarize_exception(exc)
        assert summary["code"] == ErrorCode.INVALID_INPUT.value
        assert "bad input value" in summary["message"]
        assert summary["retryable"] is False

    def test_unknown_exception_mapped(self):
        exc = RuntimeError("unexpected")
        summary = summarize_exception(exc)
        assert summary["code"] == ErrorCode.DATA_UNAVAILABLE.value
        assert "RuntimeError" in summary["message"]
        assert summary["retryable"] is True


class TestResolveWindow:
    @pytest.mark.parametrize(("expression", "days"), [("7d", 7), ("15d", 15), ("30d", 30)])
    def test_duration_aliases(self, expression, days):
        result = resolve_window(expression)
        assert isinstance(result, ResolvedWindow)
        assert result.days == days
        assert result.end_utc - result.start_utc == timedelta(days=days)
        assert result.display.endswith(f"({days} days, partial)")

    @pytest.mark.parametrize("expression", ["7d", "15d", "30d"])
    def test_duration_aliases_include_today(self, expression):
        """Kubecost counts back N days including today, ending at the close of
        the current day — verified against /model/allocation on the demo cluster."""
        result = resolve_window(expression)
        today = _common._start_of_utc_day()
        assert result.end_utc == today + timedelta(days=1)
        assert result.display_end == today.strftime("%Y-%m-%d")

    def test_lastmonth_is_a_calendar_month(self):
        result = resolve_window("lastmonth")
        assert result.start_utc.day == 1
        assert result.end_utc.day == 1
        assert result.end_utc.month != result.start_utc.month
        assert result.days in {28, 29, 30, 31}

    def test_lastweek_is_a_full_sunday_to_sunday_week(self):
        # Kubecost weeks start on Sunday; weekday() is Monday=0..Sunday=6.
        result = resolve_window("lastweek")
        assert result.start_utc.weekday() == 6
        assert result.end_utc.weekday() == 6
        assert result.days == 7

    def test_rfc3339_range(self):
        result = resolve_window("2026-07-01T00:00:00Z,2026-07-08T00:00:00Z")
        assert result.start_utc.isoformat() == "2026-07-01T00:00:00+00:00"
        assert result.end_utc.isoformat() == "2026-07-08T00:00:00+00:00"
        assert result.display == "2026-07-01 to 2026-07-07 (7 days)"

    def test_reversed_rfc3339_range_is_normalized(self):
        result = resolve_window("2026-06-01T00:00:00Z,2026-05-01T00:00:00Z")
        assert result.start_utc.isoformat() == "2026-05-01T00:00:00+00:00"
        assert result.end_utc.isoformat() == "2026-06-01T00:00:00+00:00"
        assert result.source_expression == "2026-05-01T00:00:00Z,2026-06-01T00:00:00Z"

    @pytest.mark.parametrize(
        "expression",
        [
            "not-a-window",
            "2026-07-01T00:00:00Z,not-a-date",
            "2026-07-01T00:00:00Z,2026-07-01T00:00:00Z",
        ],
    )
    def test_invalid_window_raises_tool_error(self, expression):
        with pytest.raises(McpToolError):
            resolve_window(expression)


class TestResolveWindowPartialPeriods:
    """Period-to-date windows must never report days that have not happened yet."""

    @staticmethod
    def _freeze(monkeypatch, moment):
        monkeypatch.setattr(_common, "_start_of_utc_day", lambda: moment)

    @pytest.mark.parametrize("expression", ["today", "week", "month", "7d", "30d"])
    def test_windows_covering_today_are_flagged_partial(self, expression):
        result = resolve_window(expression)
        assert result.is_partial is True
        assert ", partial" in result.display

    @pytest.mark.parametrize("expression", ["lastweek", "lastmonth"])
    def test_completed_windows_are_not_partial(self, expression):
        result = resolve_window(expression)
        assert result.is_partial is False
        assert "partial" not in result.display

    @pytest.mark.parametrize("expression", ["today", "week", "month", "7d", "30d"])
    def test_windows_never_extend_past_today(self, expression):
        tomorrow = _common._start_of_utc_day() + timedelta(days=1)
        assert resolve_window(expression).end_utc <= tomorrow

    def test_month_mid_period_reports_elapsed_days_only(self, monkeypatch):
        # Regression: 'month' previously returned the full calendar month, so on
        # 06-AUG it claimed 31 days when only 6 days of data existed.
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = resolve_window("month")
        assert result.days == 6
        assert result.start_utc == datetime(2026, 8, 1, tzinfo=UTC)
        assert result.end_utc == datetime(2026, 8, 7, tzinfo=UTC)
        assert result.display == "2026-08-01 to 2026-08-06 (6 days, partial)"

    def test_week_mid_period_reports_elapsed_days_only(self, monkeypatch):
        # Kubecost weeks start Sunday. 2026-08-06 is a Thursday, so the week
        # opened Sunday 2026-08-02: five elapsed days, not four.
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = resolve_window("week")
        assert result.days == 5
        assert result.start_utc == datetime(2026, 8, 2, tzinfo=UTC)
        assert result.display == "2026-08-02 to 2026-08-06 (5 days, partial)"

    def test_week_on_a_sunday_is_a_single_elapsed_day(self, monkeypatch):
        # Edge case for the Sunday anchor: on Sunday itself the week has just
        # opened, so 'week' must not reach back into the prior week.
        self._freeze(monkeypatch, datetime(2026, 8, 2, tzinfo=UTC))
        result = resolve_window("week")
        assert result.start_utc == datetime(2026, 8, 2, tzinfo=UTC)
        assert result.days == 1

    def test_lastweek_runs_sunday_through_saturday(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = resolve_window("lastweek")
        assert result.start_utc == datetime(2026, 7, 26, tzinfo=UTC)
        assert result.end_utc == datetime(2026, 8, 2, tzinfo=UTC)
        assert result.display == "2026-07-26 to 2026-08-01 (7 days)"

    def test_lastweek_on_a_sunday_is_the_prior_full_week(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 2, tzinfo=UTC))
        result = resolve_window("lastweek")
        assert result.start_utc == datetime(2026, 7, 26, tzinfo=UTC)
        assert result.end_utc == datetime(2026, 8, 2, tzinfo=UTC)
        assert result.days == 7

    def test_today_is_a_single_day(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = resolve_window("today")
        assert result.days == 1
        assert result.display == "2026-08-06 (1 day, partial)"

    def test_lastmonth_spans_the_whole_prior_month(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = resolve_window("lastmonth")
        assert result.days == 31
        assert result.display == "2026-07-01 to 2026-07-31 (31 days)"

    def test_lastmonth_in_january_wraps_to_december(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 1, 15, tzinfo=UTC))
        result = resolve_window("lastmonth")
        assert result.start_utc == datetime(2025, 12, 1, tzinfo=UTC)
        assert result.end_utc == datetime(2026, 1, 1, tzinfo=UTC)
        assert result.days == 31

    def test_lastmonth_handles_leap_february(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2028, 3, 10, tzinfo=UTC))
        assert resolve_window("lastmonth").days == 29


class TestResolvedWindowFromApi:
    """Ground truth taken from the window Kubecost echoes in its response.

    Boundaries below were captured from a live /model/allocation call on
    demo.kubecost.xyz on 2026-08-06.
    """

    def test_uses_the_servers_own_boundaries(self):
        result = resolved_window_from_api({"start": "2026-07-31T00:00:00Z", "end": "2026-08-07T00:00:00Z"}, "7d")
        assert result is not None
        assert result.start_utc == datetime(2026, 7, 31, tzinfo=UTC)
        assert result.end_utc == datetime(2026, 8, 7, tzinfo=UTC)
        assert result.days == 7
        assert result.source_expression == "7d"

    def test_accepts_an_in_progress_end_that_is_not_midnight(self):
        # Kubecost ends 'week'/'month' at the current instant, not a day
        # boundary. That must not be rejected as a non-whole-day span.
        result = resolved_window_from_api({"start": "2026-08-01T00:00:00Z", "end": "2026-08-06T19:26:48Z"}, "month")
        assert result is not None
        assert result.days == 6
        assert result.display_end == "2026-08-06"

    @pytest.mark.parametrize(
        "window",
        [
            None,
            {},
            "not-a-dict",
            {"start": "2026-08-01T00:00:00Z"},
            {"start": "bogus", "end": "2026-08-07T00:00:00Z"},
            {"start": "2026-08-07T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
        ],
    )
    def test_unusable_windows_return_none_rather_than_raising(self, window):
        """Callers fall back to the prediction; a bad window must not lose the response."""
        assert resolved_window_from_api(window, "7d") is None


# ---------------------------------------------------------------------------
# parse_api_timestamp
# ---------------------------------------------------------------------------


class TestParseApiTimestamp:
    """Shared by resolved_window_from_api and the allocation window-span logic."""

    def test_zulu_suffix(self):
        assert parse_api_timestamp("2026-08-01T00:00:00Z") == datetime(2026, 8, 1, tzinfo=UTC)

    def test_explicit_offset(self):
        assert parse_api_timestamp("2026-08-01T02:00:00+02:00") == datetime(2026, 8, 1, tzinfo=UTC)

    def test_naive_timestamp_is_assumed_utc(self):
        """Results are compared across responses, so everything must be tz-aware."""
        assert parse_api_timestamp("2026-08-01T00:00:00") == datetime(2026, 8, 1, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", "not-a-date", {}])
    def test_unusable_values_return_none(self, value):
        assert parse_api_timestamp(value) is None


# ---------------------------------------------------------------------------
# to_api_window
# ---------------------------------------------------------------------------


class TestToApiWindow:
    """to_api_window pre-resolves calendar aliases and passes everything else through."""

    @staticmethod
    def _freeze(monkeypatch, moment):
        monkeypatch.setattr(_common, "_start_of_utc_day", lambda: moment)

    def test_lastmonth_resolved_to_rfc3339(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = _common.to_api_window("lastmonth")
        assert result == "2026-07-01T00:00:00Z,2026-08-01T00:00:00Z"

    def test_lastweek_resolved_to_rfc3339(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = _common.to_api_window("lastweek")
        assert result == "2026-07-26T00:00:00Z,2026-08-02T00:00:00Z"

    def test_month_resolved_to_rfc3339(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = _common.to_api_window("month")
        # month = 2026-08-01 to 2026-08-07 (end = tomorrow = 2026-08-07)
        assert result == "2026-08-01T00:00:00Z,2026-08-07T00:00:00Z"

    def test_week_resolved_to_rfc3339(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = _common.to_api_window("week")
        # Kubecost weeks start Sunday; 2026-08-06 is Thursday so week opened 2026-08-02
        assert result == "2026-08-02T00:00:00Z,2026-08-07T00:00:00Z"

    def test_7d_passthrough(self):
        assert _common.to_api_window("7d") == "7d"

    def test_30d_passthrough(self):
        assert _common.to_api_window("30d") == "30d"

    def test_today_passthrough(self):
        assert _common.to_api_window("today") == "today"

    def test_explicit_rfc3339_range_passthrough(self):
        expr = "2026-07-01T00:00:00Z,2026-08-01T00:00:00Z"
        assert _common.to_api_window(expr) == expr

    def test_reversed_rfc3339_range_normalized(self):
        result = _common.to_api_window("2026-06-01T00:00:00Z,2026-05-01T00:00:00Z")
        assert result == "2026-05-01T00:00:00Z,2026-06-01T00:00:00Z"

    def test_reversed_offset_range_normalized_by_instant(self):
        result = _common.to_api_window("2026-05-01T02:00:00+01:00,2026-05-01T00:00:00Z")
        assert result == "2026-05-01T00:00:00Z,2026-05-01T02:00:00+01:00"

    @pytest.mark.parametrize(
        "expr",
        [
            "not-a-window",
            "2026-06-01T00:00:00Z,not-a-date",
            "2026-06-01T00:00:00Z,2026-06-01T00:00:00Z",
        ],
    )
    def test_non_reorderable_values_pass_through(self, expr):
        assert _common.to_api_window(expr) == expr

    def test_mixed_case_lastmonth_normalized(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 8, 6, tzinfo=UTC))
        result = _common.to_api_window("LastMonth")
        assert result == "2026-07-01T00:00:00Z,2026-08-01T00:00:00Z"


# ---------------------------------------------------------------------------
# summarize_tool_response
# ---------------------------------------------------------------------------


class TestSummarizeToolResponse:
    def test_message_only(self):
        summary = summarize_tool_response({"status": "ok", "message": "Found 8 categories."})
        assert summary.startswith("Found 8 categories.")
        assert "structuredContent" in summary

    def test_includes_recommended_action(self):
        summary = summarize_tool_response(
            {
                "status": "ok",
                "message": "Found 8 categories.",
                "recommended_action": "Drill into the highest-savings category first.",
            }
        )
        assert "Next: Drill into the highest-savings category first." in summary

    def test_includes_warnings_and_notes(self):
        summary = summarize_tool_response(
            {
                "status": "partial",
                "message": "Two of three clusters returned data.",
                "warnings": ["cluster-c timed out.", ""],
                "notes": ["Idle costs excluded."],
            }
        )
        assert "Warnings: cluster-c timed out." in summary
        assert "Notes: Idle costs excluded." in summary

    def test_truncation_hint_without_offset(self):
        summary = summarize_tool_response({"status": "ok", "message": "87 rows.", "truncated": True})
        assert "Results are truncated." in summary
        assert "offset=" not in summary

    def test_truncation_hint_with_next_offset(self):
        summary = summarize_tool_response({"status": "ok", "message": "87 rows.", "truncated": True, "next_offset": 50})
        assert "Pass offset=50 to fetch the next page." in summary

    def test_not_truncated_omits_hint(self):
        summary = summarize_tool_response({"status": "ok", "message": "87 rows.", "truncated": False})
        assert "truncated" not in summary

    def test_blank_message_falls_back_to_status(self):
        summary = summarize_tool_response({"status": "empty", "message": ""})
        assert summary.startswith("Query status: empty.")

    def test_missing_keys_do_not_raise(self):
        assert summarize_tool_response({}).startswith("Query status: unknown.")

    def test_oversized_summary_is_capped(self):
        summary = summarize_tool_response({"status": "ok", "message": "x" * 5000})
        assert len(summary) == _common._MAX_SUMMARY_CHARS
        assert summary.endswith("...")

    def test_non_list_warnings_ignored(self):
        summary = summarize_tool_response({"status": "ok", "message": "ok.", "warnings": "boom"})
        assert "Warnings" not in summary
