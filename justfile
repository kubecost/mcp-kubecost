# Task Runner
# https://github.com/casey/just

MCP_CONFIG := "./.bob/mcp.json"

default:
    @just --list
# Cross-platform sed (works on both Linux and macOS)
_sed pattern file:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "{{pattern}}" "{{file}}"
    else
        sed -i "{{pattern}}" "{{file}}"
    fi
_update_chart_version:
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION=$(uv version --short)
    echo "Updating Chart.yaml appVersion to $VERSION..."
    just _sed 's|^appVersion:.*|appVersion: "'"$VERSION"'"|' charts/mcp-kubecost/Chart.yaml
    echo "Updated charts/mcp-kubecost/Chart.yaml appVersion to $VERSION"

docker-build-run:
    #!/usr/bin/env bash
    set -euo pipefail
    docker buildx build --load --progress plain \
        -t mcp-kubecost \
        -f ./Dockerfile .
    echo ""
    echo -e "\033[33m  Image built. Wait a few seconds for the server to start.\033[0m"
    echo -e "\033[33m  Then open a new terminal and run your tests. Example command:\033[0m"
    echo -e "\033[33m  fastmcp call mcp-http.json get_container_savings_recommendations --input-json '{\"window\": \"15d\"}'\033[0m"
    docker run --rm \
      --name mcp-kubecost \
      -p 3030:3030 \
      -e KUBECOST_BASE_URL=http://host.docker.internal:9090  \
      mcp-kubecost

# ── Environment ────────────────────────────────────────────────────────────────
# Install dependencies and create virtual environment
setup-dev-environment:
    uv venv --clear
    uv sync --all-extras --active

# ── Testing ────────────────────────────────────────────────────────────────────
test:
    uv run pytest
# Integration tests
test-all:
    uv run pytest -m ""

# Integration tests on http
test-all-http:
    MCP_KUBECOST_TARGET=http://localhost:3030/mcp uv run pytest -m integration

# ── Development Server ─────────────────────────────────────────────────────────

# Start FastMCP dev server with browser inspector UI
dev:
    fastmcp dev inspector

# Start FastMCP as HTTP server on port 3030 (for debugging with logs)
serve:
    fastmcp run ./config/fastmcp-http.json

# ── FastMCP CLI ────────────────────────────────────────────────────────────────

# Inspect the MCP server (list tools, prompts, resources)
inspect:
    fastmcp inspect config/fastmcp.json

# List all tools and prompts
list:
    fastmcp list {{MCP_CONFIG}} --prompts

# Regenerate README tools + prompts tables from live FastMCP list.
# Uses tests/mcp-demo.json (committed MCPConfig) so this does not depend on
# gitignored ./.bob/mcp.json. Server logs go to stderr; hide them so only JSON
# is piped to the generator.
readme-tools:
    uv run fastmcp list tests/mcp-demo.json --prompts --json 2>/dev/null \
      | uv run scripts/generate_tools_readme.py

# Call a tool with no parameters (e.g.: just call kubecost_get_infra_costs)
call TOOL:
    fastmcp call {{MCP_CONFIG}} {{TOOL}}

# Call a tool with JSON input (e.g.: just call-json my_tool '{"key": "val"}')
call-json TOOL INPUT:
    fastmcp call {{MCP_CONFIG}} {{TOOL}} --input-json '{{INPUT}}'

# Run get_kubecost_cost_comparison for yesterday-vs-day-before and last-7-days-vs-month-ago
cost-comparison AGGREGATE="namespace":
    scripts/cost_comparison-day.sh {{MCP_CONFIG}} {{AGGREGATE}}

# ── Client Setup ───────────────────────────────────────────────────────────────

# Install MCP config for other agents
install-bob:
    fastmcp install mcp-json ./config/fastmcp.json --project $PWD --env-file .env

# Install MCP config for Claude Desktop
install-claude:
    fastmcp install claude-desktop ./config/fastmcp.json --project $PWD --env-file .env

# ── Linting ────────────────────────────────────────────────────────────────────

# Dead-code scan. Uses [tool.vulture] in pyproject.toml (FastMCP decorator
# ignore + Pydantic field whitelist). Pass no extra paths — they replace config.
vulture:
    uv run vulture

# Serve the OAuth consent screen against a stub IdP and assert it is Kubecost-branded.
# The only local path that reaches this page — STDIO serves no HTTP routes.
check-consent-branding *ARGS:
    uv run scripts/check_consent_branding.py {{ARGS}}

# Spell-check all prose and source files tracked by cspell
spell-check:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v cspell &>/dev/null; then
        echo "cspell not found — skipping spell check (install with: npm install -g cspell)"
        exit 0
    fi
    cspell lint --no-progress --config .github/cspell.json \
        README.md \
        docs/ \
        "src/mcp_kubecost/tools/**" \
        "src/mcp_kubecost/skills/**" \
        "src/mcp_kubecost/prompts/**" \
        "src/mcp_kubecost/domain/kubecost/**" \
        charts/mcp-kubecost/README.md \
        charts/mcp-kubecost/Chart.yaml \
        charts/mcp-kubecost/values.yaml \
        charts/mcp-kubecost/values.schema.json \
        "charts/mcp-kubecost/templates/**"

update-dependencies:
    just setup-dev-environment
    ./scripts/update_dependencies.py
    uv sync --all-extras --active --upgrade

# public demo test, just to help with inspector cli syntax
test-demo:
    npx @modelcontextprotocol/inspector \
    --cli https://mcp.demo.kubecost.cloud/mcp \
    --method tools/call --tool-name get_savings_overview

## IBM internal SSO testing:
test-sso:
    npx @modelcontextprotocol/inspector \
    --cli https://ibm-sso.demo.kubecost.cloud/mcp \
    --method tools/call --tool-name get_savings_overview
