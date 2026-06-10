# AGENTS.md

Development guide for AI coding agents working on this repository.

## Project Overview

FinOps MCP server that exposes read-only Kubecost cost allocation and container rightsizing data to MCP clients. Built with Python 3.12+, FastMCP 3.4+, and httpx.

Entry point: [`src/mcp_kubecost/server.py`](src/mcp_kubecost/server.py) — creates the FastMCP instance, registers tools via `register_kubecost_csv_tools()`, and skills via `register_all_skills()`.

Runtime FinOps guidance for MCP clients lives in tool docstrings, [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), and the README tone section — not here.

## Setup and Commands

```bash
uv venv .venv && uv sync --extra dev
uv run ruff format .
uv run isort .
uv run ruff check .
uv run pytest
pre-commit run --config .pre-commit-config-ci.yaml --all-files
```

Use the existing `.venv` when running Python commands. Run `ruff format` and `isort` after every Python edit.

## Architecture Map

| Change | Location |
|--------|----------|
| New MCP tool, prompt, or resource | [`src/mcp_kubecost/tools/kubecost_tools.py`](src/mcp_kubecost/tools/kubecost_tools.py) |
| Workflow guidance prompt (skill) | New module under [`src/mcp_kubecost/skills/`](src/mcp_kubecost/skills/), register in [`skills/__init__.py`](src/mcp_kubecost/skills/__init__.py) |
| CSV parsing / sizing logic | [`src/mcp_kubecost/domain/kubecost/`](src/mcp_kubecost/domain/kubecost/) |
| HTTP client / auth | [`src/mcp_kubecost/client.py`](src/mcp_kubecost/client.py) |
| HTTP custom routes (`/version`, `/reports`) | [`server.py`](src/mcp_kubecost/server.py) |
| Env-backed settings | [`src/mcp_kubecost/config/settings.py`](src/mcp_kubecost/config/settings.py) |

**Pattern A for tools:** thin handler → `client.get()` → domain helpers. Do not create separate `prompts/`, `resources/`, or `api/` packages unless deliberately refactoring.

Current MCP surface: 3 tools (`kubecost_list_windows`, `get_kubecost_workload_costs`, `get_container_savings_recommendations`), inline prompts/resources in `kubecost_tools.py`, and 2 skills in `skills/`.

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
| `Kubecost_BASE_URL` | `KUBECOST_BASE_URL` |
| `Kubecost_API_KEY` | `KUBECOST_API_KEY` |
| `Kubecost_OPEN_TOKEN` | — |
| `Kubecost_ENVIRONMENT_ID` | — |

HTTP-only:

- `MCP_KUBECOST_BASE_URL` — base URL for CSV download links in [`utils.py`](src/mcp_kubecost/utils.py)
- `CLDY_MCP_LOCAL_VERSION` — returned by the `/version` endpoint

See [`.env.example`](.env.example) for a copy-paste template.

## Transport / Local Verification

- **STDIO:** `uv run python -m mcp_kubecost.server` or `uv run fastmcp run fastmcp.json`
- **HTTP:** `uv run fastmcp run fastmcp-docker.json` (port 3030; matches Docker CMD)

There is no `run_http()` helper — use the FastMCP config files above.

## Boundaries

- Never commit `.env`, tokens, or secrets
- Never inline large CSV blobs in tool responses — use download URLs
- Do not reintroduce removed tools (`kubecost_get_cluster_cost_by_workload`, `kubecost_get_infra_costs`, `list_container_clusters`, `kubecost_get_request_sizing`)
- Only create git commits when explicitly asked

## Known Gaps

- `settings.py` timeout, retry, and token fields are not wired to `client.py`
- CI runs Python 3.11; local dev should use 3.12+ per `requires-python`
- `server.py` `instructions` mentions capabilities (RI utilization, business mappings) not yet implemented as tools — treat as aspirational

## Related Docs

- [README.md](README.md) — human setup and overview
- [README-pre-commit.md](README-pre-commit.md) — hook tiers and CI auto-fix workflow
