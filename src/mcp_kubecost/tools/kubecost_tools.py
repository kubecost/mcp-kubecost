"""Kubecost allocation & savings tools.

Returns fully-typed Pydantic structured output — no CSV files, no in-memory
resource store.  Rows are returned directly in the response payload; large
result sets are paged via the ``next_cursor`` / ``cursor`` pattern.

Contract version: 3.0
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.domain.kubecost.sizing_guidance import (
    CONTAINER_SIZING_GUIDE,
    CONTAINER_SIZING_REFERENCE,
    FIELD_DESCRIPTIONS,
    PresetName,
    build_result_interpretation,
    format_presets_resource,
    resolve_sizing_params,
)
from mcp_kubecost.tools._common import BaseToolResponse, McpToolError, QueryStatus, call_get_api

logger = logging.getLogger(__name__)

_VERSION = "3.0"

_READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

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
**Container savings filter options:**

1. **Include undersized containers?**
   Some containers are *under*-provisioned — rightsizing them would *increase* cost.
   Include these in the results? (default: **No**)

2. **Include trivial savings?**
   Containers saving less than **$10/month** may not be worth the operational effort to rightsize.
   Include these low-value recommendations? (default: **No**)

Reply with your preferences (e.g. "No to both", "Yes to undersized, No to trivial").
---
"""

_CONTAINER_SAVINGS_WINDOW_CLARIFICATION = f"""\
15 days is used by default.
When using quantiles, a minimum number of data points (15 days) is needed for meaningful calculations.

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


def _parse_allocation_response(
    response: dict[str, Any],
) -> tuple[list[str], list[dict]]:
    """Parse a Kubecost allocation API response into (dimension_columns, rows).

    Inspects ``properties`` of the first entry to discover dimensions; falls back
    to splitting ``name`` by ``/`` when no known property keys are present.
    """
    data_list: list[dict] = response.get("data", [])
    if not data_list:
        return [], []

    all_entries: list[dict] = []
    for bucket in data_list:
        all_entries.extend(bucket.values())

    if not all_entries:
        return [], []

    first_props: dict = all_entries[0].get("properties", {})
    dimension_cols = [k for k in _KNOWN_DIMENSIONS if k in first_props]

    if not dimension_cols:
        parts = all_entries[0].get("name", "").split("/")
        dimension_cols = [f"dim_{i}" for i in range(len(parts))]

    rows: list[dict] = []
    for entry in all_entries:
        row: dict = {}
        props = entry.get("properties", {})

        if dimension_cols and not dimension_cols[0].startswith("dim_"):
            for col in dimension_cols:
                val = props.get(col, "")
                if isinstance(val, list):
                    val = "|".join(val)
                row[col] = val
        else:
            parts = entry.get("name", "").split("/")
            for i, col in enumerate(dimension_cols):
                row[col] = parts[i] if i < len(parts) else ""

        window_dict = entry.get("window", {})
        row["window_start"] = _format_date(window_dict.get("start", ""))

        for field in COST_FIELDS:
            value = entry.get(field, 0.0)
            row[field] = _format_number(float(value))

        rows.append(row)

    return dimension_cols, rows


def _aggregate_by_dimensions(rows: list[dict], dimension_cols: list[str]) -> list[dict]:
    """Sum cost fields across rows sharing the same dimension values.

    Returns rows sorted by ``totalCost`` descending with idle-% columns derived
    from summed idle vs total figures.
    """
    groups: dict[tuple, dict[str, Any]] = defaultdict(lambda: defaultdict(float))

    for row in rows:
        key = tuple(row.get(dim, "") for dim in dimension_cols)
        for field in COST_FIELDS:
            if field != "totalEfficiency":
                value = row.get(field, 0)
                if isinstance(value, (int, float)):
                    groups[key][field] += float(value)
        for dim in dimension_cols:
            groups[key][dim] = row.get(dim, "")

    aggregated: list[dict] = []
    for _key, values in groups.items():
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

    aggregated.sort(key=lambda r: float(r.get("totalCost", 0)), reverse=True)

    for row in aggregated:
        for field in COST_FIELDS:
            if field in row and field != "totalEfficiency":
                row[field] = _format_number(row[field])

    return aggregated


# ---------------------------------------------------------------------------
# Container savings parsing
# ---------------------------------------------------------------------------

_SAVINGS_API_FETCH_LIMIT = 1000  # Maximum rows to request from the Kubecost API per call

SAVINGS_AGGREGATE_OPTIONS: tuple[str, ...] = ("containerName", "namespace", "clusterID")

SAVINGS_METADATA_FIELDS: list[str] = [
    "clusterID",
    "namespace",
    "controllerKind",
    "controllerName",
    "containerName",
]

SAVINGS_FIELDS: list[str] = SAVINGS_METADATA_FIELDS + [
    "monthlySavings_cpu",
    "monthlySavings_memory",
    "monthlySavings_total",
    "Recommended_cpu",
    "Recommended_memory",
    "current_cpu",
    "current_memory",
    "currentEfficiency_cpu",
    "currentEfficiency_memory",
    "currentEfficiency",
    "AvgUsage_cpu",
    "AvgUsage_memory",
    "MaxUsage_cpu",
    "MaxUsage_memory",
    "notes",
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
    if _float_field(row, "Recommended_memory") < _float_field(row, "MaxUsage_memory"):
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
        rows: flat dicts (SAVINGS_FIELDS columns) sorted by monthlySavings_total descending.
    """
    total_monthly_savings: float = float(response.get("TotalMonthlySavings", 0.0))
    count: int = int(response.get("Count", 0))
    recommendations: list[dict] = response.get("Recommendations", [])

    rows: list[dict] = []
    for rec in recommendations:
        row: dict = {}

        for field in SAVINGS_METADATA_FIELDS:
            row[field] = rec.get(field, "")

        def _nest(obj_key: str, sub_key: str, col: str) -> None:
            obj = rec.get(obj_key, {}) or {}
            val = obj.get(sub_key, 0.0)
            row[col] = _format_number(float(val)) if isinstance(val, (int, float)) else val

        _nest("monthlySavings", "cpu", "monthlySavings_cpu")
        _nest("monthlySavings", "memory", "monthlySavings_memory")
        _nest("monthlySavings", "total", "monthlySavings_total")
        _nest("normalizedRecommendedRequest", "cpuInMilliCores", "Recommended_cpu")
        _nest("normalizedRecommendedRequest", "memoryInMiB", "Recommended_memory")
        _nest("normalizedLatestKnownRequest", "cpuInMilliCores", "current_cpu")
        _nest("normalizedLatestKnownRequest", "memoryInMiB", "current_memory")
        _nest("currentEfficiency", "cpu", "currentEfficiency_cpu")
        _nest("currentEfficiency", "memory", "currentEfficiency_memory")
        _nest("currentEfficiency", "total", "currentEfficiency")
        _nest("normalizedAverageUsage", "cpuInMilliCores", "AvgUsage_cpu")
        _nest("normalizedAverageUsage", "memoryInMiB", "AvgUsage_memory")
        _nest("normalizedMaxUsage", "cpuInMilliCores", "MaxUsage_cpu")
        _nest("normalizedMaxUsage", "memoryInMiB", "MaxUsage_memory")

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
        description="Start date of the allocation window (YYYY-MM-DD).",
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
            "Aggregated allocation rows sorted by totalCost descending. "
            "Each row contains the requested dimension values (e.g. cluster, namespace) "
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
    recommended_cpu: float = Field(
        default=0.0,
        alias="Recommended_cpu",
        description="Recommended CPU request in millicores.",
    )
    recommended_memory: float = Field(
        default=0.0,
        alias="Recommended_memory",
        description="Recommended memory request in MiB.",
    )
    # NOTE: These aliases match the *flattened* keys produced by parse_request_sizing_response
    # (e.g. "current_cpu"), NOT the raw API keys (e.g. "normalizedLatestKnownRequest.cpuInMilliCores").
    # This is intentional and differs from the camelCase-alias convention used by other fields.
    current_cpu: float = Field(
        default=0.0,
        alias="current_cpu",
        description="Current CPU request in millicores.",
    )
    current_memory: float = Field(
        default=0.0,
        alias="current_memory",
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
    total_monthly_savings: float = Field(
        description=(
            "Total monthly savings across the FILTERED recommendations (USD) — the same "
            "population described by 'summary' and 'container_count'. Excludes rows removed "
            "by the include_undersized / min_monthly_savings filters."
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
# Tool registration
# ---------------------------------------------------------------------------


def register_kubecost_tools(mcp: FastMCP) -> None:
    """Register Kubecost allocation and savings tools, resources, and prompts."""

    # ── Tools ─────────────────────────────────────────────────────────────────

    @mcp.tool(
        version=_VERSION,
        annotations={"title": "List Kubecost Windows", **_READ_ANNOTATIONS},
    )
    async def kubecost_list_windows() -> WindowOptionsResponse:
        """List the valid time windows for Kubecost cost queries.

        WHAT: Returns the named windows (7d, 30d, month, etc.) accepted by
        get_kubecost_workload_costs, formatted for user presentation.

        WHEN TO USE: When the user has not yet specified a time window before
        calling get_kubecost_workload_costs.

        WHEN NOT TO USE: When the user has already stated a window — call
        get_kubecost_workload_costs directly with their value.
        """
        return WindowOptionsResponse(
            status=QueryStatus.OK,
            message="Present these window options to the user and wait for a choice.",
            recommended_action="After the user picks a window, call get_kubecost_workload_costs.",
            windows=[WindowOption(value=k, label=v) for k, v in _WINDOW_CHOICES.items()],
            note=_WINDOW_RFC3339_NOTE,
        )

    @mcp.tool(
        version=_VERSION,
        annotations={"title": "Get Kubecost Workload Costs", **_READ_ANNOTATIONS},
    )
    async def get_kubecost_workload_costs(
        window: Annotated[
            str | None,
            Field(
                description=(
                    "User-confirmed time window, examples (not limited to) '7d', '15d', '30d', 'month', or an RFC3339 "
                    "range '2026-05-01T00:00:00Z,2026-06-01T00:00:00Z'. "
                    "If omitted, the tool returns an error directing you to call "
                    "kubecost_list_windows first."
                )
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
        ] = 500,
    ) -> KubecostAllocationResponse:
        """Return Kubernetes cost allocation from Kubecost grouped by chosen dimensions.

        WHAT: Costs aggregated by cluster, namespace, pod, label, or any combination.
        Results are returned as structured rows directly in the response — no separate
        resource read required.

        WHEN TO USE: For 'spend by cluster/namespace/pod/label' questions. Confirm the
        time window with the user first (see kubecost_list_windows).

        WHEN NOT TO USE: For container rightsizing/savings use
        get_container_savings_recommendations; for non-Kubernetes cloud cost use
        run_cost_report.
        """
        if not window:
            return KubecostAllocationResponse(
                status=QueryStatus.ERROR,
                message="A time window is required before querying Kubecost allocation.",
                recommended_action=(
                    "Call kubecost_list_windows, present the options to the user, then retry with the chosen window."
                ),
                window=None,
                aggregate=aggregate,
            )

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
                aggregate=aggregate,
            )

        dimension_cols, rows = _parse_allocation_response(response)
        if not rows:
            return KubecostAllocationResponse(
                status=QueryStatus.EMPTY,
                message=f"No Kubecost allocation data for window '{window}'.",
                recommended_action="Try a wider window or a different aggregate dimension.",
                window=window,
                aggregate=aggregate,
                dimensions=dimension_cols,
            )

        aggregated = _aggregate_by_dimensions(rows, dimension_cols)
        total = sum(float(r.get("totalCost", 0) or 0) for r in aggregated)
        truncated = len(aggregated) > top_n

        return KubecostAllocationResponse(
            status=QueryStatus.OK,
            message=(
                f"Kubecost allocation by {', '.join(dimension_cols) or aggregate} "
                f"for window '{window}': ${total:,.2f} across {len(aggregated)} rows"
                + (f" (showing top {top_n})." if truncated else ".")
            ),
            recommended_action=(
                "Increase top_n or narrow the aggregate/window to retrieve all rows." if truncated else None
            ),
            window=window,
            aggregate=aggregate,
            dimensions=dimension_cols,
            total_cost=round(total, 2),
            row_count=len(aggregated),
            rows=[AllocationRow.model_validate(r) for r in aggregated[:top_n]],
            truncated=truncated,
        )

    @mcp.tool(
        version=_VERSION,
        annotations={
            "title": "Get Container Savings Recommendations",
            **_READ_ANNOTATIONS,
        },
    )
    async def get_container_savings_recommendations(
        preset: Annotated[
            PresetName | None,
            Field(description=FIELD_DESCRIPTIONS["preset"]),
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
        ] = 500,
        include_undersized: Annotated[
            bool | None,
            Field(
                description=(
                    "Include containers where rightsizing would INCREASE cost "
                    "(under-provisioned, negative savings). Default False."
                ),
            ),
        ] = None,
        min_monthly_savings: Annotated[
            float | None,
            Field(
                description=(
                    "Minimum monthly savings (USD) to include. Recommendations below this "
                    "threshold are excluded as trivial. Default 1.00; set 0.0 to include all."
                ),
                ge=0.0,
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
        no separate resource read required. Supports named presets (conservative,
        balanced, aggressive) that bundle recommended quantile/window settings;
        explicit parameters override preset values.

        WHEN TO USE: For Kubernetes container savings, over-provisioned pods/namespaces,
        or rightsizing recommendations. If the user asks HOW to rightsize (methodology,
        quantiles, CPU vs memory strategy), invoke the container_rightsizing_guide
        prompt first.

        WHEN NOT TO USE: For non-Kubernetes resources use get_rightsizing_recommendations;
        for raw Kubernetes spend use get_kubecost_workload_costs.
        """
        sizing = resolve_sizing_params(
            preset,
            window=window,
            algorithm_cpu=algorithm_cpu,
            algorithm_ram=algorithm_ram,
            q_cpu=q_cpu,
            q_ram=q_ram,
            target_cpu_utilization=target_cpu_utilization,
            target_ram_utilization=target_ram_utilization,
            include_undersized=include_undersized,
            min_monthly_savings=min_monthly_savings,
        )
        resolved_window: str = sizing["window"]
        resolved_algorithm_cpu: str = sizing["algorithm_cpu"]
        resolved_algorithm_ram: str = sizing["algorithm_ram"]
        resolved_q_cpu: float = sizing["q_cpu"]
        resolved_q_ram: float = sizing["q_ram"]
        resolved_target_cpu: float = sizing["target_cpu_utilization"]
        resolved_target_ram: float = sizing["target_ram_utilization"]
        resolved_include_undersized: bool = sizing["include_undersized"]
        resolved_min_monthly_savings: float = sizing["min_monthly_savings"]

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
                message="No container savings recommendations returned.",
                recommended_action="Try a wider window or a different filter.",
                window=resolved_window,
                total_monthly_savings=0.0,
                container_count=0,
                summary_aggregate=summary_aggregate,
            )

        rows = all_rows
        if not resolved_include_undersized:
            rows = [r for r in rows if float(r.get("monthlySavings_total", 0) or 0) > 0]
        if resolved_min_monthly_savings > 0:
            rows = [r for r in rows if float(r.get("monthlySavings_total", 0) or 0) >= resolved_min_monthly_savings]

        if not rows:
            undersized_label = "included" if resolved_include_undersized else "excluded"
            return ContainerSavingsResponse(
                status=QueryStatus.EMPTY,
                message=(
                    f"No recommendations matched the filters "
                    f"(undersized containers {undersized_label}, "
                    f"minimum monthly savings ${resolved_min_monthly_savings:,.2f})."
                ),
                recommended_action="Lower min_monthly_savings or set include_undersized=true.",
                window=resolved_window,
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
        # excluded by the undersized / min-savings filters.
        filtered_note = (
            f" (filtered from {count} recommendations returned by the API)" if filtered_count != count else ""
        )

        interpretation = build_result_interpretation(sizing, all_rows, filtered_rows=rows)

        return ContainerSavingsResponse(
            status=QueryStatus.OK,
            message=(
                f"Total monthly savings ${filtered_total_savings:,.2f} across "
                f"{filtered_count} containers{filtered_note}, "
                f"summarized by {summary_aggregate}.{caveat}"
            ),
            recommended_action=("Increase top_n to retrieve more per-container rows." if truncated else None),
            window=resolved_window,
            total_monthly_savings=round(float(filtered_total_savings), 2),
            container_count=filtered_count,
            summary_aggregate=summary_aggregate,
            summary=summary_rows,
            rows=typed_rows[:top_n],
            truncated=truncated,
            parameters={
                "preset": preset or "balanced (default)",
                "window": resolved_window,
                "algorithm_cpu": resolved_algorithm_cpu,
                "algorithm_ram": resolved_algorithm_ram,
                "q_cpu": resolved_q_cpu,
                "q_ram": resolved_q_ram,
                "target_cpu_utilization": resolved_target_cpu,
                "target_ram_utilization": resolved_target_ram,
                "include_undersized": resolved_include_undersized,
                "min_monthly_savings": resolved_min_monthly_savings,
                "filter": filter_str or "(none)",
            },
            interpretation=interpretation,
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

    @mcp.resource("kubecost://schema/sizing-presets")
    def sizing_presets_schema() -> str:
        """Named sizing presets for get_container_savings_recommendations."""
        return format_presets_resource()

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
        preset_menu = "\n".join(
            f"  - **{name}** — {desc}"
            for name, desc in [
                ("conservative", "Minimize OOM risk; includes undersized containers"),
                ("balanced", "Default — good starting point for most clusters"),
                ("aggressive", "Maximize savings; skips trivial recommendations"),
                ("custom", "Specify your own quantiles and filters"),
            ]
        )
        return f"""\
Let's find container rightsizing opportunities. I'll walk you through a few choices.

---

**Step 1 — Sizing preset**
How aggressive should the recommendations be?

{preset_menu}

Pick a preset or describe your preferences.

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
        """Explain the filter options (undersized containers, trivial savings) for container savings."""
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
        "window": window,
        "offset": 0,
        "limit": limit,
    }
    path = get_settings().kubecost_container_savings_path
    logger.debug("Kubecost request sizing: path=%s window=%s", path, window)
    return await call_get_api(path, params=params)


async def _fetch_allocation(
    aggregate: str,
    window: str,
    accumulate: bool,
    limit: int,
) -> dict[str, Any]:
    """Fetch allocation data via the shared API wrapper."""
    params: dict[str, Any] = {
        "window": window,
        "aggregate": aggregate,
        "accumulate": str(accumulate).lower(),
        "idle": "true",
        "splitIdle": "true",
        "shareIdle": "true",
        "sortBy": "totalCost",
        "sortByOrder": "desc",
        "limit": limit,
    }
    path = get_settings().kubecost_base_path
    logger.debug("Kubecost allocation: path=%s window=%s", path, window)
    return await call_get_api(path, params=params)
