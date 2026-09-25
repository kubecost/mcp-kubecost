# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Package, Helm chart, and git tags currently use **0.17.0** (`v0.17.0`). That is the GA baseline. Later 1.x versions belong under `[Unreleased]` until they are tagged.

## [Unreleased]

### Added

- Kubecost filter expressions for workload costs and cost comparison, with a consistent `applied_filter` echo across allocation and container sizing responses
### Changed

- **BREAKING (response contract 10.0 → 11.0): tool response fields are now snake_case.** Upgrading to FastMCP 4 changed the default Pydantic serialization from `by_alias=True` to `by_alias=False`, so row fields are emitted under their field names instead of the camelCase Kubecost API aliases. Rename the keys you read:
  - `get_kubecost_workload_costs` rows — `totalCost` → `total_cost`, `cpuCost` → `cpu_cost`, `cpuCostIdle` → `cpu_cost_idle`, `ramCost` → `ram_cost`, `ramCostIdle` → `ram_cost_idle`, `networkCost` → `network_cost`, `pvCost` → `pv_cost`, `gpuCost` → `gpu_cost`, `gpuCostIdle` → `gpu_cost_idle`, `loadBalancerCost` → `load_balancer_cost`, `sharedCost` → `shared_cost`, `cpuIdlePct` → `cpu_idle_pct`, `ramIdlePct` → `ram_idle_pct`, `gpuIdlePct` → `gpu_idle_pct`, `totalIdlePct` → `total_idle_pct`
  - `get_container_savings_recommendations` rows — `clusterID` → `cluster_id`, `controllerKind` → `controller_kind`, `controllerName` → `controller_name`, `containerName` → `container_name`, `monthlySavings_total` → `monthly_savings_total`, `monthlySavings_cpu` → `monthly_savings_cpu`, `monthlySavings_memory` → `monthly_savings_memory`, `Recommended_cpuInMilliCores` → `recommended_cpu_in_milli_cores`, `Recommended_memoryInMiB` → `recommended_memory_in_mib`, `current_cpuInMilliCores` → `current_cpu_in_milli_cores`, `current_memoryInMiB` → `current_memory_in_mib`, `currentEfficiency_cpu` → `current_efficiency_cpu`, `currentEfficiency_memory` → `current_efficiency_memory`, `currentEfficiency` → `current_efficiency`, `AvgUsage_cpuInMilliCores` → `avg_usage_cpu_in_milli_cores`, `AvgUsage_memoryInMiB` → `avg_usage_memory_in_mib`, `MaxUsage_cpuInMilliCores` → `max_usage_cpu_in_milli_cores`, `MaxUsage_memoryInMiB` → `max_usage_memory_in_mib`
  - `get_abandoned_workloads` rows — `clusterId` → `cluster_id`, `ingressBytesPerSecond` → `ingress_bytes_per_second`, `egressBytesPerSecond` → `egress_bytes_per_second`, `monthlySavings` → `monthly_savings`
  - `get_resource_quota_recommendations` resource changes — `used` → `current_quota` and `recommended` → `recommended_quota`. This pair also renames *semantically*: the API's `used` is the namespace's existing ResourceQuota cap, not observed pod usage.
- Upgraded to FastMCP 4 (`fastmcp>=4.0.9,<5.0`, from `>=3.4.7,<4.0`), which moves the server onto MCP Python SDK 2.x and the sessionless `2026-07-28` protocol with protocol-era negotiation. Tool, prompt, and resource names and parameters are unchanged.
- `FASTMCP_TELEMETRY_MODE` is now read by FastMCP itself as well as by this server's entrypoint. Both accept `off` and `native`, so existing values keep working, but `off` now also disables FastMCP's own native MCP spans in addition to skipping the `opentelemetry-instrument` wrapper. `propagation_only` is newly accepted by FastMCP.
- Default container image tag is now the git/ICR tag with a leading `v` (`icr.io/kubecost/mcp-kubecost:vX.Y.Z`). Helm chart `version` is still unprefixed SemVer. The unprefixed image tag is no longer published on new releases.

### Fixed

- Container image now carries a populated CA trust store. The runtime rootfs installs `ca-certificates` with rpm scriptlets disabled, so `update-ca-trust extract` never ran and its generated bundles were absent, leaving `/etc/pki/tls/cert.pem` dangling and OpenSSL with zero trusted anchors. This surfaced only after the FastMCP 4 upgrade, because FastMCP 4 verifies TLS against the system trust store (via `truststore`) where FastMCP 3 used certifi's bundled PEM — OIDC discovery against a publicly trusted issuer failed with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` and the server crash-looped. Private CAs mounted into `/etc/pki/ca-trust/source/anchors` are honoured.
- OIDC startup failures now explain the failure that actually occurred. A TLS verification failure points at the trust store and private-CA mounting, and a connection failure points at DNS and pod egress, instead of both claiming the issuer returned an HTML login page.

## [0.17.0] - 2026-09-16

General availability of the Kubecost FinOps MCP server: a read-only MCP interface over Kubecost cost allocation and optimization APIs.

### Added

- Eleven read-only MCP tools for workload costs, cost comparison, container request-sizing candidates, abandoned workloads, savings overview, PV and local-disk sizing, cluster rightsizing, unclaimed volumes, and resource-quota recommendations
- MCP prompts, skills, and resources for client workflows (including container sizing profiles and the rightsizing review)
- Helm chart for Kubernetes, with OIDC and API-key authentication
- STDIO and Streamable HTTP transports, plus `/health` and `/version` HTTP routes
- Optional OpenTelemetry extra, installed in the container image

[Unreleased]: https://github.com/kubecost/mcp-kubecost/compare/v0.17.0...HEAD
[0.17.0]: https://github.com/kubecost/mcp-kubecost/releases/tag/v0.17.0
