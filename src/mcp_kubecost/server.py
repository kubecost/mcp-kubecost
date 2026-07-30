"""Main MCP server entry point."""

from __future__ import annotations

import logging
import os
import sys
from argparse import ArgumentParser
from importlib.metadata import version as pkg_version

# Disable FastMCP banner before importing FastMCP
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.middleware import ToonMiddleware
from mcp_kubecost.middleware.toon import is_toon_enabled
from mcp_kubecost.skills import register_all_skills
from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools

_SERVER_INSTRUCTIONS = (
    "Read-only Kubecost MCP server for cost visibility and savings recommendations. "
    "Start here: get_savings_overview for a ranked summary of all savings categories "
    "on any general savings question; kubecost_list_windows to discover valid time "
    "windows; get_kubecost_workload_costs for cost allocation by cluster, namespace, "
    "or controller; get_kubecost_cost_comparison as the entry point for 'why did costs "
    "change' or spike-investigation questions, diffing two equal-length periods. "
    "Drill-down tools: get_container_savings_recommendations for container rightsizing, "
    "get_abandoned_workloads to identify idle workloads and estimate decommission savings, "
    "get_pv_sizing_recommendations for PVC storage right-sizing, "
    "get_local_disk_savings for underutilized node-local disks, "
    "get_cluster_rightsizing_recommendations for node group scale-in/out recommendations, "
    "get_unclaimed_volumes for unbound PersistentVolumes, "
    "and get_resource_quota_recommendations for namespace ResourceQuota governance."
)


def create_server(server_name) -> FastMCP:
    """Create and configure FastMCP with tools, prompts, and resources."""

    # Create the MCP server instance
    mcp = FastMCP(
        name=server_name,
        version=version,
        instructions=_SERVER_INSTRUCTIONS,
        on_duplicate="error",
        strict_input_validation=True,
    )

    # Register all toolsets
    register_kubecost_tools(mcp)

    # Register skills (MCP prompts — IDE-agnostic workflow guidance)
    register_all_skills(mcp)

    if is_toon_enabled():
        mcp.add_middleware(ToonMiddleware())
    return mcp


def _build_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


# When using the fastmcp cli, all project wide initialization must be outside the main() function.
load_dotenv(".env")  # reads variables from a .env file and sets them in os.environ
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("mcp_kubecost").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

settings = get_settings()
mcp_server_name = os.getenv("MCP_SERVER_NAME", "Kubecost_MCP")
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
version = pkg_version(distribution_name="mcp-kubecost")

logger.info(f"Starting kubecost mcp version: {version}")
mcp = create_server(mcp_server_name)


@mcp.custom_route("/version", methods=["GET"])
async def version_endpoint(_request: Request) -> JSONResponse:
    try:
        v = pkg_version("mcp-kubecost")
    except Exception:
        v = "unknown"
    return JSONResponse({"version": v})


def main() -> None:
    """Main entry point for the mcp-kubecost command."""
    # This is only used when not using fastmcp cli.

    parser: ArgumentParser = ArgumentParser()
    parser.add_argument("--version", action="version", version=f"%(prog)s {version}")
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.error("Unhandled exception in main", exc_info=True)
        sys.exit(1)
