"""Tests for unauthenticated HTTP custom routes used by Kubernetes probes."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_kubecost.branding import FAVICON_PNG
from mcp_kubecost.logging_fastmcp import HealthProbeLogFilter
from mcp_kubecost.middleware import ToolConcurrencyLimitMiddleware
from mcp_kubecost.server import KubecostMCP, favicon_endpoint, health_endpoint, mcp, version_endpoint


def _custom_route_paths() -> set[str]:
    return {route.path for route in mcp._get_additional_http_routes() if isinstance(route, Route)}


def _json_body(response: Response) -> dict:
    return json.loads(bytes(response.body))


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:1", "GET", path, "1.1", 200),
        exc_info=None,
    )


class TestHealthRoute:
    def test_registered(self):
        assert "/health" in _custom_route_paths()

    async def test_returns_ok(self):
        response = await health_endpoint(MagicMock(spec=Request))
        assert response.status_code == 200
        assert _json_body(response) == {"status": "ok"}

    def test_access_log_filter_is_installed(self):
        access = logging.getLogger("uvicorn.access")
        assert any(isinstance(f, HealthProbeLogFilter) for f in access.filters)

    def test_access_log_filter_drops_health(self):
        filt = HealthProbeLogFilter()
        assert filt.filter(_access_record("/health")) is False
        assert filt.filter(_access_record("/health?ready=1")) is False

    def test_access_log_filter_keeps_other_paths(self):
        filt = HealthProbeLogFilter()
        assert filt.filter(_access_record("/mcp")) is True
        assert filt.filter(_access_record("/version")) is True


class TestVersionRoute:
    def test_registered(self):
        assert "/version" in _custom_route_paths()

    async def test_returns_package_version(self):
        response = await version_endpoint(MagicMock(spec=Request))
        assert response.status_code == 200
        payload = _json_body(response)
        assert "version" in payload
        assert payload["version"]


class TestFaviconRoute:
    """Browsers request /favicon.ico for any HTML page that declares no icon.

    The branded OAuth pages declare one inline so they never ask, but FastMCP also
    returns bare HTML fragments with no <head>, and a browser pointed at /mcp or a
    404 asks too. Serving it keeps those out of the access log as 404s.
    """

    def test_registered(self):
        assert "/favicon.ico" in _custom_route_paths()

    async def test_serves_the_kubecost_mark_as_png(self):
        response = await favicon_endpoint(MagicMock(spec=Request))
        assert response.status_code == 200
        assert response.media_type == "image/png"
        body = bytes(response.body)
        assert body[:4] == b"\x89PNG"
        assert body == FAVICON_PNG

    async def test_is_cacheable(self):
        response = await favicon_endpoint(MagicMock(spec=Request))
        assert "max-age" in response.headers["cache-control"]


class TestRuntimeProtection:
    def test_rate_and_concurrency_middleware_are_registered(self):
        rate = next(item for item in mcp.middleware if isinstance(item, RateLimitingMiddleware))
        concurrency = next(item for item in mcp.middleware if isinstance(item, ToolConcurrencyLimitMiddleware))

        assert rate.max_requests_per_second == 10.0
        assert rate.burst_capacity == 20
        assert rate.global_limit is True
        assert concurrency.max_concurrent == 10


class TestProbesStayUnauthenticatedWithAuthEnabled:
    """Drive the real ASGI stack (auth middleware included) instead of calling
    handlers directly, so this actually exercises the "probes bypass OIDC" claim.
    """

    def _authed_app(self):
        app = FastMCP("probe-auth-test", auth=StaticTokenVerifier(tokens={"valid-token": {"client_id": "c"}}))

        @app.custom_route("/health", methods=["GET"])
        async def health(_request: Request) -> Response:
            return await health_endpoint(_request)

        @app.custom_route("/version", methods=["GET"])
        async def version(_request: Request) -> Response:
            return await version_endpoint(_request)

        @app.custom_route("/favicon.ico", methods=["GET"])
        async def favicon(_request: Request) -> Response:
            return await favicon_endpoint(_request)

        return app

    async def test_health_returns_200_without_credentials(self):
        transport = httpx.ASGITransport(app=self._authed_app().http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_version_returns_200_without_credentials(self):
        transport = httpx.ASGITransport(app=self._authed_app().http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/version")
        assert response.status_code == 200
        assert "version" in response.json()

    async def test_favicon_returns_200_without_credentials(self):
        """A browser hits this mid-OAuth, before any token exists."""
        transport = httpx.ASGITransport(app=self._authed_app().http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")

    async def test_mcp_endpoint_rejects_missing_credentials(self):
        transport = httpx.ASGITransport(app=self._authed_app().http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/mcp", json={})
        assert response.status_code == 401


class TestBrowserCors:
    """A browser MCP client preflights /mcp before it may send Authorization.

    The Streamable HTTP transport answers only GET/POST/DELETE, so without the
    app-wide CORS middleware that preflight is a bare 405 and the client never
    sends the real request.
    """

    def _app(self):
        server = KubecostMCP("cors-test", auth=StaticTokenVerifier(tokens={"valid-token": {"client_id": "c"}}))
        return server.http_app()

    async def test_preflight_allows_the_headers_an_mcp_client_sends(self):
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/mcp",
                headers={
                    "Origin": "http://127.0.0.1:6274",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id",
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        allowed = {h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")}
        assert {"authorization", "mcp-session-id", "mcp-protocol-version"} <= allowed

    async def test_401_exposes_the_discovery_hint_to_the_browser(self):
        """`resource_metadata` on the 401 is where OAuth discovery starts; a
        cross-origin caller cannot read that header unless it is exposed."""
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/mcp", json={}, headers={"Origin": "http://127.0.0.1:6274"})

        assert response.status_code == 401
        assert response.headers["access-control-allow-origin"] == "*"
        exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
        assert {"www-authenticate", "mcp-session-id"} <= exposed
