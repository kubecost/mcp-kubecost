# Kubecost FinOps MCP Server<!-- omit in toc -->

A read-only MCP server that connects your AI assistant to [Kubecost](https://www.kubecost.com/) so you can ask natural-language questions about Kubernetes cloud costs and savings — no dashboards, no SQL.

- [Who This Is For](#who-this-is-for)
- [Examples of What You Can Ask](#examples-of-what-you-can-ask)
  - [Cost Visibility](#cost-visibility)
  - [Savings Opportunities](#savings-opportunities)
- [Tools](#tools)
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
| `get_kubecost_cost_comparison` | Compare Kubernetes cost allocation between two equal-length windows to find cost spikes. |
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
| `container_savings_filter_help` | Explain the filter options (undersized containers, trivial savings) for container savings. |
| `explore_costs` | Start a guided Kubernetes cost exploration. Presents choices step-by-step. |
| `explore_cost_comparison` | Start a guided cost anomaly / spike investigation using period-over-period comparison. |
| `top_spenders` | Show top cost drivers across clusters and namespaces for a given window. |
| `cost_trend` | Show daily cost trend for a given aggregation dimension. |
| `explore_abandoned_workloads` | Start a guided abandoned-workload investigation. Walks the user through threshold and scope choices. |
| `optimization` | Guidance for rightsizing resources and diagnosing Kubernetes cost anomalies. |
| `kubecost_cost_allocation` | Guidance for investigating Kubernetes cluster costs and container allocation. |

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
