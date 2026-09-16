# Task Runner
# https://github.com/casey/just

# Enforce bash/zsh strict mode
set shell := ["bash", "-euo", "pipefail", "-c"]

# consistency across all platforms and development environments
unexport VIRTUAL_ENV
export UV_FROZEN := "1"
export UV_PYTHON_PREFERENCE := "only-managed"

MCP_CONFIG := "./.agents/mcp.json"

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

# build and run docker image on por 3030 for integration tests
docker-build-run:
    #!/usr/bin/env bash
    set -euo pipefail
    docker buildx build --load --progress plain \
        -t mcp-kubecost \
        -f ./Dockerfile .
    echo ""
    echo -e "\033[33m  Image built. Wait a few seconds for the server to start.\033[0m"
    echo -e "\033[33m  Then open a new terminal and run your tests. Example command:\033[0m"
    echo -e "\033[33m  fastmcp call ./config/mcp-http.json get_container_savings_recommendations --input-json '{\"window\": \"15d\"}'\033[0m"
    docker run --rm \
      --name mcp-kubecost \
      -p 3030:3030 \
      -e KUBECOST_BASE_URL=http://host.docker.internal:9090  \
      -e FASTMCP_LOG_LEVEL=DEBUG \
      mcp-kubecost

# Install dependencies and create virtual environment
setup-dev-environment:
    uv venv .venv --clear
    uv sync --all-extras

# Start FastMCP dev server with browser inspector UI
dev-inspector:
    fastmcp dev inspector

# Start FastMCP as HTTP server on port 3030 (for debugging with logs)
serve:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -z "${KUBECOST_BASE_URL:-}" ]]; then
        echo "Error: KUBECOST_BASE_URL is not set." >&2
        echo "Set it in your environment or copy .env.example to .env and fill it in." >&2
        exit 1
    fi
    fastmcp run ./config/fastmcp-http.json

# Inspect the MCP server (counts for tools, prompts, resources)
inspect:
    fastmcp inspect fastmcp.json

# List all tools and prompts
list:
    fastmcp list {{MCP_CONFIG}} --prompts

# Regenerate README sections for tools + prompts tables from live FastMCP list
readme-tools:
    uv run fastmcp list tests/mcp-demo.json --prompts --json 2>/dev/null \
      | uv run scripts/generate_tools_readme.py

# Call a tool with no parameters (e.g.: just call kubecost_get_infra_costs)
call TOOL:
    fastmcp call {{MCP_CONFIG}} {{TOOL}}

# Call a tool with JSON input (e.g.: just call-json my_tool '{"key": "val"}')
call-json TOOL INPUT:
    fastmcp call {{MCP_CONFIG}} {{TOOL}} --input-json '{{INPUT}}'

# Call every configured tool and prompt; CLUSTER overrides automatic cluster discovery.
call-all CLUSTER="":
    #!/usr/bin/env bash
    set -euo pipefail
    .venv/bin/python scripts/call_all_mcp.py \
        --config "{{MCP_CONFIG}}" \
        --cluster "{{CLUSTER}}" \
        2>/dev/null

# Run get_kubecost_cost_comparison for yesterday-vs-day-before and last-7-days-vs-month-ago
cost-comparison AGGREGATE="namespace":
    scripts/cost_comparison-day.sh {{MCP_CONFIG}} {{AGGREGATE}}

# ── AI Client Setup ───────────────────────────────────────────────────────────────

# Install MCP config for other agents
install-mcp-json:
    fastmcp install mcp-json ./fastmcp.json --project $PWD --env-file .env

# Install MCP config for Claude Desktop
install-claude:
    fastmcp install claude-desktop ./fastmcp.json --project $PWD --env-file .env

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

# Automatically check for outdated dependencies and update pyproject.toml
update-dependencies:
    just setup-dev-environment
    env -u UV_FROZEN ./scripts/update_dependencies.py
    env -u UV_FROZEN uv sync --all-extras

build:
    uv sync --extra dev

# Run tests
test: build
    uv run --frozen pytest -xvs tests

# Full pytest suite, same selector CI uses (`-m ""`)
test-all: build
    #!/usr/bin/env bash
    set -euo pipefail
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://demo.kubecost.xyz}"
    export MCP_KUBECOST_TARGET="${MCP_KUBECOST_TARGET:-tests/mcp-demo.json}"
    uv run pytest -m "" -xvs

# Live integration tests only. Default target is tests/mcp-demo.json (same as the CI `integration` job).
test-integration: build
    #!/usr/bin/env bash
    set -euo pipefail
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://demo.kubecost.xyz}"
    export MCP_KUBECOST_TARGET="${MCP_KUBECOST_TARGET:-tests/mcp-demo.json}"
    uv run pytest -m integration

# Integration tests against an already-running HTTP server on port 3030 (`just serve` or `just docker-build-run`)
test-integration-http-3030:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! curl -fsS --max-time 2 "http://localhost:3030/health" >/dev/null; then
        echo -e "\033[33m  Error: FastMCP server is not running on port 3030.\033[0m" >&2
        echo -e "\033[33m  Run 'just serve' or 'just docker-build-run' to start the FastMCP server.\033[0m" >&2
        exit 1
    fi
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://demo.kubecost.xyz}"
    export MCP_KUBECOST_TARGET="${MCP_KUBECOST_TARGET:-http://localhost:3030/mcp}"
    uv run pytest -m integration

# Mirror the `test` job in .github/workflows/ci.yml. Pass --no-auto-format to skip the formatter prompt.
[positional-arguments]
test-ci-locally *args:
    #!/usr/bin/env bash
    set -euo pipefail
    skip_auto_format=0
    for arg in "$@"; do
        case "$arg" in
            -- | test-ci-locally) ;;
            --no-auto-format) skip_auto_format=1 ;;
            *)
                echo "Unknown argument: $arg" >&2
                echo "Usage: just test-ci-locally [--no-auto-format]" >&2
                exit 1
                ;;
        esac
    done
    export KUBECOST_BASE_URL="${KUBECOST_BASE_URL:-https://demo.kubecost.xyz}"
    export MCP_KUBECOST_TARGET="${MCP_KUBECOST_TARGET:-tests/mcp-demo.json}"
    uv sync --extra dev

    if [[ "$skip_auto_format" -eq 1 ]]; then
        echo -e "\033[33m  Skipping just auto-format (--no-auto-format)\033[0m"
    elif [[ -t 0 ]]; then
        read -r -p $'\033[33m  Run just auto-format? [y/N] \033[0m' reply
        if [[ "${reply}" =~ ^[Yy]$ ]]; then
            just auto-format
        else
            echo -e "\033[33m  Skipping just auto-format\033[0m"
        fi
    else
        echo -e "\033[33m  Skipping just auto-format (non-interactive)\033[0m"
    fi
    echo -e "\033[33m  Running pyrefly check\033[0m"
    just pyrefly-check
    echo -e "\033[33m  Running vulture\033[0m"
    just vulture
    echo -e "\033[33m  Running check-consent-branding --check\033[0m"
    just check-consent-branding --check
    echo -e "\033[33m  Running pytest -m \"\" (unit + integration if a target is present)\033[0m"
    uv run pytest --cov=mcp_kubecost --cov-report=xml -m ""
    echo -e "\033[33m  Running repo safety checks\033[0m"
    uv run python scripts/pre_commit_hooks/check_repo_safety.py


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

# run pyrefly check
pyrefly-check:
    uv run pyrefly check

# Dead-code scan. Uses [tool.vulture] in pyproject.toml
vulture:
    uv run vulture

# Run the CI pre-commit config (ruff, yaml, safety). Stage or stash unstaged edits first so the diff stays reviewable.
auto-format:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! git diff --quiet; then
        echo -e "\033[33m  Error: unstaged changes detected. Stash or stage them before running auto-format.\033[0m" >&2
        exit 1
    fi
    uv run pre-commit run --config .github/pre-commit-config-ci.yaml --all-files
