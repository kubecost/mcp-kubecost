"""Kubecost allocation & savings tools.

Returns fully-typed Pydantic structured output — no in-memory
resource store. Rows are returned directly in the response payload; large
result sets are bounded via a ``top_n`` parameter with a ``truncated`` flag
(client-side sort+slice), or true server-side ``limit``/``offset`` pagination
where the upstream API supports it (e.g. ``get_resource_quota_recommendations``).

Contract version: 8.0
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.domain.kubecost.sizing_guidance import (
    CONTAINER_SIZING_GUIDE,
    CONTAINER_SIZING_REFERENCE,
    FIELD_DESCRIPTIONS,
    PROFILE_DESCRIPTIONS,
    ProfileName,
    build_result_interpretation,
    format_profiles_resource,
    resolve_sizing_params,
)
from mcp_kubecost.errors import ErrorCode
from mcp_kubecost.tools._common import (
    DEFAULT_WINDOW,
    MIN_QUANTILE_WINDOW,
    BaseToolResponse,
    CostRowStatus,
    McpToolError,
    QueryStatus,
    ResolvedWindow,
    call_get_api,
    normalize_window_order,
    parse_api_timestamp,
    parse_window_days,
    raise_tool_error,
    resolve_window,
    resolved_window_from_api,
    to_api_window,
)

logger = logging.getLogger(__name__)

_VERSION = "8.0"

# ---------------------------------------------------------------------------
# API path segments — combined with get_settings().kubecost_api_base_path at call time
# ---------------------------------------------------------------------------

_SEG_ALLOCATION = "/allocation"
_SEG_CONTAINER_SAVINGS = "/savings/requestSizingV2"
_SEG_ABANDONED_WORKLOADS = "/savings/abandonedWorkloads"
_SEG_SAVINGS_OVERVIEW = "/savings"
_SEG_PV_SIZING = "/savings/persistentVolumeSizing"
_SEG_LOCAL_DISKS = "/savings/localLowDisks"
_SEG_NODE_GROUP_SIZING = "/savings/nodeGroupSizing/recommendations"
_SEG_UNCLAIMED_VOLUMES = "/savings/unclaimedVolumes"
_SEG_RESOURCE_QUOTA = "/savings/resourceQuotaSizing/recommendations"


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


# ---------------------------------------------------------------------------
# UI constants
# ---------------------------------------------------------------------------

_AGGREGATE_CHOICES = {
    "cluster": "Total spend per cluster",
    "namespace": "Spend per namespace (across all clusters)",
    "cluster,namespace": "Spend per namespace broken down by cluster",
    "pod": "Spend per pod",
    "node": "Spend per node",
    "controller": "Spend per controller (Deployment, DaemonSet, etc.)",
    "label": "Spend per label",
}

_WINDOW_CHOICES = {
    "7d": "Last 7 days",
    "15d": "Last 15 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "today": "Today",
    "week": "This calendar week",
    "month": "This calendar month",
    "lastweek": "Last calendar week",
    "lastmonth": "Last calendar month",
}

_WINDOW_RFC3339_NOTE = "Also accepts an RFC3339 range, e.g. '2026-05-01T00:00:00Z,2026-06-01T00:00:00Z'."

_SAVINGS_FILTER_CLARIFICATION = """\
FILTER PREFERENCES
Description of the filtering of results. Ask for clarification or use this detail to explain results as needed:

---
**Container savings filter (`min_monthly_savings`):**

Keeps rows where `monthlySavings_total >= min_monthly_savings`.

- **Omit / null (default)** — return every recommendation, including undersized (negative savings).
- **`5.0` (recommended for noise reduction)** — focus on material savings opportunities.
- **Negative value** (e.g. `-100`) — also keep undersized workloads whose rightsizing would
  increase cost by up to that amount.

Profiles do not change this filter — every profile is filter-free unless you pass a value.
---
"""

_CONTAINER_SAVINGS_WINDOW_CLARIFICATION = f"""\
15 days is used by default.
When using quantileOfAverages or quantileOfMaxes, a minimum of 15 days is REQUIRED and enforced — \
passing a shorter window will raise an error. Use 15d, 30d, 90d or an RFC3339 range spanning ≥ 15 days.

When using Max, the only requirement is a window of 1 day or more.
1d, 3d, 7d, 15d, 30d or an RFC3339 range. {_WINDOW_RFC3339_NOTE}
"""

# ---------------------------------------------------------------------------
# Allocation parsing
# ---------------------------------------------------------------------------

# Cost fields extracted from each allocation entry (in display order).
COST_FIELDS: list[str] = [
    "cpuCost",
    "cpuCostIdle",
    "ramCost",
    "ramCostIdle",
    "networkCost",
    "pvCost",
    "gpuCost",
    "gpuCostIdle",
    "loadBalancerCost",
    "sharedCost",
    "totalCost",
    "totalEfficiency",
]

# Known Kubecost property keys that represent aggregation dimensions.
_KNOWN_DIMENSIONS: list[str] = [
    "cluster",
    "node",
    "namespace",
    "pod",
    "container",
    "controller",
    "controllerKind",
    "providerID",
    "services",
    "department",
    "environment",
    "owner",
    "product",
    "team",
    "label",
    "annotation",
]

# Aggregate token prefixes whose values live in a nested properties sub-dict.
_NESTED_DIMENSION_GROUPS: dict[str, str] = {
    "label": "labels",
    "annotation": "annotations",
}


def _dimension_columns_for_aggregate(aggregate: str) -> list[tuple[str, str | None]]:
    """Split a Kubecost ``aggregate`` string into ordered (column, property_group) pairs.

    The requested aggregate is the authoritative source of dimension names — the
    response alone cannot be trusted, since synthetic entries (``__idle__``,
    ``__unallocated__``) carry no usable ``properties``.

    ``"cluster,namespace"`` -> ``[("cluster", None), ("namespace", None)]``
    ``"label:app"``         -> ``[("app", "labels")]``, i.e. ``properties["labels"]["app"]``
    """
    columns: list[tuple[str, str | None]] = []
    for token in aggregate.split(","):
        token = token.strip()
        if not token:
            continue
        prefix, sep, key = token.partition(":")
        group = _NESTED_DIMENSION_GROUPS.get(prefix) if sep else None
        if group and key:
            columns.append((key, group))
        else:
            columns.append((token, None))
    return columns


def _format_number(value: float) -> int | float:
    """Return an int if the value is whole, otherwise round to 2 decimal places."""
    if value == int(value):
        return int(value)
    return round(value, 2)


def _format_date(iso_string: str) -> str:
    """Convert an ISO datetime string to YYYY-MM-DD; return the original on failure."""
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return iso_string


def _format_end_date(iso_string: str) -> str:
    """Convert Kubecost's exclusive window end to the inclusive last day it covers.

    Kubecost reports a 7-day window ending on Aug 7 as ``2026-08-08T00:00:00Z``.
    Stepping back one microsecond — the same way ``ResolvedWindow.display_end`` is
    derived — keeps row-level ``window_end`` in agreement with ``resolved_window``.
    """
    if not iso_string:
        return ""
    parsed = parse_api_timestamp(iso_string)
    if parsed is None:
        return iso_string
    return (parsed - timedelta(microseconds=1)).strftime("%Y-%m-%d")


def _resolve_window_defensively(window: str) -> ResolvedWindow | None:
    """Resolve a query window without preventing a valid upstream query."""
    try:
        return resolve_window(window)
    except McpToolError as exc:
        logger.warning("Could not resolve window '%s' for display: %s", window, exc)
        return None


def _window_from_allocation(response: dict[str, Any], source_expression: str) -> ResolvedWindow | None:
    """Read the window Kubecost actually queried out of an allocation response.

    Every allocation entry carries the server's own ``window`` object, which is
    authoritative. Prefer it over the client-side prediction so a divergence
    can never be reported to the user as fact.

    An accumulated response holds one bucket spanning the whole query, but a
    non-accumulated one holds a bucket per step — so the queried range is the
    span from the earliest start to the latest end across every bucket, not the
    first bucket's own (one-step) window. Entries within a bucket all share that
    bucket's window, so only the first usable one is read per bucket.
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    for bucket in response.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        for entry in bucket.values():
            if not isinstance(entry, dict):
                continue
            window = entry.get("window")
            if not isinstance(window, dict):
                continue
            start = parse_api_timestamp(window.get("start"))
            end = parse_api_timestamp(window.get("end"))
            if start is not None and end is not None:
                starts.append(start)
                ends.append(end)
                break
    if not starts:
        return None
    span = {"start": min(starts).isoformat(), "end": max(ends).isoformat()}
    return resolved_window_from_api(span, source_expression)


def _parse_allocation_response(
    response: dict[str, Any],
    aggregate: str = "",
) -> tuple[list[str], list[dict]]:
    """Parse a Kubecost allocation API response into (dimension_columns, rows).

    Dimension names come from the requested ``aggregate`` when given. Without it,
    they are discovered from the union of ``properties`` keys across all entries,
    falling back to splitting ``name`` by ``/`` when no known property keys appear
    anywhere.

    Each row resolves a dimension from ``properties`` first, then from the entry's
    ``name`` — so synthetic entries such as ``__idle__``, whose ``properties`` are
    empty, still report their name instead of collapsing into a blank key.
    """
    data = response.get("data")
    if not isinstance(data, list):
        return [], []

    all_entries: list[dict] = []
    for bucket in data:
        if not isinstance(bucket, dict):
            continue
        all_entries.extend(entry for entry in bucket.values() if isinstance(entry, dict))

    if not all_entries:
        return [], []

    columns = _dimension_columns_for_aggregate(aggregate)

    if not columns:
        # No aggregate to go by: take every known dimension present on any entry.
        seen_props: set[str] = set()
        for entry in all_entries:
            seen_props.update(entry.get("properties", {}))
        columns = [(k, None) for k in _KNOWN_DIMENSIONS if k in seen_props]

    if not columns:
        parts = all_entries[0].get("name", "").split("/")
        columns = [(f"dim_{i}", None) for i in range(len(parts))]

    dimension_cols = [col for col, _ in columns]

    rows: list[dict] = []
    for entry in all_entries:
        row: dict = {}
        props = entry.get("properties", {})
        name_parts = entry.get("name", "").split("/")
        # Positional fallback only lines up when the name has one part per column.
        positional = name_parts if len(name_parts) == len(columns) else []

        for i, (col, group) in enumerate(columns):
            if group:
                val = (props.get(group) or {}).get(col)
            else:
                val = props.get(col)
            if val is None or val == "":
                val = positional[i] if positional else ""
            if isinstance(val, list):
                val = "|".join(val)
            row[col] = val

        window_dict = entry.get("window", {})
        row["window_start"] = _format_date(window_dict.get("start", ""))
        row["window_end"] = _format_end_date(window_dict.get("end", ""))

        for field in COST_FIELDS:
            value = entry.get(field, 0.0)
            row[field] = _format_number(float(value))

        rows.append(row)

    return dimension_cols, rows


def _aggregate_by_dimensions(rows: list[dict], dimension_cols: list[str]) -> list[dict]:
    """Sum cost fields across rows sharing the same dimension values *and* window.

    The window is part of the grouping key so a non-accumulated response keeps
    its per-step rows instead of collapsing into a single total stamped with one
    step's date. An accumulated response has one window throughout, so grouping
    is unchanged for it.

    Returns rows sorted by ``window_start`` ascending then ``totalCost``
    descending, with idle-% columns derived from summed idle vs total figures.
    """
    groups: dict[tuple, dict[str, Any]] = defaultdict(lambda: defaultdict(float))

    for row in rows:
        window = (row.get("window_start", ""), row.get("window_end", ""))
        key = tuple(row.get(dim, "") for dim in dimension_cols) + window
        for field in COST_FIELDS:
            if field != "totalEfficiency":
                value = row.get(field, 0)
                if isinstance(value, (int, float)):
                    groups[key][field] += float(value)
        for dim in dimension_cols:
            groups[key][dim] = row.get(dim, "")
        groups[key]["window_start"], groups[key]["window_end"] = window

    aggregated: list[dict] = []
    for values in groups.values():
        row = dict(values)

        cpu_total = row.get("cpuCost", 0)
        ram_total = row.get("ramCost", 0)
        gpu_total = row.get("gpuCost", 0)

        row["cpuIdlePct"] = f"{(row.get('cpuCostIdle', 0) / cpu_total * 100):.1f}%" if cpu_total > 0 else "0%"
        row["ramIdlePct"] = f"{(row.get('ramCostIdle', 0) / ram_total * 100):.1f}%" if ram_total > 0 else "0%"
        row["gpuIdlePct"] = f"{(row.get('gpuCostIdle', 0) / gpu_total * 100):.1f}%" if gpu_total > 0 else "0%"

        total_cost = row.get("totalCost", 0)
        if total_cost > 0:
            total_idle = row.get("cpuCostIdle", 0) + row.get("ramCostIdle", 0) + row.get("gpuCostIdle", 0)
            row["totalIdlePct"] = f"{(total_idle / total_cost * 100):.1f}%"
        else:
            row["totalIdlePct"] = "0%"

        aggregated.append(row)

    # Chronological first so a per-step breakdown reads as a series and a top_n
    # slice keeps a contiguous prefix of it; within a step, costliest first. With
    # a single window (the accumulated case) this is plain totalCost descending.
    aggregated.sort(key=lambda r: (r.get("window_start", ""), -float(r.get("totalCost", 0))))

    for row in aggregated:
        for field in COST_FIELDS:
            if field in row and field != "totalEfficiency":
                row[field] = _format_number(row[field])

    return aggregated


# ---------------------------------------------------------------------------
# Cost comparison window validation & diffing (get_kubecost_cost_comparison)
# ---------------------------------------------------------------------------

# Bare relative windows ("7d", "15d", ...) are resolved by Kubecost relative to
# "now", so they always include a partial current day. Reject these for
# period-over-period comparisons since the partial day skews the diff.
_BARE_RELATIVE_WINDOW = re.compile(r"^\d+d$")

# All named/alias windows — both relative-to-now ones ('today', 'week', 'month') and
# fixed-calendar ones ('lastweek', 'lastmonth') — are rejected. lastweek/lastmonth are
# technically safe for a single-period fetch, but there is no Kubecost alias for
# "the period before lastmonth", making them a dead end for comparisons. RFC3339
# ranges are always explicit and unambiguous.
_REJECTED_ALIASES: frozenset[str] = frozenset({"today", "week", "month", "lastweek", "lastmonth"})


def _classify_comparison_window(window: str, field_name: str) -> tuple[str, Any]:
    """Classify a comparison window as ('rfc3339', (start, end)).

    Raises a structured ToolError for any window that is unsuitable for a
    period-over-period comparison — only explicit RFC3339 ranges are accepted.
    """
    raw = normalize_window_order((window or "").strip())
    lower = raw.lower()

    if _BARE_RELATIVE_WINDOW.match(lower):
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            message=(
                f"{field_name}='{window}' is a bare relative window. Kubecost resolves windows like "
                "'7d' relative to 'now', so they always include a partial current day and skew a diff."
            ),
            retryable=False,
            suggested_action=(
                f"Use an explicit RFC3339 range for {field_name} "
                "(e.g. '2026-06-01T00:00:00Z,2026-06-08T00:00:00Z') that ends before today."
            ),
        )

    if lower in _REJECTED_ALIASES:
        raise_tool_error(
            ErrorCode.INVALID_INPUT,
            message=(
                f"{field_name}='{window}' is a named alias. Named aliases are not accepted for comparisons "
                "because there is no corresponding alias for the preceding period."
            ),
            retryable=False,
            suggested_action=(
                f"Use an explicit RFC3339 range for {field_name} that ends before today, "
                "e.g. '2026-06-01T00:00:00Z,2026-06-08T00:00:00Z'."
            ),
        )

    if "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise_tool_error(
                ErrorCode.INVALID_INPUT,
                message=f"{field_name}='{window}' is not a valid RFC3339 range. Expected 'start,end'.",
                retryable=False,
                suggested_action=(
                    f"Provide {field_name} as an RFC3339 range e.g. '2026-06-01T00:00:00Z,2026-06-08T00:00:00Z'."
                ),
            )
        start_str, end_str = parts
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            raise_tool_error(
                ErrorCode.INVALID_INPUT,
                message=f"{field_name}='{window}' contains an invalid RFC3339 timestamp.",
                retryable=False,
                suggested_action=(
                    f"Provide {field_name} as an RFC3339 range e.g. '2026-06-01T00:00:00Z,2026-06-08T00:00:00Z'."
                ),
            )
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        if end <= start:
            raise_tool_error(
                ErrorCode.INVALID_INPUT,
                message=f"{field_name}='{window}' has an end timestamp that is not after the start.",
                retryable=False,
                suggested_action=f"Provide {field_name} as 'start,end' with end after start.",
            )
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if end > today_start:
            raise_tool_error(
                ErrorCode.INVALID_INPUT,
                message=(
                    f"{field_name}='{window}' extends into today (end={end.isoformat()}), which includes "
                    "partial/incomplete data and skews a diff."
                ),
                retryable=False,
                suggested_action=(
                    f"Provide {field_name} with an end timestamp at or before {today_start.isoformat()} "
                    "(the start of today, UTC)."
                ),
            )
        return "rfc3339", (start, end)

    raise_tool_error(
        ErrorCode.INVALID_INPUT,
        message=f"{field_name}='{window}' is not a valid comparison window.",
        retryable=False,
        suggested_action=(
            f"Provide {field_name} as an RFC3339 range ending before today, "
            "e.g. '2026-06-01T00:00:00Z,2026-06-08T00:00:00Z'."
        ),
    )


def _validate_comparison_windows(current_window: str, baseline_window: str) -> tuple[int, int]:
    """Validate that current_window and baseline_window are suitable for comparison.

    Enforces (raising a structured ToolError on any violation):
    - Neither window may be a named alias or bare relative window — only RFC3339 ranges
      are accepted. Named aliases like 'lastweek'/'lastmonth' are rejected because
      there is no corresponding alias for the preceding period.
    - RFC3339 ranges must not extend into today (partial data).

    Unequal durations are NOT an error — the caller is responsible for adding a
    warning in the response.

    Returns (current_days, baseline_days) so callers can detect and warn on mismatches.
    """
    _, current_value = _classify_comparison_window(current_window, "current_window")
    _, baseline_value = _classify_comparison_window(baseline_window, "baseline_window")

    current_start, current_end = current_value
    baseline_start, baseline_end = baseline_value
    current_days = (current_end - current_start).days
    baseline_days = (baseline_end - baseline_start).days
    return current_days, baseline_days


def _default_wow_windows() -> tuple[str, str]:
    """Return (current_window, baseline_window) as RFC3339 ranges for a week-over-week comparison.

    current_window  = the 7-day period ending at yesterday midnight UTC
                      (yesterday-6 days 00:00Z → yesterday+1 00:00Z, i.e. today 00:00Z)
    baseline_window = the 7-day period immediately before that

    Both ranges end before today so they never include a partial in-progress day.
    """
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    week_start = yesterday - timedelta(days=6)  # inclusive start of current_window
    prev_start = week_start - timedelta(weeks=1)  # inclusive start of baseline_window

    def _fmt(d) -> str:  # noqa: ANN001
        return d.strftime("%Y-%m-%dT00:00:00Z")

    # Ranges are [start, end) where end is the exclusive boundary (midnight of the day after)
    current = f"{_fmt(week_start)},{_fmt(today)}"
    baseline = f"{_fmt(prev_start)},{_fmt(week_start)}"
    return current, baseline


# Kubecost's bucket for cost that carries no value for the requested dimension.
_UNALLOCATED = "__unallocated__"

# Idle capacity is shared into every row (see _fetch_allocation), which the caller cannot infer.
_IDLE_SHARED_NOTE = (
    "Idle (unused but provisioned) capacity is distributed proportionally into each row's cost, "
    "so the rows will not sum to a separate idle line."
)


def _window_days(resolved: ResolvedWindow | None, fallback: int | None) -> float:
    """Return a window's length in days as a positive float, for use as a divisor.

    Prefers the resolved window, whose boundaries come back from Kubecost itself,
    and measures the exact span rather than ``ResolvedWindow.days`` — that field
    is an int and rounds a sub-day span down to 0. Falls back to the caller's
    validated day count, then to 1.0, so a per-day figure is never a division by zero.
    """
    if resolved is not None:
        span = (resolved.end_utc - resolved.start_utc).total_seconds() / 86400
        if span > 0:
            return span
    if fallback is not None and fallback > 0:
        return float(fallback)
    return 1.0


def _unallocated_note(diffed: list[dict[str, Any]], dimension_cols: list[str]) -> str | None:
    """Describe the cost that Kubecost could not attribute to a requested dimension.

    Returns None when no row is unallocated, so the note only appears where it
    is relevant.
    """
    current_total = 0.0
    baseline_total = 0.0
    unallocated_dims: list[str] = []
    for row in diffed:
        hit = [dim for dim in dimension_cols if row.get(dim) == _UNALLOCATED]
        if not hit:
            continue
        current_total += float(row.get("current_cost", 0) or 0)
        baseline_total += float(row.get("baseline_cost", 0) or 0)
        unallocated_dims.extend(dim for dim in hit if dim not in unallocated_dims)

    if not unallocated_dims:
        return None

    dims = ", ".join(f"'{dim}'" for dim in unallocated_dims)
    return (
        f"${current_total:,.2f} (current) and ${baseline_total:,.2f} (baseline) have no value for "
        f"{dims} and are grouped under {_UNALLOCATED}. That bucket is real spend, not an error — "
        "it cannot be attributed further at this aggregation."
    )


def _classify_cost_row(current_cost: float, baseline_cost: float) -> CostRowStatus:
    """Describe how a dimension's cost moved between the two windows.

    A dimension absent from one side arrives here as a 0.0 cost, so "appeared"
    and "disappeared" are both expressed as a zero on the opposite side. A row
    that is zero in *both* windows is ``UNCHANGED``, not ``NEW`` — nothing
    appeared.
    """
    if baseline_cost == 0 and current_cost > 0:
        return CostRowStatus.NEW
    if baseline_cost > 0 and current_cost == 0:
        return CostRowStatus.REMOVED
    if current_cost == baseline_cost:
        return CostRowStatus.UNCHANGED
    return CostRowStatus.CHANGED


def _diff_allocation_rows(
    current_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    dimension_cols: list[str],
    current_days: float = 1.0,
    baseline_days: float = 1.0,
) -> list[dict[str, Any]]:
    """Join current and baseline aggregated allocation rows on their dimension tuple.

    Returns one dict per dimension key present in either input, each with the
    dimension values, ``current_cost``, ``baseline_cost``, ``change``
    (current - baseline), ``pct_change`` (None when baseline_cost is 0, since a
    percentage of nothing is undefined), and a ``row_status`` describing the
    move. Sorted by absolute ``change`` descending.

    ``current_days``/``baseline_days`` are the lengths of the two windows, used
    to derive the per-day figures that make unequal-length periods comparable:
    a 31-day month costing more than a 30-day one has not necessarily got more
    expensive. Sorting deliberately stays on raw ``change`` so the common
    equal-length comparison keeps its familiar ordering.
    """

    def _key(row: dict[str, Any]) -> tuple:
        return tuple(row.get(dim, "") for dim in dimension_cols)

    current_by_key: dict[tuple, dict[str, Any]] = {_key(r): r for r in current_rows}
    baseline_by_key: dict[tuple, dict[str, Any]] = {_key(r): r for r in baseline_rows}

    all_keys = set(current_by_key) | set(baseline_by_key)
    diffed: list[dict[str, Any]] = []

    for key in all_keys:
        current_row = current_by_key.get(key)
        baseline_row = baseline_by_key.get(key)

        current_cost = float((current_row or {}).get("totalCost", 0) or 0)
        baseline_cost = float((baseline_row or {}).get("totalCost", 0) or 0)
        change = current_cost - baseline_cost

        pct_change: float | None = None
        if baseline_cost != 0:
            pct_change = round((change / baseline_cost) * 100, 2)

        current_daily = current_cost / current_days if current_days > 0 else 0.0
        baseline_daily = baseline_cost / baseline_days if baseline_days > 0 else 0.0
        normalized_pct_change: float | None = None
        if baseline_daily != 0:
            normalized_pct_change = round(((current_daily - baseline_daily) / baseline_daily) * 100, 2)

        source_row = current_row or baseline_row or {}
        row: dict[str, Any] = {dim: source_row.get(dim, "") for dim in dimension_cols}
        row.update(
            {
                "current_cost": _format_number(current_cost),
                "baseline_cost": _format_number(baseline_cost),
                "change": _format_number(change),
                "pct_change": pct_change,
                "row_status": _classify_cost_row(current_cost, baseline_cost),
                "current_daily_cost": _format_number(current_daily),
                "baseline_daily_cost": _format_number(baseline_daily),
                "daily_change": _format_number(current_daily - baseline_daily),
                "normalized_pct_change": normalized_pct_change,
            }
        )
        diffed.append(row)

    diffed.sort(key=lambda r: abs(float(r.get("change", 0) or 0)), reverse=True)
    return diffed


# ---------------------------------------------------------------------------
# Container savings parsing
# ---------------------------------------------------------------------------

_SAVINGS_API_FETCH_LIMIT = 1000  # Maximum rows to request from the Kubecost API per call

# Algorithms that require a minimum window of MIN_QUANTILE_WINDOW days.
_QUANTILE_ALGORITHMS: frozenset[str] = frozenset({"quantileofaverages", "quantileofmaxes"})


def _cap_raw_rows(rows: list[dict[str, Any]], resource: str) -> tuple[list[dict[str, Any]], bool]:
    """Defensively cap an upstream row list for endpoints with no server-side limit/offset support.

    Returns (capped_rows, was_capped) so callers can propagate the truncation signal.
    """
    if len(rows) > _SAVINGS_API_FETCH_LIMIT:
        logger.warning(
            "Kubecost returned %d %s rows; truncating to %d before processing.",
            len(rows),
            resource,
            _SAVINGS_API_FETCH_LIMIT,
        )
        return rows[:_SAVINGS_API_FETCH_LIMIT], True
    return rows, False


def _classify_node_recommendation(rec_str: str, before_state: NodeGroupState, after_state: NodeGroupState) -> str:
    """Classify a node group recommendation as ``'cost_saving'`` or ``'capacity'``.

    ScaleOut always adds nodes (cost increase), so it is ``'capacity'``.
    For all other types, compare the actual before/after monthly prices: if the
    recommended state costs more than the current one it is a capacity recommendation.
    A zero delta (no-op / ``'None'`` recommendation) is ``'cost_saving'`` with 0 savings.
    """
    if rec_str.lower() == "scaleout":
        return "capacity"
    delta = after_state.price_per_month - before_state.price_per_month
    return "capacity" if delta > 0 else "cost_saving"


SAVINGS_METADATA_FIELDS: list[str] = [
    "clusterID",
    "namespace",
    "controllerKind",
    "controllerName",
    "containerName",
]

NOTE_MEM_RECOMMENDATION_LESS_THAN_MAX = "memRecommendationLessThanMax"


def _float_field(row: dict, key: str) -> float:
    """Safely coerce a row field to float, returning 0.0 on any failure."""
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_savings_notes(row: dict) -> str:
    """Build a semicolon-separated notes string for a savings recommendation row."""
    notes: list[str] = []
    if _float_field(row, "Recommended_memoryInMiB") < _float_field(row, "MaxUsage_memoryInMiB"):
        notes.append(NOTE_MEM_RECOMMENDATION_LESS_THAN_MAX)
    return ";".join(notes)


def aggregate_savings_by(rows: list[dict], group_key: str) -> list[dict]:
    """Aggregate savings rows by a single dimension, summing ``monthlySavings_total``.

    Returns rows sorted by ``monthlySavings_total`` descending.  Each output row
    contains ``{group_key, monthlySavings_total, container_count, notes}``.
    """
    groups: dict[str, dict] = defaultdict(
        lambda: {"monthlySavings_total": 0.0, "container_count": 0, "notes_set": set()}
    )

    for row in rows:
        key_val = row.get(group_key, "")
        groups[key_val]["monthlySavings_total"] += float(row.get("monthlySavings_total", 0) or 0)
        groups[key_val]["container_count"] += 1
        groups[key_val][group_key] = key_val
        for note in (row.get("notes") or "").split(";"):
            if note:
                groups[key_val]["notes_set"].add(note)

    aggregated: list[dict] = []
    for values in groups.values():
        values["monthlySavings_total"] = _format_number(values["monthlySavings_total"])
        values["notes"] = ";".join(sorted(values.pop("notes_set")))
        aggregated.append(dict(values))

    aggregated.sort(key=lambda r: float(r.get("monthlySavings_total", 0) or 0), reverse=True)
    return aggregated


def parse_request_sizing_response(
    response: dict,
) -> tuple[float, int, list[dict]]:
    """Parse a Kubecost requestSizingV2 response into flat rows.

    Returns:
        total_monthly_savings: top-level TotalMonthlySavings value.
        count: top-level Count value.
        rows: flat dicts sorted by monthlySavings_total descending.
    """
    total_monthly_savings: float = float(response.get("TotalMonthlySavings", 0.0))
    count: int = int(response.get("Count", 0))
    recommendations: list[dict] = response.get("Recommendations", [])

    def _nest(rec: dict, row: dict, obj_key: str, sub_key: str, col: str) -> None:
        obj = rec.get(obj_key, {}) or {}
        val = obj.get(sub_key, 0.0)
        row[col] = _format_number(float(val)) if isinstance(val, (int, float)) else val

    rows: list[dict] = []
    for rec in recommendations:
        row: dict = {}

        for field in SAVINGS_METADATA_FIELDS:
            row[field] = rec.get(field, "")

        _nest(rec, row, "monthlySavings", "cpu", "monthlySavings_cpu")
        _nest(rec, row, "monthlySavings", "memory", "monthlySavings_memory")
        _nest(rec, row, "monthlySavings", "total", "monthlySavings_total")
        _nest(rec, row, "normalizedRecommendedRequest", "cpuInMilliCores", "Recommended_cpuInMilliCores")
        _nest(rec, row, "normalizedRecommendedRequest", "memoryInMiB", "Recommended_memoryInMiB")
        _nest(rec, row, "normalizedLatestKnownRequest", "cpuInMilliCores", "current_cpuInMilliCores")
        _nest(rec, row, "normalizedLatestKnownRequest", "memoryInMiB", "current_memoryInMiB")
        _nest(rec, row, "currentEfficiency", "cpu", "currentEfficiency_cpu")
        _nest(rec, row, "currentEfficiency", "memory", "currentEfficiency_memory")
        _nest(rec, row, "currentEfficiency", "total", "currentEfficiency")
        _nest(rec, row, "normalizedAverageUsage", "cpuInMilliCores", "AvgUsage_cpuInMilliCores")
        _nest(rec, row, "normalizedAverageUsage", "memoryInMiB", "AvgUsage_memoryInMiB")
        _nest(rec, row, "normalizedMaxUsage", "cpuInMilliCores", "MaxUsage_cpuInMilliCores")
        _nest(rec, row, "normalizedMaxUsage", "memoryInMiB", "MaxUsage_memoryInMiB")

        row["notes"] = compute_savings_notes(row)
        rows.append(row)

    rows.sort(
        key=lambda r: float(r.get("monthlySavings_total", 0) or 0),
        reverse=True,
    )
    return total_monthly_savings, count, rows


# ---------------------------------------------------------------------------
# Response models (Rule #6)
# ---------------------------------------------------------------------------


class WindowOption(BaseModel):
    """One selectable time window."""

    value: str = Field(description="Window token to pass as the 'window' parameter.")
    label: str = Field(description="Human-readable description of the window.")
    resolved: ResolvedWindow | None = Field(
        default=None,
        description=(
            "Concrete UTC range this token maps to as of now, including the day count and whether "
            "the period is still in progress. Null if resolution failed."
        ),
    )


class WindowOptionsResponse(BaseToolResponse):
    """Response from kubecost_list_windows."""

    windows: list[WindowOption] = Field(description="Valid time-window options.")
    note: str = Field(description="Additional accepted formats (e.g. RFC3339 ranges).")


class AllocationRow(BaseModel):
    """One aggregated allocation row returned by get_kubecost_workload_costs.

    Stable cost fields are typed explicitly. Dynamic dimension fields (cluster,
    namespace, pod, label, etc.) are carried as extra fields via ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    window_start: str = Field(
        default="",
        description="First date covered by this row (YYYY-MM-DD).",
    )
    window_end: str = Field(
        default="",
        description=(
            "Last date covered by this row (YYYY-MM-DD, inclusive). Equals window_start "
            "for a single-day row. Empty when the upstream response carried no window end."
        ),
    )
    # --- cost components ---
    cpu_cost: float = Field(
        default=0.0,
        alias="cpuCost",
        description="CPU request cost (USD).",
    )
    cpu_cost_idle: float = Field(
        default=0.0,
        alias="cpuCostIdle",
        description="CPU idle cost — unused capacity billed (USD).",
    )
    ram_cost: float = Field(
        default=0.0,
        alias="ramCost",
        description="RAM request cost (USD).",
    )
    ram_cost_idle: float = Field(
        default=0.0,
        alias="ramCostIdle",
        description="RAM idle cost — unused capacity billed (USD).",
    )
    network_cost: float = Field(
        default=0.0,
        alias="networkCost",
        description="Network egress/ingress cost (USD).",
    )
    pv_cost: float = Field(
        default=0.0,
        alias="pvCost",
        description="Persistent volume storage cost (USD).",
    )
    gpu_cost: float = Field(
        default=0.0,
        alias="gpuCost",
        description="GPU cost (USD).",
    )
    gpu_cost_idle: float = Field(
        default=0.0,
        alias="gpuCostIdle",
        description="GPU idle cost (USD).",
    )
    load_balancer_cost: float = Field(
        default=0.0,
        alias="loadBalancerCost",
        description="Load balancer cost (USD).",
    )
    shared_cost: float = Field(
        default=0.0,
        alias="sharedCost",
        description="Shared namespace overhead allocation (USD).",
    )
    total_cost: float = Field(
        default=0.0,
        alias="totalCost",
        description="Sum of all cost components (USD).",
    )
    # --- computed idle percentages ---
    cpu_idle_pct: str = Field(
        default="0%",
        alias="cpuIdlePct",
        description="Percentage of CPU cost that is idle (e.g. '20.0%').",
    )
    ram_idle_pct: str = Field(
        default="0%",
        alias="ramIdlePct",
        description="Percentage of RAM cost that is idle (e.g. '15.0%').",
    )
    gpu_idle_pct: str = Field(
        default="0%",
        alias="gpuIdlePct",
        description="Percentage of GPU cost that is idle (e.g. '0%').",
    )
    total_idle_pct: str = Field(
        default="0%",
        alias="totalIdlePct",
        description="Percentage of total cost that is idle (e.g. '18.5%').",
    )


class KubecostAllocationResponse(BaseToolResponse):
    """Response from get_kubecost_workload_costs."""

    window: str | None = Field(description="Time window used for the query.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    aggregate: str = Field(description="Aggregation dimension(s) requested.")
    dimensions: list[str] = Field(
        default_factory=list,
        description="Resolved dimension columns present in each result row.",
    )
    total_cost: float = Field(default=0.0, description="Sum of totalCost across all rows (USD).")
    row_count: int = Field(default=0, description="Total number of rows returned.")
    rows: list[AllocationRow] = Field(
        default_factory=list,
        description=(
            "Aggregated allocation rows sorted by window_start ascending then totalCost "
            "descending — with accumulate=true there is a single window, so this is simply "
            "totalCost descending; with accumulate=false there is one row per dimension key "
            "per day, in date order. Each row contains the requested dimension values "
            "(e.g. cluster, namespace), the window_start/window_end dates it covers, "
            "plus cost fields: totalCost, cpuCost, ramCost, networkCost, pvCost, "
            "gpuCost, sharedCost, and idle percentages cpuIdlePct, ramIdlePct, totalIdlePct."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True when the full result set was larger than top_n and rows contains "
            "only the top_n entries. Use a larger top_n or narrow the window/aggregate "
            "to retrieve the full set."
        ),
    )


class ContainerSavingsRow(BaseModel):
    """One per-container rightsizing recommendation."""

    model_config = ConfigDict(extra="allow")

    cluster_id: str = Field(
        default="",
        alias="clusterID",
        description="Cluster the container belongs to.",
    )
    namespace: str = Field(default="", description="Kubernetes namespace.")
    controller_kind: str = Field(
        default="",
        alias="controllerKind",
        description="Controller type (Deployment, StatefulSet, DaemonSet, etc.).",
    )
    controller_name: str = Field(
        default="",
        alias="controllerName",
        description="Name of the controller managing this container.",
    )
    container_name: str = Field(
        default="",
        alias="containerName",
        description="Container name within the pod.",
    )
    monthly_savings_total: float = Field(
        default=0.0,
        alias="monthlySavings_total",
        description="Estimated monthly savings from rightsizing this container (USD). Negative = undersized.",
    )
    monthly_savings_cpu: float = Field(
        default=0.0,
        alias="monthlySavings_cpu",
        description="CPU portion of monthly savings (USD).",
    )
    monthly_savings_memory: float = Field(
        default=0.0,
        alias="monthlySavings_memory",
        description="Memory portion of monthly savings (USD). Negative = memory is undersized.",
    )
    # Quantity aliases embed units (cpuInMilliCores / memoryInMiB) to match Kubecost's
    # normalized nested keys.
    recommended_cpu_in_milli_cores: float = Field(
        default=0.0,
        alias="Recommended_cpuInMilliCores",
        description="Recommended CPU request in millicores.",
    )
    recommended_memory_in_mib: float = Field(
        default=0.0,
        alias="Recommended_memoryInMiB",
        description="Recommended memory request in MiB.",
    )
    current_cpu_in_milli_cores: float = Field(
        default=0.0,
        alias="current_cpuInMilliCores",
        description="Current CPU request in millicores.",
    )
    current_memory_in_mib: float = Field(
        default=0.0,
        alias="current_memoryInMiB",
        description="Current memory request in MiB.",
    )
    current_efficiency_cpu: float = Field(
        default=0.0,
        alias="currentEfficiency_cpu",
        description="CPU utilization ratio (0–1). Low = over-provisioned.",
    )
    current_efficiency_memory: float = Field(
        default=0.0,
        alias="currentEfficiency_memory",
        description="Memory utilization ratio (0–1). Low = over-provisioned.",
    )
    current_efficiency: float = Field(
        default=0.0,
        alias="currentEfficiency",
        description="Combined CPU/memory utilization ratio (0–1). Low = over-provisioned.",
    )
    avg_usage_cpu_in_milli_cores: float = Field(
        default=0.0,
        alias="AvgUsage_cpuInMilliCores",
        description="Average CPU usage over the window in millicores.",
    )
    avg_usage_memory_in_mib: float = Field(
        default=0.0,
        alias="AvgUsage_memoryInMiB",
        description="Average memory usage over the window in MiB.",
    )
    max_usage_cpu_in_milli_cores: float = Field(
        default=0.0,
        alias="MaxUsage_cpuInMilliCores",
        description="Peak CPU usage over the window in millicores.",
    )
    max_usage_memory_in_mib: float = Field(
        default=0.0,
        alias="MaxUsage_memoryInMiB",
        description="Peak memory usage over the window in MiB.",
    )
    notes: str = Field(
        default="",
        description=(
            "Semicolon-separated advisory notes. "
            "'memRecommendationLessThanMax' means the recommended memory is below observed "
            "peak — apply this recommendation with caution."
        ),
    )


class ContainerSavingsSummaryRow(BaseModel):
    """One aggregated container-savings group for the inline summary."""

    model_config = ConfigDict(extra="allow")

    group: str = Field(description="The value of the summary_aggregate dimension.")
    monthly_savings_total: float = Field(description="Total monthly savings for this group (USD).")
    container_count: int = Field(description="Number of containers aggregated into this group.")


class ContainerSavingsResponse(BaseToolResponse):
    """Response from get_container_savings_recommendations."""

    window: str = Field(description="Time window used for the query.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    total_monthly_savings: float = Field(
        description=(
            "Total monthly savings across the FILTERED recommendations (USD) — the same "
            "population described by 'summary' and 'container_count'. Excludes rows removed "
            "by the min_monthly_savings filter (when set)."
        )
    )
    container_count: int = Field(
        description=(
            "Number of container recommendations in the FILTERED result set. This is the "
            "full filtered count and may exceed len(rows) when the result is truncated to "
            "top_n (see 'truncated')."
        )
    )
    summary_aggregate: str = Field(description="Dimension the inline summary is grouped by.")
    summary: list[ContainerSavingsSummaryRow] = Field(
        default_factory=list,
        description=(
            "Groups across the FULL filtered result set (NOT capped by top_n), aggregated "
            "by summary_aggregate and sorted by savings descending. Use this for an "
            "overview; per-container detail is in 'rows' (which IS capped by top_n)."
        ),
    )
    rows: list[ContainerSavingsRow] = Field(
        default_factory=list,
        description=(
            "Per-container rightsizing recommendations (filtered) sorted by "
            "monthly_savings_total descending, capped at the first top_n entries. "
            "Unlike 'summary', this list is limited by top_n — when more exist, "
            "truncated=True. Each row has recommended CPU/memory, current usage, "
            "efficiency, and advisory notes."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True when the filtered result has more than top_n containers. Only 'rows' is "
            "capped at top_n; 'summary', 'total_monthly_savings', and 'container_count' "
            "still reflect the full filtered set. Raise top_n or narrow the filter to see "
            "more per-container detail."
        ),
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Echo of the sizing parameters actually used for this query.",
    )
    interpretation: str = Field(
        default="",
        description="Methodology guidance for reading and acting on these results.",
    )


# ---------------------------------------------------------------------------
# New savings response models (Sub-Tasks 2–7)
# ---------------------------------------------------------------------------


class SavingsCategory(BaseModel):
    """One savings category from GET /model/savings."""

    key: str = Field(description="Kubecost savings category key (e.g. 'nodeGroupSizing').")
    savings_per_month: float = Field(default=0.0, description="Estimated monthly savings (USD).")
    last_refresh: str = Field(default="", description="ISO-8601 timestamp of the last data refresh.")
    drill_down_tool: str | None = Field(default=None, description="MCP tool name to call for detailed recommendations.")


class SavingsOverviewResponse(BaseToolResponse):
    """Response for get_savings_overview."""

    categories: list[SavingsCategory] = Field(
        default_factory=list, description="All savings categories, ranked by savings_per_month."
    )
    total_savings_per_month: float = Field(
        default=0.0, description="Sum of savings_per_month across all categories (USD)."
    )
    category_count: int = Field(default=0, description="Number of categories returned.")


class PVSizingRow(BaseModel):
    """One PVC right-sizing recommendation."""

    volume_name: str = Field(default="", description="PersistentVolume name.")
    claim_name: str = Field(default="", description="PersistentVolumeClaim name.")
    claim_namespace: str = Field(default="", description="Namespace of the PVC.")
    cluster_id: str = Field(default="", description="Kubecost cluster ID.")
    max_usage_bytes: int = Field(default=0, description="Maximum observed usage in bytes.")
    average_usage_bytes: int = Field(default=0, description="Average observed usage in bytes.")
    recommended_capacity_bytes: int = Field(default=0, description="Recommended capacity in bytes.")
    recommended_cost_monthly: float = Field(
        default=0.0, description="Estimated monthly cost at recommended size (USD)."
    )
    current_capacity_bytes: int = Field(default=0, description="Current provisioned capacity in bytes.")
    current_cost_monthly: float = Field(default=0.0, description="Current monthly cost (USD).")
    savings_monthly: float = Field(default=0.0, description="Estimated monthly savings if resized (USD).")
    storage_class: str = Field(default="", description="StorageClass name.")


class PVSizingResponse(BaseToolResponse):
    """Response for get_pv_sizing_recommendations."""

    rows: list[PVSizingRow] = Field(default_factory=list, description="PVC right-sizing recommendations.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    total_monthly_savings: float = Field(
        default=0.0, description="Total monthly savings across all filtered rows (USD)."
    )
    row_count: int = Field(
        default=0, description="Total number of rows in the filtered population (before top_n slice)."
    )
    truncated: bool = Field(default=False, description="True if result was sliced to top_n.")


class LocalDiskRow(BaseModel):
    """One underutilized node-local disk."""

    disk_name: str = Field(default="", description="Disk/node identifier.")
    cluster_id: str = Field(default="", description="Kubecost cluster ID.")
    utilization_percent: float = Field(
        default=0.0,
        description=(
            "Disk utilization as a 0–1 ratio (NOT 0–100). "
            "May theoretically exceed 1.0 in burst or overcommit scenarios — values above 1.0 "
            "are passed through as-is from the upstream Kubecost API and are not an error."
        ),
    )
    current_usage_bytes: int = Field(default=0, description="Current used bytes.")
    current_capacity_bytes: int = Field(default=0, description="Current capacity in bytes.")
    recommended_capacity_bytes: int = Field(
        default=0, description="Recommended capacity in bytes. 0 = full decommission."
    )
    current_cost_monthly: float = Field(default=0.0, description="Current monthly cost (USD).")
    savings_monthly: float = Field(default=0.0, description="Estimated monthly savings (USD).")


class LocalDiskSavingsResponse(BaseToolResponse):
    """Response for get_local_disk_savings."""

    rows: list[LocalDiskRow] = Field(default_factory=list, description="Underutilized disk recommendations.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    total_monthly_savings: float = Field(
        default=0.0, description="Total monthly savings across all filtered rows (USD)."
    )
    row_count: int = Field(
        default=0, description="Total number of rows in the filtered population (before top_n slice)."
    )
    truncated: bool = Field(default=False, description="True if result was sliced to top_n.")


class ResourceMetrics(BaseModel):
    """CPU or RAM metrics for a node group state."""

    capacity_avg: float = Field(
        default=0.0,
        description=("Average capacity. Unit depends on the resource: millicores (m) for CPU, mebibytes (Mi) for RAM."),
    )
    utilization: float = Field(
        default=0.0,
        description=(
            "Utilization ratio (usage / capacity). Typically 0–1, but may exceed 1.0 due to CPU burst, "
            "memory overcommit, or averaging artifacts across heterogeneous nodes. "
            "Values above 1.0 are not an error — they indicate the node is running above nominal capacity."
        ),
    )
    usage_avg: float | None = Field(
        default=None,
        description=(
            "Average usage (same unit as capacity_avg: millicores for CPU, mebibytes for RAM). "
            "None in the recommended (after) state."
        ),
    )
    usage_p95: float | None = Field(
        default=None,
        description=(
            "P95 usage (same unit as capacity_avg: millicores for CPU, mebibytes for RAM). "
            "None in the recommended (after) state."
        ),
    )


class NodeGroupState(BaseModel):
    """Node group state (before or after recommendation)."""

    instance_type: str = Field(default="", description="EC2/GCE/Azure instance type.")
    node_count: int = Field(default=0, description="Number of nodes.")
    price_per_month: float = Field(default=0.0, description="Total monthly cost for this node group (USD).")
    cpu: ResourceMetrics = Field(description="CPU metrics. All capacity/usage values are in millicores (m).")
    ram: ResourceMetrics = Field(description="RAM metrics. All capacity/usage values are in mebibytes (Mi).")


class NodeGroupRecommendation(BaseModel):
    """One node group rightsizing recommendation."""

    node_group: str = Field(default="", description="Node group name.")
    recommendation: str = Field(
        default="",
        description=(
            "Recommended action. Observed values: 'ScaleIn', 'ScaleOut', 'ChangeInstanceType', 'None'. Open string."
        ),
    )
    recommendation_class: str = Field(
        default="cost_saving",
        description=(
            "High-level class of this recommendation. "
            "'cost_saving' — the change reduces monthly cost (ScaleIn, ChangeInstanceType to cheaper type). "
            "'capacity' — the change adds headroom or reliability at higher or equal cost (ScaleOut, "
            "ChangeInstanceType to a larger type). Consumers should not treat 'capacity' rows as savings "
            "opportunities; they represent reliability recommendations."
        ),
    )
    before: NodeGroupState = Field(description="Current node group state.")
    after: NodeGroupState = Field(description="Recommended node group state.")
    monthly_cost_delta: float = Field(
        default=0.0,
        description=(
            "Signed monthly cost change: after.price_per_month - before.price_per_month (USD). "
            "Negative means cost decreases (saving); positive means cost increases (capacity investment). "
            "Use this field when you need the exact signed delta."
        ),
    )
    savings_per_month: float = Field(
        default=0.0,
        description=(
            "Estimated monthly savings if this recommendation is applied (USD). "
            "Always >= 0: negative monthly_cost_delta rows contribute their absolute value; "
            "positive monthly_cost_delta rows (capacity recommendations) contribute 0. "
            "Use total_savings_per_month on the response for the cluster-level savings figure."
        ),
    )


class ClusterRightsizingResponse(BaseToolResponse):
    """Response for get_cluster_rightsizing_recommendations."""

    cluster: str = Field(default="", description="Cluster ID queried.")
    profile: str = Field(default="production", description="Sizing profile used.")
    window: str = Field(default="", description="Time window used.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    recommendations: list[NodeGroupRecommendation] = Field(
        default_factory=list, description="Node group recommendations."
    )
    total_savings_per_month: float = Field(
        default=0.0,
        description=(
            "Total estimated monthly savings across all cost_saving recommendations (USD). "
            "Always >= 0. Capacity recommendations (ScaleOut, etc.) do not reduce this figure even "
            "if they increase cost — they are excluded from the total. "
            "Compare to net_cost_change for the signed total."
        ),
    )
    net_cost_change: float = Field(
        default=0.0,
        description=(
            "Sum of monthly_cost_delta across all recommendations (USD). "
            "May be negative (net saving), zero, or positive (net capacity investment). "
            "This is the signed total; total_savings_per_month is the non-negative savings view."
        ),
    )
    recommendation_count: int = Field(default=0, description="Number of recommendations.")
    truncated: bool = Field(
        default=False, description="True if upstream returned >1000 recommendations and results were capped."
    )
    warnings: list[str] = Field(default_factory=list, description="API-level warnings (if any).")


class UnclaimedVolumeProperties(BaseModel):
    """Properties of an unclaimed PersistentVolume."""

    cluster: str = Field(default="", description="Kubecost cluster ID.")
    provider: str = Field(default="", description="Cloud provider (e.g. GCP, AWS).")
    service: str = Field(default="", description="Service name.")
    name: str = Field(default="", description="Volume name.")
    provider_id: str = Field(default="", description="Provider-specific volume ID.")


class UnclaimedVolumeRow(BaseModel):
    """One unclaimed PersistentVolume."""

    volume_name: str = Field(default="", description="PersistentVolume name.")
    monthly_cost: float = Field(default=0.0, description="Monthly cost -- full savings if deleted (USD).")
    properties: UnclaimedVolumeProperties = Field(description="Volume properties.")


class UnclaimedVolumesResponse(BaseToolResponse):
    """Response for get_unclaimed_volumes."""

    rows: list[UnclaimedVolumeRow] = Field(default_factory=list, description="Unclaimed volumes.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    total_monthly_cost: float = Field(default=0.0, description="Total monthly cost of filtered volumes (USD).")
    row_count: int = Field(
        default=0, description="Total number of rows in the filtered population (before top_n slice)."
    )
    truncated: bool = Field(default=False, description="True if result was sliced to top_n.")


class QuotaResourceChange(BaseModel):
    """One resource type change within a namespace quota recommendation.

    Note on field semantics: ``current_quota`` and ``recommended_quota`` are
    **quota** values, not raw pod-level observed usage. For example,
    ``current_quota='3500m'`` means the namespace's existing ResourceQuota cap is
    3500 millicores — *not* that pods are consuming 3500m. ``is_downsize=True``
    means the recommended quota is *lower* than the current one (tightening).
    """

    resource_type: str = Field(default="", description="Resource type (e.g. 'requests.cpu', 'requests.memory').")
    category: str = Field(default="", description="Resource category (e.g. 'compute').")
    current_quota: str = Field(
        default="",
        alias="used",
        description=(
            "Current ResourceQuota value for this resource type "
            "(e.g. '3500m' CPU or '8Gi' memory). This is the quota cap, not raw "
            "observed pod usage — the name 'used' in the Kubecost API is misleading."
        ),
    )
    recommended_quota: str = Field(
        default="",
        alias="recommended",
        description=(
            "Recommended ResourceQuota value (e.g. '4725m' CPU or '10Gi' memory). "
            "Apply this value to the namespace's ResourceQuota object. "
            "When is_downsize=True this is lower than current_quota (quota tightening)."
        ),
    )
    is_new_resource: bool = Field(default=False, description="True if this resource type has no existing quota entry.")
    is_downsize: bool = Field(
        default=False,
        description=(
            "True if the recommended_quota is lower than current_quota. "
            "Despite the name, this means the quota is being *tightened*, not that "
            "current resource usage exceeds the quota."
        ),
    )


class QuotaNamespaceRecommendation(BaseModel):
    """ResourceQuota recommendation for one namespace."""

    cluster: str = Field(default="", description="Kubecost cluster ID.")
    namespace: str = Field(default="", description="Kubernetes namespace.")
    category: str = Field(default="", description="Category (e.g. 'compute').")
    is_new_resource_quota: bool = Field(
        default=False, description="True if no ResourceQuota exists yet for this namespace."
    )
    resources: list[QuotaResourceChange] = Field(default_factory=list, description="Per-resource-type changes.")


class ResourceQuotaResponse(BaseToolResponse):
    """Response for get_resource_quota_recommendations."""

    recommendations: list[QuotaNamespaceRecommendation] = Field(
        default_factory=list, description="Namespace quota recommendations."
    )
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    item_count: int = Field(
        default=0, description="Total item count reported by the API (may exceed len(recommendations) when paginating)."
    )
    total_monthly_savings: float = Field(
        default=0.0, description="Total monthly savings (USD). May be 0 -- this is a correctness tool."
    )
    truncated: bool = Field(default=False, description="True if more pages exist.")
    next_offset: int | None = Field(
        default=None,
        description=(
            "The offset value to pass in the next call to retrieve the following page. "
            "Null when truncated=False (no further pages exist)."
        ),
    )


class AbandonedWorkloadRow(BaseModel):
    """One pod identified as potentially abandoned due to low network activity."""

    pod: str = Field(default="", description="Pod name.")
    namespace: str = Field(default="", description="Kubernetes namespace.")
    node: str = Field(default="", description="Node the pod is running on.")
    cluster_id: str = Field(
        default="",
        alias="clusterId",
        description="Kubecost cluster identifier.",
    )
    owner_name: str = Field(
        default="",
        description="Controller name managing this pod (empty if unmanaged).",
    )
    owner_kind: str = Field(
        default="",
        description="Controller kind (deployment, statefulset, daemonset, etc.).",
    )
    ingress_bytes_per_second: float = Field(
        default=0.0,
        alias="ingressBytesPerSecond",
        description="Average inbound network traffic in bytes/second over the lookback window.",
    )
    egress_bytes_per_second: float = Field(
        default=0.0,
        alias="egressBytesPerSecond",
        description="Average outbound network traffic in bytes/second over the lookback window.",
    )
    allocated_cpu_cores: float = Field(
        default=0.0,
        description="CPU cores allocated to the pod.",
    )
    allocated_ram_bytes: float = Field(
        default=0.0,
        description="RAM bytes allocated to the pod.",
    )
    monthly_savings: float = Field(
        default=0.0,
        alias="monthlySavings",
        description="Estimated monthly cost savings if this workload is decommissioned (USD).",
    )


class AbandonedWorkloadsResponse(BaseToolResponse):
    """Response from get_abandoned_workloads."""

    days: int = Field(description="Lookback window in days used for the query.")
    resolved_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the queried window. Null if resolution failed.",
    )
    threshold_bytes_per_second: int = Field(
        description=(
            "Network traffic threshold (bytes/second) used to identify abandoned workloads. "
            "Pods with both ingress and egress below this value are flagged."
        )
    )
    cluster_filter: str = Field(
        default="",
        description="Cluster filter applied. Empty string means all clusters.",
    )
    workload_count: int = Field(default=0, description="Number of abandoned workloads returned.")
    total_monthly_savings: float = Field(
        default=0.0,
        description="Total estimated monthly savings if all returned workloads are decommissioned (USD).",
    )
    rows: list[AbandonedWorkloadRow] = Field(
        default_factory=list,
        description=(
            "Abandoned workloads sorted by monthly_savings descending. "
            "Each row has pod/namespace/cluster identity, network traffic rates, "
            "and the estimated monthly cost if decommissioned."
        ),
    )
    truncated: bool = Field(
        default=False,
        description="True when the API returned exactly 'limit' rows — more may exist. Raise limit to retrieve all.",
    )


class CostComparisonRow(BaseModel):
    """One dimension's cost comparison between the current and baseline window."""

    model_config = ConfigDict(extra="allow")

    current_cost: float = Field(default=0.0, description="Total cost in the current window (USD).")
    baseline_cost: float = Field(default=0.0, description="Total cost in the baseline window (USD).")
    change: float = Field(default=0.0, description="current_cost - baseline_cost (USD). Positive = cost increased.")
    pct_change: float | None = Field(
        default=None,
        description=(
            "Percent change from baseline to current. Null when baseline_cost is 0 "
            "(see row_status) since percent change is undefined."
        ),
    )
    row_status: CostRowStatus = Field(
        default=CostRowStatus.UNCHANGED,
        description=(
            "How this dimension moved: 'new' (zero baseline cost, non-zero current), "
            "'removed' (non-zero baseline cost, zero current), 'unchanged' (identical "
            "cost in both windows, including zero in both), or 'changed'."
        ),
    )
    current_daily_cost: float = Field(
        default=0.0, description="current_cost divided by the number of days in the current window (USD/day)."
    )
    baseline_daily_cost: float = Field(
        default=0.0, description="baseline_cost divided by the number of days in the baseline window (USD/day)."
    )
    daily_change: float = Field(
        default=0.0,
        description=(
            "current_daily_cost - baseline_daily_cost (USD/day). Use this instead of `change` "
            "when the two windows differ in length."
        ),
    )
    normalized_pct_change: float | None = Field(
        default=None,
        description=(
            "Percent change between the per-day costs, so periods of unequal length are "
            "comparable. Equals pct_change when both windows are the same length. Null when "
            "baseline_daily_cost is 0."
        ),
    )


class CostComparisonResponse(BaseToolResponse):
    """Response from get_kubecost_cost_comparison."""

    current_window: str = Field(description="Normalized current-period window used for the query.")
    baseline_window: str = Field(description="Normalized baseline-period window used for the query.")
    resolved_current_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the current window. Null if resolution failed.",
    )
    resolved_baseline_window: ResolvedWindow | None = Field(
        default=None,
        description="Resolved UTC boundaries and display string for the baseline window. Null if resolution failed.",
    )
    aggregate: str = Field(description="Aggregation dimension(s) requested.")
    dimensions: list[str] = Field(
        default_factory=list, description="Resolved dimension columns present in each result row."
    )
    total_current_cost: float = Field(default=0.0, description="Sum of current_cost across all rows (USD).")
    total_baseline_cost: float = Field(default=0.0, description="Sum of baseline_cost across all rows (USD).")
    total_change: float = Field(default=0.0, description="total_current_cost - total_baseline_cost (USD).")
    row_count: int = Field(default=0, description="Total number of rows in the full diffed population.")
    rows: list[CostComparisonRow] = Field(
        default_factory=list,
        description=(
            "Diff rows sorted by absolute change descending, capped at top_n. "
            "Each row contains the requested dimension values plus current_cost, "
            "baseline_cost, change, pct_change, row_status, and the per-day figures "
            "(current_daily_cost, baseline_daily_cost, daily_change, normalized_pct_change)."
        ),
    )
    truncated: bool = Field(
        default=False,
        description="True when the full diffed result set was larger than top_n.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings about this comparison (e.g. unequal period lengths).",
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "How to read these numbers — idle-cost handling and, when present, what the "
            "__unallocated__ rows represent. Not problems with the comparison; see warnings for those."
        ),
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_kubecost_tools(mcp: FastMCP) -> None:
    """Register Kubecost allocation and savings tools, resources, and prompts."""

    # ── Tools ─────────────────────────────────────────────────────────────────

    @mcp.tool(
        version=_VERSION,
        annotations=ToolAnnotations(
            title="List Kubecost Windows",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def kubecost_list_windows() -> WindowOptionsResponse:
        """List the valid time windows for Kubecost cost queries, each resolved to real dates.

        WHAT: Returns the named windows (7d, 15d, 30d, month, etc.) accepted by
        tools that support date windows, each with the concrete UTC date range it maps to
        right now, its day count, and whether the period is still in progress.
        Note that UTC is the only supported timezone in Kubecost.

        WHEN TO USE: Run any time to understand what the valid time windows are.

        WHEN NOT TO USE: When a valid time window is already established.
        """
        return WindowOptionsResponse(
            status=QueryStatus.OK,
            message="Here is a list of possible time window formats accepted by the MCP.",
            recommended_action="Use any of the window options when running tools that require a time window.",
            windows=[
                WindowOption(value=k, label=v, resolved=_resolve_window_defensively(k))
                for k, v in _WINDOW_CHOICES.items()
            ],
            note=_WINDOW_RFC3339_NOTE,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=ToolAnnotations(
            title="Get Kubecost Workload Costs",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_kubecost_workload_costs(
        window: Annotated[
            str,
            Field(
                description=(
                    "Time window for the query. Examples: '7d', '15d', '30d', 'month', or an RFC3339 "
                    "range '2026-05-01T00:00:00Z,2026-06-01T00:00:00Z'. "
                    "Defaults to '" + DEFAULT_WINDOW + "'."
                )
            ),
        ] = DEFAULT_WINDOW,
        aggregate: Annotated[
            str,
            Field(
                description=(
                    "Aggregation dimension(s): a single value ('cluster') or a "
                    "comma-separated list ('cluster,namespace'). "
                    "Also accepts: pod, node, controller, label, container, "
                    "controllerKind, department, environment, owner, product, team."
                ),
            ),
        ] = "cluster,namespace",
        accumulate: Annotated[
            bool,
            Field(
                description=(
                    "True (default) returns one total for the entire window. "
                    "False returns a daily breakdown — use only for trend/time-series analysis."
                ),
            ),
        ] = True,
        limit: Annotated[
            int,
            Field(
                description="Maximum allocation entries to fetch from the API.",
                ge=1,
                le=100000,
            ),
        ] = 100000,
        top_n: Annotated[
            int,
            Field(
                description=(
                    "Maximum rows to include in the response. "
                    "When the full result exceeds top_n, the response sets truncated=True. "
                    "Increase top_n or narrow the window/aggregate to retrieve more rows."
                ),
                ge=1,
                le=10000,
            ),
        ] = 20,
        min_total_cost: Annotated[
            float,
            Field(
                description=(
                    "Minimum totalCost (USD) to include a row. Rows below this threshold "
                    "are excluded as trivial noise. Default $1.00; set 0.0 to include all."
                ),
                ge=0.0,
            ),
        ] = 1.0,
    ) -> KubecostAllocationResponse:
        """Return Kubernetes cost allocation from Kubecost grouped by chosen dimensions.

        WHAT: Costs aggregated by cluster, namespace, pod, label, or any combination.
        Results are returned as structured rows.

        WHEN TO USE: For 'spend by cluster/namespace/pod/label' questions.
        If the user has not specified a window, call kubecost_list_windows first.

        WHEN NOT TO USE: For container rightsizing/savings use
        get_container_savings_recommendations. For period-over-period cost
        change / spike investigation, use get_kubecost_cost_comparison instead.
        """
        if not window:
            return KubecostAllocationResponse(
                status=QueryStatus.ERROR,
                message="A time window is required before querying Kubecost allocation.",
                recommended_action=(
                    "Call kubecost_list_windows to understand the accepted time window options. "
                    "If the request is not clear, present the options to the user."
                ),
                window=None,
                aggregate=aggregate,
            )

        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window

        try:
            response = await _fetch_allocation(
                aggregate=aggregate,
                window=window,
                accumulate=accumulate,
                limit=limit,
            )
        except McpToolError as exc:
            return KubecostAllocationResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
                window=window,
                resolved_window=resolved_window,
                aggregate=aggregate,
            )

        # The response carries the range Kubecost actually queried; prefer it.
        resolved_window = _window_from_allocation(response, window) or resolved_window
        window_display = resolved_window.display if resolved_window else window

        dimension_cols, rows = _parse_allocation_response(response, aggregate)
        if not rows:
            return KubecostAllocationResponse(
                status=QueryStatus.EMPTY,
                message=f"No Kubecost allocation data for window {window_display}.",
                recommended_action="Try a wider window or a different aggregate dimension.",
                window=window,
                resolved_window=resolved_window,
                aggregate=aggregate,
                dimensions=dimension_cols,
            )

        aggregated = _aggregate_by_dimensions(rows, dimension_cols)
        total = sum(float(r.get("totalCost", 0) or 0) for r in aggregated)

        # Client-side filter: remove rows below the minimum cost threshold
        if min_total_cost > 0:
            filtered = [r for r in aggregated if float(r.get("totalCost", 0) or 0) >= min_total_cost]
        else:
            filtered = aggregated

        if not filtered:
            return KubecostAllocationResponse(
                status=QueryStatus.EMPTY,
                message=(
                    f"No rows with totalCost >= ${min_total_cost:,.2f} for window {window_display}. "
                    f"({len(aggregated)} rows totaling ${total:,.2f} were below the threshold.)"
                ),
                recommended_action="Lower min_total_cost or widen the window to surface more rows.",
                window=window,
                resolved_window=resolved_window,
                aggregate=aggregate,
                dimensions=dimension_cols,
                total_cost=round(total, 2),
                row_count=0,
            )

        filtered_total = sum(float(r.get("totalCost", 0) or 0) for r in filtered)
        truncated = len(filtered) > top_n

        filtered_note = (
            f" (filtered from {len(aggregated)} rows; excluded {len(aggregated) - len(filtered)} "
            f"below ${min_total_cost:,.2f})"
            if len(filtered) != len(aggregated)
            else ""
        )

        # Spell out the row shape when not accumulating so per-day rows are never
        # read as whole-window totals.
        breakdown_note = ""
        if not accumulate:
            day_count = len({r.get("window_start", "") for r in filtered})
            breakdown_note = (
                f" Daily breakdown: one row per {', '.join(dimension_cols) or aggregate} "
                f"per day across {day_count} day(s)."
            )

        return KubecostAllocationResponse(
            status=QueryStatus.OK,
            message=(
                f"Kubecost allocation by {', '.join(dimension_cols) or aggregate} "
                f"for window {window_display}: ${filtered_total:,.2f} across {len(filtered)} rows"
                + (f" (showing top {top_n})" if truncated else "")
                + filtered_note
                + "."
                + breakdown_note
            ),
            recommended_action=(
                "Increase top_n or narrow the aggregate/window to retrieve all rows." if truncated else None
            ),
            window=window,
            resolved_window=resolved_window,
            aggregate=aggregate,
            dimensions=dimension_cols,
            total_cost=round(filtered_total, 2),
            row_count=len(filtered),
            rows=[AllocationRow.model_validate(r) for r in filtered[:top_n]],
            truncated=truncated,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=ToolAnnotations(
            title="Get Kubecost Cost Comparison",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_kubecost_cost_comparison(
        current_window: Annotated[
            str | None,
            Field(
                description=(
                    "The more recent period to inspect. Must be an RFC3339 range 'start,end' that ends "
                    "before today (UTC). Named aliases ('lastweek', 'lastmonth', '7d', etc.) are REJECTED "
                    "— use explicit dates. Defaults to the 7-day rolling window ending yesterday (UTC)."
                ),
                examples=["2026-07-14T00:00:00Z,2026-07-21T00:00:00Z"],
            ),
        ] = None,
        baseline_window: Annotated[
            str | None,
            Field(
                description=(
                    "The prior period to compare against. Must be an RFC3339 range ending before today "
                    "(UTC). Periods of different lengths are allowed — a warning will be included in the "
                    "response. Defaults to the 7-day period immediately before the default current_window, "
                    "giving a rolling week-over-week comparison."
                ),
                examples=["2026-07-07T00:00:00Z,2026-07-14T00:00:00Z"],
            ),
        ] = None,
        aggregate: Annotated[
            str,
            Field(
                description=(
                    "Aggregation dimension(s): a single value ('cluster') or a "
                    "comma-separated list ('cluster,namespace'). "
                    "Also accepts: pod, node, controller, label, container, "
                    "controllerKind, department, environment, owner, product, team."
                ),
            ),
        ] = "cluster,namespace",
        top_n: Annotated[
            int,
            Field(
                description=(
                    "Maximum rows to include in the response. "
                    "When the full diffed result exceeds top_n, the response sets truncated=True."
                ),
                ge=1,
                le=10000,
            ),
        ] = 20,
    ) -> CostComparisonResponse:
        """Compare Kubernetes cost allocation between two time windows to find cost changes and spikes.

        WHAT: Fetches allocation data for current_window and baseline_window separately, aggregates
        each by the chosen dimension(s), and returns a per-dimension diff (current_cost, baseline_cost,
        change, pct_change, row_status) sorted by absolute change descending. Each row also carries
        per-day figures (current_daily_cost, baseline_daily_cost, daily_change, normalized_pct_change)
        — use those whenever the two windows differ in length, since a 31-day month costs more than a
        30-day one at identical daily spend.

        WHEN TO USE: Investigating "why did costs change" or "what spiked." Once you've identified the
        responsible dimension, drill into get_container_savings_recommendations, get_abandoned_workloads,
        or get_cluster_rightsizing_recommendations for that dimension.

        WHEN NOT TO USE: For a single-period snapshot with no comparison, use get_kubecost_workload_costs
        instead.

        DEFAULTS: Omitting both window parameters performs a rolling week-over-week comparison (the 7
        days ending yesterday vs. the 7 days before that); the current in-progress day is never included.

        WINDOW RULES (enforced): Both windows must be explicit RFC3339 ranges ending before today. Named
        aliases ("lastweek", "lastmonth") and bare relative windows ("7d", "today", "week", "month") are
        rejected — use explicit dates. Windows may differ in length; a warning is added to the response
        when they do.

        IDLE: Idle (unused but provisioned) capacity is shared proportionally into every row, so the
        rows never sum to a separate idle line. Cost with no value for a requested dimension is
        grouped under __unallocated__ and explained in the response notes.

        """
        default_current_window, default_baseline_window = _default_wow_windows()
        current_window = normalize_window_order(current_window or default_current_window)
        baseline_window = normalize_window_order(baseline_window or default_baseline_window)
        current_days, baseline_days = _validate_comparison_windows(current_window, baseline_window)
        resolved_current_window = _resolve_window_defensively(current_window)
        resolved_baseline_window = _resolve_window_defensively(baseline_window)
        current_display = resolved_current_window.display if resolved_current_window else current_window
        baseline_display = resolved_baseline_window.display if resolved_baseline_window else baseline_window

        try:
            current_response = await _fetch_allocation(
                aggregate=aggregate,
                window=current_window,
                accumulate=True,
                limit=100000,
            )
            baseline_response = await _fetch_allocation(
                aggregate=aggregate,
                window=baseline_window,
                accumulate=True,
                limit=100000,
            )
        except McpToolError as exc:
            return CostComparisonResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
                current_window=current_window,
                baseline_window=baseline_window,
                resolved_current_window=resolved_current_window,
                resolved_baseline_window=resolved_baseline_window,
                aggregate=aggregate,
            )

        # Both responses carry the ranges Kubecost actually queried; prefer them.
        resolved_current_window = _window_from_allocation(current_response, current_window) or resolved_current_window
        resolved_baseline_window = (
            _window_from_allocation(baseline_response, baseline_window) or resolved_baseline_window
        )
        current_display = resolved_current_window.display if resolved_current_window else current_window
        baseline_display = resolved_baseline_window.display if resolved_baseline_window else baseline_window

        current_dims, current_rows = _parse_allocation_response(current_response, aggregate)
        baseline_dims, baseline_rows = _parse_allocation_response(baseline_response, aggregate)
        dimension_cols = current_dims or baseline_dims

        if not current_rows and not baseline_rows:
            return CostComparisonResponse(
                status=QueryStatus.EMPTY,
                message=(f"No Kubecost allocation data for either window ({current_display} or {baseline_display})."),
                recommended_action="Try different windows or a different aggregate dimension.",
                current_window=current_window,
                baseline_window=baseline_window,
                resolved_current_window=resolved_current_window,
                resolved_baseline_window=resolved_baseline_window,
                aggregate=aggregate,
                dimensions=dimension_cols,
            )

        current_aggregated = _aggregate_by_dimensions(current_rows, dimension_cols) if current_rows else []
        baseline_aggregated = _aggregate_by_dimensions(baseline_rows, dimension_cols) if baseline_rows else []

        current_span_days = _window_days(resolved_current_window, current_days)
        baseline_span_days = _window_days(resolved_baseline_window, baseline_days)
        diffed = _diff_allocation_rows(
            current_aggregated,
            baseline_aggregated,
            dimension_cols,
            current_days=current_span_days,
            baseline_days=baseline_span_days,
        )

        total_current = sum(float(r.get("current_cost", 0) or 0) for r in diffed)
        total_baseline = sum(float(r.get("baseline_cost", 0) or 0) for r in diffed)
        truncated = len(diffed) > top_n

        top_mover = diffed[0] if diffed else None
        top_mover_desc = ""
        if top_mover:
            dim_desc = ", ".join(f"{dim}={top_mover.get(dim, '')}" for dim in dimension_cols)
            top_mover_desc = f" Biggest mover: {dim_desc} (change ${top_mover.get('change', 0):,.2f})."

        response_warnings: list[str] = []
        if current_days is not None and baseline_days is not None and current_days != baseline_days:
            response_warnings.append(
                f"The comparison periods have a different number of days: ({current_days} vs {baseline_days}). "
                "Compare daily_change and normalized_pct_change rather than change and pct_change."
            )

        response_notes: list[str] = [_IDLE_SHARED_NOTE]
        unallocated_note = _unallocated_note(diffed, dimension_cols)
        if unallocated_note:
            response_notes.append(unallocated_note)

        return CostComparisonResponse(
            status=QueryStatus.OK,
            message=(
                f"Compared {current_display} (${total_current:,.2f}) vs {baseline_display} "
                f"(${total_baseline:,.2f}) by {', '.join(dimension_cols) or aggregate}: "
                f"{len(diffed)} row(s)" + (f" (showing top {top_n})" if truncated else "") + "." + top_mover_desc
            ),
            recommended_action=(
                "Drill into get_container_savings_recommendations, get_abandoned_workloads, or "
                "get_cluster_rightsizing_recommendations for the dimension(s) with the largest change."
            ),
            current_window=current_window,
            baseline_window=baseline_window,
            resolved_current_window=resolved_current_window,
            resolved_baseline_window=resolved_baseline_window,
            aggregate=aggregate,
            dimensions=dimension_cols,
            total_current_cost=round(total_current, 2),
            total_baseline_cost=round(total_baseline, 2),
            total_change=round(total_current - total_baseline, 2),
            row_count=len(diffed),
            rows=[CostComparisonRow.model_validate(r) for r in diffed[:top_n]],
            truncated=truncated,
            warnings=response_warnings,
            notes=response_notes,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=ToolAnnotations(
            title="Get Container Savings Recommendations",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_container_savings_recommendations(
        profile: Annotated[
            ProfileName | None,
            Field(description=FIELD_DESCRIPTIONS["profile"]),
        ] = None,
        window: Annotated[
            str | None,
            Field(description=FIELD_DESCRIPTIONS["window"]),
        ] = None,
        algorithm_cpu: Annotated[
            str | None,
            Field(description=FIELD_DESCRIPTIONS["algorithm_cpu"]),
        ] = None,
        algorithm_ram: Annotated[
            str | None,
            Field(description=FIELD_DESCRIPTIONS["algorithm_ram"]),
        ] = None,
        q_cpu: Annotated[
            float | None,
            Field(description=FIELD_DESCRIPTIONS["q_cpu"], ge=0.0, le=1.0),
        ] = None,
        q_ram: Annotated[
            float | None,
            Field(description=FIELD_DESCRIPTIONS["q_ram"], ge=0.0, le=1.0),
        ] = None,
        target_cpu_utilization: Annotated[
            float | None,
            Field(description=FIELD_DESCRIPTIONS["target_cpu_utilization"], ge=0.0, le=1.0),
        ] = None,
        target_ram_utilization: Annotated[
            float | None,
            Field(description=FIELD_DESCRIPTIONS["target_ram_utilization"], ge=0.0, le=1.0),
        ] = None,
        filter_str: Annotated[
            str,
            Field(description=_SAVINGS_FILTER_CLARIFICATION),
        ] = "",
        top_n: Annotated[
            int,
            Field(
                description=(
                    "Maximum per-container rows to include in the response. "
                    "When the filtered result exceeds top_n, truncated=True is set. "
                    "The summary always covers the full filtered set regardless of top_n."
                ),
                ge=1,
                le=10000,
            ),
        ] = 20,
        min_monthly_savings: Annotated[
            float | None,
            Field(
                description=FIELD_DESCRIPTIONS["min_monthly_savings"],
            ),
        ] = None,
        summary_aggregate: Annotated[
            Literal["containerName", "namespace", "clusterID"],
            Field(
                description=(
                    "Dimension to group the inline summary by: 'containerName' (default), "
                    "'namespace', or 'clusterID'. 'containerName' combines same-named "
                    "containers across clusters/namespaces — per-container detail is in 'rows'."
                ),
            ),
        ] = "containerName",
    ) -> ContainerSavingsResponse:
        """Return Kubernetes container rightsizing recommendations and potential savings.

        WHAT: Which workloads are over-provisioned and how much can be saved by
        rightsizing them. Structured rows are returned directly in the response —
        no separate resource read required. Supports named profiles (production,
        high-availability, development) that bundle recommended quantile/window
        and target-utilization settings; explicit parameters override profile values.

        WHEN TO USE: For Kubernetes container savings, over-provisioned pods/namespaces,
        or rightsizing recommendations. If the user asks HOW to rightsize (methodology,
        quantiles, CPU vs memory strategy), invoke the container_rightsizing_guide
        prompt first.

        WHEN NOT TO USE: For raw Kubernetes spend by cluster/namespace/pod, use
        get_kubecost_workload_costs.
        """
        sizing = resolve_sizing_params(
            profile,
            window=window,
            algorithm_cpu=algorithm_cpu,
            algorithm_ram=algorithm_ram,
            q_cpu=q_cpu,
            q_ram=q_ram,
            target_cpu_utilization=target_cpu_utilization,
            target_ram_utilization=target_ram_utilization,
            min_monthly_savings=min_monthly_savings,
        )
        resolved_window: str = normalize_window_order(sizing["window"])
        sizing["window"] = resolved_window
        resolved_window_display = _resolve_window_defensively(resolved_window)
        window_display = resolved_window_display.display if resolved_window_display else resolved_window
        resolved_algorithm_cpu: str = sizing["algorithm_cpu"]
        resolved_algorithm_ram: str = sizing["algorithm_ram"]
        resolved_q_cpu: float = sizing["q_cpu"]
        resolved_q_ram: float = sizing["q_ram"]
        resolved_target_cpu: float = sizing["target_cpu_utilization"]
        resolved_target_ram: float = sizing["target_ram_utilization"]
        resolved_min_monthly_savings: float | None = sizing["min_monthly_savings"]

        uses_quantile = (
            resolved_algorithm_cpu.lower() in _QUANTILE_ALGORITHMS
            or resolved_algorithm_ram.lower() in _QUANTILE_ALGORITHMS
        )
        if uses_quantile:
            window_days = parse_window_days(resolved_window)
            min_days = parse_window_days(MIN_QUANTILE_WINDOW)
            if window_days is not None and min_days is not None and window_days < min_days:
                raise_tool_error(
                    ErrorCode.INVALID_INPUT,
                    message=(
                        f"Window '{resolved_window}' is too short for quantile algorithms. "
                        f"quantileOfAverages and quantileOfMaxes require at least {MIN_QUANTILE_WINDOW} "
                        "of data to produce a meaningful distribution. "
                        f"Use '{MIN_QUANTILE_WINDOW}', '30d', '90d', or an RFC3339 range spanning "
                        f"≥ {min_days} days."
                    ),
                    retryable=False,
                    suggested_action=(
                        f"Re-call with window='{MIN_QUANTILE_WINDOW}' or longer, "
                        "or switch to algorithm_cpu='max' / algorithm_ram='max' for short windows."
                    ),
                )

        try:
            response = await _fetch_request_sizing(
                window=resolved_window,
                algorithm_cpu=resolved_algorithm_cpu,
                algorithm_ram=resolved_algorithm_ram,
                q_cpu=resolved_q_cpu,
                q_ram=resolved_q_ram,
                target_cpu_utilization=resolved_target_cpu,
                target_ram_utilization=resolved_target_ram,
                filter_str=filter_str or "",
                limit=_SAVINGS_API_FETCH_LIMIT,
            )
        except McpToolError as exc:
            return ContainerSavingsResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
                window=resolved_window,
                total_monthly_savings=0.0,
                container_count=0,
                summary_aggregate=summary_aggregate,
            )

        total_savings, count, all_rows = parse_request_sizing_response(response)

        if not all_rows:
            return ContainerSavingsResponse(
                status=QueryStatus.EMPTY,
                message=f"No container savings recommendations returned for {window_display}.",
                recommended_action="Try a wider window or a different filter.",
                window=resolved_window,
                resolved_window=resolved_window_display,
                total_monthly_savings=0.0,
                container_count=0,
                summary_aggregate=summary_aggregate,
            )

        rows = all_rows
        if resolved_min_monthly_savings is not None:
            rows = [r for r in rows if float(r.get("monthlySavings_total", 0) or 0) >= resolved_min_monthly_savings]

        if not rows:
            threshold_label = (
                f"${resolved_min_monthly_savings:,.2f}" if resolved_min_monthly_savings is not None else "(none)"
            )
            return ContainerSavingsResponse(
                status=QueryStatus.EMPTY,
                message=(f"No recommendations matched the filters (minimum monthly savings {threshold_label})."),
                recommended_action=(
                    "Omit min_monthly_savings to return all recommendations, "
                    "lower the threshold, or pass a negative value to include undersized workloads."
                ),
                window=resolved_window,
                resolved_window=resolved_window_display,
                total_monthly_savings=0.0,
                container_count=0,
                summary_aggregate=summary_aggregate,
            )

        aggregated_summary = aggregate_savings_by(rows, summary_aggregate)
        summary_rows = [
            ContainerSavingsSummaryRow(
                group=str(r.get(summary_aggregate, "")),
                monthly_savings_total=float(r.get("monthlySavings_total", 0) or 0),
                container_count=int(r.get("container_count", 0) or 0),
            )
            for r in aggregated_summary
        ]

        # Totals/counts must describe the FILTERED dataset that 'rows', 'summary',
        # and 'interpretation' are built from — not the unfiltered API payload.
        filtered_total_savings = sum(float(r.get("monthlySavings_total", 0) or 0) for r in rows)
        filtered_count = len(rows)

        # Build typed per-container rows using model_validate for field coercion
        typed_rows = [ContainerSavingsRow.model_validate(r) for r in rows]
        truncated = len(typed_rows) > top_n

        caveat = (
            " Same-named containers across clusters/namespaces are combined in the summary; "
            "per-cluster detail is in 'rows'."
            if summary_aggregate == "containerName"
            else ""
        )
        # Preserve the unfiltered API figure so the caller still sees how much was
        # excluded by the min_monthly_savings filter.
        filtered_note = (
            f" (filtered from {count} recommendations returned by the API)" if filtered_count != count else ""
        )

        interpretation = build_result_interpretation(sizing, all_rows, filtered_rows=rows)

        return ContainerSavingsResponse(
            status=QueryStatus.OK,
            message=(
                f"Total monthly savings ${filtered_total_savings:,.2f} across "
                f"{filtered_count} containers for {window_display}{filtered_note}, "
                f"summarized by {summary_aggregate}.{caveat}"
            ),
            recommended_action=("Increase top_n to retrieve more per-container rows." if truncated else None),
            window=resolved_window,
            resolved_window=resolved_window_display,
            total_monthly_savings=round(float(filtered_total_savings), 2),
            container_count=filtered_count,
            summary_aggregate=summary_aggregate,
            summary=summary_rows,
            rows=typed_rows[:top_n],
            truncated=truncated,
            parameters={
                "profile": profile or "production (default)",
                "window": resolved_window,
                "algorithm_cpu": resolved_algorithm_cpu,
                "algorithm_ram": resolved_algorithm_ram,
                "q_cpu": resolved_q_cpu,
                "q_ram": resolved_q_ram,
                "target_cpu_utilization": resolved_target_cpu,
                "target_ram_utilization": resolved_target_ram,
                "min_monthly_savings": resolved_min_monthly_savings,
                "filter": filter_str or "(none)",
            },
            interpretation=interpretation,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Abandoned Workloads"),
    )
    async def get_abandoned_workloads(
        days: Annotated[
            int,
            Field(
                description=(
                    "Lookback window in days. Pods with average network traffic below "
                    "'threshold' over this many days are flagged as abandoned. Default 2."
                ),
                ge=1,
                le=90,
            ),
        ] = 2,
        threshold: Annotated[
            int,
            Field(
                description=(
                    "Network traffic threshold in **bytes per second**. Pods whose average "
                    "ingress AND egress both fall below this value are considered abandoned. "
                    "Default 500 bytes/second (~43 KB/day) — effectively idle. "
                    "Lower values surface only the most dormant pods; raise to catch "
                    "light-traffic workloads."
                ),
                ge=0,
            ),
        ] = 500,
        cluster: Annotated[
            str,
            Field(
                description=(
                    "Optional Kubecost cluster ID to restrict results. Leave empty (default) "
                    "to query all clusters. Only add a cluster filter when the user has "
                    "already expressed interest in a specific cluster."
                ),
            ),
        ] = "",
        limit: Annotated[
            int,
            Field(
                description="Maximum workloads to return. Default 20.",
                ge=1,
                le=10000,
            ),
        ] = 20,
    ) -> AbandonedWorkloadsResponse:
        """Return pods with abnormally low network traffic — likely abandoned workloads.

        WHAT: Surfaces running pods whose average network ingress AND egress are both
        below 'threshold' bytes/second over the lookback period. These workloads are
        still consuming compute and memory costs despite appearing idle, and are
        candidates for decommissioning. Results include estimated monthly savings per pod.

        WHEN TO USE: When investigating wasted spend from dormant or forgotten workloads,
        or when a user asks about idle pods, unused deployments, or cleanup opportunities.
        Do not filter by cluster unless the user has indicated a specific cluster.

        WHEN NOT TO USE: For container rightsizing (over-provisioned but active workloads),
        use get_container_savings_recommendations. For raw cost by namespace/cluster use
        get_kubecost_workload_costs.
        """
        resolved_window = _resolve_window_defensively(f"{days}d")
        window_display = resolved_window.display if resolved_window else f"{days} days"
        try:
            raw = await _fetch_abandoned_workloads(
                days=days,
                threshold=threshold,
                cluster=cluster,
                limit=limit,
            )
        except McpToolError as exc:
            return AbandonedWorkloadsResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
                days=days,
                threshold_bytes_per_second=threshold,
                cluster_filter=cluster,
            )

        rows = _parse_abandoned_workloads_response(raw)
        if not rows:
            return AbandonedWorkloadsResponse(
                status=QueryStatus.EMPTY,
                message=(
                    f"No abandoned workloads found with threshold={threshold} bytes/s "
                    f"over {window_display}" + (f" in cluster '{cluster}'" if cluster else "") + "."
                ),
                recommended_action=("Try lowering 'threshold' or increasing 'days' to surface more workloads."),
                days=days,
                resolved_window=resolved_window,
                threshold_bytes_per_second=threshold,
                cluster_filter=cluster,
            )

        total_savings = round(sum(r.get("monthlySavings", 0.0) or 0.0 for r in rows), 2)
        truncated = len(rows) >= limit

        return AbandonedWorkloadsResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(rows)} abandoned workload(s) with estimated monthly savings "
                f"of ${total_savings:,.2f}"
                + (f" in cluster '{cluster}'" if cluster else " across all clusters")
                + f" (threshold={threshold} bytes/s, {window_display})"
                + (" — result may be truncated, increase limit for more." if truncated else ".")
            ),
            recommended_action=(
                "Review the pods with the highest monthly_savings first. "
                "Confirm the pod is truly idle before decommissioning — check with the owning team."
            ),
            days=days,
            resolved_window=resolved_window,
            threshold_bytes_per_second=threshold,
            cluster_filter=cluster,
            workload_count=len(rows),
            total_monthly_savings=total_savings,
            rows=[AbandonedWorkloadRow.model_validate(r, from_attributes=False) for r in rows],
            truncated=truncated,
        )

    # ── Savings overview and new savings tools ───────────────────────────────

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Savings Overview"),
    )
    async def get_savings_overview() -> SavingsOverviewResponse:
        """Return a ranked summary of all Kubecost savings categories.

        WHAT: Calls GET /model/savings and returns all 8 savings categories ranked
        by estimated monthly savings descending. Each category includes the name,
        estimated savings per month, last refresh time, and (where available) the
        name of the drill-down tool to call for details. No truncation — there are
        only 8 categories.

        WHEN TO USE: As the first response to any general "how can I save money?"
        or "what are my biggest savings opportunities?" question. Presents the full
        ranked summary and offers to drill into any category.

        WHEN NOT TO USE: When the user has already identified a specific category
        (e.g. "show me container rightsizing") — call the drill-down tool directly.
        """
        try:
            raw = await _fetch_savings_overview()
        except McpToolError as exc:
            return SavingsOverviewResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
            )

        data = raw if isinstance(raw, dict) else {}
        # Strip non-category top-level keys
        skip_keys = {"cluster", "profile"}
        drill_down_map = {
            "containerRequestSizing": "get_container_savings_recommendations",
            "abandonedWorkloads": "get_abandoned_workloads",
            "nodeGroupSizing": "get_cluster_rightsizing_recommendations",
            "persistentVolumeSizing": "get_pv_sizing_recommendations",
            "underutilizedLocalDisks": "get_local_disk_savings",
            "unclaimedVolumes": "get_unclaimed_volumes",
            "resourceQuotaSizing": "get_resource_quota_recommendations",
            "orphanedResources": None,
        }
        categories: list[SavingsCategory] = []
        for key, entry in data.items():
            if key in skip_keys or not isinstance(entry, dict):
                continue
            categories.append(
                SavingsCategory(
                    key=key,
                    savings_per_month=round(float(entry.get("savingsPerMonth", 0.0) or 0.0), 2),
                    last_refresh=entry.get("lastRefresh", ""),
                    drill_down_tool=drill_down_map.get(key),
                )
            )
        categories.sort(key=lambda c: c.savings_per_month, reverse=True)
        total = round(sum(c.savings_per_month for c in categories), 2)

        if not categories:
            return SavingsOverviewResponse(
                status=QueryStatus.EMPTY,
                message="No savings categories returned by Kubecost.",
                recommended_action="Verify the Kubecost API is reachable and warm.",
            )

        actionable = [c.drill_down_tool for c in categories if c.drill_down_tool and c.savings_per_month > 0]
        return SavingsOverviewResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(categories)} savings categories with estimated total monthly savings of ${total:,.2f}."
            ),
            recommended_action=(
                "Drill into the highest-savings category first. "
                + (f"Available drill-down tools: {', '.join(actionable)}." if actionable else "")
            ),
            categories=categories,
            total_savings_per_month=total,
            category_count=len(categories),
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get PV Sizing Recommendations"),
    )
    async def get_pv_sizing_recommendations(
        window: Annotated[
            str,
            Field(description="Time window for usage data. Default '15d'. " + _WINDOW_RFC3339_NOTE),
        ] = DEFAULT_WINDOW,
        overhead_percent: Annotated[
            int,
            Field(
                description=(
                    "Overhead buffer added on top of max observed usage when computing recommended "
                    "capacity (percent). Default 50 means recommend 1.5× max usage."
                ),
                ge=0,
                le=500,
            ),
        ] = 50,
        top_n: Annotated[
            int,
            Field(
                description="Maximum recommendations to return, sorted by savings_monthly desc. Default 20.",
                ge=1,
                le=1000,
            ),
        ] = 20,
        min_monthly_savings: Annotated[
            float,
            Field(description="Minimum monthly savings (USD) to include a recommendation. Default $1.00.", ge=0.0),
        ] = 1.0,
    ) -> PVSizingResponse:
        """Return PersistentVolumeClaim right-sizing recommendations ranked by monthly savings.

        WHAT: Calls GET /model/savings/persistentVolumeSizing and returns recommendations
        to shrink over-provisioned PVCs based on observed usage. Each row includes the
        current and recommended capacity (in bytes), current and recommended monthly cost,
        and estimated monthly savings. The full filtered set is fetched before slicing so
        that total_monthly_savings and row_count describe the full population.

        WHEN TO USE: When investigating storage over-provisioning or when the savings
        overview shows persistentVolumeSizing has significant savings.

        WHEN NOT TO USE: For unclaimed (unbound) volumes, use get_unclaimed_volumes.
        For node-level local disk savings, use get_local_disk_savings.
        """
        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window
        try:
            raw = await _fetch_pv_sizing(window=window, overhead_percent=overhead_percent)
        except McpToolError as exc:
            return PVSizingResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
            )

        recs: list[dict[str, Any]] = raw if isinstance(raw, list) else raw.get("recommendations", [])
        filtered = [r for r in recs if float(r.get("savingsMonthly", 0.0) or 0.0) >= min_monthly_savings]
        filtered.sort(key=lambda r: float(r.get("savingsMonthly", 0.0) or 0.0), reverse=True)
        total_savings = round(sum(float(r.get("savingsMonthly", 0.0) or 0.0) for r in filtered), 2)
        truncated = len(filtered) > top_n
        sliced = filtered[:top_n]

        if not sliced:
            return PVSizingResponse(
                status=QueryStatus.EMPTY,
                message=f"No PV sizing recommendations found with min_monthly_savings=${min_monthly_savings:.2f}.",
                recommended_action="Lower min_monthly_savings or check the savings overview for total PV savings.",
            )

        return PVSizingResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(filtered)} PV sizing recommendation(s) for {window_display} with total monthly savings "
                f"of ${total_savings:,.2f}" + (f" (showing top {top_n})." if truncated else ".")
            ),
            recommended_action=(
                "Review the highest-savings recommendations first. "
                "Confirm storage class supports shrinking before resizing."
            ),
            rows=[
                PVSizingRow(
                    volume_name=r.get("volumeName", ""),
                    claim_name=r.get("claimName", ""),
                    claim_namespace=r.get("claimNamespace", ""),
                    cluster_id=r.get("clusterId", ""),
                    max_usage_bytes=int(r.get("maxUsageBytes", 0) or 0),
                    average_usage_bytes=int(r.get("averageUsageBytes", 0) or 0),
                    recommended_capacity_bytes=int(r.get("recommendedCapacityBytes", 0) or 0),
                    recommended_cost_monthly=round(float(r.get("recommendedCostMonthly", 0.0) or 0.0), 4),
                    current_capacity_bytes=int(r.get("currentCapacityBytes", 0) or 0),
                    current_cost_monthly=round(float(r.get("currentCostMonthly", 0.0) or 0.0), 4),
                    savings_monthly=round(float(r.get("savingsMonthly", 0.0) or 0.0), 4),
                    storage_class=r.get("storageClass", ""),
                )
                for r in sliced
            ],
            total_monthly_savings=total_savings,
            row_count=len(filtered),
            truncated=truncated,
            resolved_window=resolved_window,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Local Disk Savings"),
    )
    async def get_local_disk_savings(
        window: Annotated[
            str,
            Field(description="Time window for usage data. Default '15d'. " + _WINDOW_RFC3339_NOTE),
        ] = DEFAULT_WINDOW,
        overhead_percent: Annotated[
            int,
            Field(
                description="Overhead buffer (percent) added when computing recommended capacity. Default 50.",
                ge=0,
                le=500,
            ),
        ] = 50,
        top_n: Annotated[
            int,
            Field(description="Maximum disks to return, sorted by savings_monthly desc. Default 20.", ge=1, le=1000),
        ] = 20,
        min_monthly_savings: Annotated[
            float,
            Field(description="Minimum monthly savings (USD) to include a disk. Default $1.00.", ge=0.0),
        ] = 1.0,
    ) -> LocalDiskSavingsResponse:
        """Return underutilized node-local disk savings recommendations.

        WHAT: Returns disks that are attached to nodes that are underutilized.
        Each row includes:
        disk name, cluster, utilization ratio (0–1 scale),
        current and recommended capacity in bytes, and estimated
        monthly savings.

        Note: utilization_percent is a 0–1 ratio, NOT a 0–100 percentage.

        WHEN TO USE: When investigating node-level local storage waste, or when the
        savings overview shows underutilizedLocalDisks has significant savings.

        WHEN NOT TO USE: For right-sizing storage for workloads (pods):
        use get_pv_sizing_recommendations.
        For unclaimed volumes, use get_unclaimed_volumes.
        """
        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window
        try:
            raw = await _fetch_local_disks(window=window, overhead_percent=overhead_percent)
        except McpToolError as exc:
            return LocalDiskSavingsResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
            )

        disks: list[dict[str, Any]] = raw if isinstance(raw, list) else raw.get("unutilizedDisks", [])
        disks, was_capped = _cap_raw_rows(disks, "local disk")
        filtered = [d for d in disks if float(d.get("savingsMonthly", 0.0) or 0.0) >= min_monthly_savings]
        filtered.sort(key=lambda d: float(d.get("savingsMonthly", 0.0) or 0.0), reverse=True)
        total_savings = round(sum(float(d.get("savingsMonthly", 0.0) or 0.0) for d in filtered), 2)
        truncated = was_capped or len(filtered) > top_n
        sliced = filtered[:top_n]

        if not sliced:
            return LocalDiskSavingsResponse(
                status=QueryStatus.EMPTY,
                message=f"No underutilized local disks found with min_monthly_savings=${min_monthly_savings:.2f}.",
                recommended_action="Lower min_monthly_savings or check the savings overview for total disk savings.",
            )

        return LocalDiskSavingsResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(filtered)} underutilized disk(s) for {window_display} with total monthly savings "
                f"of ${total_savings:,.2f}" + (f" (showing top {top_n})." if truncated else ".")
            ),
            recommended_action=(
                "Create a report for significant savings. Also combine savings by namespace or cluster."
            ),
            rows=[
                LocalDiskRow(
                    disk_name=d.get("diskName", ""),
                    cluster_id=d.get("clusterId", ""),
                    utilization_percent=round(float(d.get("utilizationPercent", 0.0) or 0.0), 6),
                    current_usage_bytes=int(d.get("currentUsageBytes", 0) or 0),
                    current_capacity_bytes=int(d.get("currentCapacityBytes", 0) or 0),
                    recommended_capacity_bytes=int(d.get("recommendedCapacityBytes", 0) or 0),
                    current_cost_monthly=round(float(d.get("currentCostMonthly", 0.0) or 0.0), 4),
                    savings_monthly=round(float(d.get("savingsMonthly", 0.0) or 0.0), 4),
                )
                for d in sliced
            ],
            total_monthly_savings=total_savings,
            row_count=len(filtered),
            truncated=truncated,
            resolved_window=resolved_window,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Cluster Rightsizing Recommendations"),
    )
    async def get_cluster_rightsizing_recommendations(
        cluster: Annotated[
            str,
            Field(
                description=(
                    "Kubecost cluster ID to fetch node group sizing recommendations for. "
                    "Omitting cluster returns an empty recommendations list (not an API error). "
                    "If you don't know the cluster name, call get_kubecost_workload_costs "
                    "with aggregate='cluster' first to discover available cluster IDs."
                )
            ),
        ],
        window: Annotated[
            str,
            Field(description="Time window for usage data. Default '15d'. " + _WINDOW_RFC3339_NOTE),
        ] = DEFAULT_WINDOW,
        profile: Annotated[
            Literal["development", "production", "high-availability"],
            Field(
                description=(
                    "Sizing conservativeness profile. 'production' (default) balances savings "
                    "and reliability. 'development' is more aggressive. 'high-availability' "
                    "is most conservative."
                )
            ),
        ] = "production",
    ) -> ClusterRightsizingResponse:
        """Return node group scale-in/scale-out/instance-type recommendations for a cluster.

        WHAT: Calls GET /model/savings/nodeGroupSizing/recommendations for the given cluster
        and returns recommendations to right-size node groups (scale in, scale out, or change
        instance type). Each recommendation includes before/after node count, instance type,
        monthly price, CPU/RAM utilization, and estimated monthly savings.

        Note: omitting cluster does NOT return an API error — it returns 200 with an empty
        recommendations list. Provide a cluster ID to get useful results.

        WHEN TO USE: When investigating node-level infrastructure savings, or when the savings
        overview shows nodeGroupSizing has significant savings.

        WHEN NOT TO USE: For container CPU/memory rightsizing, use get_container_savings_recommendations.
        For abandoned pods, use get_abandoned_workloads.
        """
        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window
        try:
            raw = await _fetch_node_group_sizing(cluster=cluster, window=window, profile=profile)
        except McpToolError as exc:
            return ClusterRightsizingResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
                cluster=cluster,
                profile=profile,
                window=window,
                resolved_window=resolved_window,
            )

        recs_raw, was_capped = _cap_raw_rows(raw.get("recommendations", []), "node group sizing")
        warnings: list[str] = raw.get("warnings") or []
        window_info = raw.get("window", {})
        window_str = window_info.get("start", window) if isinstance(window_info, dict) else window

        recs_raw_sorted = sorted(recs_raw, key=lambda r: float(r.get("savingsPerMonth", 0.0) or 0.0), reverse=True)

        def _resource_metrics(res: dict) -> ResourceMetrics:
            cap = res.get("capacity", {}) or {}
            usage = res.get("usage", {}) or {}
            return ResourceMetrics(
                capacity_avg=round(float(cap.get("avg", 0.0) or 0.0), 4),
                utilization=round(float(res.get("utilization", 0.0) or 0.0), 6),
                usage_avg=round(float(usage.get("avg", 0.0) or 0.0), 4) if usage else None,
                usage_p95=round(float(usage.get("p95", 0.0) or 0.0), 4) if usage.get("p95") is not None else None,
            )

        def _node_group_state(state: dict) -> NodeGroupState:
            resources = state.get("resources", {}) or {}
            cpu_res = resources.get("cpu", {}) or {}
            ram_res = resources.get("ram", {}) or {}
            return NodeGroupState(
                instance_type=state.get("instanceType", ""),
                node_count=int(state.get("nodeCount", 0) or 0),
                price_per_month=round(float(state.get("pricePerMonth", 0.0) or 0.0), 2),
                cpu=_resource_metrics(cpu_res),
                ram=_resource_metrics(ram_res),
            )

        recommendations: list[NodeGroupRecommendation] = []
        for r in recs_raw_sorted:
            before_state = _node_group_state(r.get("before", {}))
            after_state = _node_group_state(r.get("after", {}))
            monthly_cost_delta = round(after_state.price_per_month - before_state.price_per_month, 2)
            rec_class = _classify_node_recommendation(r.get("recommendation", ""), before_state, after_state)
            # savings_per_month: clamped non-negative view for cost_saving rows; 0 for capacity rows
            savings_value = max(0.0, -monthly_cost_delta) if rec_class == "cost_saving" else 0.0
            recommendations.append(
                NodeGroupRecommendation(
                    node_group=r.get("nodeGroup", ""),
                    recommendation=r.get("recommendation", ""),
                    recommendation_class=rec_class,
                    before=before_state,
                    after=after_state,
                    monthly_cost_delta=monthly_cost_delta,
                    savings_per_month=round(savings_value, 2),
                )
            )

        # total_savings_per_month: clamped, non-negative sum of cost_saving rows only
        total_savings = round(sum(rec.savings_per_month for rec in recommendations), 2)
        # net_cost_change: signed sum across all recommendations (may be negative, zero, or positive)
        net_cost_change = round(sum(rec.monthly_cost_delta for rec in recommendations), 2)

        if not recommendations:
            return ClusterRightsizingResponse(
                status=QueryStatus.EMPTY,
                message=(
                    f"No node group sizing recommendations found for cluster '{cluster}'. "
                    "Verify the cluster ID is correct and that Kubecost has usage data for it."
                ),
                recommended_action=(
                    "Call get_kubecost_workload_costs with aggregate='cluster' to list available cluster IDs."
                ),
                cluster=cluster,
                profile=profile,
                window=window_str,
                total_savings_per_month=0.0,
                net_cost_change=0.0,
                warnings=warnings,
            )

        # Separate capacity from cost-saving recommendations in the summary for clarity.
        capacity_count = sum(1 for rec in recommendations if rec.recommendation_class == "capacity")
        capacity_note = (
            f" ({capacity_count} capacity recommendation(s) excluded from savings total.)" if capacity_count else ""
        )

        return ClusterRightsizingResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(recommendations)} node group recommendation(s) for cluster '{cluster}' "
                f"for {window_display} with estimated monthly savings of ${total_savings:,.2f}.{capacity_note}"
            ),
            recommended_action=(
                "Review cost_saving recommendations (ScaleIn, ChangeInstanceType) first for quickest savings. "
                "Capacity recommendations (ScaleOut) improve reliability but do not reduce cost. "
                "Validate node counts against workload headroom before applying."
            ),
            cluster=cluster,
            profile=profile,
            window=window_str,
            resolved_window=resolved_window,
            recommendations=recommendations,
            total_savings_per_month=total_savings,
            net_cost_change=net_cost_change,
            recommendation_count=len(recommendations),
            truncated=was_capped,
            warnings=warnings,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Unclaimed Volumes"),
    )
    async def get_unclaimed_volumes(
        window: Annotated[
            str,
            Field(description="Time window for cost data. Default '15d'. " + _WINDOW_RFC3339_NOTE),
        ] = DEFAULT_WINDOW,
        top_n: Annotated[
            int,
            Field(description="Maximum volumes to return, sorted by monthly_cost desc. Default 20.", ge=1, le=1000),
        ] = 20,
        min_monthly_cost: Annotated[
            float,
            Field(description="Minimum monthly cost (USD) to include a volume. Default $1.00.", ge=0.0),
        ] = 1.0,
    ) -> UnclaimedVolumesResponse:
        """Return PersistentVolumes that are provisioned but not bound to any PVC.

        WHAT: Returns volumes that exist in the cluster but have not attached to a workload.
        This is generally a sign that they are no longer needed (100% wasted cost).

        Note: These volumes have no PVC attached — deletion is generally safe, but confirm
        with your storage or platform team before removing any volume.

        WHEN TO USE: When investigating unattached storage waste, or when the savings
        overview shows unclaimedVolumes has significant savings.

        WHEN NOT TO USE: For over-provisioned PVCs that ARE in use, use
        get_pv_sizing_recommendations. For node-local disk savings, use get_local_disk_savings.
        """
        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window
        try:
            raw = await _fetch_unclaimed_volumes(window=window)
        except McpToolError as exc:
            return UnclaimedVolumesResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
            )

        data = raw if isinstance(raw, dict) else {}
        volumes, was_capped = _cap_raw_rows(data.get("volumes", []), "unclaimed volume")
        total_monthly_cost_api = round(_float_field(data, "monthlyCost"), 2)

        filtered = [v for v in volumes if float(v.get("monthlyCost", 0.0) or 0.0) >= min_monthly_cost]
        filtered.sort(key=lambda v: float(v.get("monthlyCost", 0.0) or 0.0), reverse=True)
        total_cost = round(sum(float(v.get("monthlyCost", 0.0) or 0.0) for v in filtered), 2)
        truncated = was_capped or len(filtered) > top_n
        sliced = filtered[:top_n]

        if not sliced:
            return UnclaimedVolumesResponse(
                status=QueryStatus.EMPTY,
                message=f"No unclaimed volumes found with min_monthly_cost=${min_monthly_cost:.2f}.",
                recommended_action=(
                    "Lower min_monthly_cost or check the savings overview for total unclaimed volume savings."
                ),
                total_monthly_cost=total_monthly_cost_api,
            )

        return UnclaimedVolumesResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {len(filtered)} unclaimed volume(s) for {window_display} with total monthly cost "
                f"of ${total_cost:,.2f}" + (f" (showing top {top_n})." if truncated else ".")
            ),
            recommended_action=(
                "These volumes have no associated workload — this typically means that they are no longer needed. "
                "This tends to be any easy savings opportunity. Confirm with application owner."
            ),
            rows=[
                UnclaimedVolumeRow(
                    volume_name=v.get("volumeName", ""),
                    monthly_cost=round(float(v.get("monthlyCost", 0.0) or 0.0), 4),
                    properties=UnclaimedVolumeProperties(
                        cluster=v.get("properties", {}).get("cluster", ""),
                        provider=v.get("properties", {}).get("provider", ""),
                        service=v.get("properties", {}).get("service", ""),
                        name=v.get("properties", {}).get("name", ""),
                        provider_id=v.get("properties", {}).get("providerID", ""),
                    ),
                )
                for v in sliced
            ],
            total_monthly_cost=total_cost,
            row_count=len(filtered),
            truncated=truncated,
            resolved_window=resolved_window,
        )

    @mcp.tool(
        version=_VERSION,
        annotations=_read_only("Get Resource Quota Recommendations"),
    )
    async def get_resource_quota_recommendations(
        window: Annotated[
            str,
            Field(description="Time window for usage data. Default '15d'. " + _WINDOW_RFC3339_NOTE),
        ] = DEFAULT_WINDOW,
        profile: Annotated[
            Literal["development", "production", "high-availability"],
            Field(
                description=(
                    "Sizing conservativeness profile. 'production' (default) balances correctness "
                    "and headroom. 'development' is more aggressive. 'high-availability' adds more buffer."
                )
            ),
        ] = "production",
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum namespace recommendations to return per page (server-side pagination). "
                    "Default 20. Unlike other savings tools, this is true API-side pagination — "
                    "not a post-fetch client-side slice."
                ),
                ge=1,
                le=1000,
            ),
        ] = 20,
        offset: Annotated[
            int,
            Field(description="Pagination offset (0-based). Default 0.", ge=0),
        ] = 0,
    ) -> ResourceQuotaResponse:
        """Return namespace-level ResourceQuota sizing recommendations.

        WHAT: Calls GET /model/savings/resourceQuotaSizing/recommendations and returns
        per-namespace recommendations to create or resize ResourceQuota objects. Each
        recommendation covers one namespace and contains a list of resource type changes
        (CPU requests, memory requests, etc.). isNewResourceQuota=true means no quota
        exists yet (create action); isDownsize=true means reducing an existing quota.

        Note: total_monthly_savings may be 0 — this is a configuration-correctness tool,
        not primarily a dollar-savings tool. It helps prevent over-allocation and enforce
        namespace-level resource governance.

        Note on pagination: limit and offset are true server-side pagination parameters
        (unlike top_n in other savings tools which is client-side slicing after a broad
        fetch). Use offset to page through large result sets.

        WHEN TO USE: When investigating namespace resource governance, quota drift, or
        when the savings overview shows resourceQuotaSizing has recommendations.

        WHEN NOT TO USE: For container CPU/memory rightsizing within a namespace, use
        get_container_savings_recommendations. For node-level savings, use
        get_cluster_rightsizing_recommendations.
        """
        window = normalize_window_order(window)
        resolved_window = _resolve_window_defensively(window)
        window_display = resolved_window.display if resolved_window else window
        try:
            raw = await _fetch_resource_quota_recommendations(
                window=window, profile=profile, limit=limit, offset=offset
            )
        except McpToolError as exc:
            return ResourceQuotaResponse(
                status=QueryStatus.ERROR,
                message=str(exc),
                recommended_action="Check Kubecost connectivity and credentials, then retry.",
            )

        # This endpoint echoes the range it actually queried; prefer it over the prediction.
        resolved_window = resolved_window_from_api(raw.get("window"), window) or resolved_window
        window_display = resolved_window.display if resolved_window else window

        recs_raw: list[dict[str, Any]] = raw.get("recommendations", [])
        # P0: Cap oversized upstream responses — this endpoint lacks a top_n client-side guard
        # that every other row-returning tool has. At ~900 bytes/row, 1,873 rows ≈ 1.7MB, which
        # exceeds typical MCP client response-size limits and produces "no visible payload".
        recs_raw, was_capped = _cap_raw_rows(recs_raw, "resource quota")
        item_count = int(raw.get("itemCount", len(recs_raw)) or len(recs_raw))
        total_monthly_savings = round(float(raw.get("totalMonthlySavings", 0.0) or 0.0), 2)
        truncated = was_capped or item_count > offset + len(recs_raw)
        next_offset = (offset + len(recs_raw)) if truncated else None

        # P0: Integrity check — warn when rows carry blank cluster/namespace but have resources.
        # Blank dimensions indicate an unattributed row from the API, not a missing-quota entry.
        integrity_warnings: list[str] = []
        blank_rows = [r for r in recs_raw if (not r.get("cluster") or not r.get("namespace")) and r.get("resources")]
        if blank_rows:
            logger.warning(
                "ResourceQuota API returned %d row(s) with blank cluster or namespace but populated resources",
                len(blank_rows),
            )
            integrity_warnings.append(
                f"{len(blank_rows)} row(s) had a blank cluster or namespace field with populated resources — "
                "these may be unattributed recommendations from the Kubecost API."
            )

        # P0: Dedupe check — warn on duplicate (cluster, namespace, category) keys.
        seen_keys: set[tuple[str, str, str]] = set()
        dupe_count = 0
        for r in recs_raw:
            key = (r.get("cluster", ""), r.get("namespace", ""), r.get("category", ""))
            if key in seen_keys:
                dupe_count += 1
            else:
                seen_keys.add(key)
        if dupe_count:
            logger.warning("ResourceQuota API returned %d duplicate (cluster, namespace, category) key(s)", dupe_count)
            integrity_warnings.append(
                f"{dupe_count} duplicate (cluster, namespace, category) combination(s) detected in API response."
            )

        if not recs_raw:
            return ResourceQuotaResponse(
                status=QueryStatus.EMPTY,
                message=f"No resource quota recommendations found for {window_display}.",
                recommended_action="Try a different window or profile, or check the savings overview.",
                item_count=item_count,
                total_monthly_savings=total_monthly_savings,
                resolved_window=resolved_window,
                next_offset=None,
            )

        recommendations = [
            QuotaNamespaceRecommendation(
                cluster=r.get("cluster", ""),
                namespace=r.get("namespace", ""),
                category=r.get("category", ""),
                is_new_resource_quota=bool(r.get("isNewResourceQuota", False)),
                resources=[
                    QuotaResourceChange(
                        resource_type=res.get("resourceType", ""),
                        category=res.get("category", ""),
                        used=res.get("used", ""),
                        recommended=res.get("recommended", ""),
                        is_new_resource=bool(res.get("isNewResource", False)),
                        is_downsize=bool(res.get("isDownsize", False)),
                    )
                    for res in r.get("resources", [])
                ],
            )
            for r in recs_raw
        ]

        return ResourceQuotaResponse(
            status=QueryStatus.OK,
            message=(
                f"Found {item_count} namespace quota recommendation(s) for {window_display}"
                + (f" (page offset={offset}, showing {len(recommendations)})." if offset > 0 or truncated else ".")
                + (f" Integrity warnings: {'; '.join(integrity_warnings)}" if integrity_warnings else "")
            ),
            recommended_action=(
                "Focus on namespaces with is_downsize=true for immediate savings. "
                "Create missing quotas (is_new_resource_quota=true) to enforce governance."
                + (" Use next_offset to retrieve further pages." if truncated else "")
            ),
            recommendations=recommendations,
            item_count=item_count,
            total_monthly_savings=total_monthly_savings,
            truncated=truncated,
            next_offset=next_offset,
            resolved_window=resolved_window,
        )

    # ── Resources (Rule #17) ───────────────────────────────────────────────────

    @mcp.resource("kubecost://schema/allocation-params")
    def allocation_params_schema() -> str:
        """Valid parameter values for get_kubecost_workload_costs."""
        agg_lines = "\n".join(f"  {k:30s} — {v}" for k, v in _AGGREGATE_CHOICES.items())
        win_lines = "\n".join(f"  {k:12s} — {v}" for k, v in _WINDOW_CHOICES.items())
        return f"""\
aggregate dimensions (single or comma-separated):
{agg_lines}
  <any>                          — also accepts: node, container, controllerKind,
                                   label, annotation, department, environment,
                                   owner, product, team

window formats:
{win_lines}
  <RFC3339 range>  — e.g. "2026-05-01T00:00:00Z,2026-06-01T00:00:00Z"

accumulate:
  true  (default) — single total for the window
  false           — daily breakdown (use for trend analysis only)
"""

    @mcp.resource("kubecost://schema/cost-fields")
    def cost_fields_schema() -> str:
        """Definitions of cost columns returned by get_kubecost_workload_costs."""
        return """\
cpuCost          — CPU request cost
cpuCostIdle      — CPU idle cost (unused capacity)
ramCost          — RAM request cost
ramCostIdle      — RAM idle cost
networkCost      — egress/ingress network cost
pvCost           — persistent volume storage cost
gpuCost          — GPU cost
gpuCostIdle      — GPU idle cost
loadBalancerCost — load balancer cost
sharedCost       — shared namespace overhead allocation
totalCost        — sum of all cost components
totalEfficiency  — utilization ratio 0–1 (request vs actual use)
"""

    @mcp.resource("kubecost://schema/sizing-profiles")
    def sizing_profiles_schema() -> str:
        """Named sizing profiles for get_container_savings_recommendations."""
        return format_profiles_resource()

    @mcp.resource("kubecost://guides/container-sizing")
    def container_sizing_guide_resource() -> str:
        """Full container request sizing reference for CPU and memory reservations."""
        return CONTAINER_SIZING_REFERENCE

    # ── Prompts (Rule #17) ────────────────────────────────────────────────────

    @mcp.prompt()
    def container_rightsizing_guide() -> str:
        """Explain how to properly size Kubernetes container CPU and memory requests.

        Use when the user asks about rightsizing methodology, quantiles, or
        CPU vs memory sizing strategy — before calling the savings tool.
        """
        return CONTAINER_SIZING_GUIDE

    @mcp.prompt()
    def explore_container_savings() -> str:
        """Start a guided container rightsizing exploration. Presents choices step-by-step."""
        # Generated from SIZING_PROFILES so the menu can never drift from what the profiles send.
        profile_menu = "\n".join(
            [f"  - **{name}**: {desc}" for name, desc in PROFILE_DESCRIPTIONS.items()]
            + ["  - **custom**: Specify your own quantiles and filters"]
        )
        return f"""\
Let's find container rightsizing opportunities. I'll walk you through a few choices.

---

**Step 1 — Sizing profile**
What environment are these workloads running in?

{profile_menu}

Pick a profile or describe your preferences.

---

**Step 2 — Time window**
{_CONTAINER_SAVINGS_WINDOW_CLARIFICATION}

---

**Step 3 — Filter preferences**
{_SAVINGS_FILTER_CLARIFICATION}

---

Once you've answered all three, call `get_container_savings_recommendations` with your choices.
Present the Executive Summary with a chart, the interpretation block, and a summary table.
"""

    @mcp.prompt()
    def container_savings_window_help() -> str:
        """Explain the time window options for the container savings tool."""
        return _CONTAINER_SAVINGS_WINDOW_CLARIFICATION

    @mcp.prompt()
    def container_savings_filter_help() -> str:
        """Explain the min_monthly_savings filter for container savings."""
        return _SAVINGS_FILTER_CLARIFICATION

    @mcp.prompt()
    def explore_costs() -> str:
        """Start a guided Kubernetes cost exploration. Presents choices step-by-step."""
        agg_menu = "\n".join(f"  {i + 1}. **{k}** — {v}" for i, (k, v) in enumerate(_AGGREGATE_CHOICES.items()))
        window_menu = "\n".join(f"  {i + 1}. **{k}** — {v}" for i, (k, v) in enumerate(_WINDOW_CHOICES.items()))
        return f"""\
Let's explore your Kubernetes costs. I'll walk you through a few quick choices.

---

**Step 1 — Time window**
How far back would you like to look?

{window_menu}

Pick a number, 7d, 30d, 90d or RFC3339 CSV range

---

**Step 2 — Group costs by**
Once you've chosen a time window, pick how to break down the spend:

{agg_menu}
  {len(_AGGREGATE_CHOICES) + 1}. **Custom** — type any dimension or comma-separated combination

Pick an option or describe what you want

---

**Step 3 — View type**
- **A. Summary totals** (default) — one row per dimension for the whole time window
- **B. Daily trend** — one row per day per dimension (good for spotting spikes)

---

Once you've answered all three, I'll call `get_kubecost_workload_costs` with your choices and
present an Executive Summary with a chart.
"""

    @mcp.prompt()
    def explore_cost_comparison() -> str:
        """Start a guided cost anomaly / spike investigation using period-over-period comparison."""
        agg_menu = "\n".join(f"  - **{k}** — {v}" for k, v in _AGGREGATE_CHOICES.items())
        return f"""\
Let's find out what changed in your Kubernetes costs. I'll walk you through picking two
comparable periods, then compare them.

---

**Step 1 — Pick two periods**
Both windows must be explicit RFC3339 ranges ending before today (UTC). Named aliases like
'lastweek', 'lastmonth', '7d', etc. are not accepted — there is no alias for "the period
before lastmonth", so aliases are a dead end for comparisons.

Example (rolling week-over-week):
  current_window='2026-07-13T00:00:00Z,2026-07-20T00:00:00Z'
  baseline_window='2026-07-06T00:00:00Z,2026-07-13T00:00:00Z'

Ranges of different lengths are allowed; a warning will appear in the response when they differ.

---

**Step 2 — Group costs by**
{agg_menu}

---

Once you've answered both, call `get_kubecost_cost_comparison` with current_window,
baseline_window, and aggregate.

Then present:
1. A 2-3 bullet Executive Summary highlighting the biggest movers (largest absolute change).
2. A summary table sorted by change descending, calling out any dimension whose `row_status`
   is `new` or `removed`. When the response warns that the periods differ in length, quote
   `daily_change` and `normalized_pct_change` instead of `change` and `pct_change`.
3. Based on which dimension changed most, suggest the matching drill-down tool:
   - Container/pod-level increase → `get_container_savings_recommendations`
   - Idle/dormant workload appeared → `get_abandoned_workloads`
   - Node/cluster-level shift → `get_cluster_rightsizing_recommendations`
"""

    @mcp.prompt()
    def top_spenders() -> str:
        """Show top cost drivers across clusters and namespaces for a given window."""
        return """\
Show me the top Kubernetes cost drivers.

First, call `kubecost_list_windows` and present the window options to the user.
Wait for their reply, then call `get_kubecost_workload_costs` with:
- aggregate: "cluster,namespace"
- window: <user's chosen window>
- accumulate: true
- top_n: 20

Then present:
1. A 2-3 bullet Executive Summary of the biggest cost drivers and any anomalies.
2. An SVG bar chart of the top 10 by totalCost.
"""

    @mcp.prompt()
    def cost_trend(window: str, aggregate: str) -> str:
        """Show daily cost trend for a given aggregation dimension."""
        return f"""\
Show me the daily cost trend for my clusters.

First, call `kubecost_list_windows` and present the window options to the user.
Wait for their reply, then call `get_kubecost_workload_costs` with:
- aggregate: "{aggregate}"
- window: <user's chosen window>
- accumulate: false
- top_n: 10

Then present:
1. A 2-3 bullet summary of trend direction and any notable spikes.
2. An SVG line chart (date on X axis, totalCost on Y axis, one line per {aggregate}).
"""

    @mcp.prompt()
    def explore_abandoned_workloads() -> str:
        """Start a guided abandoned-workload investigation. Walks the user through threshold and scope choices."""
        return """\
Let's find abandoned Kubernetes workloads — running pods that appear idle and are costing money.

---

**Step 1 — Lookback window**
How many days back should we look for low network activity?
- **2 days** (default) — catches recently idle workloads; less noise
- **7 days** — broader view, catches workloads idle for a week
- **30 days** — conservative; only flags long-term dormant pods

---

**Step 2 — Traffic threshold**
Pods with average network traffic (both ingress AND egress) below this value are flagged.
- **500 bytes/second** (default) — ~43 KB/day; effectively idle
- **1000 bytes/second** — catches very light-traffic workloads too
- **Custom** — specify a number in bytes/second

---

**Step 3 — Scope**
- **All clusters** (default) — start here to get the full picture
- **Specific cluster** — filter to a single cluster ID if you already know which one to investigate

---

Once you've answered, call `get_abandoned_workloads` with your choices.
Present a summary table sorted by monthly savings, highlight the top 3 candidates, and
suggest next steps (confirm with owning team before decommissioning).
"""


# ---------------------------------------------------------------------------
# Private API fetch helpers
# ---------------------------------------------------------------------------


async def _fetch_request_sizing(
    window: str,
    algorithm_cpu: str,
    algorithm_ram: str,
    q_cpu: float,
    q_ram: float,
    target_cpu_utilization: float,
    target_ram_utilization: float,
    filter_str: str,
    limit: int,
) -> dict[str, Any]:
    """Fetch container savings recommendations via the shared API wrapper."""
    params: dict[str, Any] = {
        "algorithmCPU": algorithm_cpu,
        "algorithmRAM": algorithm_ram,
        "qCPU": q_cpu,
        "qRAM": q_ram,
        "targetCPUUtilization": target_cpu_utilization,
        "targetRAMUtilization": target_ram_utilization,
        "filter": filter_str,
        "window": to_api_window(window),
        "offset": 0,
        "limit": limit,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_CONTAINER_SAVINGS}"
    logger.debug("Kubecost request sizing: path=%s window=%s", path, window)
    return await call_get_api(path, params=params)


async def _fetch_allocation(
    aggregate: str,
    window: str,
    accumulate: bool,
    limit: int,
) -> dict[str, Any]:
    """Fetch allocation data via the shared API wrapper.

    ``shareIdle`` distributes idle cost across the returned rows, so no separate
    ``__idle__`` row is produced. ``splitIdle`` — which only controls how a
    *standalone* idle row is broken up — is therefore not sent; alongside
    ``shareIdle`` it is a no-op.
    """
    params: dict[str, Any] = {
        "window": to_api_window(window),
        "aggregate": aggregate,
        "accumulate": str(accumulate).lower(),
        "idle": "true",
        "shareIdle": "true",
        "sortBy": "totalCost",
        "sortByOrder": "desc",
        "limit": limit,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_ALLOCATION}"
    logger.debug("Kubecost allocation: path=%s window=%s", path, window)
    return await call_get_api(path, params=params)


async def _fetch_abandoned_workloads(
    days: int,
    threshold: int,
    cluster: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch abandoned workloads from the Kubecost savings API."""
    params: dict[str, Any] = {
        "days": days,
        "threshold": threshold,
        "offset": "",
        "limit": limit,
        "filter": f'cluster:"{cluster}"' if cluster else "",
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_ABANDONED_WORKLOADS}"
    logger.debug("Kubecost abandoned workloads: path=%s days=%s threshold=%s", path, days, threshold)
    result = await call_get_api(path, params=params)
    # API returns a bare JSON array
    if isinstance(result, list):
        return result
    # Defensive: handle unexpected dict wrapper
    if isinstance(result, dict):
        wrapped_rows = result.get("data", result.get("workloads", []))
        if isinstance(wrapped_rows, list):
            return [row for row in wrapped_rows if isinstance(row, dict)]
    return []


def _parse_abandoned_workloads_response(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the abandonedWorkloads API response into a list of row dicts.

    Each row is ready for AbandonedWorkloadRow.model_validate().  Nested objects
    (allocation, owners) are unpacked into flat fields used by the response model.
    Rows are sorted by monthlySavings descending.
    """
    rows: list[dict[str, Any]] = []
    for item in raw:
        # Flatten owner info — take the first owner if present
        owners: list[dict] = item.get("owners") or []
        first_owner = owners[0] if owners else {}

        allocation: dict = item.get("allocation") or {}

        row: dict[str, Any] = {
            "pod": item.get("pod", ""),
            "namespace": item.get("namespace", ""),
            "node": item.get("node", ""),
            "clusterId": item.get("clusterId", ""),
            "owner_name": first_owner.get("name", ""),
            "owner_kind": first_owner.get("kind", ""),
            "ingressBytesPerSecond": float(item.get("ingressBytesPerSecond", 0.0) or 0.0),
            "egressBytesPerSecond": float(item.get("egressBytesPerSecond", 0.0) or 0.0),
            "allocated_cpu_cores": float(allocation.get("cpuCores", 0.0) or 0.0),
            "allocated_ram_bytes": float(allocation.get("ramBytes", 0.0) or 0.0),
            "monthlySavings": float(item.get("monthlySavings", 0.0) or 0.0),
        }
        rows.append(row)

    rows.sort(key=lambda r: r.get("monthlySavings", 0.0), reverse=True)
    return rows


async def _fetch_savings_overview() -> dict[str, Any]:
    """Fetch the savings overview from GET /model/savings."""
    path = f"{get_settings().kubecost_api_base_path}{_SEG_SAVINGS_OVERVIEW}"
    logger.debug("Kubecost savings overview: path=%s", path)
    result = await call_get_api(path, params={})
    # API returns { code, data, meta } — unwrap data
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result if isinstance(result, dict) else {}


async def _fetch_pv_sizing(window: str, overhead_percent: int) -> dict[str, Any]:
    """Fetch PV sizing recommendations with a broad fixed limit so callers can sort before slicing."""
    params: dict[str, Any] = {
        "window": to_api_window(window),
        "overheadPercent": overhead_percent,
        "offset": 0,
        "limit": _SAVINGS_API_FETCH_LIMIT,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_PV_SIZING}"
    logger.debug("Kubecost PV sizing: path=%s window=%s", path, window)
    result = await call_get_api(path, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result if isinstance(result, dict) else {}


async def _fetch_local_disks(window: str, overhead_percent: int) -> dict[str, Any]:
    """Fetch local disk savings recommendations."""
    params: dict[str, Any] = {
        "window": to_api_window(window),
        "overheadPercent": overhead_percent,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_LOCAL_DISKS}"
    logger.debug("Kubecost local disks: path=%s window=%s", path, window)
    result = await call_get_api(path, params=params)
    # API returns { unutilizedDisks: [...] } directly (no code/data wrapper)
    if isinstance(result, dict):
        return result
    return {}


async def _fetch_node_group_sizing(cluster: str, window: str, profile: str) -> dict[str, Any]:
    """Fetch node group sizing recommendations; unwraps the { code, data } wrapper."""
    params: dict[str, Any] = {
        "cluster": cluster,
        "window": to_api_window(window),
        "profile": profile,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_NODE_GROUP_SIZING}"
    logger.debug("Kubecost node group sizing: path=%s cluster=%s", path, cluster)
    result = await call_get_api(path, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result if isinstance(result, dict) else {}


async def _fetch_unclaimed_volumes(window: str) -> dict[str, Any]:
    """Fetch unclaimed volumes; unwraps the { code, data } wrapper."""
    params: dict[str, Any] = {"window": to_api_window(window)}
    path = f"{get_settings().kubecost_api_base_path}{_SEG_UNCLAIMED_VOLUMES}"
    logger.debug("Kubecost unclaimed volumes: path=%s window=%s", path, window)
    result = await call_get_api(path, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result if isinstance(result, dict) else {}


async def _fetch_resource_quota_recommendations(
    window: str,
    profile: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Fetch resource quota recommendations; hardcodes show='all' (only confirmed valid value)."""
    params: dict[str, Any] = {
        "window": to_api_window(window),
        "profile": profile,
        "show": "all",
        "limit": limit,
        "offset": offset,
    }
    path = f"{get_settings().kubecost_api_base_path}{_SEG_RESOURCE_QUOTA}"
    logger.debug("Kubecost resource quota: path=%s window=%s", path, window)
    result = await call_get_api(path, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result if isinstance(result, dict) else {}
