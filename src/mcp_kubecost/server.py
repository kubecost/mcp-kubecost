"""Main MCP server entry point."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from argparse import ArgumentParser
from collections.abc import Callable
from importlib.metadata import version as pkg_version

# Disable FastMCP banner before importing FastMCP
os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from mcp.server.streamable_http import MCP_PROTOCOL_VERSION_HEADER, MCP_SESSION_ID_HEADER
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_kubecost.branding import FAVICON_MEDIA_TYPE, FAVICON_PNG, KUBECOST_WEBSITE_URL, server_icons
from mcp_kubecost.client import kubecost_client_lifespan
from mcp_kubecost.config.oidc import create_oidc_provider
from mcp_kubecost.config.settings import AuthMode, apply_http_rich_logging, get_settings
from mcp_kubecost.errors import ConfigError
from mcp_kubecost.middleware import TextContentSummaryMiddleware, ToolConcurrencyLimitMiddleware
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


# Browser clients (MCP Inspector in direct mode, any in-page MCP client) send
# `Authorization` and `mcp-session-id` on /mcp, which makes every request a
# preflighted one. The Streamable HTTP transport answers only GET/POST/DELETE,
# so an unhandled OPTIONS returns 405 with no CORS headers and the client never
# gets to send the real request. The MCP SDK wraps the OAuth routes in its own
# CORS middleware but allows only `mcp-protocol-version` there, so token
# requests using HTTP Basic client auth are blocked the same way.
#
# Credentials stay off: MCP clients authenticate with a bearer token in the
# header, never a cookie, so `*` origins carry no ambient-authority risk.
# `www-authenticate` is exposed because that is where the client reads the
# `resource_metadata` URL that starts OAuth discovery.
_BROWSER_CORS = Middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Last-Event-ID",
        MCP_SESSION_ID_HEADER,
        MCP_PROTOCOL_VERSION_HEADER,
    ],
    expose_headers=[MCP_SESSION_ID_HEADER, "WWW-Authenticate"],
    max_age=600,
)


# ---------------------------------------------------------------------------
# DCR rate-limiting ASGI middleware
# ---------------------------------------------------------------------------
# The FastMCP RateLimitingMiddleware added in create_server() is MCP-message
# middleware and never sees plain HTTP OAuth routes such as
# POST /oauth/mcp/register. Registration is unauthenticated and writes one
# encrypted file per unique metadata set to OIDC_STORAGE_PATH, so without a
# limit a single caller can fill the storage volume.
#
# Two token buckets apply: one per client IP and one per pod. The per-pod
# bucket is the real disk-fill guard, since a spoofed or rotating source IP
# defeats the per-IP one. The defaults are deliberately generous. After a
# restart with ephemeral keys every client re-registers at once, and the
# derived client ID makes a repeated identical registration idempotent, so the
# cost of a legitimate burst is low.
#
# The per-IP key is scope["client"], which uvicorn rewrites from
# X-Forwarded-For only for peers listed in FORWARDED_ALLOW_IPS. The chart
# disables this trust by default; operators can opt in with known proxy IPs
# or CIDRs. Do not parse the header here: an untrusted header must never
# choose the bucket. Clients behind an untrusted proxy share its IP budget.
#
# None of this is configurable through Helm. Registration is a one-time event
# per client and these ceilings are far above any legitimate rate.
_DCR_PER_IP_PER_MINUTE = 30
_DCR_PER_IP_BURST = 15
_DCR_GLOBAL_PER_MINUTE = 120
_DCR_GLOBAL_BURST = 60
_DCR_MAX_TRACKED_IPS = 1000
_DCR_REGISTER_PATH = "/oauth/mcp/register"


class _TokenBucket:
    """Token bucket with fractional refill. ``tokens`` starts full."""

    __slots__ = ("capacity", "last", "rate", "tokens")

    def __init__(self, *, per_minute: float, burst: int, now: float) -> None:
        self.rate = per_minute / 60.0
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last = now

    def refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def consume(self) -> None:
        self.tokens -= 1.0


class _DcrRateLimiter:
    """Per-IP plus per-pod token buckets for POST /oauth/mcp/register."""

    def __init__(
        self,
        *,
        per_ip_per_minute: float = _DCR_PER_IP_PER_MINUTE,
        per_ip_burst: int = _DCR_PER_IP_BURST,
        global_per_minute: float = _DCR_GLOBAL_PER_MINUTE,
        global_burst: int = _DCR_GLOBAL_BURST,
        max_tracked_ips: int = _DCR_MAX_TRACKED_IPS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_ip_per_minute = per_ip_per_minute
        self._per_ip_burst = per_ip_burst
        self._max_tracked_ips = max_tracked_ips
        self._clock = clock
        self._global = _TokenBucket(per_minute=global_per_minute, burst=global_burst, now=clock())
        self._buckets: dict[str, _TokenBucket] = {}

    @property
    def tracked_ips(self) -> int:
        return len(self._buckets)

    def allow(self, client_ip: str) -> bool:
        """Return True and consume one token from both buckets, or False and consume none."""
        now = self._clock()
        self._global.refill(now)
        if self._global.tokens < 1.0:
            return False
        bucket = self._buckets.get(client_ip)
        if bucket is None:
            self._evict_idle(now)
            if len(self._buckets) >= self._max_tracked_ips:
                return False
            bucket = self._buckets[client_ip] = _TokenBucket(
                per_minute=self._per_ip_per_minute, burst=self._per_ip_burst, now=now
            )
        bucket.refill(now)
        if bucket.tokens < 1.0:
            return False
        bucket.consume()
        self._global.consume()
        return True

    def _evict_idle(self, now: float) -> None:
        """Drop idle buckets when the map reaches its hard cap.

        A bucket idle for ``capacity / rate`` seconds is indistinguishable from a
        fresh one, so dropping it loses no state. If none can be evicted,
        new IPs are rejected until space becomes available; active IPs keep
        their budgets. The scan visits at most ``max_tracked_ips`` entries.
        """
        if len(self._buckets) < self._max_tracked_ips:
            return
        horizon = self._per_ip_burst / (self._per_ip_per_minute / 60.0)
        stale = [ip for ip, b in self._buckets.items() if now - b.last >= horizon]
        for ip in stale:
            del self._buckets[ip]


_dcr_limiter = _DcrRateLimiter()

_DCR_429_BODY = json.dumps(
    {
        "error": "rate_limited",
        "error_description": "Too many client registration requests; retry after 60 seconds",
    }
).encode()


class _DcrRateLimitMiddleware:
    """ASGI middleware that rate-limits POST /oauth/mcp/register per client IP and per pod."""

    def __init__(self, app, limiter: _DcrRateLimiter | None = None) -> None:
        self._app = app
        self._limiter = limiter or _dcr_limiter

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "POST" and scope.get("path") == _DCR_REGISTER_PATH:
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            if not self._limiter.allow(ip):
                logger.warning("Rate-limited POST %s from %s", _DCR_REGISTER_PATH, ip)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", b"60"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": _DCR_429_BODY, "more_body": False})
                return
        await self._app(scope, receive, send)


class KubecostMCP(FastMCP):
    """FastMCP that applies ``_BROWSER_CORS`` to the whole HTTP app.

    Overriding ``http_app`` rather than passing ``middleware=`` at startup is
    what makes this reach the deployed server: the container runs ``fastmcp
    run`` (see ``otel_entrypoint``), which builds the app itself and offers no
    way to inject ASGI middleware.
    """

    def http_app(self, *args, middleware: list[Middleware] | None = None, **kwargs):
        extra: list[Middleware] = [_BROWSER_CORS]
        if get_settings().auth_mode == AuthMode.OIDC:
            extra.append(Middleware(_DcrRateLimitMiddleware))
        return super().http_app(*args, middleware=[*extra, *(middleware or [])], **kwargs)


def create_server(server_name) -> FastMCP:
    """Create and configure FastMCP with tools, prompts, and resources."""

    # Build OIDC auth provider (None when OIDC is not enabled)
    auth = create_oidc_provider()
    settings = get_settings()

    # Create the MCP server instance
    mcp = KubecostMCP(
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
    mcp.add_middleware(TextContentSummaryMiddleware(settings.legacy_text_content))

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

    Content type is PNG despite the ``.ico`` path; browsers honour the header
    rather than the extension. MCP clients do not use this route; they read ``serverInfo.icons``.
    """
    return Response(
        content=FAVICON_PNG,
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
