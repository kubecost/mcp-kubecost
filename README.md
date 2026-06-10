# Kubecost FinOps MCP Server

Business-first MCP server built with FastMCP 3.4+ for FinOps analytics on Kubecost data.

## What This Server Provides

- Three FinOps tools for Kubernetes workload cost analysis and container rightsizing recommendations.
- Reusable prompts for executive reporting, showback, cost exploration, and savings workflows.
- Reference resources for Kubecost query parameters, cost fields, and sizing presets.
- Dual runtime support: STDIO (local/desktop) and Streamable HTTP (service deployment).

## Project Structure

- `src/mcp_kubecost/server.py` — MCP composition root and HTTP custom routes (`/version`, `/reports`).
- `src/mcp_kubecost/client.py` — Async httpx client for the Kubecost API.
- `src/mcp_kubecost/tools/kubecost_tools.py` — Tool handlers, inline prompts, and schema resources.
- `src/mcp_kubecost/skills/` — Workflow guidance prompts (cost allocation, optimization).
- `src/mcp_kubecost/domain/kubecost/` — CSV parsing, aggregation, and sizing guidance.
- `src/mcp_kubecost/config/` — Environment-backed settings.
- `k8s/` — Kubernetes deployment manifests.
- `scripts/` — Pre-commit hooks and repo safety checks.

## Setup (uv)

1. Create and sync environment:

   ```bash
   uv venv .venv
   uv sync --extra dev
   ```

2. Copy [`.env.example`](.env.example) to `.env` and configure:

   **Kubecost API auth** (used by `client.py`):

   - `Kubecost_BASE_URL=https://your-kubecost-host` — defaults to the demo host if unset
   - `Kubecost_API_KEY=...` — basic auth (API key mode)
   - Or Apptio OpenToken: `Kubecost_OPEN_TOKEN=...` and `Kubecost_ENVIRONMENT_ID=...`

   **Server config:**

   - `MCP_SERVER_NAME=Kubecost FinOps MCP` — display name in MCP clients
   - `MCP_KUBECOST_BASE_URL=https://your-mcp-host` — required in HTTP mode so tools generate clickable CSV download links

## Run

**STDIO** (local/desktop):

```bash
uv run python -m mcp_kubecost.server
# or
uv run mcp-kubecost
# or
uv run fastmcp run fastmcp.json
```

**HTTP** (service deployment, port 3030):

```bash
uv run fastmcp run fastmcp-docker.json
```

Example Cursor/client config for the public demo:

```json
{
  "mcpServers": {
    "mcp-kubecost": {
      "type": "streamable-http",
      "url": "https://mcp.demo.kubecost.cloud/mcp"
    }
  }
}
```

## Docker / Kubernetes

```bash
just docker-build-load      # build image locally
just docker-build-push      # bump version, push to ECR
just update-mcp-kubecost    # kubectl apply deployment
```

The Docker image runs `fastmcp run fastmcp-docker.json` on port 3030. Demo ingress: `mcp.demo.kubecost.cloud`.

## Quality Checks

- Format: `uv run ruff format .`
- Imports: `uv run isort .`
- Lint: `uv run ruff check .`
- Tests: `uv run pytest` — MCP contract and tool behavior tests in `tests/`

See [README-pre-commit.md](README-pre-commit.md) for local vs CI pre-commit hook tiers.

## FinOps Writing Tone Guide

- Prioritize financial outcomes and decision support over implementation details.
- Always disclose whether idle cost is included in any reported spend.
- Use language appropriate for finance, platform, and engineering leadership audiences.
- Include impact, confidence, and recommended next actions in summaries.

## For AI Coding Agents

See [AGENTS.md](AGENTS.md) for architecture, conventions, and development workflow.

## Security and Repository Safety

- Never hardcode credentials; use environment variables only.
- Do not commit `.env` files or token files.
- Large files are blocked in CI if they exceed configured thresholds.
- CI includes a lightweight secret-pattern and large-file safety check.
