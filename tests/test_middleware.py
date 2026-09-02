"""Tests for project-owned FastMCP middleware."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams

from mcp_kubecost.middleware import TextContentSummaryMiddleware, ToolConcurrencyLimitMiddleware


async def test_tool_concurrency_limit_queues_excess_calls():
    middleware = ToolConcurrencyLimitMiddleware(max_concurrent=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    call_count = 0

    async def call_next(context: MiddlewareContext[CallToolRequestParams]) -> ToolResult:
        nonlocal call_count
        del context
        call_count += 1
        if call_count == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return MagicMock(spec=ToolResult)

    first = asyncio.create_task(middleware.on_call_tool(MagicMock(), call_next))
    await first_started.wait()
    second = asyncio.create_task(middleware.on_call_tool(MagicMock(), call_next))
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    await asyncio.gather(first, second)
    assert second_started.is_set()


def test_tool_concurrency_limit_rejects_non_positive_max():
    with pytest.raises(ValueError, match="max_concurrent must be greater than 0"):
        ToolConcurrencyLimitMiddleware(max_concurrent=0)

    with pytest.raises(ValueError, match="max_concurrent must be greater than 0"):
        ToolConcurrencyLimitMiddleware(max_concurrent=-1)


# ---------------------------------------------------------------------------
# TextContentSummaryMiddleware
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "status": "ok",
    "message": "Found 8 savings categories totalling $25,997.73/month.",
    "recommended_action": "Drill into the highest-savings category first.",
    "categories": [{"key": "nodeGroupSizing", "savings_per_month": 11020.35}],
}


def _call_next_returning(result: ToolResult):
    async def call_next(context):
        del context
        return result

    return call_next


async def _run(middleware: TextContentSummaryMiddleware, result: ToolResult) -> ToolResult:
    return await middleware.on_call_tool(MagicMock(), _call_next_returning(result))


async def test_summary_replaces_text_and_keeps_structured_content():
    result = ToolResult(structured_content=_PAYLOAD)
    # FastMCP's default: the whole payload serialized into the text block too.
    assert "nodeGroupSizing" in result.content[0].text

    out = await _run(TextContentSummaryMiddleware(legacy_text_content=False), result)

    assert len(out.content) == 1
    text = out.content[0].text
    assert text.startswith("Found 8 savings categories totalling $25,997.73/month.")
    assert "Next: Drill into the highest-savings category first." in text
    assert "nodeGroupSizing" not in text
    assert out.structured_content == _PAYLOAD


async def test_legacy_flag_leaves_full_json_text():
    result = ToolResult(structured_content=_PAYLOAD)
    original = result.content[0].text

    out = await _run(TextContentSummaryMiddleware(legacy_text_content=True), result)

    assert out.content[0].text == original
    assert "nodeGroupSizing" in out.content[0].text


async def test_error_result_keeps_its_text():
    result = ToolResult(content="[UPSTREAM_ERROR] Kubecost returned 503", structured_content=_PAYLOAD, is_error=True)

    out = await _run(TextContentSummaryMiddleware(legacy_text_content=False), result)

    assert out.content[0].text == "[UPSTREAM_ERROR] Kubecost returned 503"


async def test_result_without_structured_content_passes_through():
    result = ToolResult(content="plain text result")

    out = await _run(TextContentSummaryMiddleware(legacy_text_content=False), result)

    assert out.content[0].text == "plain text result"
    assert out.structured_content is None
