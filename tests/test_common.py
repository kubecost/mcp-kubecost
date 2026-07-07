"""Tests for tools/_common.py utilities."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError as McpToolError

from mcp_kubecost.client import KubecostClientError
from mcp_kubecost.errors import ErrorCode, ToolError
from mcp_kubecost.tools._common import extract_list, format_tool_error, safe_path_segment, summarize_exception

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
        exc = KubecostClientError(status_code=401, message="unauth", url="http://x")
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
