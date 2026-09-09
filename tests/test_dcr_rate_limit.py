"""Tests for the POST /oauth/mcp/register rate limiter."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_kubecost.server import _DCR_REGISTER_PATH, _DcrRateLimiter, _DcrRateLimitMiddleware


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(**kwargs) -> tuple[_DcrRateLimiter, _Clock]:
    clock = _Clock()
    defaults = dict(global_per_minute=60, global_burst=3)
    return _DcrRateLimiter(**{**defaults, **kwargs}, clock=clock), clock


class TestDcrRateLimiter:
    def test_burst_and_refill(self):
        limiter, clock = _limiter()
        assert [limiter.allow() for _ in range(4)] == [True, True, True, False]
        clock.now += 0.5
        assert limiter.allow() is False
        clock.now += 0.5
        assert limiter.allow() is True
        assert limiter.allow() is False
        clock.now += 3600
        assert [limiter.allow() for _ in range(4)] == [True, True, True, False]


def _app(limiter: _DcrRateLimiter) -> Starlette:
    async def ok(_request):
        return JSONResponse({"ok": True}, status_code=201)

    return Starlette(
        routes=[
            Route(_DCR_REGISTER_PATH, ok, methods=["GET", "POST"]),
            Route("/mcp", ok, methods=["POST"]),
        ],
        middleware=[Middleware(_DcrRateLimitMiddleware, limiter=limiter)],
    )


def _proxied_app(limiter: _DcrRateLimiter, trusted_hosts: str) -> Any:
    # Uvicorn uses narrower ASGI event types than Starlette/httpx for the same protocol.
    return ProxyHeadersMiddleware(cast(Any, _app(limiter)), trusted_hosts=trusted_hosts)


@pytest.fixture
def client():
    limiter, _ = _limiter(global_burst=2)
    transport = httpx.ASGITransport(app=_app(limiter), client=("9.9.9.9", 12345))
    return httpx.AsyncClient(transport=transport, base_url="https://mcp.example")


class TestDcrRateLimitMiddleware:
    @pytest.mark.parametrize(
        ("trusted_hosts", "peer", "expected"),
        [
            ("", "10.0.0.1", [201, 201, 429]),
            ("10.0.0.0/24", "192.0.2.1", [201, 201, 429]),
            ("10.0.0.0/24", "10.0.0.1", [201, 201, 429]),
        ],
    )
    async def test_forwarded_ips_cannot_split_global_budget(self, trusted_hosts, peer, expected):
        limiter, _ = _limiter(global_burst=2)
        app = _proxied_app(limiter, trusted_hosts)
        transport = httpx.ASGITransport(app=app, client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example") as client:
            statuses = [
                (await client.post(_DCR_REGISTER_PATH, headers={"X-Forwarded-For": f"198.51.100.{i}"})).status_code
                for i in range(3)
            ]
        assert statuses == expected

    async def test_trusted_proxy_appending_real_ip_does_not_trust_spoofed_prefix(self):
        limiter, _ = _limiter(global_burst=2)
        app = _proxied_app(limiter, "10.0.0.0/24")
        transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example") as client:
            statuses = [
                (
                    await client.post(_DCR_REGISTER_PATH, headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.42"})
                ).status_code
                for i in range(3)
            ]
        assert statuses == [201, 201, 429]

    async def test_register_post_is_limited(self, client):
        async with client:
            statuses = [(await client.post(_DCR_REGISTER_PATH, json={})).status_code for _ in range(3)]
            response = await client.post(_DCR_REGISTER_PATH, json={})
        assert statuses == [201, 201, 429]
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert response.headers["content-type"] == "application/json"
        assert json.loads(response.content)["error"] == "rate_limited"

    async def test_other_paths_and_methods_are_untouched(self, client):
        async with client:
            for _ in range(5):
                assert (await client.post("/mcp", json={})).status_code == 201
                assert (await client.get(_DCR_REGISTER_PATH)).status_code == 201
            # The register POST budget is still intact.
            assert (await client.post(_DCR_REGISTER_PATH, json={})).status_code == 201
