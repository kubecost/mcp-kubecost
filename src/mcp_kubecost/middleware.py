"""Server middleware owned by mcp-kubecost."""

from __future__ import annotations

import asyncio

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams, TextContent

from mcp_kubecost.tools._common import summarize_tool_response


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


class TextContentSummaryMiddleware(Middleware):
    """Replace a tool result's duplicated JSON text block with a short summary.

    Tools return Pydantic models, so FastMCP emits the same payload twice: once
    serialized into ``content[0].text`` and once as ``structuredContent``. This
    keeps the structured half intact and reduces the text half to a summary
    built from the response envelope (see ``summarize_tool_response``).

    ``legacy_text_content=True`` restores the full-JSON text block for clients
    that ignore ``structuredContent``.
    """

    def __init__(self, legacy_text_content: bool) -> None:
        self.legacy_text_content = legacy_text_content

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        if self.legacy_text_content or result.is_error or not result.structured_content:
            return result
        # Mutate rather than rebuild: ToolResult.__init__ re-runs
        # to_jsonable_python over structured_content, which is wasted work on an
        # already-serialized dict and costs more the larger the payload.
        result.content = [TextContent(type="text", text=summarize_tool_response(result.structured_content))]
        return result
