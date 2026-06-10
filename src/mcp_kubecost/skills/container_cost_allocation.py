"""Container Cost Allocation skill — Kubernetes cost visibility."""

from fastmcp import FastMCP

SKILL_CONTENT = """\
# Container Cost Allocation

## When to Use
Use this skill when investigating Kubernetes cluster costs, container cost allocation \
by namespace/service/label, or any workload-level spend breakdown.

## Available Tools
- `get_kubecost_workload_costs` — Kubernetes cost allocation by
     any dimension (cluster, namespace, pod, node, label, etc.)

## Common Workflows

### Cluster inventory / cost overview
1. `get_kubecost_workload_costs` with aggregate="cluster", window="lastmonth"
2. Review total spend per cluster and top cost drivers

### Namespace cost breakdown
1. `get_kubecost_workload_costs` with aggregate="cluster,namespace", window="7d"
2. Identify highest-spend namespaces per cluster

### Team/application cost allocation
1. `get_kubecost_workload_costs` with aggregate="namespace" or aggregate="pod"
2. Use namespace as proxy for team/application grouping

### Daily cost trend analysis
1. `get_kubecost_workload_costs` with accumulate=false, window="30d"
2. Spot cost spikes or gradual increases over time

## Parameter Guidance

### aggregate parameter
- Single dimension: "cluster", "namespace", "pod", "node", "controller", "label"
- Multiple dimensions: "cluster,namespace" or "namespace,pod" (comma-separated)
- Also accepts: "container", "controllerKind", "annotation", "department", "environment", "owner", "product", "team"

### window parameter (REQUIRED) - prompt user to select an option
- Durations: "7d", "30d", "90d"
- Named periods: "today", "week", "month", "lastweek", "lastmonth"
- RFC3339 ranges: "2024-01-01T00:00:00Z,2024-01-31T23:59:59Z"

### accumulate parameter
- `true` (default): Single total for the window — use for cost comparisons and showback
- `false`: Daily breakdown — use only for trend analysis and time-series visualization

### top_n parameter
- Controls how many rows appear in the inline summary (default: 15)
- Full data is always available via the CSV download link

## Presentation
- The tool returns an Executive Summary with a summary CSV and a full CSV download link
- Always render the summary CSV as a bar chart (comparisons) or line chart (trends)
- Always show the 'Download CSV' link — never open or display the raw CSV inline

## Do NOT
- Use `run_cost_report` for Kubernetes costs — use `get_kubecost_workload_costs`
- Use the old tools `kubecost_get_cluster_cost_by_workload`, `kubecost_get_infra_costs`, \
`list_container_clusters`, or `kubecost_get_request_sizing` — they have been removed
"""


def register_container_cost_allocation_skill(mcp: FastMCP) -> None:
    """Register the container cost allocation skill as an MCP prompt."""

    @mcp.prompt()
    def kubecost_cost_allocation() -> str:
        """Guidance for investigating Kubernetes cluster costs and container allocation.

        Use this when analyzing costs by cluster, namespace, label, or workload.
        """
        return SKILL_CONTENT
