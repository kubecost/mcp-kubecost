"""Call every tool and prompt exposed by an MCP configuration."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.mcp_config import MCPConfig

CONFIG_DEFAULT = "./.bob/mcp.json"


def as_jsonable(value: Any) -> Any:
    """Convert an MCP/Pydantic result into values accepted by json.dumps."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=False, exclude_none=False)
    return value


def error_result(kind: str, name: str, error: Exception) -> dict[str, Any]:
    return {
        "status": "error",
        "message": f"{kind} call failed: {error}",
        "name": name,
    }


def print_result(kind: str, name: str, result: Any) -> None:
    print(f"===== {kind}: {name} =====")
    print(json.dumps(as_jsonable(result), ensure_ascii=False, indent=2))


def cluster_from_result(result: Any) -> str:
    """Extract the first cluster ID from a workload-cost tool result."""
    payload = as_jsonable(result)
    if not isinstance(payload, dict):
        return ""

    structured = payload.get("structured_content") or payload.get("structuredContent") or payload
    if not isinstance(structured, dict):
        return ""

    rows = structured.get("rows", [])
    if not isinstance(rows, list):
        return ""

    for row in rows:
        if isinstance(row, dict) and row.get("cluster"):
            return str(row["cluster"])
    return ""


def tool_arguments(name: str, cluster: str) -> dict[str, Any]:
    if name == "get_cluster_rightsizing_recommendations":
        return {"cluster": cluster}
    return {}


def prompt_arguments(name: str) -> dict[str, str]:
    if name == "cost_trend":
        return {"window": "15d", "aggregate": "namespace"}
    return {}


async def call_all(config_path: Path, cluster: str) -> None:
    config = MCPConfig.from_file(config_path)
    client = Client(config, timeout=120, init_timeout=30)

    try:
        async with client:
            tools = await client.list_tools()
            prompts = await client.list_prompts()

            workload_tool = next(
                (tool for tool in tools if tool.name == "get_kubecost_workload_costs"),
                None,
            )
            if workload_tool is not None:
                workload_args = {"aggregate": "cluster", "top_n": 100, "min_total_cost": 0}
                try:
                    workload_result = await client.call_tool_mcp(workload_tool.name, workload_args, timeout=120)
                    print_result("TOOL", workload_tool.name, workload_result)
                    if not cluster:
                        cluster = cluster_from_result(workload_result)
                except Exception as error:
                    print_result("TOOL", workload_tool.name, error_result("Tool", workload_tool.name, error))

            for tool in tools:
                if workload_tool is not None and tool.name == workload_tool.name:
                    continue

                try:
                    result = await client.call_tool_mcp(tool.name, tool_arguments(tool.name, cluster), timeout=120)
                    print_result("TOOL", tool.name, result)
                except Exception as error:
                    print_result("TOOL", tool.name, error_result("Tool", tool.name, error))

            for prompt in prompts:
                try:
                    result = await client.get_prompt(prompt.name, prompt_arguments(prompt.name))
                    print_result("PROMPT", prompt.name, result)
                except Exception as error:
                    print_result("PROMPT", prompt.name, error_result("Prompt", prompt.name, error))
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(CONFIG_DEFAULT))
    parser.add_argument(
        "--cluster",
        default="",
        help="Cluster ID for node-group rightsizing; otherwise discover the first cluster.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(call_all(args.config, args.cluster))
    except Exception as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
