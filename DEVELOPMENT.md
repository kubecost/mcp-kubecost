# MCP Kubecost Development Guide<!-- omit in toc -->

- [AI Coding Agents](#ai-coding-agents)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Configuration](#configuration)
- [Run](#run)
- [Testing and Quality](#testing-and-quality)
- [Docker / Kubernetes](#docker--kubernetes)
- [Security](#security)

## AI Coding Agents

See [AGENTS.md](AGENTS.md) for architecture conventions, response-limit patterns, tool authoring rules, and known gaps.

## Project Structure

```
src/mcp_kubecost/
  server.py                  # FastMCP composition root; registers tools, skills, HTTP routes
  client.py                  # Async httpx client for Kubecost API requests
  tools/
    kubecost_tools.py        # All 10 tool handlers, inline prompts, and schema resources
  skills/
    container_cost_allocation.py  # Cost visibility skill (MCP prompt)
    optimization.py               # Savings/rightsizing skill (MCP prompt)
    __init__.py                   # register_all_skills()
  domain/kubecost/           # aggregation, sizing helpers
  config/settings.py         # Pydantic settings backed by env vars
  errors.py                  # ToolError / ErrorCode for structured LLM-facing errors
k8s/                         # Kubernetes deployment manifests
scripts/                     # Pre-commit hooks and repo safety checks
tests/                       # pytest test suite
```

## Setup

```bash
uv venv .venv && uv sync --extra dev
source .venv/bin/activate
```

## Configuration

Copy [`.env.example`](.env.example) to `.env` and set:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `KUBECOST_BASE_URL` | `client.py` + `settings.py` | Kubecost host (e.g. `https://your-kubecost-host`) |
| `KUBECOST_API_KEY` | `client.py` + `settings.py` | Basic auth API key |
| `MCP_KUBECOST_BASE_URL` | `utils.py` | Base URL for download links (HTTP mode only) |
| `MCP_SERVER_NAME` | `settings.py` | Display name in MCP clients |
| `TOON_ENABLED` | `settings.py` | `false` returns plain JSON instead of TOON (default: `true`) |
| `REQUEST_TIMEOUT_SECONDS` | `settings.py` (not wired) | HTTP timeout |
| `FASTMCP_LOG_LEVEL` | FastMCP | Log verbosity |

## Run

**STDIO** (local/desktop):

```bash
uv run mcp-kubecost
# or
uv run python -m mcp_kubecost.server
# or
uv run fastmcp run fastmcp.json
```

**HTTP** (service deployment, port 3030):

```bash
uv run fastmcp run fastmcp-http.json
```

## Testing and Quality

```bash
uv run pytest                          # full test suite
uv run ruff format .                   # format
uv run ruff check . --fix              # lint
uvx pre-commit run --config .pre-commit-config-ci.yaml --all-files
```

Run `ruff format` and `isort` after every Python edit. See [README-pre-commit.md](README-pre-commit.md) for local vs CI hook tiers.

## Docker / Kubernetes

```bash
just docker-build-load      # build image locally
just docker-build-push      # bump version, push to ECR
just update-mcp-kubecost    # kubectl apply deployment
```

The Docker image runs `fastmcp run fastmcp-http.json` on port 3030. Demo ingress: `https://mcp.demo.kubecost.cloud/mcp`.

## Security

- Never hardcode credentials; use environment variables only.
- Do not commit `.env` files or token files.
- CI includes a lightweight secret-pattern scan and large-file safety check.
