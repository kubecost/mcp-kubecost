# MCP Kubecost Development Guide<!-- omit in toc -->

- [AI Coding Agents](#ai-coding-agents)
- [Setup](#setup)
- [Configuration](#configuration)
- [Run](#run)
- [Readme Generation](#readme-generation)
- [Testing and Quality](#testing-and-quality)
- [Docker / Kubernetes](#docker--kubernetes)
- [Telemetry](#telemetry)
  - [On the "beta" version numbers](#on-the-beta-version-numbers)
- [Security](#security)

## AI Coding Agents

See [`AGENTS.md`](../../AGENTS.md) for the project structure and architecture map, response-limit patterns, tool authoring rules, the shared `tools/_common.py` contract, and known gaps. It is the single source of truth for where code lives — this guide covers how to set up, run, and ship.

## Setup

```bash
uv venv .venv && uv sync --extra dev
```

Invoke the venv's interpreter directly (`.venv/bin/python`, `.venv/bin/pytest`); there is no need to activate it.

Tracing lives in a separate `otel` extra — add `--extra otel` (or use `just setup-dev-environment`, which installs all extras) if you are working on telemetry. See [the Telemetry section](#telemetry).

## Configuration

Copy [`.env.example`](../../.env.example) to `.env` and set any variable needed to change defaults. `KUBECOST_BASE_URL` is the only required one. Auth and TLS variables are documented in [`README.md`](../auth/README.md).

## Run

**STDIO** (local/desktop):

```bash
uv run mcp-kubecost
# or
uv run python -m mcp_kubecost.server
# or
uv run fastmcp run config/fastmcp.json
```

**HTTP** (service deployment, port 3030):

```bash
uv run fastmcp run config/fastmcp-http.json   # or: just serve
```

Install or upgrade the application with the Helm chart:

```bash
helm upgrade --install mcp-kubecost ./charts/mcp-kubecost \
  --namespace mcp-kubecost --create-namespace
```

## Readme Generation

This project uses the vscode extension `Markdown All in One` for formatting and generating the table of contents and in the README.md files.

In addition, there is a script that can be run to regenerate the README tools and prompts tables from the live MCP server.

```bash
just readme-tools
```

> [!WARNING]
> This rewrites **everything** in README.md between `## Tools` and `## Quick Start`, not just the tables. Any hand-written section in that range is deleted. Review the diff before keeping it.

## Testing and Quality

```bash
.venv/bin/pytest                       # unit suite (integration tests deselected by default)
.venv/bin/pytest -m integration        # integration only — hits https://demo.kubecost.xyz
.venv/bin/pytest -m ""                 # everything, as CI runs it
.venv/bin/ruff format .                # format
.venv/bin/ruff check . --fix           # lint
.venv/bin/pyrefly check                # type check
just vulture                           # blocking dead-code scan
uvx pre-commit run --config .github/pre-commit-config-ci.yaml --all-files
```

Run `ruff format`, `ruff check --fix`, and `pyrefly check` after every Python edit. Since this guide now lives under [`docs/development/`](../development/), run the pre-commit command from the repository root.

**Pyrefly** is the project's type checker, configured under `[tool.pyrefly]` in `pyproject.toml` and enforced in CI at the `basic` preset (0 errors). The `[tool.basedpyright]` block is retained only for IDEs without Pyrefly language-server support; it is not an enforced gate, and Pyrefly wins if the two disagree.

CI runs on Python 3.12 ([`ci.yml`](../../.github/workflows/ci.yml)), matching `requires-python`. See [`pre-commit-checks.md`](pre-commit-checks.md) for local vs CI hook tiers.

## Docker / Kubernetes

```bash
just docker-build-run       # build the image locally and run it on port 3030
just docker-build-push      # bump the patch version, build multi-arch, push to ECR,
                            # and update the Helm chart image tag
```

The Docker image's `CMD` is `/app/.venv/bin/mcp-kubecost-http` ([`otel_entrypoint.py`](../../src/mcp_kubecost/otel_entrypoint.py)), which wraps the server with `opentelemetry-instrument` unless `FASTMCP_TELEMETRY_MODE=off`, and listens on port 3030.

## Telemetry

OpenTelemetry is an optional extra (`uv sync --extra otel`). The Docker image installs it explicitly, so deployed behaviour is unchanged; plain installs skip ~13 packages. Runtime export is still gated by `FASTMCP_TELEMETRY_MODE` — see [`README.md`](../../README.md#telemetry-experimental) for the variables.

`otel_entrypoint.py` degrades rather than failing: with telemetry enabled but the extra missing, it warns on stderr and starts the server untraced.

### On the "beta" version numbers

`opentelemetry-distro` and the instrumentation packages are versioned `0.65b0` and classified beta. That is a versioning policy, not a maturity signal, and there is no non-beta alternative:

- OpenTelemetry Python runs two permanent tracks. Its `RELEASING.md`: *"The version number for unstable components in the `main` branch is always `0.Yb0`."* Every instrumentation package sits on that track.
- Stable `opentelemetry-sdk` 1.44.0 itself hard-pins `opentelemetry-semantic-conventions==0.65b0`, so no OTel Python setup avoids a 0.x package.
- FastMCP already depends on `opentelemetry-api` (stable 1.x) and emits its own MCP spans through it.

Dropping the distro would forfeit automatic httpx spans on every Kubecost API call — the most useful signal this server produces — without escaping the 0.x track.

## Security

Authentication, OIDC, API keys, and pod hardening: [`README.md`](../auth/README.md).

- Never hardcode credentials; use environment variables only.
- CI includes a lightweight secret-pattern scan and large-file safety check.
- The Docker image strips `pip` (and `ensurepip`) from the uv-managed CPython before the distroless copy. The runtime never installs packages, and leaving pip in the image makes Trivy report its vendored `msgpack` 1.1.2 and `setuptools` 70.3.0 (`CVE-2025-47273`, `CVE-2026-59890`). Those are not project dependencies — they are not in `uv.lock`.
