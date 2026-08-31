"""Tests for project-owned FastMCP middleware."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams

from mcp_kubecost.middleware import ToolConcurrencyLimitMiddleware


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
