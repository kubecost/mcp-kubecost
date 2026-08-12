# Kubecost FinOps MCP Server<!-- omit in toc -->

A read-only MCP server that connects your AI assistant to [Kubecost](https://www.kubecost.com/) so you can ask natural-language questions about Kubernetes cloud costs and savings — no dashboards, no SQL.

- [Who This Is For](#who-this-is-for)
- [Examples of What You Can Ask](#examples-of-what-you-can-ask)
  - [Cost Visibility](#cost-visibility)
  - [Savings Opportunities](#savings-opportunities)
- [Tools](#tools)
- [Authentication to Kubecost](#authentication-to-kubecost)
- [Telemetry (OpenTelemetry)](#telemetry-opentelemetry)
  - [Current behavior (FastMCP 3.4.x)](#current-behavior-fastmcp-34x)
  - [FastMCP 4.0 (when GA)](#fastmcp-40-when-ga)
- [Quick Start](#quick-start)

## Who This Is For

- **FinOps Practitioners** who want to answer questions about their Kubernetes costs and savings with natural language questions.
- **Engineering managers** who need spend summaries and savings reports on demand.
- **Platform engineers** who want cost visibility in their IDE or AI chats without switching to the Kubecost UI.

> [!NOTE]
> As of version 1.x, the server is read-only. It never modifies your cluster or Kubecost configuration.

## Examples of What You Can Ask

### Cost Visibility

- "What are my top 10 cost drivers over the last 30 days?"

- "Why have my costs changed this month? Focus on the most expensive namespaces."

### Savings Opportunities

- "Where are my biggest savings opportunities?"

- "What risks are there to adopting the savings recommendations?"

- "Show me abandoned workloads — pods that are running but appear idle."

## Tools

**11 tools** — all read-only, all structured for LLM consumption:

| Tool | Description |
|------|-------------|
| `kubecost_list_windows` | List the valid time windows for Kubecost cost queries. |
| `get_kubecost_workload_costs` | Return Kubernetes cost allocation from Kubecost grouped by chosen dimensions. |
| `get_kubecost_cost_comparison` | Compare Kubernetes cost allocation between two time windows to find cost changes and spikes. |
| `get_container_savings_recommendations` | Return Kubernetes container rightsizing recommendations and potential savings. |
| `get_abandoned_workloads` | Return pods with abnormally low network traffic — likely abandoned workloads. |
| `get_savings_overview` | Return a ranked summary of all Kubecost savings categories. |
| `get_pv_sizing_recommendations` | Return PersistentVolumeClaim right-sizing recommendations ranked by monthly savings. |
| `get_local_disk_savings` | Return underutilized node-local disk savings recommendations. |
| `get_cluster_rightsizing_recommendations` | Return node group scale-in/scale-out/instance-type recommendations for a cluster. |
| `get_unclaimed_volumes` | Return PersistentVolumes that are provisioned but not bound to any PVC. |
| `get_resource_quota_recommendations` | Return namespace-level ResourceQuota sizing recommendations. |

**11 prompts** — step-by-step workflows your assistant can follow:

| Prompt | Description |
|--------|-------------|
| `container_rightsizing_guide` | Explain how to properly size Kubernetes container CPU and memory requests. |
| `explore_container_savings` | Start a guided container rightsizing exploration. Presents choices step-by-step. |
| `container_savings_window_help` | Explain the time window options for the container savings tool. |
| `container_savings_filter_help` | Explain the min_monthly_savings filter for container savings. |
| `explore_costs` | Start a guided Kubernetes cost exploration. Presents choices step-by-step. |
| `explore_cost_comparison` | Start a guided cost anomaly / spike investigation using period-over-period comparison. |
| `top_spenders` | Show top cost drivers across clusters and namespaces for a given window. |
| `cost_trend` | Show daily cost trend for a given aggregation dimension. |
| `explore_abandoned_workloads` | Start a guided abandoned-workload investigation. Walks the user through threshold and scope choices. |
| `optimization` | Guidance for rightsizing resources and diagnosing Kubernetes cost anomalies. |
| `kubecost_cost_allocation` | Guidance for investigating Kubernetes cluster costs and container allocation. |

### Container sizing profiles

`get_container_savings_recommendations` accepts a `profile` that bundles the sizing knobs, so you can ask for "production sizing" instead of picking quantiles by hand:

| Profile | Best for | Window | Quantiles | Target utilization |
|--------|----------|--------|-----------|--------------------|
| `high-availability` | Latency-sensitive APIs, stateful services | 30d | P95 CPU / P99 RAM | 0.50 |
| `production` (default) | General workloads, first pass | 15d | P80 CPU / P95 RAM | 0.65 |
| `development` | Dev/test, batch, cost-reduction sprints | 15d | P80 CPU / P95 RAM | 0.80 |

Target utilization is the utilization the new request should run at — Kubecost computes `recommended = usage / target`. **Lower means a bigger request and more headroom**, so `high-availability` at 0.50 is the safest and `development` at 0.80 the most aggressive. Memory is not compressible, so an undersized memory request causes OOM kills rather than throttling — don't run `development` against production workloads.

Profiles never filter results. Pass `min_monthly_savings=5.0` to hide small opportunities, or a negative value to keep undersized workloads. Any explicit parameter overrides the profile. Ask for the `container_rightsizing_guide` prompt for the full methodology.

## Authentication to Kubecost

By default, the server calls Kubecost unauthenticated. This is fine for testing or when another layer of authentication is in place.

Kubecost Enterprise can be configured with SAML/OIDC authentication. When enabled, the MCP server will require an API key to be sent in the `X-API-KEY` request header or the `KUBECOST_API_KEY` environment variable.

The API key is sent to Kubecost as an `X-API-KEY` request header. Two sources feed it, header first:

| Source | Scope |
|--------|-------|
| `X-API-KEY` header on the incoming MCP request | Per request — HTTP transport only |
| `KUBECOST_API_KEY` environment variable | Process-wide fallback |

Set `REQUIRE_CLIENT_API_KEY=true` to reject HTTP requests that arrive without the header. The check runs before the environment fallback, so a configured `KUBECOST_API_KEY` will not satisfy it. STDIO runs are never gated, since a STDIO client has no way to send headers.

## Telemetry (OpenTelemetry)

HTTP deployments can export traces via OpenTelemetry. The Docker image includes the OTEL SDK by default and httpx/starlette auto-instrumentation; export is gated at runtime. This can be removed by omitting `--extra otel` in [Dockerfile](Dockerfile).

Outside Docker, tracing is an **optional extra** — install it with `uv sync --extra otel` or `pip install 'mcp-kubecost[otel]'`. Without it the server runs normally; enabling `FASTMCP_TELEMETRY_MODE` logs a warning and starts untraced rather than failing.

### Current behavior (FastMCP 3.4.x)

| Variable | Role |
|----------|------|
| `FASTMCP_TELEMETRY_MODE` | Process-wide switch. `off` (the default when unset) runs bare `fastmcp`. Any other value (e.g. `native`) wraps the process with `opentelemetry-instrument`. |
| `OTEL_SERVICE_NAME` | Service name on exported spans. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint. Required when telemetry is enabled (not set in the image). |
| `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` | Set both to `none` when the endpoint is a traces-only backend — see below. |

When enabled, traces include FastMCP MCP operation spans (tools, prompts, resources) plus HTTP client/server spans from auto-instrumentation. Set these in the Helm chart's `config` values or `.env` — see [`.env.example`](.env.example) and [`charts/mcp-kubecost/values.yaml`](charts/mcp-kubecost/values.yaml).

> [!NOTE]
> On FastMCP 3.4.x, `FASTMCP_TELEMETRY_MODE` is **not** read by FastMCP itself. This server reuses that name so the same env var will keep working after a FastMCP 4 upgrade. STDIO local runs are not wrapped unless you invoke `opentelemetry-instrument` yourself.

> [!IMPORTANT]
> `opentelemetry-distro` turns on **all three** signals by default — traces, metrics, and logs all
> go to `OTEL_EXPORTER_OTLP_ENDPOINT`. Traces-only backends such as Tempo and Jaeger implement only
> the OTLP `TraceService`, so metrics and logs fail there with
> `StatusCode.UNIMPLEMENTED: unknown service opentelemetry.proto.collector.metrics.v1.MetricsService`.
> When pointing at one of those, also set:
>
> ```
> OTEL_METRICS_EXPORTER=none
> OTEL_LOGS_EXPORTER=none
> ```
>
> Leave them unset only if the endpoint is a full collector that accepts every signal.

### FastMCP 4.0 (when GA)

FastMCP 4 introduces native `FASTMCP_TELEMETRY_MODE` semantics:

| Mode | FastMCP MCP spans | Trace context in `_meta` |
|------|-------------------|--------------------------|
| `native` (default in FastMCP) | Emitted | Propagated |
| `propagation_only` | Suppressed | Propagated (for external MCP-aware instrumentors) |
| `off` | Suppressed | Untouched |

After upgrading to FastMCP 4:

- `FASTMCP_TELEMETRY_MODE` will control **both** this server’s OTEL SDK wrapper **and** FastMCP’s own span emission.
- Prefer `native` for normal deployments; use `propagation_only` only if another layer already owns MCP spans.
- Keep setting `OTEL_EXPORTER_OTLP_ENDPOINT` (and related `OTEL_*` vars) whenever you want spans exported — FastMCP still only uses the OpenTelemetry API until an SDK is configured.

## Quick Start

**Point at the public demo** (no credentials needed):

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

See [DEVELOPMENT.md](DEVELOPMENT.md) for build, test, and deployment instructions.
