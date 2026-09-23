# Kubecost FinOps MCP Server<!-- omit in toc -->

## Why<!-- omit in toc -->

AI tools now enable FinOps practitioners to engage in interactive dialogue to understand their datasets- moving away from countless dashboards that can have tedious interfaces and limited, if any, intelligence.

FInOps executives, who may not know all of the technical terminology behind Kubernetes can now have a conversation with a expert-level Kubernetes engineer+FinOps analyst that can communicate at any level.

## What<!-- omit in toc -->

This MCP server exposes Kubecost’s real-time cost allocation and optimization APIs to your AI assistant. It empowers your assistant to:

- **Synthesize massive datasets:** Condense millions of cloud and container infrastructure metrics into clean, digestible reports.
- **Interpret technical context:** Automatically translate complex Kubernetes terminology into clear, accessible business-value insights.
- **Support interactive deep-dives:** Maintain conversational context so you can continuously interrogate data trends and drill down from high-level cluster overviews into root-cause cost anomalies.

## Who This Is For<!-- omit in toc -->

- **FinOps Practitioners** who want to answer questions about their Kubernetes costs and savings with natural language questions.
- **Engineering managers** who need spend summaries and savings reports on demand.
- **Platform engineers** who want cost visibility in their IDE or AI chats without switching to the Kubecost UI.

## Table of Contents<!-- omit in toc -->

- [How to install](#how-to-install)
- [Connecting an AI assistant](#connecting-an-ai-assistant)
- [Examples of What You Can Ask](#examples-of-what-you-can-ask)
  - [Cost Visibility](#cost-visibility)
  - [Savings Opportunities](#savings-opportunities)
- [Tools](#tools)
  - [Container sizing profiles](#container-sizing-profiles)
- [Telemetry (experimental)](#telemetry-experimental)
- [Authentication Options](#authentication-options)
- [Development](#development)
- [License](#license)

## How to install

For most, the preferred method for installing the MCP is to use the [Kubecost Helm Chart](https://github.com/kubecost/kubecost). It is included by default in Kubecost v3.3+.

This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies. Additional detail can be found in the [mcp-kubecost helm chart readme](charts/mcp-kubecost/README.md).

> [!NOTE]
> The MCP server is read-only. It never modifies your cluster or Kubecost configuration.

## Connecting an AI assistant

Point your assistant at the server's `/mcp` endpoint. [docs/clients/README.md](docs/clients/README.md) has setup for Claude, Claude Code, ChatGPT, and generic `mcpServers` JSON.

## Examples of What You Can Ask

### Cost Visibility

- "What are my top 10 cost drivers over the last 30 days?"

- "Why have my costs changed this month? Focus on the most expensive namespaces."

### Savings Opportunities

- "Where are my biggest savings opportunities?"

- "What risks are there to adopting the savings recommendations?"

- "Which workloads are under-provisioned? I want the reliability risks, not the savings."

- "If I apply these rightsizing recommendations, will my bill actually go down?"

- "Show me abandoned workloads — pods that are running but appear idle."

## Tools

**11 tools** — all read-only, all structured for LLM consumption:

| Tool                                      | Description                                                                                  |
| ----------------------------------------- | -------------------------------------------------------------------------------------------- |
| `kubecost_list_windows`                   | List the valid time windows for Kubecost cost queries, each resolved to real dates.          |
| `get_kubecost_workload_costs`             | Return Kubernetes cost allocation from Kubecost grouped by chosen dimensions.                |
| `get_kubecost_cost_comparison`            | Compare Kubernetes cost allocation between two time windows to find cost changes and spikes. |
| `get_container_savings_recommendations`   | Return Kubernetes container request-sizing candidates and request opportunity.               |
| `get_abandoned_workloads`                 | Return pods with abnormally low network traffic — possible abandoned workloads.              |
| `get_savings_overview`                    | Return a ranked summary of all Kubecost savings categories.                                  |
| `get_pv_sizing_recommendations`           | Return PersistentVolumeClaim right-sizing recommendations ranked by monthly savings.         |
| `get_local_disk_savings`                  | Return underutilized node-local disk savings recommendations.                                |
| `get_cluster_rightsizing_recommendations` | Return node group scale-in/scale-out/instance-type recommendations for a cluster.            |
| `get_unclaimed_volumes`                   | Return PersistentVolumes that are provisioned but not bound to any PVC.                      |
| `get_resource_quota_recommendations`      | Return namespace-level ResourceQuota sizing recommendations.                                 |

**12 prompts** — step-by-step workflows your assistant can follow:

| Prompt                          | Description                                                                                          |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `container_rightsizing_guide`   | Explain how to properly size Kubernetes container CPU and memory requests.                           |
| `explore_container_savings`     | Start a guided container rightsizing exploration. Presents choices step-by-step.                     |
| `rightsizing_review`            | Run a full container rightsizing review — candidates, reliability risks, what would                  |
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

Pick a profile on **consequence of failure**, not on which environment the workload runs in — a staging cluster that gates releases deserves more headroom than a forgotten production batch job.

| Profile                | Best for                                       | Window | Quantiles         | Target utilization (CPU / RAM) |
| ---------------------- | ---------------------------------------------- | ------ | ----------------- | ------------------------------ |
| `production` (default) | Noticed but tolerated if slow                  | 15d    | P80 CPU / P95 RAM | 0.65 / 0.65                    |
| `high-availability`    | Breaches an SLO, drops revenue, or loses state | 30d    | P95 CPU / P99 RAM | 0.50 / 0.50                    |
| `development`          | Costs nothing but a retry                      | 15d    | P80 CPU / P95 RAM | 0.80 / 0.65                    |

Target utilization is the utilization the new request should run at — Kubecost computes `recommended = usage / target`. **Lower means a bigger request and more headroom**, so `high-availability` at 0.50 is the safest and `development` at 0.80 the most aggressive on CPU.

Note that `development` raises the **CPU** target only. No profile buys savings on memory: CPU is compressible, so an under-provisioned CPU request means the workload runs slower under contention and recovers on its own. Memory is not, and a memory request below normal usage moves the pod up the eviction order when its node runs short. If you want that trade anyway, set `target_ram_utilization` explicitly.

Profiles never filter results. Pass `min_monthly_savings=5.0` to hide small opportunities; it only trims the reduction candidates in `rows` and never touches `undersized_rows`, which carries workloads reserving _less_ than they use. Any explicit parameter overrides the profile. Ask for the `container_rightsizing_guide` prompt for the methodology, or `kubecost://guides/sizing-mechanics` for why the advice is what it is.

## Telemetry (experimental)

The Kubecost MCP supports sending OpenTelemetry data to your own infrastructure. Please reference the [docs/telemetry/README.md](docs/telemetry/README.md) for more information.

## Authentication Options

Authentication, OIDC, API keys, and pod hardening are in [docs/auth/README.md](docs/auth/README.md).

## Development

See [docs/development/README.md](docs/development/README.md) for build, test, and deployment instructions.

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
