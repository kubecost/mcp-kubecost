"""Tests for TOON serialization middleware."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from unittest.mock import AsyncMock

import py_toon_format
import pytest
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent

from mcp_kubecost.middleware import ToonMiddleware
from mcp_kubecost.server import create_server


def _make_context():
    """Create a minimal mock MiddlewareContext for on_call_tool."""
    return AsyncMock()


def _make_call_next(result: ToolResult):
    """Create a call_next coroutine that returns the given ToolResult."""

    async def call_next(context):
        return result

    return call_next


@pytest.fixture
def toon() -> Callable[[ToolResult], Coroutine[None, None, ToolResult]]:
    """Run a ToolResult through the ToonMiddleware and return the output."""
    middleware = ToonMiddleware()

    async def run(result: ToolResult) -> ToolResult:
        return await middleware.on_call_tool(_make_context(), _make_call_next(result))

    return run


def test_server_registers_toon_middleware():
    """The configured MCP server includes TOON result serialization."""
    server = create_server("test-toon-middleware")

    assert sum(isinstance(middleware, ToonMiddleware) for middleware in server.middleware) == 1


class TestToonMiddlewareJsonContent:
    """JSON text content is converted to TOON format."""

    async def test_simple_dict(self, toon):
        data = {"name": "test", "value": 42}
        result = ToolResult(content=[TextContent(type="text", text='{"name":"test","value":42}')])

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0].type == "text"
        assert output.content[0].text == py_toon_format.dumps(data)

    async def test_multiple_json_blocks(self, toon):
        block1 = TextContent(type="text", text='{"a": 1}')
        block2 = TextContent(type="text", text='{"b": 2}')
        result = ToolResult(content=[block1, block2])

        output = await toon(result)

        assert len(output.content) == 2
        assert output.content[0].text == py_toon_format.dumps({"a": 1})
        assert output.content[1].text == py_toon_format.dumps({"b": 2})


class TestToonMiddlewareStructuredContent:
    """structured_content is converted to TOON and placed in content."""

    async def test_structured_content_converted(self, toon):
        data = {"status": "ok", "policies": [{"id": "p1", "name": "Budget"}]}
        result = ToolResult(structured_content=data)

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0].type == "text"
        assert output.content[0].text == py_toon_format.dumps(data)

    async def test_structured_content_takes_priority(self, toon):
        """When structured_content is present, content blocks are replaced."""
        data = {"key": "value"}
        result = ToolResult(
            structured_content=data,
            content=[TextContent(type="text", text='{"other": "ignored"}')],
        )

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0].text == py_toon_format.dumps(data)

    async def test_lossy_structured_content_falls_back_to_json(self, toon):
        """Multiline fields and empty mappings are preserved rather than corrupted."""
        data = {
            "interpretation": "Review the top opportunity.\nDo not reduce memory requests.",
            "summary": {},
        }
        result = ToolResult(structured_content=data)

        output = await toon(result)

        assert json.loads(output.content[0].text) == data
        assert output.content[0].text != py_toon_format.dumps(data)


class TestToonMiddlewarePassthrough:
    """Non-JSON content and errors pass through unchanged."""

    async def test_non_json_text_passes_through(self, toon):
        plain_text = "This is a plain text response, not JSON"
        result = ToolResult(content=[TextContent(type="text", text=plain_text)])

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0].text == plain_text

    async def test_invalid_json_passes_through(self, toon):
        broken_json = '{"missing_closing_brace": true'
        result = ToolResult(content=[TextContent(type="text", text=broken_json)])

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0].text == broken_json

    async def test_lossy_json_content_falls_back_to_json(self, toon):
        data = {"interpretation": "Review the top opportunity.\nDo not reduce memory requests."}
        result = ToolResult(content=[TextContent(type="text", text=json.dumps(data))])

        output = await toon(result)

        assert json.loads(output.content[0].text) == data
        assert output.content[0].text != py_toon_format.dumps(data)

    async def test_error_result_passes_through(self, toon):
        error_text = '{"code": "auth_failed", "message": "Invalid API key"}'
        result = ToolResult(
            content=[TextContent(type="text", text=error_text)],
            is_error=True,
        )

        output = await toon(result)

        assert output.is_error is True
        assert output.content[0].text == error_text

    async def test_non_text_content_passes_through(self, toon):
        image_block = ImageContent(type="image", data="base64data", mimeType="image/png")
        result = ToolResult(content=[image_block])

        output = await toon(result)

        assert len(output.content) == 1
        assert output.content[0] == image_block

    async def test_empty_content_list(self, toon):
        result = ToolResult(content=[])

        output = await toon(result)

        assert output.content == []
