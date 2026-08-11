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
_update_version_refs:
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION=$(uv version --short)
    echo "Updating version references to $VERSION..."

    # Update the canonical Helm chart image tag.
    just _sed 's|^  tag:.*|  tag: "'"$VERSION"'"|' charts/mcp-kubecost/values.yaml
    echo "Updated charts/mcp-kubecost/values.yaml image tag to $VERSION"

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

docker-build-push:
    #!/usr/bin/env bash
    set -euo pipefail
    # just test-basic
    uv version --bump patch
    VERSION=$(uv version --short)
    REGION=us-east-1
    BASE_REGISTRY_PATH=297945954695.dkr.ecr.$REGION.amazonaws.com
    CONTAINER_IMAGE=$BASE_REGISTRY_PATH/mcp-kubecost:$VERSION
    echo "Building and pushing base image..."
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      -t $CONTAINER_IMAGE \
      -f Dockerfile \
      --push .
    just _update_version_refs
    echo ""
    echo "✅ Image pushed to $CONTAINER_IMAGE"

# ── Environment ────────────────────────────────────────────────────────────────
# Install dependencies and create virtual environment
setup:
    uv venv --clear
    uv sync --all-extras --active

# ── Testing ────────────────────────────────────────────────────────────────────
test:
    uv run pytest

test-all:
    uv run pytest -m ""

# ── Development Server ─────────────────────────────────────────────────────────

# Start FastMCP dev server with browser inspector UI
dev:
    fastmcp dev inspector

# Start FastMCP as HTTP server on port 9000 (for debugging with logs)
serve:
    fastmcp run ./fastmcp-http.json

# ── FastMCP CLI ────────────────────────────────────────────────────────────────

# Inspect the MCP server (list tools, prompts, resources)
inspect:
    fastmcp inspect

# List all tools and prompts
list:
    fastmcp list {{MCP_CONFIG}} --prompts

# Regenerate README tools + prompts tables from live FastMCP list
readme-tools:
    uv run fastmcp list {{MCP_CONFIG}} --prompts --json 2>/dev/null \
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

# Install MCP config for Bob (IBM Bob Shell)
install-bob:
    fastmcp install mcp-json ./fastmcp.json --project $PWD --env-file .env

# Install MCP config for Claude Desktop
install-claude:
    fastmcp install claude-desktop ./fastmcp.json --project $PWD --env-file .env
