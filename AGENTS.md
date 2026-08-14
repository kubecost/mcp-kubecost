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
uvx pre-commit run --config .pre-commit-config-ci.yaml --all-files
```

Run `ruff format`, `ruff check --fix`, and `pyrefly check` after every Python edit. If the venv is stale after a dependency change, refresh it with `uv sync --extra dev` (or `just setup`).

`just --list` shows the full task runner surface (`just test`, `just serve`, `just inspect`, `just call-json <tool> '<json>'`).

## Architecture Map

| Change | Location |
|--------|----------|
| New MCP tool, prompt, or resource | [`src/mcp_kubecost/tools/kubecost_tools.py`](src/mcp_kubecost/tools/kubecost_tools.py) |
| Response envelope, window parsing, API call wrapper, error raising | [`src/mcp_kubecost/tools/_common.py`](src/mcp_kubecost/tools/_common.py) |
| Workflow guidance prompt (skill) | New module under [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), register in [`skills/__init__.py`](src/mcp_kubecost/skills/__init__.py) |
| Sizing profiles, aggregation helpers | [`src/mcp_kubecost/domain/kubecost/`](src/mcp_kubecost/domain/kubecost/) |
| HTTP client / auth | [`src/mcp_kubecost/client.py`](src/mcp_kubecost/client.py) |
| HTTP custom routes (`/health`, `/version`) | [`server.py`](src/mcp_kubecost/server.py) |
| Env-backed settings | [`src/mcp_kubecost/config/settings.py`](src/mcp_kubecost/config/settings.py) |

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
- Default windows are computed **at import time** as a rolling 7-day window: `current_window` = the 7 days ending yesterday UTC, `baseline_window` = the 7 days before that. A long-lived process therefore serves stale defaults; callers should pass explicit windows.
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

`_VERSION` in `kubecost_tools.py` is a single module constant applied to **every** tool's `version=`, so bumping it relabels all 11. Bump on a breaking response-shape change and update the "Contract version" line in the module docstring to match. Currently **7.0**.

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

`KUBECOST_BASE_URL` is the only required variable. The rest have defaults: `KUBECOST_API_BASE_PATH`, `KUBECOST_API_KEY`, `REQUIRE_CLIENT_API_KEY`, `KUBECOST_SSL_VERIFY`, `SSL_CA_BUNDLE`, `REQUEST_TIMEOUT_SECONDS`, `REQUEST_RETRY_COUNT`, `DEFAULT_WINDOW`, `USE_CAC_VIEWS`, `FASTMCP_LOG_LEVEL`, `FASTMCP_ENABLE_RICH_LOGGING` (forced off in HTTP mode), `FASTMCP_TELEMETRY_MODE`, `OTEL_*`, `OIDC_REDIRECT_PATH` (`/auth-mcp`; use `/auth/callback` when MCP has a dedicated hostname), `OIDC_VERIFY_ID_TOKEN` (false; set true for IBM w3id opaque access tokens). `MCP_SERVER_NAME` is read in `server.py` and is not in `.env.example`.

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

## Transport / Local Verification

- **STDIO:** `.venv/bin/mcp-kubecost`, `.venv/bin/python -m mcp_kubecost.server`, or `uv run fastmcp run fastmcp.json`
- **HTTP:** `uv run fastmcp run fastmcp-http.json` (port 3030)
- **Docker:** `CMD` is `/app/.venv/bin/mcp-kubecost-http` ([`otel_entrypoint.py`](src/mcp_kubecost/otel_entrypoint.py)), which wraps the server with `opentelemetry-instrument` unless `FASTMCP_TELEMETRY_MODE=off`

OpenTelemetry lives in an optional `otel` extra; the Dockerfile installs it with `--extra otel`, plain `uv sync --extra dev` does not. Nothing under `src/` imports `opentelemetry` — `otel_entrypoint.py` only names the binary in an `execvp` argv, and falls back to starting untraced if it is missing. `FASTMCP_TELEMETRY_MODE` is this server's own switch: FastMCP 3.4.7 does not read it (verified — zero hits in the installed package). The `0.65b0` versions are OpenTelemetry's permanent prerelease track for instrumentation, not a maturity signal; see [DEVELOPMENT.md](DEVELOPMENT.md#telemetry) before "upgrading" away from them.

There is no `run_http()` helper — use the FastMCP config files above.

Call a live tool against the public demo:

```bash
just call-json get_kubecost_cost_comparison '{"aggregate": "namespace"}'
```

## Boundaries

- Never commit `.env`, tokens, or secrets
- Do not reintroduce removed tools (`kubecost_get_cluster_cost_by_workload`, `kubecost_get_infra_costs`, `list_container_clusters`, `kubecost_get_request_sizing`)
- Only create git commits when explicitly asked
- **`just readme-tools` is destructive.** `scripts/generate_tools_readme.py` rewrites *everything* in README.md between `## Tools` and `## Quick Start`, not just the tables — it will delete hand-written sections in that range. Review its diff before keeping it, or edit the tables by hand.

## Known Gaps

- `settings.py` `request_timeout_seconds` and `retry_count` are not wired to `client.py`, which hardcodes `timeout=60.0` and has no retry loop. (`kubecost_base_url`, `KUBECOST_API_KEY`, `ssl_verify`, `kubecost_api_base_path`, and `use_cac_views` are wired.)
- `_default_wow_windows()` is evaluated at import time, so default comparison windows go stale in a long-running process.
- The `just serve` comment says port 9000; `fastmcp-http.json` actually binds 3030.

## Related Docs

- [DEVELOPMENT.md](DEVELOPMENT.md) — human setup, run, Docker/Kubernetes workflow
- [README.md](README.md) — overview and client configuration
- [README-auth.md](README-auth.md) — MCP OIDC, Kubecost API keys, and pod hardening
- [README-pre-commit.md](README-pre-commit.md) — hook tiers and CI auto-fix workflow
