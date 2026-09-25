"""Optimization skill — rightsizing and cost anomaly investigation workflows."""

from fastmcp import FastMCP

SKILL_CONTENT = """\
# Optimization

## When to Use
Use this skill when investigating savings opportunities, rightsizing resources, \
or diagnosing why costs changed (spikes, drops, anomalies).

## Reading Savings Figures — Read This First

**Request opportunity is not an invoice reduction.** Container and quota rightsizing
measure reserved capacity a workload could stop reserving. The bill changes one step
later, when freed capacity lets pods pack onto fewer nodes and a node is actually removed.
Node-group rightsizing is that step. Never add the two together and present the sum as
achievable — they are largely the same dollars counted at two stages.

**A recommendation is a candidate, not a verdict.** Kubecost sizes from CPU and memory
usage. It cannot see CPU throttling, out-of-memory history, quality-of-service class,
workload revision, latency, or the business calendar. So the honest claim is "given this
usage history and these parameters, here is a defensible value" — not "this is safe to
apply." Do not describe any row as safe.

**Rightsizing returns requests, not limits.** Nothing in these responses supports a limit
value.

## Available Tools

### Savings overview (start here)
- `get_savings_overview` — All savings categories ranked by monthly savings; each includes a
  `drill_down_tool`. Start here for any "how can I save money?" question.

### Container request sizing (Kubecost)
- `get_container_savings_recommendations` — CPU/RAM rightsizing. Profiles: production, high-availability,
  development. Returns `rows` (reduction candidates) and `undersized_rows` (reliability findings —
  never filtered by `min_monthly_savings`). `sort_by` ranks by dollars or proportional cut.
- `rightsizing_review` prompt — End-to-end review: candidates, reliability findings, realization check,
  and four pre-apply questions. Use when the user wants to act.
- `container_rightsizing_guide` prompt — Sizing methodology (call when user asks HOW to rightsize).
- `explore_container_savings` prompt — Guided step-by-step exploration.
- Resource `kubecost://guides/container-sizing` — Full sizing reference.
- Resource `kubecost://guides/sizing-mechanics` — Requests vs limits, QoS, eviction, throttling.
- Resource `kubecost://schema/sizing-profiles` — Profile parameter bundles.

### Abandoned workload detection (Kubecost)
- `get_abandoned_workloads` — Pods with low network traffic (ingress + egress below threshold).
- `explore_abandoned_workloads` prompt — Guided threshold and scope walkthrough.

### Storage & disk savings (Kubecost)
- `get_pv_sizing_recommendations` — Right-size over-provisioned PVCs.
- `get_local_disk_savings` — Underutilized node-local disks; `recommended_capacity_bytes=0` means
  full decommission.
- `get_unclaimed_volumes` — PVs with no PVC binding (pure waste); confirm before deleting.

### Cluster node rightsizing (Kubecost)
- `get_cluster_rightsizing_recommendations` — Scale node groups in/out or change instance type.
  Requires a cluster ID (discover via `get_kubecost_workload_costs` with `aggregate='cluster'`).

### Namespace quota sizing (Kubecost)
- `get_resource_quota_recommendations` — ResourceQuota changes per namespace. `isNewResourceQuota=true`
  means create; `isDownsize=true` means reduce. Configuration-correctness tool; savings may be $0.

### Cost anomaly / spike investigation (Kubecost)
- `get_kubecost_cost_comparison` — Per-dimension cost diff between two RFC3339 periods, sorted by
  absolute change. Entry point for "why did costs change?" questions.
- `explore_cost_comparison` prompt — Guided period-picking and diff interpretation.

## Common Workflows

### General savings investigation (any "how can I save?" question)
1. Call `get_savings_overview` -- get the full ranked list of all savings categories
2. Identify the category with the highest `savings_per_month`
3. Call the `drill_down_tool` listed on that category for detailed recommendations
4. Present results sorted by savings; offer to drill into the next-highest category
5. Present categories individually. Do not sum them: containerRequestSizing and
   nodeGroupSizing describe the same money at two stages, as do persistentVolumeSizing
   and unclaimedVolumes

### Kubernetes/Container rightsizing investigation
1. If the user wants to act on the results, invoke `rightsizing_review` and follow it --
   it covers the realization step and the pre-apply checks this list only summarizes.
   If they ask about methodology, invoke `container_rightsizing_guide` first
2. `get_container_savings_recommendations` with `profile="production"` for a first pass.
   Choose the profile on consequence of failure, not on environment name
3. Report `undersized_rows` alongside the savings -- those workloads reserve less than
   their usage justifies. Never reduce those requests, and never let a savings threshold
   be the reason they went unmentioned
4. Optionally pass `min_monthly_savings=5.0` to cut noise, or `sort_by="pct_change_cpu"` to
   surface small badly-oversized workloads that dollar ranking buries
5. Re-run with `profile="high-availability"` for latency-sensitive or stateful workloads
6. Call `get_cluster_rightsizing_recommendations` to check whether the freed capacity can
   actually consolidate nodes. If it cannot, say so -- the opportunity is real but it buys
   scheduling headroom, not a smaller bill
7. Before recommending anything be applied, confirm all four: **evidence** (window and
   parameters, and whether the window covers the workload's real cycle), **policy** (a
   tested floor from a load test or past incident that the data cannot know about),
   **authority** (owner, and the Git values file or overlay where the request actually
   lives -- not the live object under GitOps), and **rollback** (the signal that reverts
   the change, chosen before applying). If one is missing, that gap is the next task

### Abandoned workload discovery
1. Invoke `explore_abandoned_workloads` prompt to walk the user through threshold and scope choices
2. Call `get_abandoned_workloads` with defaults first (days=2, threshold=500) to get an initial picture
3. Compare `total_monthly_savings` / `total_count` with get_savings_overview —
   `returned_monthly_savings` is this page only. While `truncated=True`, call again
   with `offset=next_offset`
4. Sort results by `monthly_savings` -- focus review on highest-cost idle pods
5. Check `owner_kind` on every row. Network traffic is the only signal, so scheduled work is
   flagged by construction: a weekly job is silent on 5 of 7 days, and a 2-day lookback
   cannot tell it from a dead workload. Re-run with `days` past the job's interval (7 for
   weekly, 30+ for monthly) before drawing a conclusion. Queue consumers and pods writing
   only to local storage also register little traffic while doing real work
6. Confirm with the owning team before decommissioning; do NOT suggest deletion without confirmation
7. To widen the search: increase `days` (e.g. 7 or 30) or `threshold` (e.g. 1000 bytes/s)

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
    def container_optimization() -> str:
        """Guidance for rightsizing resources and diagnosing Kubernetes cost anomalies.

        Use when investigating savings opportunities, rightsizing workloads or nodes,
        or figuring out why costs changed (spikes, drops).
        """
        return SKILL_CONTENT
