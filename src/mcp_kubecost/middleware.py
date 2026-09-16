"""Server middleware owned by mcp-kubecost."""

from __future__ import annotations

import asyncio

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams


class ToolConcurrencyLimitMiddleware(Middleware):
    """Bound simultaneous and total-duration tool calls within this server process."""

    def __init__(self, max_concurrent: int, timeout_seconds: float) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be greater than 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._semaphore:
                    return await call_next(context)
        except TimeoutError as exc:
            raise ToolError(
                f"Tool call exceeded the {self.timeout_seconds:g}-second deadline. Retry a narrower query."
            ) from exc
