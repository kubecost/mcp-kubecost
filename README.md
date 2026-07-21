# Kubecost FinOps MCP Server<!-- omit in toc -->

A read-only MCP server that connects your AI assistant to [Kubecost](https://www.kubecost.com/) so you can ask natural-language questions about Kubernetes cloud costs and savings — no dashboards, no SQL.

- [Who This Is For](#who-this-is-for)
- [What You Can Ask](#what-you-can-ask)
  - [Cost visibility](#cost-visibility)
  - [Savings opportunities](#savings-opportunities)
- [What's Included](#whats-included)
- [Quick Start](#quick-start)

## Who This Is For

- **FinOps Practitioners** who want to answer questions about their Kubernetes costs and savings with natural language questions.
- **Engineering managers** who need spend summaries and savings reports on demand.
- **Platform engineers** who want cost visibility in their IDE or AI chats without switching to the Kubecost UI.

> [!NOTE]
> As of version 1.x, the server is read-only. It never modifies your cluster or Kubecost configuration.

## What You Can Ask

### Cost visibility

- "What are my top 10 most expensive namespaces over the last 30 days?"

- "Show me CPU and memory costs per cluster for this month."

- "Which pods are driving the most spend in the `production` namespace?"

- "Give me a daily cost trend broken down by namespace for the last two weeks."

### Savings opportunities

- "Where can I save the most money in my cluster right now?"

- "Which containers are over-provisioned and by how much?"

- "Show me abandoned workloads — pods that are running but appear idle."

- "Which PersistentVolumeClaims are significantly over-provisioned?"

- "Are there any PersistentVolumes with no PVC binding that I can delete?"

- "What would I save if I right-sized my node groups?"

- "Which namespaces have resource quotas that are too loose or missing entirely?"

## What's Included

**10 tools** — all read-only, all structured for LLM consumption:

| Tool | What it answers |
|------|----------------|
| `kubecost_list_windows` | Available time windows for cost queries |
| `get_kubecost_workload_costs` | Spend by cluster, namespace, pod, label, or any combination |
| `get_savings_overview` | All Kubecost savings categories ranked by estimated monthly savings |
| `get_container_savings_recommendations` | CPU/RAM rightsizing recommendations with conservative/balanced/aggressive presets |
| `get_abandoned_workloads` | Running pods with near-zero network traffic (idle waste) |
| `get_pv_sizing_recommendations` | Over-provisioned PersistentVolumeClaims |
| `get_local_disk_savings` | Underutilized node-local disks |
| `get_unclaimed_volumes` | PersistentVolumes with no PVC binding |
| `get_cluster_rightsizing_recommendations` | Node group scale-in and instance type change recommendations |
| `get_resource_quota_recommendations` | Namespace ResourceQuota right-sizing |

**Guided prompts** — step-by-step workflows your assistant can follow:

- `explore_costs` — Interactive cost exploration wizard
- `top_spenders` — Top cost drivers by cluster and namespace
- `cost_trend` — Daily cost trend analysis
- `explore_container_savings` — Container rightsizing walkthrough
- `container_rightsizing_guide` — Methodology guide for CPU vs memory sizing
- `explore_abandoned_workloads` — Abandoned workload investigation workflow

**Skills** — high-level guidance prompts for common FinOps workflows:

- `kubecost_cost_allocation` — Kubernetes cost visibility workflows
- `optimization` — Savings investigation and rightsizing workflows

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
