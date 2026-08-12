"""Optimization skill — rightsizing and cost anomaly investigation workflows."""

from fastmcp import FastMCP

SKILL_CONTENT = """\
# Optimization

## When to Use
Use this skill when investigating savings opportunities, rightsizing resources, \
or diagnosing why costs changed (spikes, drops, anomalies).

## Available Tools

### Savings overview (start here)
- `get_savings_overview` — Returns all 8 Kubecost savings categories ranked by estimated monthly savings.
  Use as step 0 for any general "how can I save money?" question. Each category includes a drill_down_tool
  hint pointing to the relevant detailed tool.

### Container request sizing (Kubecost)
- `get_container_savings_recommendations` — Quantile-based container CPU/RAM rightsizing from Kubecost requestSizingV2.
   Supports named profiles (production, high-availability, development) — the same names the
   node-group and quota tools take.
- `container_rightsizing_guide` prompt — Methodology for CPU vs memory sizing (call when user asks HOW to rightsize).
- `explore_container_savings` prompt — Guided workflow for container savings exploration.
- Resource `kubecost://guides/container-sizing` — Full sizing reference.
- Resource `kubecost://schema/sizing-profiles` — Profile parameter bundles.

### Abandoned workload detection (Kubecost)
- `get_abandoned_workloads` — Surfaces pods with abnormally low network traffic (both ingress and egress
  below a configurable bytes/second threshold). These pods are running but appear idle and are candidates
  for decommissioning. Returns estimated monthly savings per pod.
- `explore_abandoned_workloads` prompt — Guided workflow for abandoned workload investigation.

### Storage & disk savings (Kubecost)
- `get_pv_sizing_recommendations` — Right-sizes over-provisioned PersistentVolumeClaims. Returns top recommendations
  sorted by monthly savings with current vs recommended capacity and cost.
- `get_local_disk_savings` — Surfaces underutilized node-local disks. utilization_percent is a 0-1 ratio;
  recommended_capacity_bytes=0 means full decommission is recommended.
- `get_unclaimed_volumes` — Lists PersistentVolumes with no PVC binding (pure waste). Deletion saves the full
  monthly_cost — confirm with storage team before removing.

### Cluster node rightsizing (Kubecost)
- `get_cluster_rightsizing_recommendations` — Recommends scaling node groups in/out or changing instance type.
  Requires a cluster ID (use get_kubecost_workload_costs with aggregate='cluster' to discover IDs).
  recommendation values: 'ScaleIn', 'None', 'ChangeInstanceType' (open string -- may include future values).

### Namespace quota sizing (Kubecost)
- `get_resource_quota_recommendations` — Recommends ResourceQuota changes per namespace. isNewResourceQuota=true
  means create a new quota; isDownsize=true means reduce an existing one. total_monthly_savings may be 0 --
  this is a configuration-correctness tool. Uses true server-side pagination (limit/offset).

### Cost anomaly / spike investigation (Kubecost)
- `get_kubecost_cost_comparison` — Diffs Kubernetes allocation costs between two equal-length periods
  (two equal-duration RFC3339 ranges) and returns a per-dimension
  change/pct_change table sorted by absolute change descending. This is the entry point for "why did
  costs change" or "what spiked" questions -- run it first to find which dimension moved most, then
  drill into the tool that matches that dimension.
- `explore_cost_comparison` prompt — Guided workflow for picking two comparable periods and interpreting
  the diff.

## Common Workflows

### General savings investigation (any "how can I save?" question)
1. Call `get_savings_overview` -- get the full ranked list of all savings categories
2. Identify the category with the highest `savings_per_month`
3. Call the `drill_down_tool` listed on that category for detailed recommendations
4. Present results sorted by savings; offer to drill into the next-highest category

### Kubernetes/Container rightsizing investigation
1. If user asks about methodology, invoke `container_rightsizing_guide` prompt first
2. `get_container_savings_recommendations` with `profile="production"` for a first pass
3. Check interpretation block for undersized memory (negative memory savings) -- never downsize those
4. Re-run with `profile="high-availability"` for latency-sensitive or stateful workloads
5. Optionally pass `min_monthly_savings=5.0` to focus on material savings, or a negative floor to keep undersized rows

### Abandoned workload discovery
1. Invoke `explore_abandoned_workloads` prompt to walk the user through threshold and scope choices
2. Call `get_abandoned_workloads` with defaults first (days=2, threshold=500) to get an initial picture
3. Sort results by `monthlySavings` -- focus review on highest-cost idle pods
4. Confirm with the owning team before decommissioning; do NOT suggest deletion without confirmation
5. To widen the search: increase `days` (e.g. 7 or 30) or `threshold` (e.g. 1000 bytes/s)

### Storage savings investigation
1. Call `get_savings_overview` to confirm storage categories have meaningful savings
2. Call `get_pv_sizing_recommendations` for PVC right-sizing opportunities
3. Call `get_unclaimed_volumes` for zero-effort deletion candidates (no PVC binding)
4. Call `get_local_disk_savings` for node-level disk decommission opportunities
5. Always confirm with storage/platform team before resizing or deleting volumes

### Node group rightsizing investigation
1. Use get_kubecost_workload_costs with aggregate='cluster' to discover cluster IDs if needed
2. Call `get_cluster_rightsizing_recommendations` with the cluster ID and profile='production'
3. Focus on 'ScaleIn' and 'ChangeInstanceType' recommendations for quickest savings
4. Validate recommended node counts against current workload headroom before applying

### Cost anomaly / spike investigation ("why did costs change?")
1. Invoke `explore_cost_comparison` prompt to walk the user through picking two comparable periods
2. Call `get_kubecost_cost_comparison` with current_window, baseline_window, and an aggregate dimension
3. Identify the top mover(s) by absolute `change` in the sorted diff table
4. Drill into the matching tool based on which dimension moved most:
   - Container/pod-level cost increase → `get_container_savings_recommendations`
   - A newly idle/dormant workload (`row_status=removed`, or low traffic) → `get_abandoned_workloads`
   - Node/cluster-level shift → `get_cluster_rightsizing_recommendations`
5. Read each row's `row_status`: `new` had zero cost in the baseline period (a newly appeared
   workload), `removed` has zero cost now (it disappeared), `unchanged` cost the same in both
6. When the response warns the periods differ in length, rank by `daily_change` and quote
   `normalized_pct_change` -- a 31-day month costs more than a 30-day one at identical daily spend
"""


def register_optimization_skill(mcp: FastMCP) -> None:
    """Register the optimization skill as an MCP prompt."""

    @mcp.prompt()
    def optimization() -> str:
        """Guidance for rightsizing resources and diagnosing Kubernetes cost anomalies.

        Use when investigating savings opportunities, rightsizing workloads or nodes,
        or figuring out why costs changed (spikes, drops).
        """
        return SKILL_CONTENT
