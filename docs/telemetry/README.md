# Telemetry in Kubecost MCP

This MCP server can be configured to export traces via OpenTelemetry. The container image includes the OTEL SDK by default with FastMCP's auto-instrumentation; export is gated at runtime.

This feature is considered experimental.

### Current behavior (FastMCP 3.4.x)

| Variable | Role |
|----------|------|
| `FASTMCP_TELEMETRY_MODE` | Process-wide switch. `off` (the default when unset) runs bare `fastmcp`. Any other value (e.g. `native`) wraps the process with `opentelemetry-instrument`. |
| `OTEL_SERVICE_NAME` | Service name on exported spans. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint. Required when telemetry is enabled (not set in the image). |
| `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` | Set both to `none` when the endpoint is a traces-only backend — see below. |

When enabled, traces include FastMCP MCP operation spans (tools, prompts, resources) plus HTTP client/server spans from auto-instrumentation. Set these in the Helm chart's `config` values or `.env` — see [`.env.example`](../../.env.example) and [`charts/mcp-kubecost/values.yaml`](../../charts/mcp-kubecost/values.yaml).

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