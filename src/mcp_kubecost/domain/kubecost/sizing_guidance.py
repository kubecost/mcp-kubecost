"""Container request-sizing guidance for Kubecost rightsizing tools."""

from __future__ import annotations

from typing import Any, Literal

from mcp_kubecost.tools._common import float_field as _float_field

_SAVINGS_COMPONENTS = ("monthlySavings_cpu", "monthlySavings_memory")


def shortfall(row: dict[str, Any]) -> float:
    """Monthly cost of this container's under-provisioning (non-positive).

    Sums only the *negative* per-resource savings, so a saving available on one resource
    cannot offset a shortfall on the other. ``monthlySavings_total`` does exactly that
    offsetting, which makes it the wrong way to rank these rows: a container saving $30 on
    CPU while short $20 on memory has a positive total and would sort behind one that is
    short only $2 on each, inverting worst-first order.
    """
    return sum(min(0.0, _float_field(row, key)) for key in _SAVINGS_COMPONENTS)


def is_undersized(row: dict[str, Any]) -> bool:
    """True when rightsizing this container would raise cost on CPU or memory.

    A negative per-resource saving means the current request sits below what observed
    usage justifies. That is a reliability finding, not a savings opportunity, so these
    rows are reported separately and are never removed by the savings filter.
    """
    return shortfall(row) < 0


def row_label(row: dict[str, Any]) -> str:
    """Identify a container unambiguously for display.

    A container name alone is not an identity: the same name routinely appears in several
    clusters and namespaces, and listing it three times reads like a repeat rather than
    three distinct workloads. Falls back to whatever parts are present.
    """
    parts = [str(row.get(key) or "").strip() for key in ("clusterID", "namespace", "containerName")]
    populated = [p for p in parts if p]
    return "/".join(populated) if populated else "unknown"


def unique_labels(rows: list[dict[str, Any]], limit: int) -> list[str]:
    """First ``limit`` distinct row labels, in order."""
    seen: dict[str, None] = {}
    for row in rows:
        seen.setdefault(row_label(row), None)
        if len(seen) >= limit:
            break
    return list(seen)


def pct_change(current: float, recommended: float) -> float | None:
    """Signed percent change from the current request to the recommended one.

    Negative means the request would shrink. Returns None when there is no current
    request to compare against — a percent change from zero is undefined, not 100%.
    """
    if current <= 0:
        return None
    return round((recommended - current) / current * 100.0, 1)


ProfileName = Literal["high-availability", "production", "development"]
SortBy = Literal["monthly_savings", "pct_change_cpu", "pct_change_memory"]

# Display and analysis thresholds
CPU_SPIKE_THRESHOLD = 3.0  # Max/Avg ratio indicating significant CPU burst behavior
HEAVILY_OVERPROVISIONED_CPU_THRESHOLD = 0.2  # 20% efficiency
HEAVILY_OVERPROVISIONED_RAM_THRESHOLD = 0.3  # 30% efficiency
MIN_AVG_CPU_FOR_SPIKE_DETECTION = 0.01  # Ignore containers with negligible CPU; must be > 0 — used as a division guard
MAX_UNDERSIZED_DISPLAY = 5
MAX_SPIKEY_CONTAINERS_CHECK = 10
MAX_SPIKEY_CONTAINERS_DISPLAY = 3
MAX_EFFICIENCY_EXAMPLES = 3

DEFAULT_SIZING_PARAMS: dict[str, Any] = {
    "window": "15d",
    "algorithm_cpu": "quantileOfAverages",
    "algorithm_ram": "quantileOfMaxes",
    "q_cpu": 0.8,
    "q_ram": 0.95,
    "target_cpu_utilization": 0.65,
    "target_ram_utilization": 0.65,
    "min_monthly_savings": None,
}

# Every profile pins every key in DEFAULT_SIZING_PARAMS so the full parameter set is readable in
# isolation — no cross-referencing the defaults to know what a profile actually sends.
# `min_monthly_savings: None` is deliberate and load-bearing as documentation: profiles never filter.
SIZING_PROFILES: dict[ProfileName, dict[str, Any]] = {
    "high-availability": {
        "window": "30d",
        "algorithm_cpu": "quantileOfAverages",
        "algorithm_ram": "quantileOfMaxes",
        "q_cpu": 0.95,
        "q_ram": 0.99,
        "target_cpu_utilization": 0.50,
        "target_ram_utilization": 0.50,
        "min_monthly_savings": None,
    },
    # Must stay identical to DEFAULT_SIZING_PARAMS — enforced by tests.
    "production": {
        "window": "15d",
        "algorithm_cpu": "quantileOfAverages",
        "algorithm_ram": "quantileOfMaxes",
        "q_cpu": 0.80,
        "q_ram": 0.95,
        "target_cpu_utilization": 0.65,
        "target_ram_utilization": 0.65,
        "min_monthly_savings": None,
    },
    # CPU target only is raised above production. Memory is not compressible, so no profile
    # trades memory headroom for savings — enforced by tests.
    "development": {
        "window": "15d",
        "algorithm_cpu": "quantileOfAverages",
        "algorithm_ram": "quantileOfMaxes",
        "q_cpu": 0.80,
        "q_ram": 0.95,
        "target_cpu_utilization": 0.80,
        "target_ram_utilization": 0.65,
        "min_monthly_savings": None,
    },
}


def _pct(quantile: float) -> str:
    # round, not int — float representation makes int(0.29 * 100) truncate to 28.
    return f"P{round(quantile * 100)}"


# (tagline, trailing guidance) — the only hardcoded prose. Every number in a profile description is
# generated from SIZING_PROFILES below, so the two can never disagree.
_PROFILE_TAGLINES: dict[ProfileName, tuple[str, str]] = {
    "production": ("Default", "Recommended first pass for most clusters."),
    "high-availability": ("More headroom", "Use for latency-sensitive or stateful services."),
    "development": (
        "More CPU savings",
        "Trades CPU headroom only; memory is sized as production. Dev/test, batch, cost sprints.",
    ),
}


def _describe(name: ProfileName) -> str:
    """Render a profile description from its actual parameter values."""
    tagline, guidance = _PROFILE_TAGLINES[name]
    profile = SIZING_PROFILES[name]
    target_cpu = profile["target_cpu_utilization"]
    target_ram = profile["target_ram_utilization"]
    target = (
        f"target utilization {target_cpu:.2f}"
        if target_cpu == target_ram
        else f"target utilization {target_cpu:.2f} CPU / {target_ram:.2f} RAM"
    )
    return (
        f"{tagline} — {_pct(profile['q_cpu'])} CPU / {_pct(profile['q_ram'])} RAM "
        f"over {profile['window']}; {target}. {guidance}"
    )


# Ordering is the menu order rendered by format_profiles_resource() and the explore prompt.
PROFILE_DESCRIPTIONS: dict[ProfileName, str] = {
    name: _describe(name) for name in ("production", "high-availability", "development")
}

# Colon, not an em-dash — every description already opens with a "tagline —" clause.
_PROFILE_BULLETS = "\n".join(f"- **{name}**: {desc}" for name, desc in PROFILE_DESCRIPTIONS.items())

CONTAINER_SIZING_GUIDE = f"""\
# Container Request Sizing Guide

## What These Recommendations Cover
Kubecost sizes **requests** — the capacity a workload reserves. It does not recommend
limits. A request is a reservation, not a ceiling: a container can use more than it
requests whenever its node has room to spare.

## Core Principle
**CPU is compressible. Memory is not.**

Cut a CPU request too far and the workload still bursts freely on a quiet node. On a
busy node it gets a smaller share of the contended CPU. Slower, and it recovers on
its own.

Cut a memory request too far and nothing happens immediately — the request is not a
kill boundary, the memory *limit* is. What changes is how the workload fares when its
node runs short of memory: it becomes one of the first pods evicted, and the further
its usage sits above its request, the sooner. Recovery means a restart.

So memory is still the resource to be conservative with, but the failure is
node-pressure dependent rather than immediate.

## Recommended Targets

| Resource | Request — quantile of observed usage |
|----------|--------------------------------------|
| CPU      | P80 to P95 |
| Memory   | P95 to P99 |

**Limits are your own decision.** Nothing in these responses supports a limit value.
Two things worth knowing before you set one:

- A **CPU limit** is a hard quota shared by every thread in the container. On a node
  with many cores, a container that spreads work across threads can burn its whole
  quota in a fraction of each enforcement window and then stall — while the node sits
  idle. Tail latency suffers badly, and average CPU still looks low. Leaving the CPU
  limit unset is a common and defensible choice for latency-sensitive services; the
  request alone still handles scheduling and fair sharing.
- A **memory limit** is the hard kill boundary. Observed peak plus 20 to 30% headroom
  is the usual starting point.

Mechanism for both: `kubecost://guides/sizing-mechanics`.

## Kubecost Parameter Mapping

- **algorithm_cpu**: `quantileOfAverages` (default) — smooths daily noise; best for CPU requests
- **algorithm_ram**: `quantileOfMaxes` (default) — captures peak memory; safer against OOM
- **q_cpu / q_ram**: quantile (0 to 1). P90 = 0.90, P95 = 0.95, P99 = 0.99
- **target_*_utilization**: the utilization the new request should run at.
  Kubecost computes `recommended = usage / targetUtilization`.
  Lower target → larger request → more headroom. Higher target → smaller request → more savings and more risk.
- **window**: 15 to 30 days is the sweet spot for quantiles (15d minimum for meaningful stats)

## Profiles

Use the `profile` parameter on `get_container_savings_recommendations`. The same three
names are accepted by the node-group and resource-quota tools, so there is one sizing
vocabulary across the server. They are not the same mechanism, though: this tool expands
a profile into sizing knobs you can override individually, while the other tools pass the
name straight through to Kubecost as an opaque enum.

Profiles only change sizing knobs (quantiles, window, target utilization). They do
**not** apply a savings filter — every profile returns the full recommendation set
by default. Pass `min_monthly_savings=5.0` when you want less noise and the biggest
reduction candidates; it only ever trims the savings list, never `undersized_rows`.

{_PROFILE_BULLETS}

## When to Use Each Profile

Pick on **consequence of failure**, not on which environment the workload runs in. A
staging cluster that gates releases deserves more headroom than a forgotten production
batch job.

| If being slow or restarting would... | Profile |
|--------------------------------------|--------|
| breach an SLO, drop revenue, or lose state | high-availability |
| be noticed but tolerated | production |
| cost nothing but a retry | development |

`development` raises the **CPU** target only (0.80 vs 0.65), so it recommends smaller
CPU requests than `production` while sizing memory identically. No profile trades
memory headroom for savings — memory is not compressible. If you want that anyway, set
`target_ram_utilization` explicitly and accept the eviction risk.

## Managed Runtimes Need a Second Look

JVM, Go, Node.js, and Python workloads hold memory the application no longer needs.
Garbage collectors and allocators return pages lazily, and a large share of a
container's memory sits outside the region the runtime itself reports. Observed usage
can overstate what the application needs, understate it, or both at once, depending on
the runtime.

Two consequences:

- Treat memory recommendations for these workloads as a starting point, and check them
  against runtime metrics (heap, stacks, worker count) before applying.
- **A CPU change is also a memory change.** These runtimes size their own thread and
  worker counts from the CPU they can see, so resizing CPU shifts memory behaviour too.
  Re-measure memory after changing CPU.

## Savings Here Are Request Opportunity

Reducing requests frees reserved capacity. The invoice changes one step later, when
that freed capacity lets pods pack onto fewer nodes and a node is actually removed.
Read these figures as the size of the opportunity, then call
`get_cluster_rightsizing_recommendations` to see whether it can be realized.

## Practical Workflow

1. Start with `profile="production"` and review the top reduction candidates
2. Read `undersized_rows` — those workloads are under-provisioned. Reliability
   findings, not savings, and they are never removed by `min_monthly_savings`
3. Before reducing CPU, confirm the workload is not already being throttled
4. Before reducing memory, confirm the pod is not Guaranteed (request equals limit) —
   a request-only change alters its quality-of-service class
5. For critical services, re-run with `profile="high-availability"`
6. Revisit every 30 to 60 days or after traffic changes. Record the window and sizing
   parameters alongside any approval, so a later reviewer knows which evidence it rested on

Call `get_container_savings_recommendations` with your chosen profile to get data-backed
recommendations.
"""

CONTAINER_SIZING_REFERENCE = """\
# Container Sizing Reference

## Statistical Toolbox

| Method | Best for | Avoid when |
|--------|----------|------------|
| Mean/Average | Very stable, predictable workloads | Periodic spikes, bursty traffic |
| Percentiles (P50 to P99) | Almost all production workloads | Window too short (<1 day) |
| Maximum | Memory limits, safety ceilings | Routine request sizing (wastes 2 to 10x) |


## CPU Reservations

| Workload | CPU request |
|----------|-------------|
| Latency-sensitive (APIs) | P95 — the `high-availability` profile |
| General workloads | P80 at a 0.65 target — the `production` profile |
| Batch/background | P80 at a 0.80 target — the `development` profile |

The recommended request is not the quantile itself. Kubecost divides the quantile by
the target utilization, so P80 usage at a 0.65 target produces a request comfortably
above P80.

## Memory Reservations

- Request: P95 to P99 of working set over 15 to 30 days
- Limit (not returned by these tools): observed max + 20 to 30% headroom
- Watch for memory growth trends — a flat P99 on a rising trend is a time bomb
- Managed runtimes (JVM, Go, Node.js, Python) hold memory the application has
  finished with, so observed usage is a weaker signal for them than for native
  processes

## Time Window

| Window | Risk |
|--------|------|
| < 1 day | Misses weekly/monthly patterns |
| 14 to 30 days | Sweet spot — captures weekly cycles |
| > 90 days | Bakes in stale behavior |

## Result Column Glossary

- **currentEfficiency_*** — request vs actual usage (low = over-provisioned)
- **AvgUsage_cpuInMilliCores / AvgUsage_memoryInMiB** — mean usage over the window
- **MaxUsage_cpuInMilliCores / MaxUsage_memoryInMiB** — peak usage (large gap from Avg = burst/spike behavior)
- **monthlySavings_memory < 0** — undersized memory; do NOT reduce memory request
- **Recommended_cpuInMilliCores / Recommended_memoryInMiB** — suggested request
  based on quantiles and target utilization
- **pct_change_cpu / pct_change_memory** — signed change from the current request.
  Negative means the request would shrink. Null when there is no current request to
  compare against. Use these for a relative-materiality threshold; dollars alone
  rank a 10% cut on a huge workload above an 80% cut on a small one
- **undersized_rows** — workloads under-provisioned on CPU or memory. Reliability
  findings, not savings. Never removed by `min_monthly_savings`

## Sources

Summarized from Kubernetes and Kubecost documentation, Robusta KRR and the Vertical
Pod Autoscaler recommendation policies, and *The Technical Guide to Kubernetes
Rightsizing* (LearnKube, 2026). Mechanism detail: `kubecost://guides/sizing-mechanics`.
"""

SIZING_MECHANICS = """\
# Sizing Mechanics

Why the request-sizing advice is what it is. Read this when a recommendation looks
surprising, or before applying one to a workload you cannot afford to disrupt.

## Requests and Limits Do Different Jobs

| | Request | Limit |
|---|---------|-------|
| Scheduling | reserves capacity; decides which node fits | ignored |
| CPU under contention | sets the workload's relative share | hard quota; work waits |
| Memory ceiling | none | the kill boundary |
| Eviction | protects usage below the request | not considered |

A memory request creates no ceiling. A pod requesting 3Gi with no limit can grow until
the node runs out. This is why "under-provisioned memory causes an OOM kill" is
imprecise — that is the limit's behaviour. Kubecost's request-sizing API returns
requests only.

## Quality of Service Class

Derived from requests and limits when the pod is created:

- **Guaranteed** — every container sets CPU and memory requests and limits, and each
  request equals its limit
- **Burstable** — some request or limit is set, but the above does not hold
- **BestEffort** — nothing set

**The trap:** applying a request-only recommendation to a Guaranteed pod breaks
request == limit, so the pod comes back Burstable on its next rollout. It loses the
strongest protection from the kernel's out-of-memory killer and becomes eligible for
eviction ranking it was previously insulated from. Nothing in a savings recommendation
can detect this, because Kubecost does not report limits.

Check before you apply:

```
kubectl get pod -n NAMESPACE POD -o jsonpath='{.status.qosClass}'
```

If it returns `Guaranteed`, change the request and limit together or leave it alone.

## Eviction Order Under Node Memory Pressure

When a node runs short, the kubelet ranks pods by whether usage exceeds requests, then
pod priority, then how far usage sits above the request. A pod using less than it
requested is protected ahead of one exceeding its request.

Separately, the kernel biases its own kill choice by QoS: Guaranteed pods are
effectively exempt, and for Burstable pods the bias improves as the memory request
grows relative to node capacity. So a smaller memory request makes a pod a more likely
victim twice over — once in the kubelet's ranking, once in the kernel's.

## CPU Throttling Hides Behind Low Averages

A CPU limit is a quota per short enforcement window, shared by every thread in the
container. A container that spreads work across several threads can consume its entire
quota early in each window and then stall for the remainder — even while the node has
idle CPU. Averaged over five minutes, that container looks comfortably under its limit.

The consequence is worst on large nodes, because runtimes commonly size their worker
counts from the CPU count they can see while the quota stays the same. The same pod can
lose most of its throughput and gain an order of magnitude in tail latency purely by
moving to a node with more cores.

Two implications for rightsizing:

- Low observed CPU is not evidence of low CPU need. It may be evidence of throttling.
- Verify before reducing a CPU request, using the ratio of throttled enforcement
  periods to total periods:

```
rate(container_cpu_cfs_throttled_periods_total[5m])
  / rate(container_cpu_cfs_periods_total[5m])
```

A non-trivial ratio alongside flat throughput and rising latency means the limit is
already delaying useful work. Reducing the request will make contention worse.

## Exclusive CPUs

If the kubelet runs the CPU Manager static policy, a pod gets dedicated cores only when
it is Guaranteed **and** requests a whole number of CPUs. A quantile-derived
recommendation is almost always fractional, so applying one drops the workload back
into the shared pool. Recovering that placement is a node-level operation, not a pod
edit. Check with your platform team before resizing CPU-pinned workloads.

## What These Recommendations Cannot See

Kubecost supplies CPU and memory usage. A recommendation built from it does not know
about CPU throttling, out-of-memory history, quality-of-service class, workload
revision, request latency, error rates, or the business calendar. Replicas and
successive revisions are pooled into one distribution, so a recommendation may describe
no single version of the workload precisely.

That makes the output a **candidate**: given this usage history and these sizing
parameters, here is a defensible value. It is not a finding that the value is safe for
the application. Closing that gap needs evidence from outside Kubecost.

## Sources

Kubernetes documentation on quality of service, node-pressure eviction, and CPU
management policies; Linux cgroup v2 CPU and memory controllers; Robusta KRR and
Vertical Pod Autoscaler recommendation policies; and *The Technical Guide to Kubernetes
Rightsizing* (LearnKube, 2026).
"""

FIELD_DESCRIPTIONS = {
    "window": ("Observation window for usage metrics. 15d minimum for meaningful quantile calculations."),
    "algorithm_cpu": (
        "CPU sizing algorithm. 'quantileOfAverages' (default) smooths daily noise — "
        "best for CPU requests. 'max' almost never appropriate for requests."
    ),
    "algorithm_ram": (
        "RAM sizing algorithm. 'quantileOfMaxes' (default) captures peak memory, which is the "
        "safer basis for a memory request."
    ),
    "q_cpu": (
        "CPU quantile (0 to 1). P80=0.80, P95=0.95. Default 0.80. The recommended request is "
        "not the quantile itself — Kubecost divides it by target_cpu_utilization, so P80 usage "
        "at a 0.65 target yields a request well above P80. Raise toward 0.95 for "
        "latency-sensitive workloads."
    ),
    "q_ram": (
        "RAM quantile (0 to 1). Default 0.95. Target P95 to P99; memory is not compressible, so "
        "an under-provisioned memory request makes the pod an early eviction candidate when its "
        "node runs short."
    ),
    "target_cpu_utilization": (
        "Utilization the new CPU request should run at (0 to 1). "
        "Kubecost computes recommended = usage / target. "
        "Lower (e.g. 0.50) leaves more headroom; higher (e.g. 0.80) recommends a smaller request. "
        "Default 0.65 means sizing so usage hits 65% of the recommended request."
    ),
    "target_ram_utilization": (
        "Utilization the new RAM request should run at (0 to 1). Same formula as CPU. "
        "Prefer 0.50 to 0.65: memory is not compressible, and a request below normal usage "
        "moves the pod up the eviction order under node memory pressure."
    ),
    "profile": (
        "Named sizing profile. 'production' (default) targets 0.65 CPU and RAM; "
        "'high-availability' targets 0.50 for more headroom; 'development' targets 0.80 CPU but "
        "keeps RAM at 0.65, so it trades CPU headroom only. "
        "Same three names as the node-group and resource-quota tools' profile parameter, but here "
        "the profile expands into sizing knobs — any explicitly passed parameter overrides it. "
        "Profiles do not apply a savings filter."
    ),
    "min_monthly_savings": (
        "Minimum monthlySavings_total (USD) to keep in 'rows'. Default null returns every "
        "reduction candidate. Pass 5.0 to cut noise and focus on material opportunities. "
        "This filter never touches 'undersized_rows' — under-provisioned workloads stay visible "
        "regardless of the threshold."
    ),
    "sort_by": (
        "How to rank 'rows'. 'monthly_savings' (default) ranks by dollars. 'pct_change_cpu' or "
        "'pct_change_memory' rank by the largest proportional reduction, which surfaces small "
        "workloads that are badly oversized — dollars alone bury them beneath big workloads "
        "that are only slightly oversized."
    ),
}


def resolve_sizing_params(
    profile: ProfileName | None = None,
    *,
    window: str | None = None,
    algorithm_cpu: str | None = None,
    algorithm_ram: str | None = None,
    q_cpu: float | None = None,
    q_ram: float | None = None,
    target_cpu_utilization: float | None = None,
    target_ram_utilization: float | None = None,
    min_monthly_savings: float | None = None,
) -> dict[str, Any]:
    """Merge defaults → profile → explicit overrides."""
    params = dict(DEFAULT_SIZING_PARAMS)
    if profile:
        params.update(SIZING_PROFILES[profile])
    overrides = {
        "window": window,
        "algorithm_cpu": algorithm_cpu,
        "algorithm_ram": algorithm_ram,
        "q_cpu": q_cpu,
        "q_ram": q_ram,
        "target_cpu_utilization": target_cpu_utilization,
        "target_ram_utilization": target_ram_utilization,
        "min_monthly_savings": min_monthly_savings,
    }
    for key, value in overrides.items():
        # Use `is not None` — False and 0.0 are valid overrides and must not be skipped.
        # min_monthly_savings may intentionally stay None (no filter).
        if value is not None:
            params[key] = value
    if profile:
        params["profile"] = profile
    return params


def format_profiles_resource() -> str:
    """Format sizing profiles for MCP resource."""
    lines = ["# Container Sizing Profiles\n"]
    for name, desc in PROFILE_DESCRIPTIONS.items():
        overrides = SIZING_PROFILES[name]
        lines.append(f"## {name}")
        lines.append(desc)
        if overrides:
            for key, val in overrides.items():
                lines.append(f"  - {key}: {val}")
        else:
            lines.append("  (uses all defaults)")
        lines.append("")
    lines.append("Explicit parameters passed to the tool override profile values.")
    return "\n".join(lines)


def build_result_interpretation(
    params: dict[str, Any],
    all_rows: list[dict[str, Any]],
    *,
    filtered_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Build the dynamic interpretation block for container savings output.

    ``all_rows`` is the unfiltered API population, used to count under-provisioned
    workloads. ``filtered_rows`` is what the caller will actually see.
    """
    display_rows = filtered_rows if filtered_rows is not None else all_rows
    lines: list[str] = [
        "---",
        "**How to read these results:**",
        "- CPU is compressible, memory is not. Be more conservative with memory recommendations.",
        "- Low `currentEfficiency` = request far above observed use — the strongest reduction candidates.",
        "- A large gap between `AvgUsage` and `MaxUsage` means bursty demand; size for the burst.",
        "- `pct_change_cpu` / `pct_change_memory` are signed — negative means the request would shrink.",
    ]

    profile = params.get("profile")
    q_cpu = float(params.get("q_cpu") or DEFAULT_SIZING_PARAMS["q_cpu"])
    q_ram = float(params.get("q_ram") or DEFAULT_SIZING_PARAMS["q_ram"])
    window = params.get("window", DEFAULT_SIZING_PARAMS["window"])
    algorithm_cpu = params.get("algorithm_cpu", DEFAULT_SIZING_PARAMS["algorithm_cpu"])
    algorithm_ram = params.get("algorithm_ram", DEFAULT_SIZING_PARAMS["algorithm_ram"])

    profile_label = f" ({profile} profile)" if profile else ""
    lines.append(
        f"- Active sizing{profile_label}: {_pct(q_cpu)} CPU ({algorithm_cpu}), "
        f"{_pct(q_ram)} RAM ({algorithm_ram}), {window} window."
    )

    # Worst shortfall first, so the names below match the order of `undersized_rows`
    # rather than naming whichever five the API happened to return first.
    undersized = sorted((r for r in all_rows if is_undersized(r)), key=shortfall)
    if undersized:
        names = ", ".join(unique_labels(undersized, MAX_UNDERSIZED_DISPLAY))
        extra = (
            f" (+{len(undersized) - MAX_UNDERSIZED_DISPLAY} more)" if len(undersized) > MAX_UNDERSIZED_DISPLAY else ""
        )
        lines.append(
            f"- ⚠️ {len(undersized)} container(s) are under-provisioned ({names}{extra}) — listed "
            f"separately in `undersized_rows`. Reliability findings, not savings; do not reduce those requests."
        )

    spikey: list[dict[str, Any]] = []
    for row in display_rows[:MAX_SPIKEY_CONTAINERS_CHECK]:
        avg_cpu = _float_field(row, "AvgUsage_cpuInMilliCores")
        max_cpu = _float_field(row, "MaxUsage_cpuInMilliCores")
        if (
            avg_cpu > MIN_AVG_CPU_FOR_SPIKE_DETECTION  # guards the division below; constant must stay > 0
            and max_cpu / avg_cpu >= CPU_SPIKE_THRESHOLD
        ):
            spikey.append(row)
    if spikey:
        names = ", ".join(unique_labels(spikey, MAX_SPIKEY_CONTAINERS_DISPLAY))
        lines.append(
            f"- Bursty CPU in {names} (Max/Avg ≥ {CPU_SPIKE_THRESHOLD:.0f}x) — widen the window or "
            f"raise the CPU quantile before reducing these."
        )

    for row in display_rows[:MAX_EFFICIENCY_EXAMPLES]:
        eff_cpu = _float_field(row, "currentEfficiency_cpu")
        eff_ram = _float_field(row, "currentEfficiency_memory")
        if eff_cpu < HEAVILY_OVERPROVISIONED_CPU_THRESHOLD and eff_ram < HEAVILY_OVERPROVISIONED_RAM_THRESHOLD:
            lines.append(
                f"- {row_label(row)}: efficiency {eff_cpu:.0%} CPU / {eff_ram:.0%} RAM → heavily "
                f"over-provisioned; strongest reduction candidate."
            )

    lines.extend(
        [
            "",
            "**Before applying:**",
            "- These are requests, not limits. The figures are request opportunity — the invoice moves "
            "once freed capacity lets nodes consolidate (`get_cluster_rightsizing_recommendations`).",
            "- Not accounted for: CPU throttling, out-of-memory history, quality-of-service class, "
            "workload revision. Replicas and revisions are pooled into one distribution.",
            "- Reducing CPU: confirm the workload is not already being throttled. Low CPU usage can be "
            "the symptom rather than spare headroom.",
            "- Reducing memory: confirm the pod is not Guaranteed (request equals limit) — changing the "
            "request alone alters its quality-of-service class.",
            "- Methodology: `container_rightsizing_guide` prompt. Mechanism: `kubecost://guides/sizing-mechanics`.",
        ]
    )
    return "\n".join(lines)
