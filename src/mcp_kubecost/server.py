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
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.skills import register_all_skills
from mcp_kubecost.tools.kubecost_tools import register_kubecost_csv_tools
from mcp_kubecost.utils import OUTPUT_DIR


def create_server(server_name) -> FastMCP:
    """Create and configure FastMCP with tools, prompts, and resources."""

    # Create the MCP server instance
    mcp = FastMCP(
        name=server_name,
        version=version,
        instructions=(
            "This is a Kubecost MCP server providing read-only access to cloud "
            "financial management data. Use the available tools to query cost reports, "
            "view rightsizing recommendations, check Reserved Instance utilization, "
            "inspect business mappings, review governance policies, detect anomalies, "
            "and manage views across your cloud accounts."
        ),
    )

    # Register all toolsets
    register_kubecost_csv_tools(mcp)

    # Register skills (MCP prompts — IDE-agnostic workflow guidance)
    register_all_skills(mcp)
    return mcp


def _build_headers(api_token: str | None) -> dict[str, str]:
    if not api_token:
        return {}
    return {"Authorization": f"Bearer {api_token}"}


# When using the fastmcp cli, all project wide initialization must be outside the main() function.
load_dotenv(".env")  # reads variables from a .env file and sets them in os.environ
logger = logging.getLogger(__name__)

settings = get_settings()
mcp_server_name = os.getenv("MCP_SERVER_NAME", "Kubecost MCP")
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
version = pkg_version(distribution_name="mcp-kubecost")

logger.info(f"Starting kubecost mcp version: {version}")
mcp = create_server(mcp_server_name)


@mcp.custom_route("/version", methods=["GET"])
async def version_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse({"version": os.environ.get("CLDY_MCP_LOCAL_VERSION", "unknown")})


@mcp.custom_route("/reports", methods=["GET"])
async def list_reports(_request: Request) -> JSONResponse:
    """List all available CSV reports."""
    files = sorted(
        OUTPUT_DIR.glob("*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return JSONResponse({"reports": [{"filename": f.name, "size_bytes": f.stat().st_size} for f in files]})


@mcp.custom_route("/reports/{filename}", methods=["GET"])
async def download_report(request: Request) -> FileResponse | PlainTextResponse:
    """Download a specific CSV report by filename."""
    filename = request.path_params["filename"]
    output_root = OUTPUT_DIR.resolve()
    path = (OUTPUT_DIR / filename).resolve()
    if not path.is_relative_to(output_root):
        return PlainTextResponse("Forbidden", status_code=403)
    if not path.is_file() or path.suffix != ".csv":
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path, media_type="text/csv", filename=filename)


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
