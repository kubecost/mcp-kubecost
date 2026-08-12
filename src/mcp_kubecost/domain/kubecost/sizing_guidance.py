"""Container request-sizing guidance for Kubecost rightsizing tools."""

from __future__ import annotations

from typing import Any, Literal


def _float_field(row: dict, key: str) -> float:
    """Safely coerce a row field to float, returning 0.0 on any failure.

    Duplicated from kubecost_tools._float_field intentionally — the domain
    layer must not import from the tools layer.
    """
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


ProfileName = Literal["high-availability", "production", "development"]

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
    "development": {
        "window": "15d",
        "algorithm_cpu": "quantileOfAverages",
        "algorithm_ram": "quantileOfMaxes",
        "q_cpu": 0.80,
        "q_ram": 0.95,
        "target_cpu_utilization": 0.80,
        "target_ram_utilization": 0.80,
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
        "More savings",
        "Accepts CPU throttle and RAM OOM risk; dev/test, batch, and cost-reduction sprints only.",
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

## Core Principle
**CPU is compressible. Memory is not.**

- Under-reserved CPU → throttling (slow, recoverable)
- Under-reserved memory → OOM kill (hard failure, restart required)

Accept more under-provisioning risk for CPU than for memory.

## Recommended Targets

| Resource | Request / Reservation | Limit / Hard Cap |
|----------|----------------------|------------------|
| CPU      | P80 to P95              | P99 or 2 to 3x request |
| Memory   | P95 to P99              | observed max + 20 to 30% headroom |

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
savings opportunities. Pass a **negative** threshold (e.g. `-100`) to keep undersized
workloads whose rightsizing would increase cost by up to that amount.

{_PROFILE_BULLETS}

## When to Use Each Profile

| Workload type | Profile |
|---------------|--------|
| Latency-sensitive APIs, stateful services | high-availability |
| General workloads, first pass | production |
| Dev/test, batch, cost reduction sprints | development |

`development` uses the same quantiles as `production` and differs only in target utilization
(0.80 vs 0.65), which it raises on **both** CPU and RAM — so it shrinks the memory request too.
Memory is not compressible, so an undersized request means OOM kills, not throttling. Do not use
`development` on production or HA workloads.

## Practical Workflow

1. Start with `profile="production"` and review top savings opportunities
2. Check for negative memory savings (undersized) — never downsize those
3. Optionally pass `min_monthly_savings=5.0` to focus on material savings, or a negative
   floor to keep undersized rows while dropping extreme cost-increase outliers
4. For critical services, re-run with `profile="high-availability"`
5. Revisit every 30 to 60 days or after traffic changes

Call `get_container_savings_recommendations` with your chosen profile to get data-backed recommendations.
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

| Workload | CPU target |
|----------|------------|
| Latency-sensitive (APIs) | P95 to P99 requests, high limit |
| Batch/background | P50 to P90 requests |

## Memory Reservations

- Request: P95 to P99 of working set (RSS) over 15 to 30 days
- Limit: P99.9 OR observed max + 20 to 30% headroom (whichever is larger)
- Watch for memory growth trends — flat P99 on a growing trend is a time bomb

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
"""

FIELD_DESCRIPTIONS = {
    "window": ("Observation window for usage metrics. 15d minimum for meaningful quantile calculations."),
    "algorithm_cpu": (
        "CPU sizing algorithm. 'quantileOfAverages' (default) smooths daily noise — "
        "best for CPU requests (P90 to P95 target). 'max' almost never appropriate for requests."
    ),
    "algorithm_ram": (
        "RAM sizing algorithm. 'quantileOfMaxes' (default) captures peak memory — safer against OOM kills."
    ),
    "q_cpu": (
        "CPU quantile (0 to 1). P90=0.90, P95=0.95. Target P90 to P95 for requests; "
        "CPU is compressible- moderate under-provisioning is acceptable for most workloads."
    ),
    "q_ram": (
        "RAM quantile (0 to 1). Target P95 to P99 for requests; memory is not compressible — "
        "OOM kills will disrupt workloads."
    ),
    "target_cpu_utilization": (
        "Utilization the new CPU request should run at (0 to 1). "
        "Kubecost computes recommended = usage / target. "
        "Lower (e.g. 0.50) leaves more headroom; higher (e.g. 0.80) recommends a smaller request. "
        "Default 0.65 means sizing so usage hits 65% of the recommended request."
    ),
    "target_ram_utilization": (
        "Utilization the new RAM request should run at (0 to 1). Same formula as CPU. "
        "Memory is not compressible — prefer 0.50 to 0.65 unless the workload can tolerate OOM risk."
    ),
    "profile": (
        "Named sizing profile: 'production' (default, target 0.65), "
        "'high-availability' (more headroom, target 0.50), "
        "or 'development' (more savings, target 0.80). "
        "Same three names as the node-group and resource-quota tools' profile parameter, but here "
        "the profile expands into sizing knobs — any explicitly passed parameter overrides it. "
        "Profiles do not apply a savings filter."
    ),
    "min_monthly_savings": (
        "Minimum monthlySavings_total (USD) to keep. Default null returns every recommendation. "
        "Pass 5.0 to cut noise and focus on material savings. "
        "Pass a negative value (e.g. -100) to include undersized workloads whose rightsizing "
        "would increase cost by up to that amount."
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
    all_rows: list[dict],
    *,
    filtered_rows: list[dict] | None = None,
) -> str:
    """Build dynamic result interpretation block for savings tool output."""
    display_rows = filtered_rows if filtered_rows is not None else all_rows
    lines: list[str] = [
        "---",
        "**How to read these results:**",
        "- CPU is compressible (throttle) — memory is not (OOM kill). Treat memory recommendations conservatively.",
        "- Low `currentEfficiency` = over-provisioned (safe to downsize). High efficiency = already tight.",
        "- Large gap between `AvgUsage` and `MaxUsage` (millicores/MiB) indicates burst/spike behavior.",
        "- Negative `monthlySavings_memory` = undersized RAM — do NOT reduce memory request.",
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

    undersized_memory = [r for r in all_rows if _float_field(r, "monthlySavings_memory") < 0]
    if undersized_memory:
        names = ", ".join(r.get("containerName", "unknown") for r in undersized_memory[:MAX_UNDERSIZED_DISPLAY])
        extra = (
            f" (+{len(undersized_memory) - MAX_UNDERSIZED_DISPLAY} more)"
            if len(undersized_memory) > MAX_UNDERSIZED_DISPLAY
            else ""
        )
        lines.append(
            f"- ⚠️ {len(undersized_memory)} container(s) have negative memory savings "
            f"({names}{extra}) — under-provisioned RAM. Do NOT reduce those memory requests. "
            f"Omit `min_monthly_savings` (or pass a negative floor) to keep undersized rows visible."
        )

    spikey: list[str] = []
    for row in display_rows[:MAX_SPIKEY_CONTAINERS_CHECK]:
        avg_cpu = _float_field(row, "AvgUsage_cpuInMilliCores")
        max_cpu = _float_field(row, "MaxUsage_cpuInMilliCores")
        if (
            avg_cpu > MIN_AVG_CPU_FOR_SPIKE_DETECTION  # guards the division below; constant must stay > 0
            and max_cpu / avg_cpu >= CPU_SPIKE_THRESHOLD
        ):
            name = row.get("containerName", "unknown")
            spikey.append(name)
    if spikey:
        names = ", ".join(spikey[:MAX_SPIKEY_CONTAINERS_DISPLAY])
        lines.append(
            f"- CPU spikes detected in {names} (Max/Avg ≥ 3x) — consider longer window "
            f"or higher CPU quantile before downsizing."
        )

    for row in display_rows[:MAX_EFFICIENCY_EXAMPLES]:
        name = row.get("containerName", "unknown")
        eff_cpu = _float_field(row, "currentEfficiency_cpu")
        eff_ram = _float_field(row, "currentEfficiency_memory")
        mem_savings = _float_field(row, "monthlySavings_memory")
        if (
            eff_cpu < HEAVILY_OVERPROVISIONED_CPU_THRESHOLD
            and eff_ram < HEAVILY_OVERPROVISIONED_RAM_THRESHOLD
            and mem_savings >= 0
        ):
            lines.append(
                f"- {name}: efficiency {eff_cpu:.0%} CPU / {eff_ram:.0%} RAM "
                f"→ heavily over-provisioned; savings are safe to pursue."
            )
        elif mem_savings < 0:
            lines.append(f"- {name}: negative memory savings → under-provisioned RAM; do NOT reduce memory request.")

    lines.append("- For methodology details, invoke the `container_rightsizing_guide` prompt.")
    return "\n".join(lines)
