#!/usr/bin/env python3
"""Serve the OAuth consent screen and verify it is Kubecost-branded.

``tests/test_branding.py`` covers the HTML *builders*. This script covers the
page as actually **served**: it starts a stub OIDC discovery endpoint, boots the
real server with ``AUTH_MODE=oidc``, performs Dynamic Client Registration, walks
``/authorize`` to the consent page, and asserts both that the branding applied
and that the security-critical parts of the flow still work.

Why a script and not a pytest fixture: the consent screen is unreachable from
any other local verification path. STDIO serves no HTTP routes, and the
``mcp-kubecost`` Kiro power runs STDIO with no ``AUTH_MODE``, so neither can
render this page. Bringing up a real identity provider is the only alternative.

The stub IdP only serves discovery metadata and an empty JWKS. That is enough
for ``OIDCProxy`` to initialize and to mint an upstream authorize redirect, which
is as far as consent goes. No token is ever exchanged, so no signing keys are
needed.

Usage:
    uv run scripts/check_consent_branding.py            # report + assertions
    uv run scripts/check_consent_branding.py --check    # quiet, exit 1 on failure
    uv run scripts/check_consent_branding.py --save /tmp/consent.html
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urljoin

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- Stub identity provider ---------------------------------------------------


def _start_stub_idp(port: int) -> HTTPServer:
    """Serve just enough OIDC discovery for OIDCProxy to initialize."""
    issuer = f"http://127.0.0.1:{port}"
    discovery = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/.well-known/openid-configuration"):
                body = json.dumps(discovery).encode()
            elif self.path.startswith("/jwks"):
                body = json.dumps({"keys": []}).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass  # keep the script's output readable

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- Server under test --------------------------------------------------------


def _start_server(port: int, idp_port: int, storage: Path) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "KUBECOST_BASE_URL": "https://demo.kubecost.xyz",
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER_URL": f"http://127.0.0.1:{idp_port}/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "kubecost-mcp",
        "OIDC_CLIENT_SECRET": "stub-secret",
        "MCP_EXTERNAL_URL": f"http://127.0.0.1:{port}",
        "OIDC_STORAGE_PATH": str(storage),
        "OIDC_JWT_SIGNING_KEY": "k" * 40,
        "FASTMCP_TELEMETRY_MODE": "off",
        "FASTMCP_SHOW_SERVER_BANNER": "false",
        "FASTMCP_LOG_LEVEL": "WARNING",
    }
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "fastmcp",
            "run",
            str(REPO_ROOT / "src/mcp_kubecost/server.py"),
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):  # ~30s; `uv`/import cost dominates
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"server did not become ready on {base}")


# --- Flow ---------------------------------------------------------------------


def _consent_page(client: httpx.Client, base: str, port: int, name: str) -> httpx.Response:
    reg = client.post(
        f"{base}/oauth/mcp/register",
        json={
            "client_name": name,
            "redirect_uris": [f"http://127.0.0.1:{port}/callback"],
            # OIDCProxy requires both grant types on a DCR registration.
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid profile",
            "token_endpoint_auth_method": "none",
        },
    )
    reg.raise_for_status()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    r = client.get(
        f"{base}/oauth/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": reg.json()["client_id"],
            "redirect_uri": f"http://127.0.0.1:{port}/callback",
            "scope": "openid profile",
            "state": "state-" + name,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    if r.status_code not in (301, 302, 303, 307):
        raise RuntimeError(f"/authorize did not redirect to consent: {r.status_code} {r.text[:200]}")
    # 'remember' mode gates silent consent on Sec-Fetch-Site; force the prompt.
    return client.get(urljoin(base, r.headers["location"]), headers={"Sec-Fetch-Site": "cross-site"})


def _form_fields(html: str) -> tuple[str, str]:
    txn = re.search(r'name="txn_id" value="([^"]+)"', html)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not txn or not csrf:
        raise RuntimeError("consent form fields missing — branding may have damaged the form")
    return txn.group(1), csrf.group(1)


def run_checks(base: str) -> tuple[list[tuple[str, bool, str]], str]:
    """Return the (description, ok, detail) list plus the served consent HTML."""
    from mcp_kubecost.branding import ACCENT, FONT_STACK, INK, KUBECOST_WEBSITE_URL

    results: list[tuple[str, bool, str]] = []

    def check(desc: str, ok: bool, detail: str = "") -> None:
        results.append((desc, bool(ok), detail))

    # The callback port is only ever a registered redirect_uri — the flow stops
    # at consent, so nothing binds it. One free port keeps the DCR registrations
    # consistent without reserving fixed numbers.
    callback_port = _free_port()

    with httpx.Client(timeout=20, follow_redirects=False) as client:
        page = _consent_page(client, base, callback_port, "Branding Probe")
        html = page.text

        check("consent page served", page.status_code == 200, f"HTTP {page.status_code}")
        check("Kubecost font stack applied", FONT_STACK in html)
        check("Kubecost ink color applied", INK in html)
        check("Kubecost accent color applied", ACCENT in html)
        check("logo is an inline data: URI", "data:image/png;base64," in html)
        # Without a declared icon a browser requests /favicon.ico and logs a 404.
        check("page declares an inline favicon", 'rel="icon"' in html)

        favicon = client.get(f"{base}/favicon.ico")
        check("GET /favicon.ico serves an image", favicon.status_code == 200, f"HTTP {favicon.status_code}")
        check(
            "favicon is the Kubecost PNG",
            favicon.headers.get("content-type", "").startswith("image/png") and favicon.content[:4] == b"\x89PNG",
            favicon.headers.get("content-type", ""),
        )
        check("server name links to kubecost.com", KUBECOST_WEBSITE_URL in html)
        check("no gofastmcp.com link or asset", "gofastmcp.com" not in html)
        check("no 'FastMCP' string in page", "FastMCP" not in html)
        check("consent copy names Kubecost", "This Kubecost MCP server" in html)
        check(
            "page fetches no external asset",
            "http://" not in html.split("<body")[0].replace("http://www.w3.org", ""),
        )

        csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html)
        check("CSP still present", csp is not None)
        if csp:
            import html as html_module

            policy = html_module.unescape(csp.group(1))
            check("CSP not relaxed for fonts", "font-src" not in policy, policy)

        # The flow must still work through the restyled page.
        txn, csrf = _form_fields(html)
        check("consent form fields intact", bool(txn and csrf))

        approve = client.post(
            f"{base}/oauth/mcp/consent",
            data={"txn_id": txn, "csrf_token": csrf, "submit": "true", "action": "approve"},
        )
        loc = approve.headers.get("location", "")
        check("approve redirects to upstream IdP", "/authorize" in loc and "client_id=kubecost-mcp" in loc, loc[:70])

        txn2, csrf2 = _form_fields(_consent_page(client, base, callback_port, "Deny Probe").text)
        deny = client.post(
            f"{base}/oauth/mcp/consent",
            data={"txn_id": txn2, "csrf_token": csrf2, "submit": "true", "action": "deny"},
        )
        check("deny returns access_denied", "error=access_denied" in deny.headers.get("location", ""))

        txn3, _ = _form_fields(_consent_page(client, base, callback_port, "CSRF Probe").text)
        forged = client.post(
            f"{base}/oauth/mcp/consent",
            data={"txn_id": txn3, "csrf_token": "forged", "submit": "true", "action": "approve"},
        )
        check("forged CSRF token rejected", forged.status_code >= 400, f"HTTP {forged.status_code}")

    return results, html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="quiet; exit 1 on any failure")
    parser.add_argument("--save", metavar="PATH", help="write the served consent HTML here for inspection")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "src"))

    idp_port, server_port = _free_port(), _free_port()
    # A fresh dir per run: a reused FileTreeStore keeps stale client registrations
    # and codes that can turn into false passes or spurious failures on a re-run.
    storage = Path(tempfile.mkdtemp(prefix="mcp-kubecost-consent-check-"))
    idp = _start_stub_idp(idp_port)
    proc = None
    try:
        proc = _start_server(server_port, idp_port, storage)
        results, html = run_checks(f"http://127.0.0.1:{server_port}")
    finally:
        if proc is not None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        idp.shutdown()
        shutil.rmtree(storage, ignore_errors=True)

    if args.save:
        Path(args.save).write_text(html)

    failed = [r for r in results if not r[1]]
    if not args.check:
        print()
        for desc, ok, detail in results:
            suffix = f"  ({detail})" if detail and not ok else ""
            print(f"  {'PASS' if ok else 'FAIL'}  {desc}{suffix}")
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
        if args.save:
            print(f"consent HTML written to {args.save}")
    elif failed:
        for desc, _ok, detail in failed:
            print(f"FAIL {desc}" + (f" ({detail})" if detail else ""), file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
