# AGENTS.md

Development guide for AI coding agents working on this repository.

## Project Overview

FinOps MCP server that exposes read-only Kubecost cost allocation and container rightsizing data to MCP clients. Built with Python 3.12+, FastMCP 3.4+, and httpx.

Entry point: [`src/mcp_kubecost/server.py`](src/mcp_kubecost/server.py) — creates the FastMCP instance, registers tools

Runtime FinOps guidance for MCP clients lives in tool docstrings, [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), and the README tone section — not here.

## Setup and Commands

```bash
uv venv .venv && uv sync --extra dev
source .venv/bin/activate
uv run pytest
uvx pre-commit run --config .pre-commit-config-ci.yaml --all-files
```

Use the existing `.venv` when running Python commands. Run `ruff format` and `isort` after every Python edit.

## Architecture Map

| Change | Location |
|--------|----------|
| New MCP tool, prompt, or resource | [`src/mcp_kubecost/tools/kubecost_tools.py`](src/mcp_kubecost/tools/kubecost_tools.py) |
| Workflow guidance prompt (skill) | New module under [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), register in [`skills/__init__.py`](src/mcp_kubecost/skills/__init__.py) |
| HTTP client / auth | [`src/mcp_kubecost/client.py`](src/mcp_kubecost/client.py) |
| HTTP custom routes (`/version`, `/reports`) | [`server.py`](src/mcp_kubecost/server.py) |
| Env-backed settings | [`src/mcp_kubecost/config/settings.py`](src/mcp_kubecost/config/settings.py) |

**Pattern A for tools:** thin handler → `client.get()` → domain helpers. Do not create separate `prompts/`, `resources/`, or `api/` packages unless deliberately refactoring.

Current MCP surface: 10 tools (`kubecost_list_windows`, `get_kubecost_workload_costs`, `get_container_savings_recommendations`, `get_abandoned_workloads`, `get_savings_overview`, `get_pv_sizing_recommendations`, `get_local_disk_savings`, `get_cluster_rightsizing_recommendations`, `get_unclaimed_volumes`, `get_resource_quota_recommendations`), inline prompts/resources in `kubecost_tools.py`, and 2 skills in `skills/`.

## Response Limits Pattern

All tools follow a consistent pattern for bounding response size and filtering noise:

```
API call (broad fetch, large limit)
  → Client-side filter (remove trivial/noise rows)
  → Sort by impact (totalCost or monthlySavings descending)
  → Slice to top_n (default 20)
  → Summary metadata covers FULL filtered set (totals, row_count, truncated flag)
```

| Tool | top_n / limit default | Client-side filter | Filter default |
|------|-----------------------|-------------------|----------------|
| `get_kubecost_workload_costs` | `top_n=20` | `min_total_cost` | $1.00 |
| `get_container_savings_recommendations` | `top_n=20` | `min_monthly_savings` | $1.00 |
| `get_abandoned_workloads` | `limit=20` | (API-side threshold) | 500 bytes/s |

Design rules:
- Default to **20 rows** in every tool response — enough for an LLM to reason over without token bloat.
- Always expose a `top_n` or `limit` parameter so callers can request more when needed.
- Response metadata (`total_cost`, `row_count`, `truncated`) must describe the full filtered population, not just the sliced rows.
- When the Kubecost API has no server-side filter for a field (e.g. `totalCost`), apply the filter client-side after fetch.
- Set `truncated=True` when rows are sliced so the caller knows more data exists.

## Code Conventions

- Python 3.12+, `from __future__ import annotations`
- Ruff (line-length 120, rules E/F/I/UP/B) + isort
- Import order: stdlib → third-party → `mcp_kubecost.*`
- Structured errors via [`errors.py`](src/mcp_kubecost/errors.py) (`ToolError`, `ErrorCode`) for LLM-facing failures
- Keep tool handlers thin; push parsing and aggregation into the domain layer
- Minimize scope — focused diffs, no drive-by refactors

## Environment Variables

Two naming schemes exist today. Do not add a third without a dedicated unification PR.

| Used by `client.py` (live) | Used by `settings.py` (partially unused) |
|----------------------------|------------------------------------------|
| `KUBECOST_BASE_URL` | `KUBECOST_BASE_URL` |
| `KUBECOST_API_KEY` | `KUBECOST_API_KEY` |

See [`.env.example`](.env.example) for a copy-paste template.

## Transport / Local Verification

- **STDIO:** `uv run python -m mcp_kubecost.server` or `uv run fastmcp run fastmcp.json`
- **HTTP:** `uv run fastmcp run fastmcp-http.json` (port 3030; matches Docker CMD)

There is no `run_http()` helper — use the FastMCP config files above.

## Boundaries

- Never commit `.env`, tokens, or secrets
- Do not reintroduce removed tools (`kubecost_get_cluster_cost_by_workload`, `kubecost_get_infra_costs`, `list_container_clusters`, `kubecost_get_request_sizing`)
- Only create git commits when explicitly asked

## Known Gaps

- `settings.py` timeout and retry fields are not wired to `client.py` (base URL and API key are)
- CI runs Python 3.11; local dev should use 3.12+ per `requires-python`
- `server.py` `instructions` mentions capabilities (RI utilization, business mappings) not yet implemented as tools — treat as aspirational

## Related Docs

- [README.md](README.md) — human setup and overview
- [README-pre-commit.md](README-pre-commit.md) — hook tiers and CI auto-fix workflow
