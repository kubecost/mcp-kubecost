"""Server middleware owned by mcp-kubecost."""

from __future__ import annotations

import asyncio

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams


class ToolConcurrencyLimitMiddleware(Middleware):
    """Bound simultaneous tool executions within this server process."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be greater than 0")
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        async with self._semaphore:
            return await call_next(context)
