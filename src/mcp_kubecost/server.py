"""Main MCP server entry point."""

from __future__ import annotations

import json
import logging
import os
import sys
from argparse import ArgumentParser
from importlib.metadata import version as pkg_version

# Disable FastMCP banner before importing FastMCP
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_kubecost.branding import FAVICON_MEDIA_TYPE, FAVICON_SVG, KUBECOST_WEBSITE_URL, server_icons
from mcp_kubecost.client import kubecost_client_lifespan
from mcp_kubecost.config.oidc import create_oidc_provider
from mcp_kubecost.config.settings import apply_http_rich_logging, get_settings
from mcp_kubecost.errors import ConfigError
from mcp_kubecost.middleware import ToolConcurrencyLimitMiddleware
from mcp_kubecost.skills import register_all_skills
from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools

_SERVER_INSTRUCTIONS = (
    "Read-only Kubecost MCP server for cost visibility and savings recommendations. "
    "Start here: get_savings_overview for a ranked summary of all savings categories "
    "on any general savings question; kubecost_list_windows to discover valid time "
    "windows; get_kubecost_workload_costs for cost allocation by cluster, namespace, "
    "or controller; get_kubecost_cost_comparison as the entry point for 'why did costs "
    "change' or spike-investigation questions, diffing two periods. "
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

    # Build OIDC auth provider (None when OIDC is not enabled)
    auth = create_oidc_provider()
    settings = get_settings()

    # Create the MCP server instance
    mcp = FastMCP(
        name=server_name,
        version=version,
        instructions=_SERVER_INSTRUCTIONS,
        on_duplicate="error",
        strict_input_validation=True,
        auth=auth,
        lifespan=kubecost_client_lifespan,
        # FastMCP reads these off the server instance when it renders the OAuth
        # consent screen: the icon becomes the page logo and the name becomes a
        # link to the website. See mcp_kubecost.branding for the rest of the theme.
        icons=server_icons(),
        website_url=KUBECOST_WEBSITE_URL,
    )
    mcp.add_middleware(
        RateLimitingMiddleware(
            max_requests_per_second=settings.rate_limit_requests_per_second,
            burst_capacity=settings.rate_limit_burst_capacity,
            global_limit=True,
        )
    )
    mcp.add_middleware(ToolConcurrencyLimitMiddleware(settings.max_concurrent_tool_calls))

    # Register all toolsets
    register_kubecost_tools(mcp)

    # Register skills (MCP prompts — IDE-agnostic workflow guidance)
    register_all_skills(mcp)

    return mcp


# When using the fastmcp cli, all project wide initialization must be outside the main() function.
load_dotenv(".env")  # reads variables from a .env file and sets them in os.environ
apply_http_rich_logging()  # HTTP: fastmcp.settings.enable_rich_logging = False
import mcp_kubecost.logging_fastmcp  # noqa: E402,F401 -- must follow load_dotenv so FASTMCP_LOG_LEVEL is set

logger = logging.getLogger(__name__)

settings = get_settings()
logging.getLogger("mcp_kubecost").setLevel(getattr(logging, settings.log_level, logging.INFO))
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("Effective settings:\n%s", json.dumps(settings.to_loggable_dict(), indent=2))
mcp_server_name = os.getenv("MCP_SERVER_NAME", "mcp-kubecost")
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
version = pkg_version(distribution_name="mcp-kubecost")

logger.info(f"Starting kubecost mcp version: {version}")
try:
    mcp = create_server(mcp_server_name)
except ConfigError as exc:
    logger.error("%s", exc)
    sys.exit(1)


@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(_request: Request) -> JSONResponse:
    """Unauthenticated process-up check for Kubernetes probes. Does not call Kubecost.

    Uvicorn access logs for this path are dropped — probe traffic is too noisy.
    """
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/version", methods=["GET"])
async def version_endpoint(_request: Request) -> JSONResponse:
    try:
        v = pkg_version("mcp-kubecost")
    except Exception:
        v = "unknown"
    return JSONResponse({"version": v})


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon_endpoint(_request: Request) -> Response:
    """Serve the Kubecost mark so browsers stop 404ing on the implicit favicon.

    The branded OAuth pages declare the icon inline and never request this, but
    FastMCP also returns a few bare HTML fragments with no ``<head>`` to inject
    into, and a browser pointed at ``/mcp`` or any 404 asks for ``/favicon.ico``
    too. Unauthenticated, like ``/health`` and ``/version`` — it is a logo, and
    the request arrives before any OAuth flow completes.

    Content type is SVG despite the ``.ico`` path; browsers honour the header
    rather than the extension. Not an MCP concern — clients read ``serverInfo.icons``.
    """
    return Response(
        content=FAVICON_SVG,
        media_type=FAVICON_MEDIA_TYPE,
        # Immutable content, so let browsers stop asking. Public: no auth, no secrets.
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
