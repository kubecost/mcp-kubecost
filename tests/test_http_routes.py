"""Tests for unauthenticated HTTP custom routes used by Kubernetes probes."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_kubecost.logging_fastmcp import HealthProbeLogFilter
from mcp_kubecost.server import health_endpoint, mcp, version_endpoint


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
