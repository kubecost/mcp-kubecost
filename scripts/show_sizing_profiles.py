#!/usr/bin/env python3
"""Inspect and validate the container sizing profiles.

Renders `SIZING_PROFILES` as a table alongside the values it *implies* — the request
multiplier each target utilization produces, a worked example, and the wire-format
parameters Kubecost actually receives — plus a pass/fail invariant report.

The point is legibility. `target_cpu_utilization: 0.50` does not visibly say "2x the
observed usage", which is how a profile once shipped with its CPU and RAM targets
inverted. This script shows the arithmetic.

Every value is imported from the source modules; nothing here restates a profile.

This is NOT a replacement for tests/test_sizing_guidance.py — invariants 1-4 below are
already enforced there. The script adds a human-readable view plus checks 5-7.

Usage:

    uv run scripts/show_sizing_profiles.py
    uv run scripts/show_sizing_profiles.py --profile development
    uv run scripts/show_sizing_profiles.py --usage-cpu 250 --usage-ram 1024
    uv run scripts/show_sizing_profiles.py --json
    uv run scripts/show_sizing_profiles.py --check        # exit 1 on any violation
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from mcp_kubecost.domain.kubecost.sizing_guidance import (
    DEFAULT_SIZING_PARAMS,
    PROFILE_DESCRIPTIONS,
    SIZING_PROFILES,
    ProfileName,
)
from mcp_kubecost.tools._common import MIN_QUANTILE_WINDOW, parse_window_days, to_api_window

# The node-group and resource-quota tools accept the same three profile names, but pass them
# straight through to Kubecost as an opaque enum rather than expanding them into sizing knobs.
# Mirrored here rather than introspected: those values live in Literal annotations inside the
# tool-registration closure in kubecost_tools.py (~2752, ~2977), and reaching them would mean
# building a server just to read a schema.
NODE_GROUP_PROFILE_VALUES = ("development", "production", "high-availability")

# Order the ladder is asserted in, safest first.
LADDER_ORDER: tuple[ProfileName, ...] = ("high-availability", "production", "development")
BASELINE_PROFILE: ProfileName = "production"

DEFAULT_USAGE_CPU_MILLICORES = 100.0
DEFAULT_USAGE_RAM_MIB = 512.0

TARGET_KEYS = ("target_cpu_utilization", "target_ram_utilization")
QUANTILE_KEYS = ("q_cpu", "q_ram")

DELTA_CAVEAT = (
    "Request-vs-production compares target utilization only. Profiles with a higher quantile "
    "(high-availability reads P95 CPU vs production's P80) also read a larger point of the same "
    "usage distribution, so the real difference is larger by a data-dependent amount."
)
EXAMPLE_CAVEAT = (
    "The worked example treats the given usage as the usage at each profile's own quantile, "
    "isolating the target-utilization effect. Profiles with different quantiles read different "
    "points of the distribution, so real requests differ by more than shown."
)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def build_derived(name: ProfileName, usage_cpu: float, usage_ram: float) -> dict[str, Any]:
    """Compute what a profile's target utilizations actually imply.

    Kubecost computes ``recommended = usage / targetUtilization``, so a lower target
    yields a larger request.
    """
    profile = SIZING_PROFILES[name]
    target_cpu = profile["target_cpu_utilization"]
    target_ram = profile["target_ram_utilization"]
    baseline = SIZING_PROFILES[BASELINE_PROFILE]
    # Rounded so the JSON payload reads cleanly — 0.65 / 0.5 is 30.000000000000004 raw.
    return {
        "cpu_headroom_multiplier": round(1 / target_cpu, 4),
        "ram_headroom_multiplier": round(1 / target_ram, 4),
        "cpu_request_vs_production_pct": round((baseline["target_cpu_utilization"] / target_cpu - 1) * 100, 4),
        "ram_request_vs_production_pct": round((baseline["target_ram_utilization"] / target_ram - 1) * 100, 4),
        "example_cpu_millicores": round(usage_cpu / target_cpu, 4),
        "example_ram_mib": round(usage_ram / target_ram, 4),
    }


def build_api_params(name: ProfileName) -> dict[str, Any]:
    """Return the Kubecost query parameters this profile produces.

    Mirrors ``_fetch_request_sizing()`` in tools/kubecost_tools.py — the same key names
    and the same ``to_api_window()`` pass-through. ``filter``, ``offset``, and ``limit``
    are omitted: they come from the caller, not the profile.
    """
    profile = SIZING_PROFILES[name]
    return {
        "algorithmCPU": profile["algorithm_cpu"],
        "algorithmRAM": profile["algorithm_ram"],
        "qCPU": profile["q_cpu"],
        "qRAM": profile["q_ram"],
        "targetCPUUtilization": profile["target_cpu_utilization"],
        "targetRAMUtilization": profile["target_ram_utilization"],
        "window": to_api_window(profile["window"]),
    }


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def check_invariants() -> list[dict[str, Any]]:
    """Check every structural rule the profiles must satisfy."""
    results: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    missing = [n for n in LADDER_ORDER if n not in SIZING_PROFILES]
    if missing:
        add("target ladder", False, f"profile(s) missing from SIZING_PROFILES: {', '.join(missing)}")
    else:
        for key in TARGET_KEYS:
            values = [SIZING_PROFILES[n][key] for n in LADDER_ORDER]
            ok = values[0] < values[1] < values[2]
            shown = " < ".join(f"{n} {v}" for n, v in zip(LADDER_ORDER, values, strict=True))
            add(f"target ladder ({key})", ok, shown if ok else f"expected ascending, got {shown}")

    offenders = [n for n, p in SIZING_PROFILES.items() if p["target_ram_utilization"] > p["target_cpu_utilization"]]
    add(
        "RAM target never exceeds CPU target",
        not offenders,
        "memory is not compressible — an undersized RAM request OOM-kills rather than throttles"
        if not offenders
        else f"squeezes RAM harder than CPU: {', '.join(offenders)}",
    )

    expected_keys = set(DEFAULT_SIZING_PARAMS)
    unpinned = {n: expected_keys ^ set(p) for n, p in SIZING_PROFILES.items() if set(p) != expected_keys}
    add(
        "every profile pins all default keys",
        not unpinned,
        "each profile is readable without cross-referencing the defaults"
        if not unpinned
        else "; ".join(f"{n} differs by {sorted(d)}" for n, d in unpinned.items()),
    )

    production = SIZING_PROFILES.get(BASELINE_PROFILE)
    if production is None:
        add(f"{BASELINE_PROFILE} equals the defaults", False, f"{BASELINE_PROFILE} profile is missing")
    else:
        drifted = {
            k: (v, DEFAULT_SIZING_PARAMS.get(k)) for k, v in production.items() if DEFAULT_SIZING_PARAMS.get(k) != v
        }
        add(
            f"{BASELINE_PROFILE} equals the defaults",
            not drifted,
            "identical to DEFAULT_SIZING_PARAMS"
            if not drifted
            else "; ".join(f"{k}: profile {p!r} vs default {d!r}" for k, (p, d) in drifted.items()),
        )

    min_days = parse_window_days(MIN_QUANTILE_WINDOW)
    short = []
    for name, profile in SIZING_PROFILES.items():
        days = parse_window_days(profile["window"])
        # parse_window_days returns None for RFC3339 ranges and calendar aliases, which
        # cannot be compared without a reference timestamp — assumed to satisfy the minimum.
        if days is not None and min_days is not None and days < min_days:
            short.append(f"{name} ({profile['window']})")
    add(
        f"window >= {MIN_QUANTILE_WINDOW} for quantile algorithms",
        not short,
        "quantile algorithms need the minimum window to produce meaningful statistics"
        if not short
        else f"below the minimum: {', '.join(short)}",
    )

    filtering = [n for n, p in SIZING_PROFILES.items() if p["min_monthly_savings"] is not None]
    add(
        "profiles never apply a savings filter",
        not filtering,
        "min_monthly_savings is None everywhere"
        if not filtering
        else f"profiles applying a filter: {', '.join(filtering)}",
    )

    # Targets are divisors, so 0.0 is a divide-by-zero, not merely an odd choice.
    out_of_range: list[str] = []
    for name, profile in SIZING_PROFILES.items():
        for key in TARGET_KEYS + QUANTILE_KEYS:
            value = profile[key]
            if not 0 < value <= 1:
                out_of_range.append(f"{name}.{key}={value}")
    add(
        "targets and quantiles within (0, 1]",
        not out_of_range,
        "targets are divisors — 0.0 would be a divide-by-zero"
        if not out_of_range
        else f"out of range: {', '.join(out_of_range)}",
    )

    # One vocabulary across the server was a deliberate choice; this keeps it from rotting.
    # Kubecost owns the node-group/quota spelling, so divergence means our side must move back.
    diverged = set(SIZING_PROFILES) ^ set(NODE_GROUP_PROFILE_VALUES)
    add(
        "profile names match the node-group / quota vocabulary",
        not diverged,
        "one sizing vocabulary across every tool"
        if not diverged
        else f"only in one vocabulary: {', '.join(sorted(diverged))}",
    )

    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format(value: Any) -> str:
    return "none" if value is None else str(value)


def _param_cell(key: str, value: Any) -> str:
    """Render a parameter, marking it when it differs from the default."""
    text = _format(value)
    if value != DEFAULT_SIZING_PARAMS.get(key):
        return f"[yellow]{text}*[/yellow]"
    return text


def _profile_table(names: list[ProfileName], title: str, first_column: str, **kwargs: Any) -> Table:
    """Build a table with profiles as columns.

    Transposed on purpose: profile names and algorithm values are long enough that a
    row-per-profile layout truncates on an 80-column terminal.
    """
    table = Table(title=title, title_style="bold", header_style="bold", **kwargs)
    # overflow="fold" wraps long names instead of ellipsizing them — a truncated
    # "target_cpu_utili…" is exactly the kind of ambiguity this script exists to remove.
    table.add_column(first_column, style="white", overflow="fold")
    for name in names:
        table.add_column(name, style="cyan", justify="right", overflow="fold")
    return table


def _split_uniform(
    names: list[ProfileName],
    values: dict[ProfileName, dict[str, Any]],
    keys: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Split *keys* into those identical across every shown profile and those that vary.

    Used only for the algorithm keys: they hold by far the widest values and are the
    same in every profile today, so repeating them per column just crowds the table off
    an 80-column terminal. If one ever diverges it is promoted back to a row.
    """
    uniform = [k for k in keys if len({values[n][k] for n in names}) == 1]
    return uniform, [k for k in keys if k not in uniform]


def render_parameters(console: Console, names: list[ProfileName]) -> None:
    profiles = {n: SIZING_PROFILES[n] for n in names}
    uniform, varying = _split_uniform(names, profiles, ("algorithm_cpu", "algorithm_ram"))

    table = _profile_table(names, "Sizing profile parameters", "parameter")
    for key in DEFAULT_SIZING_PARAMS:
        if key in uniform:
            continue
        table.add_row(key, *(_param_cell(key, profiles[n][key]) for n in names))
    console.print(table)

    if uniform:
        shared = ", ".join(f"{k}={profiles[names[0]][k]}" for k in uniform)
        console.print(f"[white]same for every profile shown: {shared}[/white]")
    if varying:
        console.print(f"[white]differs between profiles: {', '.join(varying)}[/white]")
    console.print("[white][yellow]*[/yellow] differs from DEFAULT_SIZING_PARAMS[/white]\n")


def render_derived(console: Console, names: list[ProfileName], usage_cpu: float, usage_ram: float) -> None:
    table = _profile_table(
        names,
        f"What those targets imply (example usage: {usage_cpu:g}m CPU / {usage_ram:g} MiB)",
        "",
        caption="recommended = usage / target_utilization — a lower target means a larger request",
        caption_style="white",
    )
    derived = {n: build_derived(n, usage_cpu, usage_ram) for n in names}
    rows: list[tuple[str, str]] = [
        ("CPU headroom multiplier", "cpu_headroom_multiplier"),
        ("RAM headroom multiplier", "ram_headroom_multiplier"),
        ("CPU request vs production", "cpu_request_vs_production_pct"),
        ("RAM request vs production", "ram_request_vs_production_pct"),
        ("example CPU request", "example_cpu_millicores"),
        ("example RAM request", "example_ram_mib"),
    ]
    formatters = {
        "cpu_headroom_multiplier": lambda v: f"{v:.2f}x",
        "ram_headroom_multiplier": lambda v: f"{v:.2f}x",
        "cpu_request_vs_production_pct": _signed_pct,
        "ram_request_vs_production_pct": _signed_pct,
        "example_cpu_millicores": lambda v: f"{v:.0f}m",
        "example_ram_mib": lambda v: f"{v:.0f} MiB",
    }
    for label, key in rows:
        table.add_row(label, *(formatters[key](derived[n][key]) for n in names))
    console.print(table)
    console.print(f"[white]{DELTA_CAVEAT}[/white]")
    console.print(f"[white]{EXAMPLE_CAVEAT}[/white]\n")


def _signed_pct(value: float) -> str:
    if abs(value) < 0.5:
        return "baseline"
    color = "green" if value > 0 else "yellow"
    return f"[{color}]{value:+.0f}%[/{color}]"


def render_api_params(console: Console, names: list[ProfileName]) -> None:
    table = _profile_table(
        names,
        "Kubecost API parameters sent",
        "query param",
        caption="mirrors _fetch_request_sizing(); filter/offset/limit come from the caller",
        caption_style="white",
    )
    params = {n: build_api_params(n) for n in names}
    uniform, _ = _split_uniform(names, params, ("algorithmCPU", "algorithmRAM"))
    for key in params[names[0]]:
        if key in uniform:
            continue
        table.add_row(key, *(_format(params[n][key]) for n in names))
    console.print(table)
    if uniform:
        shared = ", ".join(f"{k}={params[names[0]][k]}" for k in uniform)
        console.print(f"[white]same for every profile shown: {shared}[/white]")
    console.print()


def render_invariants(console: Console, results: list[dict[str, Any]]) -> None:
    table = Table(title="Invariants", title_style="bold", header_style="bold")
    table.add_column("", width=1)
    table.add_column("check")
    table.add_column("detail", style="white")
    for result in results:
        marker = "[green]✓[/green]" if result["ok"] else "[red]✗[/red]"
        style = None if result["ok"] else "red"
        table.add_row(marker, result["name"], result["detail"], style=style)
    console.print(table)

    failed = [r for r in results if not r["ok"]]
    if failed:
        console.print(f"\n[bold red]{len(failed)} invariant(s) violated.[/bold red]")
    else:
        console.print(f"\n[green]All {len(results)} invariants hold.[/green]")


def render_vocabulary(console: Console) -> None:
    """Show where these profile names apply, and how the two mechanisms differ.

    One vocabulary, two mechanisms: the container tool expands a profile into the sizing
    knobs above (each individually overridable), while the node-group and quota tools send
    the name to Kubecost untouched.
    """
    shared = sorted(set(PROFILE_DESCRIPTIONS) & set(NODE_GROUP_PROFILE_VALUES))
    table = Table(title="Where these profile names apply", title_style="bold", header_style="bold")
    table.add_column("tool", style="white")
    table.add_column("what `profile` does there")
    table.add_row("get_container_savings_recommendations", "expands into the sizing knobs above")
    table.add_row("get_cluster_rightsizing_recommendations", "passed to Kubecost as an opaque enum")
    table.add_row("get_resource_quota_recommendations", "passed to Kubecost as an opaque enum")
    console.print()
    console.print(table)
    console.print(f"[white]all three accept: {', '.join(shared)}[/white]")
    console.print(
        "[white]Same names, different mechanisms — only the container tool lets you override "
        "individual sizing parameters on top of a profile.[/white]"
    )
    missing = set(PROFILE_DESCRIPTIONS) ^ set(NODE_GROUP_PROFILE_VALUES)
    if missing:
        console.print(f"[yellow]vocabularies have diverged: {', '.join(sorted(missing))}[/yellow]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_payload(names: list[ProfileName], usage_cpu: float, usage_ram: float) -> dict[str, Any]:
    """Assemble the full JSON payload."""
    invariants = check_invariants()
    return {
        "profiles": {n: dict(SIZING_PROFILES[n]) for n in names},
        "descriptions": {n: PROFILE_DESCRIPTIONS[n] for n in names},
        "derived": {n: build_derived(n, usage_cpu, usage_ram) for n in names},
        "api_params": {n: build_api_params(n) for n in names},
        "invariants": invariants,
        "vocabulary": {
            "container_savings_profile": list(PROFILE_DESCRIPTIONS),
            "node_group_and_quota_profile": list(NODE_GROUP_PROFILE_VALUES),
            "aligned": set(PROFILE_DESCRIPTIONS) == set(NODE_GROUP_PROFILE_VALUES),
        },
        "caveats": [DELTA_CAVEAT, EXAMPLE_CAVEAT],
        "example_usage": {"cpu_millicores": usage_cpu, "ram_mib": usage_ram},
        "ok": all(r["ok"] for r in invariants),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect and validate the container sizing profiles.",
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILE_DESCRIPTIONS),
        help="Restrict output to a single profile (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report invariants only and exit non-zero on any violation",
    )
    parser.add_argument(
        "--usage-cpu",
        type=float,
        default=DEFAULT_USAGE_CPU_MILLICORES,
        help=f"Worked-example CPU usage in millicores (default: {DEFAULT_USAGE_CPU_MILLICORES:g})",
    )
    parser.add_argument(
        "--usage-ram",
        type=float,
        default=DEFAULT_USAGE_RAM_MIB,
        help=f"Worked-example memory usage in MiB (default: {DEFAULT_USAGE_RAM_MIB:g})",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.usage_cpu <= 0 or args.usage_ram <= 0:
        print("error: --usage-cpu and --usage-ram must be greater than 0", file=sys.stderr)
        return 2

    # Filter the typed key list rather than casting the raw argument.
    names: list[ProfileName] = [n for n in PROFILE_DESCRIPTIONS if args.profile is None or n == args.profile]

    if args.check:
        invariants = check_invariants()
        ok = all(r["ok"] for r in invariants)
        if args.json:
            print(json.dumps({"invariants": invariants, "ok": ok}, indent=2))
        else:
            render_invariants(Console(no_color=args.no_color), invariants)
        return 0 if ok else 1

    payload = build_payload(names, args.usage_cpu, args.usage_ram)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    console = Console(no_color=args.no_color)
    console.print()
    render_parameters(console, names)
    render_derived(console, names, args.usage_cpu, args.usage_ram)
    render_api_params(console, names)
    render_invariants(console, payload["invariants"])
    render_vocabulary(console)
    console.print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
