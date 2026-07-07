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


PresetName = Literal["conservative", "balanced", "aggressive"]

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
    "include_undersized": False,
    "min_monthly_savings": 1.0,
}

SIZING_PRESETS: dict[PresetName, dict[str, Any]] = {
    "balanced": {},
    "conservative": {
        "q_cpu": 0.95,
        "q_ram": 0.99,
        "window": "30d",
        "target_cpu_utilization": 0.75,
        "target_ram_utilization": 0.75,
        "include_undersized": True,
    },
    "aggressive": {
        "q_cpu": 0.80,
        "q_ram": 0.95,
        "window": "15d",
        "target_cpu_utilization": 0.55,
        "min_monthly_savings": 10.0,
        "include_undersized": False,
    },
}

PRESET_DESCRIPTIONS: dict[PresetName, str] = {
    "balanced": "Default behavior — P80 CPU / P95 RAM over 15d; moderate CPU throttle risk.",
    "conservative": "Minimize OOM risk — P95 CPU / P99 RAM over 30d; includes undersized containers.",
    "aggressive": "Maximize savings — P80 CPU over 15d; filters trivial savings; accepts CPU throttle risk.",
}

CONTAINER_SIZING_GUIDE = """\
# Container Request Sizing Guide

## Core Principle
**CPU is compressible. Memory is not.**

- Under-reserved CPU → throttling (slow, recoverable)
- Under-reserved memory → OOM kill (hard failure, restart required)

Accept more under-provisioning risk for CPU than for memory.

## Recommended Targets

| Resource | Request / Reservation | Limit / Hard Cap |
|----------|----------------------|------------------|
| CPU      | P90 to P95              | P99 or 2 to 3x request |
| Memory   | P95 to P99              | observed max + 20 to 30% headroom |

## Kubecost Parameter Mapping

- **algorithm_cpu**: `quantileOfAverages` (default) — smooths daily noise; best for CPU requests
- **algorithm_ram**: `quantileOfMaxes` (default) — captures peak memory; safer against OOM
- **q_cpu / q_ram**: quantile (0 to 1). P90 = 0.90, P95 = 0.95, P99 = 0.99
- **target_*_utilization**: headroom factor — lower = more aggressive downsizing
- **window**: 15 to 30 days is the sweet spot for quantiles (15d minimum for meaningful stats)

## Presets

Use the `preset` parameter on `get_container_savings_recommendations`:

- **balanced** — default; good starting point for most clusters
- **conservative** — production-critical workloads; surfaces undersized memory
- **aggressive** — cost-focused; skips trivial savings, accepts CPU throttle risk

## When to Use Each Preset

| Workload type | Preset |
|---------------|--------|
| Production APIs, stateful services | conservative |
| General workloads, first pass | balanced |
| Dev/test, batch, cost reduction sprints | aggressive |

## Practical Workflow

1. Start with `preset="balanced"` and review top savings opportunities
2. Check for negative memory savings (undersized) — never downsize those
3. For critical services, re-run with `preset="conservative"`
4. Revisit every 30 to 60 days or after traffic changes

Call `get_container_savings_recommendations` with your chosen preset to get data-backed recommendations.
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
- **AvgUsage_*** — mean usage over the window
- **MaxUsage_*** — peak usage (large gap from Avg = burst/spike behavior)
- **monthlySavings_memory < 0** — undersized memory; do NOT reduce memory request
- **Recommended_*** — suggested request based on quantiles and target utilization
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
        "Headroom factor for CPU recommendations (0 to 1). Lower = more aggressive downsizing. "
        "Default 0.65 means sizing so usage hits 65% of the recommended request."
    ),
    "target_ram_utilization": (
        "Headroom factor for RAM recommendations (0 to 1). Keep conservative — "
        "OOM risk outweighs the cost of a few extra MB."
    ),
    "preset": (
        "Named sizing preset: 'conservative' (minimize OOM), 'balanced' (default), "
        "or 'aggressive' (maximize savings). Explicit params override preset values."
    ),
}


def resolve_sizing_params(
    preset: PresetName | None = None,
    *,
    window: str | None = None,
    algorithm_cpu: str | None = None,
    algorithm_ram: str | None = None,
    q_cpu: float | None = None,
    q_ram: float | None = None,
    target_cpu_utilization: float | None = None,
    target_ram_utilization: float | None = None,
    include_undersized: bool | None = None,
    min_monthly_savings: float | None = None,
) -> dict[str, Any]:
    """Merge defaults → preset → explicit overrides."""
    params = dict(DEFAULT_SIZING_PARAMS)
    if preset:
        params.update(SIZING_PRESETS[preset])
    overrides = {
        "window": window,
        "algorithm_cpu": algorithm_cpu,
        "algorithm_ram": algorithm_ram,
        "q_cpu": q_cpu,
        "q_ram": q_ram,
        "target_cpu_utilization": target_cpu_utilization,
        "target_ram_utilization": target_ram_utilization,
        "include_undersized": include_undersized,
        "min_monthly_savings": min_monthly_savings,
    }
    for key, value in overrides.items():
        # Use `is not None` — False and 0.0 are valid overrides and must not be skipped
        if value is not None:
            params[key] = value
    if preset:
        params["preset"] = preset
    return params


def format_presets_resource() -> str:
    """Format sizing presets for MCP resource."""
    lines = ["# Container Sizing Presets\n"]
    for name, desc in PRESET_DESCRIPTIONS.items():
        overrides = SIZING_PRESETS[name]
        lines.append(f"## {name}")
        lines.append(desc)
        if overrides:
            for key, val in overrides.items():
                lines.append(f"  - {key}: {val}")
        else:
            lines.append("  (uses all defaults)")
        lines.append("")
    lines.append("Explicit parameters passed to the tool override preset values.")
    return "\n".join(lines)


def _pct(quantile: float) -> str:
    return f"P{int(quantile * 100)}"


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
        "- Large gap between `AvgUsage` and `MaxUsage` indicates burst/spike behavior.",
        "- Negative `monthlySavings_memory` = undersized RAM — do NOT reduce memory request.",
    ]

    preset = params.get("preset")
    q_cpu = params.get("q_cpu", DEFAULT_SIZING_PARAMS["q_cpu"])
    q_ram = params.get("q_ram", DEFAULT_SIZING_PARAMS["q_ram"])
    window = params.get("window", DEFAULT_SIZING_PARAMS["window"])
    algorithm_cpu = params.get("algorithm_cpu", DEFAULT_SIZING_PARAMS["algorithm_cpu"])
    algorithm_ram = params.get("algorithm_ram", DEFAULT_SIZING_PARAMS["algorithm_ram"])

    preset_label = f" ({preset} preset)" if preset else ""
    lines.append(
        f"- Active sizing{preset_label}: {_pct(q_cpu)} CPU ({algorithm_cpu}), "
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
            f"({names}{extra}) — under-provisioned RAM. Re-run with "
            f'`preset="conservative"` and `include_undersized=True` to review.'
        )

    spikey: list[str] = []
    for row in display_rows[:MAX_SPIKEY_CONTAINERS_CHECK]:
        avg_cpu = _float_field(row, "AvgUsage_cpu")
        max_cpu = _float_field(row, "MaxUsage_cpu")
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
