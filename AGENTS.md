# AGENTS.md

Development guide for AI coding agents working on this repository.

## Project Overview

FinOps MCP server that exposes read-only Kubecost cost allocation and container rightsizing data to MCP clients. Built with Python 3.12+, FastMCP 3.4+, and httpx.

Entry point: [`src/mcp_kubecost/server.py`](src/mcp_kubecost/server.py) — creates the FastMCP instance, registers tools, skills, and the `/health` and `/version` HTTP routes.

Runtime FinOps guidance for MCP clients lives in tool docstrings, [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), and the README tone section — not here.

## Setup and Commands

The virtual environment lives at `.venv/`. Invoke its interpreter directly — do **not** run `source .venv/bin/activate`, and do not use `uvx pytest` (it resolves in an isolated environment without the project's dependencies, so test collection fails).

```bash
.venv/bin/pytest                        # unit suite; integration tests are deselected by default
.venv/bin/pytest -m integration         # integration only — hits https://demo.kubecost.xyz
.venv/bin/pytest -m ""                  # everything, as CI runs it
.venv/bin/ruff format .
.venv/bin/ruff check . --fix
.venv/bin/pyrefly check                 # type check
just vulture                            # dead-code scan (configured; do not pass extra paths)
just check-consent-branding             # serves the OAuth consent screen and asserts branding
uvx pre-commit run --config .github/pre-commit-config-ci.yaml --all-files
```

Run `ruff format`, `ruff check --fix`, and `pyrefly check` after every Python edit. If the venv is stale after a dependency change, refresh it with `uv sync --extra dev` (or `just setup`).

`just --list` shows the full task runner surface (`just test`, `just serve`, `just inspect`, `just vulture`, `just call-json <tool> '<json>'`).

## Architecture Map

| Change | Location |
|--------|----------|
| New MCP tool, prompt, or resource | [`src/mcp_kubecost/tools/kubecost_tools.py`](src/mcp_kubecost/tools/kubecost_tools.py) |
| Response envelope, window parsing, API call wrapper, error raising | [`src/mcp_kubecost/tools/_common.py`](src/mcp_kubecost/tools/_common.py) |
| Workflow guidance prompt (skill) | New module under [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), register in [`skills/__init__.py`](src/mcp_kubecost/skills/__init__.py) |
| Sizing profiles, aggregation helpers | [`src/mcp_kubecost/domain/kubecost/`](src/mcp_kubecost/domain/kubecost/) |
| HTTP client / auth | [`src/mcp_kubecost/client.py`](src/mcp_kubecost/client.py) |
| HTTP custom routes (`/health`, `/version`, `/favicon.ico`) | [`server.py`](src/mcp_kubecost/server.py) |
| OAuth consent / error page look and feel | [`src/mcp_kubecost/branding.py`](src/mcp_kubecost/branding.py) |
| Env-backed settings | [`src/mcp_kubecost/config/settings.py`](src/mcp_kubecost/config/settings.py) |
| FastMCP run configs (stdio / HTTP / public demo client) | [`config/`](config/) |

**Pattern A for tools:** thin handler → `call_get_api()` → domain helpers → typed Pydantic response. Do not create separate `prompts/`, `resources/`, or `api/` packages unless deliberately refactoring.

Current MCP surface — **11 tools**, **11 prompts** (9 inline in `kubecost_tools.py` + 2 skills), **4 resources**:

| Tools | |
|---|---|
| `kubecost_list_windows` | `get_kubecost_workload_costs` |
| `get_kubecost_cost_comparison` | `get_container_savings_recommendations` |
| `get_abandoned_workloads` | `get_savings_overview` |
| `get_pv_sizing_recommendations` | `get_local_disk_savings` |
| `get_cluster_rightsizing_recommendations` | `get_unclaimed_volumes` |
| `get_resource_quota_recommendations` | |

Resources: `kubecost://schema/allocation-params`, `kubecost://schema/cost-fields`, `kubecost://schema/sizing-profiles`, `kubecost://guides/container-sizing`.

### `tools/_common.py` — shared contract

Every tool response extends `BaseToolResponse` (`status: QueryStatus`, `message`, `recommended_action`). Reach for these before writing anything new:

- `QueryStatus` (`ok` / `empty` / `partial` / `error`) — never return an empty list with no explanation
- `resolve_window()`, `resolved_window_from_api()`, `to_api_window()`, `ResolvedWindow` — window parsing and display
- `call_get_api()` — the API wrapper all tools go through
- `raise_tool_error(ErrorCode..., ...)` — the LLM-facing failure path (wraps `errors.ToolError`); prefer it over raising bare exceptions
- `extract_list()`, `validate_response()`, `safe_path_segment()`

### `get_kubecost_cost_comparison` — window rules and row contract

- Both windows must be **explicit RFC3339 ranges** ending before today (UTC).
- **All named aliases are rejected** (`lastweek`, `lastmonth`, `7d`, `today`, etc.) — there is no alias for "the period before lastmonth", making aliases a dead end for comparisons.
- RFC3339 ranges of **different lengths are allowed**; a `warnings` entry flags the mismatch and points the caller at the normalized fields.
- Default windows are computed **for each tool call** as a rolling 7-day window: `current_window` = the 7 days ending yesterday UTC, `baseline_window` = the 7 days before that. Callers may still pass explicit windows for reproducible reports.
- Each row carries `row_status` (`new` / `removed` / `unchanged` / `changed`) — there is no `is_new` boolean. A dimension costing zero in **both** windows is `unchanged`, not `new`.
- Each row also carries per-day figures — `current_daily_cost`, `baseline_daily_cost`, `daily_change`, `normalized_pct_change` — so unequal-length periods are comparable. Rows still sort by absolute raw `change`.
- The response `notes` list explains idle handling and, when present, `__unallocated__` rows. `notes` is guidance; `warnings` is "something may be wrong".

### Idle and unallocated cost

`_fetch_allocation` sends `idle=true` + `shareIdle=true`, which distributes idle capacity proportionally across the returned rows — **no `__idle__` row is ever produced**. Do not add `splitIdle`: it only controls how a standalone idle row is broken up, so alongside `shareIdle` it is a verified no-op.

Cost with no value for a requested dimension comes back under `__unallocated__` (e.g. ~$135/week for `aggregate=cluster,controller` on the demo cluster). It is real spend, not an error.

## Response Limits Pattern

All row-returning tools follow a consistent pattern for bounding response size and filtering noise:

```
API call (broad fetch, large limit)
  → Client-side filter (remove trivial/noise rows)
  → Sort by impact (totalCost or monthlySavings descending)
  → Slice to top_n (default 20)
  → Summary metadata covers FULL filtered set (totals, row_count, truncated flag)
```

| Tool | Cap | Client-side filter | Filter default |
|------|-----|-------------------|----------------|
| `get_kubecost_workload_costs` | `top_n=20` | `min_total_cost` | $1.00 |
| `get_kubecost_cost_comparison` | `top_n=20` | (none — diff is already aggregated) | — |
| `get_container_savings_recommendations` | `top_n=20` | `min_monthly_savings` | none (suggest $5.00) |
| `get_abandoned_workloads` | `limit=20` | (API-side threshold) | 500 bytes/s |
| `get_pv_sizing_recommendations` | `top_n=20` | `min_monthly_savings` | $1.00 |
| `get_local_disk_savings` | `top_n=20` | `min_monthly_savings` | $1.00 |
| `get_unclaimed_volumes` | `top_n=20` | `min_monthly_cost` | $1.00 |
| `get_resource_quota_recommendations` | `limit=20` | (none) | — |

Design rules:
- Default to **20 rows** in every tool response — enough for an LLM to reason over without token bloat.
- Always expose a `top_n` or `limit` parameter so callers can request more when needed.
- Response metadata (`total_cost`, `row_count`, `truncated`) must describe the full filtered population, not just the sliced rows.
- When the Kubecost API has no server-side filter for a field (e.g. `totalCost`), apply the filter client-side after fetch.
- Set `truncated=True` when rows are sliced so the caller knows more data exists.
- Note that `get_container_savings_recommendations` takes `min_monthly_savings=None` as the default (no filter). Pass `5.0` to cut noise; pass a negative value to keep undersized workloads. Profiles do not change this filter.

## Container Sizing Profiles

`SIZING_PROFILES` in [`sizing_guidance.py`](src/mcp_kubecost/domain/kubecost/sizing_guidance.py) is the single source of truth for the `profile` parameter on `get_container_savings_recommendations`:

| Profile | Window | Quantiles | Target utilization |
|--------|--------|-----------|--------------------|
| `high-availability` | 30d | P95 CPU / P99 RAM | 0.50 |
| `production` (default) | 15d | P80 CPU / P95 RAM | 0.65 |
| `development` | 15d | P80 CPU / P95 RAM | 0.80 |

Rules to preserve when touching this:

- Kubecost computes `recommended = usage / targetUtilization`, so a **lower** target means a **larger** request and more headroom. The ladder must stay `high-availability < production < development`.
- No profile may set `target_ram_utilization` above `target_cpu_utilization` — memory is not compressible, so an undersized RAM request OOM-kills rather than throttles. A test enforces this.
- Every profile pins every key in `DEFAULT_SIZING_PARAMS` (also enforced by a test) so each dict is readable without cross-referencing the defaults. `production` must stay identical to `DEFAULT_SIZING_PARAMS`.
- `PROFILE_DESCRIPTIONS` and the `explore_container_savings` prompt menu are **generated** from `SIZING_PROFILES`. Change values there only — never restate quantiles or targets in prose.
- Profiles never apply a savings filter; `min_monthly_savings` stays `None` in all three.
- These are the **same three names** `get_cluster_rightsizing_recommendations` and `get_resource_quota_recommendations` take on their `profile` parameter — one sizing vocabulary across the server, checked by an invariant. Kubecost owns that spelling (the node-group and quota tools send `profile` to the API verbatim), so if the two ever diverge, our side moves back, not theirs. The mechanisms still differ: here a profile expands into individually overridable sizing knobs; there it is an opaque pass-through enum.

Before and after changing a profile, run [`scripts/show_sizing_profiles.py`](scripts/show_sizing_profiles.py). It renders the parameters, the request multiplier each target implies (`0.50` → `2.00x` usage), a worked example, and the exact Kubecost query params — then checks every rule above. `--check` exits non-zero on a violation; `--json` for scripting.

```bash
uv run scripts/show_sizing_profiles.py          # full report
uv run scripts/show_sizing_profiles.py --check  # invariants only, exit 1 on failure
```

## Tool Response Shape

FastMCP serializes each returned Pydantic model **twice** — once as a JSON `TextContent` block and once as `structuredContent`. This is deliberate: the MCP specification (2025-11-25) says a tool returning structured content SHOULD also return the serialized JSON in a text block, for clients that do not read `structuredContent`. Do not "optimize" it away with `ToolResult` or middleware. To shrink a response, shrink the payload — fewer fields, lower `top_n`.

`_VERSION` in `kubecost_tools.py` is a single module constant applied to **every** tool's `version=`, so bumping it relabels all 11. Bump on a breaking response-shape change and update the "Contract version" line in the module docstring to match. Currently **8.0**.

## Code Conventions

- Python 3.12+, `from __future__ import annotations`
- Ruff (line-length 120, rules E/F/I/UP/B) — `ruff check --fix` handles import sorting (rule I)
- Pyrefly (`preset = "basic"`) must stay at 0 errors
- Import order: stdlib → third-party → `mcp_kubecost.*`
- Structured errors via `raise_tool_error()` in [`tools/_common.py`](src/mcp_kubecost/tools/_common.py), backed by `ToolError` / `ErrorCode` in [`errors.py`](src/mcp_kubecost/errors.py)
- Keep tool handlers thin; push parsing and aggregation into the domain layer
- Minimize scope — focused diffs, no drive-by refactors
- Prefer up-to-date dependencies (libraries and GitHub Actions alike) to avoid known CVEs

## Environment Variables

All configuration flows through `get_settings()` in [`config/settings.py`](src/mcp_kubecost/config/settings.py) — `client.py` reads no environment variables directly. [`.env.example`](.env.example) is the complete, accurate template; copy it to `.env`.

`KUBECOST_BASE_URL` is the only universally required variable. OIDC additionally requires `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_BASE_URL` (the public authorization-server base, normally `https://host/oauth/mcp`), and `OIDC_RESOURCE_BASE_URL` (the public base hosting `MCP_HTTP_PATH`, normally `https://host`). The rest have defaults: `KUBECOST_API_BASE_PATH`, `KUBECOST_API_KEY`, `REQUIRE_CLIENT_API_KEY`, `KUBECOST_SSL_VERIFY`, `SSL_CA_BUNDLE`, `REQUEST_TIMEOUT_SECONDS`, `REQUEST_RETRY_COUNT`, `DEFAULT_WINDOW`, `USE_CAC_VIEWS`, `FASTMCP_LOG_LEVEL`, `FASTMCP_ENABLE_RICH_LOGGING` (forced off in HTTP mode), `FASTMCP_TELEMETRY_MODE`, `OTEL_*`, `OIDC_REDIRECT_PATH` (`/callback`, relative to `OIDC_BASE_URL`), and `OIDC_STORAGE_PATH` (`/var/lib/mcp-kubecost/oauth`). Opaque OIDC access tokens are detected from the token response — there is no `OIDC_VERIFY_ID_TOKEN` setting. `MCP_SERVER_NAME` is read in `server.py` and is not in `.env.example`.

Two variables are read outside `get_settings()`, in [`otel_entrypoint.py`](src/mcp_kubecost/otel_entrypoint.py), because they shape the `fastmcp run` argv before the server process exists: `FASTMCP_TELEMETRY_MODE` and `MCP_HTTP_PATH` (the protected-resource route, `--path`; default `/mcp`, independent of the OAuth authorization-server prefix). The entrypoint calls `load_dotenv()` itself so both still work from `.env`, and validates `MCP_HTTP_PATH` the way `_get_oidc_redirect_path()` validates its input. Running `fastmcp run config/fastmcp-http.json` directly bypasses the entrypoint and ignores both.

Add new settings to `Settings` and `.env.example` together; do not read `os.getenv` from a tool or client module.

## Kubecost Authentication

OPTIONAL
The key is sent to Kubecost as an **`X-API-KEY` request header**. There is no Basic auth — it was tried, does not work against Kubecost, and was removed. Do not reintroduce an `auth=` tuple in `client.py`.

[`auth.py`](src/mcp_kubecost/auth.py) resolves the key per request, header first:

1. An `X-API-KEY` header on the incoming MCP request (HTTP transport only)
2. `KUBECOST_API_KEY` from the environment
3. Neither — the request goes out unauthenticated, which is a supported default

The per-request read uses FastMCP's `get_http_headers()`, which returns `{}` when there is no active HTTP request. That is why this works unchanged on STDIO and why no tool handler needs a `Context` — resolution lives at the client boundary, not in the tool layer. Do not thread a key parameter through `call_get_api()` or the `_fetch_*` helpers.

`REQUIRE_CLIENT_API_KEY=true` rejects HTTP requests with no header, raising `MissingClientApiKeyError` → `ErrorCode.AUTHENTICATION_FAILED`. The check sits *between* steps 1 and 2, so a configured `KUBECOST_API_KEY` does not satisfy it. It is skipped entirely on STDIO, where a client cannot send headers.

## OAuth Page Branding

FastMCP renders the browser-facing OAuth pages (consent, OAuth errors, unregistered client) and styles them with **its own** logo and palette. [`branding.py`](src/mcp_kubecost/branding.py) makes them read as Kubecost, using two seams:

1. `server.py` passes `icons=server_icons()` and `website_url=` to `FastMCP()`. FastMCP reads both off the server instance when rendering, which sets the page logo and hyperlinks the server name. This is supported API — no patching.
2. `install_oauth_page_branding()`, called from `create_oidc_provider()`, rebinds FastMCP's three page builders to wrappers that append a Kubecost stylesheet plus an inline `<link rel="icon">` to the returned HTML, and rewrite the handful of strings that name FastMCP to the reader.

There is no theming hook in FastMCP 3.4.7 — `fastmcp.utilities.ui` composes its palette into a `<style>` block at render time, and `require_authorization_consent="external"` means "host consent yourself", not "restyle it". Rebinding the builders is the only option short of reimplementing FastMCP's CSRF and cookie handling, so keep the overlay **purely presentational**: never touch the consent form, its CSRF token, or the transaction fields.

Rules to preserve:

- The palette and font stack come from the **Kubecost UI's own design tokens** (`demo.kubecost.xyz` stylesheet). Change the constants in `branding.py`, not individual CSS rules.
- Keep the logo an inline SVG `data:` URI and the CSS inline. The pages must fetch nothing external — otherwise they need new reverse-proxy rules (see [docs/auth/auth-technical.md](docs/auth/auth-technical.md)) and break in air-gapped clusters.
- Do not set `consent_csp_policy`. `style-src 'unsafe-inline'` and `img-src data:` are already in FastMCP's default consent CSP, so the overlay needs no CSP relaxation. Adding a webfont would, which is why Space Grotesk is declared but never fetched.
- Copy substitutions in `_COPY_SUBSTITUTIONS` must each be a **single-line** substring of FastMCP's template; nothing may span a line break, or reindentation upstream silently breaks it.
- Branding is installed only under `AUTH_MODE=oidc`; the other modes serve no browser pages. `install_oauth_page_branding()` is idempotent and warns rather than raises when a builder has been renamed — un-branded pages are cosmetic, not a startup failure.
- `tests/test_branding.py` is the FastMCP-upgrade tripwire. If it fails after a dependency bump, re-check the builder names and markup in `fastmcp/server/auth/oauth_proxy/ui.py` before relaxing an assertion.

### Icons: two audiences, two mechanisms

`server_icons()` feeds both, and they are unrelated transports for the same mark. Do not conflate them:

| Audience | Mechanism | Notes |
|---|---|---|
| **MCP clients** | `serverInfo.icons` (`FastMCP(icons=...)`) | The *only* icon mechanism MCP has. Advertised on **every** transport, STDIO included. Verify with the Kiro power. |
| **Browsers** | inline `<link rel="icon">` + `GET /favicon.ico` | Browser convention, invisible to MCP clients. |

- `sizes=["any"]` is the spec's spelling for a scalable format — not a pixel size. `theme` (`"light"` / `"dark"`) rides on `Icon`'s `extra="allow"`; it is not a declared field in the installed SDK, so a test pins that it survives serialization.
- **Keep the light variant at `icons[0]`.** FastMCP renders `icons[0]` on its OAuth pages, which are light-background, and mint `#63e892` washes out there. `ACCENT` (`#31c46c`) is the light-background variant; `ACCENT_BRIGHT` is for dark client chrome.
- **`_svg_data_uri()` must percent-encode quotes.** The SVG uses `"` for its own attributes; leaving those raw terminates the enclosing `href`/`src` attribute and leaks the rest of the URI onto the page as visible text. FastMCP escapes its own `<img src>`, so this only bit the `<link rel="icon">` we add. A test asserts no raw quote survives.
- Declaring the icon inline **suppresses** the `/favicon.ico` request rather than serving it, which is what makes it work on a shared Kubecost hostname where root `/favicon.ico` belongs to the Kubecost frontend. The route exists for what the overlay cannot reach: FastMCP's bare HTML fragments (no `<head>` to inject into), plus browsers pointed at `/mcp` or any 404.

### Verifying the consent screen

`tests/test_branding.py` covers the HTML **builders**. To check the page as actually **served**, run:

```bash
just check-consent-branding                          # 16 checks, human-readable report
just check-consent-branding --check                  # quiet, exit 1 on any failure
just check-consent-branding --save /tmp/consent.html # dump the served HTML to eyeball
```

[`scripts/check_consent_branding.py`](scripts/check_consent_branding.py) starts a stub OIDC discovery endpoint, boots the real server with `AUTH_MODE=oidc`, performs DCR, walks `/authorize` to the consent page, and asserts the palette, font, logo, and copy applied **and** that the flow still works — approve redirects upstream, deny returns `access_denied`, a forged CSRF token is rejected. The stub IdP serves only discovery metadata and an empty JWKS; consent never exchanges a token, so no signing keys are needed.

The script separates the two seams by design. Disable `install_oauth_page_branding()` and the palette/copy checks fail while the logo and website-link checks still pass, because those come from `FastMCP(icons=..., website_url=...)` instead. A failure therefore tells you *which* seam broke.

**This is the only local path that reaches the consent screen.** Nothing else renders it:

| Path | Reaches consent screen? | Why |
|------|------------------------|-----|
| `just check-consent-branding` | **yes** | HTTP + `AUTH_MODE=oidc` + a stub IdP |
| `.venv/bin/pytest` | no — builders only | calls `create_consent_html()` directly, no server |
| STDIO (`.venv/bin/mcp-kubecost`) | no | serves no HTTP routes at all |
| `just serve` / `config/fastmcp-http.json` | no | HTTP, but `AUTH_MODE` is unset so `auth=None` |
| `mcp-kubecost` Kiro power | no | STDIO, and sets no `AUTH_MODE` |

One caveat if you test favicon behaviour by hand: over plain **http** the consent CSP's `img-src https: data:` blocks the browser's implicit `/favicon.ico` fetch, so a local http server shows no request either way and the check looks like a false pass. Reproduce over **https**, where `img-src https:` matches the page's own origin. Chrome's `--screenshot` mode also skips favicon fetching entirely; use `--dump-dom`.

Do not try to verify consent branding through the Kiro power or `just inspect` — see [Kiro power](#kiro-power-local-mcp-client-check) for what those *can* check.

## Transport / Local Verification

- **STDIO:** `.venv/bin/mcp-kubecost`, `.venv/bin/python -m mcp_kubecost.server`, or `uv run fastmcp run config/fastmcp.json`
- **HTTP:** `uv run fastmcp run config/fastmcp-http.json` (port 3030)
- **Docker:** `CMD` is `/app/.venv/bin/mcp-kubecost-http` ([`otel_entrypoint.py`](src/mcp_kubecost/otel_entrypoint.py)), which wraps the server with `opentelemetry-instrument` unless `FASTMCP_TELEMETRY_MODE=off`

OpenTelemetry lives in an optional `otel` extra; the Dockerfile installs it with `--extra otel`, plain `uv sync --extra dev` does not. Nothing under `src/` imports `opentelemetry` — `otel_entrypoint.py` only names the binary in an `execvp` argv, and falls back to starting untraced if it is missing. `FASTMCP_TELEMETRY_MODE` is this server's own switch: FastMCP 3.4.7 does not read it (verified — zero hits in the installed package). The `0.65b0` versions are OpenTelemetry's permanent prerelease track for instrumentation, not a maturity signal; see [docs/development/README.md](docs/development/README.md#telemetry) before "upgrading" away from them.

There is no `run_http()` helper — use the FastMCP config files above.

Call a live tool against the public demo:

```bash
just call-json get_kubecost_cost_comparison '{"aggregate": "namespace"}'
```

### Kiro power (local MCP client check)

A Kiro power may be installed at `.kiro/powers/mcp-kubecost/`, wrapping this repo as an MCP server for the IDE. **`.kiro/` is gitignored** — the power is a developer's local config, not part of the repo, so never treat its presence or its `mcp.json` contents as guaranteed. It runs the STDIO entry point (`uv run --project . mcp-kubecost`) and typically points `KUBECOST_BASE_URL` at a local instance, which will fail without a port-forward; repoint it at `https://demo.kubecost.xyz` to exercise it.

The power is a genuine end-to-end client check and the only path that shows what a **real MCP client** sees. Use it to confirm:

- the server starts under a real client handshake and `tools/list` returns **11 tools**, **11 prompts**, **4 resources**
- tool calls round-trip and return `status: ok`
- `serverInfo` advertises what `FastMCP()` was given — two themed Kubecost SVG `data:` URIs in `icons`, and `websiteUrl` of `https://www.kubecost.com`

That last one matters: `icons=` and `website_url=` were added for the consent screen, but they are also sent to every MCP client, so the power is the only way to verify the icon actually reaches clients. Client-side rendering is still uneven — Claude Code has an open issue to read `serverInfo.icons` — so a client showing no icon is not necessarily a bug here.

The power **cannot** test anything HTTP-only — consent screen, OAuth error pages, `/health`, `/version`, `/favicon.ico`, `X-API-KEY` header resolution, or `REQUIRE_CLIENT_API_KEY`. STDIO serves no HTTP routes, and `get_http_headers()` returns `{}` there by design. Reach for `just check-consent-branding` or `config/fastmcp-http.json` instead.

A newly added power is not visible to an in-flight agent session — the client wires powers up at session start, so start a new session before expecting to call it.

## Boundaries

- Never commit `.env`, tokens, or secrets
- Do not reintroduce removed tools (`kubecost_get_cluster_cost_by_workload`, `kubecost_get_infra_costs`, `list_container_clusters`, `kubecost_get_request_sizing`)
- Only create git commits when explicitly asked
- **`just readme-tools` is destructive.** `scripts/generate_tools_readme.py` rewrites *everything* in README.md between `## Tools` and `## Quick Start`, not just the tables — it will delete hand-written sections in that range. Review its diff before keeping it, or edit the tables by hand.

## Related Docs

- [docs/development/README.md](docs/development/README.md) — human setup, run, Docker/Kubernetes workflow
- [README.md](README.md) — overview and client configuration
- [docs/auth/README.md](docs/auth/README.md) — MCP OIDC, Kubecost API keys, and pod hardening
- [docs/development/pre-commit-checks.md](docs/development/pre-commit-checks.md) — hook tiers and CI auto-fix workflow
