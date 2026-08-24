# Kubecost FinOps MCP Server<!-- omit in toc -->

A read-only MCP server that connects your AI assistant to [Kubecost](https://www.kubecost.com/) so you can ask natural-language questions about Kubernetes cloud costs and savings — no dashboards, no SQL.

- [Who This Is For](#who-this-is-for)
- [Examples of What You Can Ask](#examples-of-what-you-can-ask)
  - [Cost Visibility](#cost-visibility)
  - [Savings Opportunities](#savings-opportunities)
- [Tools](#tools)
  - [Container sizing profiles](#container-sizing-profiles)
- [Telemetry (experimental)](#telemetry-experimental)
- [Installation / Helm Chart](#installation--helm-chart)
  - [Authentication Options](#authentication-options)
- [Development](#development)
- [License](#license)

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

| Tool                                      | Description                                                                                  |
| ----------------------------------------- | -------------------------------------------------------------------------------------------- |
| `kubecost_list_windows`                   | List the valid time windows for Kubecost cost queries, each resolved to real dates.          |
| `get_kubecost_workload_costs`             | Return Kubernetes cost allocation from Kubecost grouped by chosen dimensions.                |
| `get_kubecost_cost_comparison`            | Compare Kubernetes cost allocation between two time windows to find cost changes and spikes. |
| `get_container_savings_recommendations`   | Return Kubernetes container rightsizing recommendations and potential savings.               |
| `get_abandoned_workloads`                 | Return pods with abnormally low network traffic — likely abandoned workloads.                |
| `get_savings_overview`                    | Return a ranked summary of all Kubecost savings categories.                                  |
| `get_pv_sizing_recommendations`           | Return PersistentVolumeClaim right-sizing recommendations ranked by monthly savings.         |
| `get_local_disk_savings`                  | Return underutilized node-local disk savings recommendations.                                |
| `get_cluster_rightsizing_recommendations` | Return node group scale-in/scale-out/instance-type recommendations for a cluster.            |
| `get_unclaimed_volumes`                   | Return PersistentVolumes that are provisioned but not bound to any PVC.                      |
| `get_resource_quota_recommendations`      | Return namespace-level ResourceQuota sizing recommendations.                                 |

**11 prompts** — step-by-step workflows your assistant can follow:

| Prompt                          | Description                                                                                          |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `container_rightsizing_guide`   | Explain how to properly size Kubernetes container CPU and memory requests.                           |
| `explore_container_savings`     | Start a guided container rightsizing exploration. Presents choices step-by-step.                     |
| `container_savings_window_help` | Explain the time window options for the container savings tool.                                      |
| `container_savings_filter_help` | Explain the min_monthly_savings filter for container savings.                                        |
| `explore_costs`                 | Start a guided Kubernetes cost exploration. Presents choices step-by-step.                           |
| `explore_cost_comparison`       | Start a guided cost anomaly / spike investigation using period-over-period comparison.               |
| `top_spenders`                  | Show top cost drivers across clusters and namespaces for a given window.                             |
| `cost_trend`                    | Show daily cost trend for a given aggregation dimension.                                             |
| `explore_abandoned_workloads`   | Start a guided abandoned-workload investigation. Walks the user through threshold and scope choices. |
| `optimization`                  | Guidance for rightsizing resources and diagnosing Kubernetes cost anomalies.                         |
| `kubecost_cost_allocation`      | Guidance for investigating Kubernetes cluster costs and container allocation.                        |

### Container sizing profiles

`get_container_savings_recommendations` accepts a `profile` that bundles the sizing knobs, so you can ask for "production sizing" instead of picking quantiles by hand:

| Profile                | Best for                                  | Window | Quantiles         | Target utilization |
| ---------------------- | ----------------------------------------- | ------ | ----------------- | ------------------ |
| `high-availability`    | Latency-sensitive APIs, stateful services | 30d    | P95 CPU / P99 RAM | 0.50               |
| `production` (default) | General workloads, first pass             | 15d    | P80 CPU / P95 RAM | 0.65               |
| `development`          | Dev/test, batch, cost-reduction sprints   | 15d    | P80 CPU / P95 RAM | 0.80               |

Target utilization is the utilization the new request should run at — Kubecost computes `recommended = usage / target`. **Lower means a bigger request and more headroom**, so `high-availability` at 0.50 is the safest and `development` at 0.80 the most aggressive. Memory is not compressible, so an undersized memory request causes OOM kills rather than throttling — don't run `development` against production workloads.

Profiles never filter results. Pass `min_monthly_savings=5.0` to hide small opportunities, or a negative value to keep undersized workloads. Any explicit parameter overrides the profile. Ask for the `container_rightsizing_guide` prompt for the full methodology.

## Telemetry (experimental)

The Kubecost MCP supports sending OpenTelemetry data to your own infrastructure. Please reference the [docs/telemetry/README.md](docs/telemetry/README.md) for more information.

## Installation / Helm Chart

The MCP, by default, is bundled with the Kubecost helm installation. For many, that will be the easiest method for installing the MCP.

This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies. Additional detail can be found in the [helm chart readme](charts/mcp-kubecost/README.md).

### Authentication Options

Authentication, OIDC, API keys, and pod hardening are in [docs/auth/README.md](docs/auth/README.md).


## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for build, test, and deployment instructions. 

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
