"""MCP surface contract tests — ensure tools, prompts, and resources stay registered."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from tests.fixtures import EXPECTED_PROMPTS, EXPECTED_RESOURCES, EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_mcp_tools_registered(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        tool_names = {tool.name for tool in await client.list_tools()}

    assert tool_names == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_mcp_prompts_registered(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        prompt_names = {prompt.name for prompt in await client.list_prompts()}

    assert prompt_names == EXPECTED_PROMPTS


@pytest.mark.asyncio
async def test_mcp_resources_registered(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        resource_uris = {str(resource.uri) for resource in await client.list_resources()}

    assert resource_uris == EXPECTED_RESOURCES


@pytest.mark.asyncio
async def test_tool_schemas_have_descriptions(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        tools = await client.list_tools()

    for tool in tools:
        assert tool.description, f"Tool {tool.name} is missing a description"
        assert tool.inputSchema is not None, f"Tool {tool.name} is missing inputSchema"


@pytest.mark.asyncio
async def test_resources_are_readable(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        for uri in sorted(EXPECTED_RESOURCES):
            contents = await client.read_resource(uri)
            assert contents, f"Resource {uri} returned empty content"
