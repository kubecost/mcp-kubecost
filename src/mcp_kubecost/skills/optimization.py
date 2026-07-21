"""Optimization skill — rightsizing and Reserved Instance workflows."""

from fastmcp import FastMCP

SKILL_CONTENT = """\
# Optimization

## When to Use
Use this skill when investigating savings opportunities, rightsizing resources, \
planning Reserved Instance purchases, or reviewing RI utilization.

## Available Tools

### Savings overview (start here)
- `get_savings_overview` — Returns all 8 Kubecost savings categories ranked by estimated monthly savings.
  Use as step 0 for any general "how can I save money?" question. Each category includes a drill_down_tool
  hint pointing to the relevant detailed tool.

### Container request sizing (Kubecost)
- `get_container_savings_recommendations` — Quantile-based container CPU/RAM rightsizing from Kubecost requestSizingV2.
   Supports named presets (conservative, balanced, aggressive).
- `container_rightsizing_guide` prompt — Methodology for CPU vs memory sizing (call when user asks HOW to rightsize).
- `explore_container_savings` prompt — Guided workflow for container savings exploration.
- Resource `kubecost://guides/container-sizing` — Full sizing reference.
- Resource `kubecost://schema/sizing-presets` — Preset parameter bundles.

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

## Common Workflows

### General savings investigation (any "how can I save?" question)
1. Call `get_savings_overview` -- get the full ranked list of all savings categories
2. Identify the category with the highest `savings_per_month`
3. Call the `drill_down_tool` listed on that category for detailed recommendations
4. Present results sorted by savings; offer to drill into the next-highest category

### Kubernetes/Container rightsizing investigation
1. If user asks about methodology, invoke `container_rightsizing_guide` prompt first
2. `get_container_savings_recommendations` with `preset="balanced"` for a first pass
3. Check interpretation block for undersized memory (negative memory savings) -- never downsize those
4. Re-run with `preset="conservative"` for production-critical workloads
5. Use `include_undersized=True` to surface containers that need MORE resources

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
"""


def register_optimization_skill(mcp: FastMCP) -> None:
    """Register the optimization skill as an MCP prompt."""

    @mcp.prompt()
    def optimization() -> str:
        """Guidance for rightsizing resources and planning Reserved Instance purchases.

        Use when investigating savings opportunities, checking RI utilization,
        or planning commitment purchases. Always check rightsizing before RI planning.
        """
        return SKILL_CONTENT
