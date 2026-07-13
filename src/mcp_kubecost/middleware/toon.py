"""TOON serialization middleware for FastMCP.

Intercepts tool results and converts JSON text content to TOON
(Token-Oriented Object Notation) for 30-60% token savings in LLM responses.

The middleware is transparent:
- Tools keep returning Pydantic models (schema preserved via output_schema)
- FastMCP serializes them to JSON as usual
- This middleware rewrites the JSON text blocks to TOON format
- It falls back to JSON if TOON cannot round-trip the result losslessly
- Errors and non-JSON content pass through unchanged
"""

from __future__ import annotations

import json
import logging

import mcp.types as mt
import py_toon_format
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

logger = logging.getLogger(__name__)


def _serialize_losslessly(data: object) -> str:
    """Return TOON only when decoding it reproduces the JSON representation."""
    json_text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    try:
        toon_text = py_toon_format.dumps(data)
        decoded_json_text = json.dumps(
            py_toon_format.loads(toon_text),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        logger.debug("TOON serialization failed; returning JSON", exc_info=True)
        return json_text

    if decoded_json_text != json_text:
        logger.debug("TOON serialization was not lossless; returning JSON")
        return json_text

    return toon_text


class ToonMiddleware(Middleware):
    """Convert JSON tool output to TOON format for token efficiency.

    Register once in server.py — applies to all tools automatically:

        mcp.add_middleware(ToonMiddleware())
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result: ToolResult = await call_next(context)

        if result.is_error:
            return result

        if result.structured_content is not None:
            result.content = [TextContent(type="text", text=_serialize_losslessly(result.structured_content))]
            return result

        new_content: list[mt.Content] = []
        for block in result.content:
            if isinstance(block, TextContent):
                try:
                    data = json.loads(block.text)
                    new_content.append(TextContent(type="text", text=_serialize_losslessly(data)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Not JSON or can't encode — pass through unchanged
                    new_content.append(block)
            else:
                new_content.append(block)

        result.content = new_content
        return result
