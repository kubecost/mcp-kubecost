"""Kubecost allocation tools — thin pass-through over the Kubecost client (Pattern A)."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import EmbeddedResource, TextContent
from pydantic import Field

from mcp_kubecost import utils
from mcp_kubecost.client import KubecostClientError, get
from mcp_kubecost.config.settings import get_settings
from mcp_kubecost.domain.kubecost import kubecost_csv
from mcp_kubecost.domain.kubecost.kubecost_csv import (
    COST_FIELDS,
    SAVINGS_AGGREGATE_OPTIONS,
    SAVINGS_FIELDS,
    SUMMARY_COST_FIELDS,
    aggregate_savings_by,
    parse_request_sizing_response,
)
from mcp_kubecost.domain.kubecost.sizing_guidance import (
    CONTAINER_SIZING_GUIDE,
    CONTAINER_SIZING_REFERENCE,
    FIELD_DESCRIPTIONS,
    PresetName,
    build_result_interpretation,
    format_presets_resource,
    resolve_sizing_params,
)

logger = logging.getLogger(__name__)


def _csv_to_structured_data(csv_str: str) -> dict[str, Any]:
    """Convert CSV string to structured dict with headers and rows for structured_content."""
    reader = csv.DictReader(io.StringIO(csv_str))
    rows = list(reader)
    if not rows:
        return {"headers": [], "rows": []}

    headers = list(rows[0].keys())
    return {
        "headers": headers,
        "rows": rows,
        "row_count": len(rows),
        "column_count": len(headers),
    }


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

# ── Window clarification helpers ───────────────────────────────────────────────

_WINDOW_MENU = "\n".join(f"  • **{k}** — {v}" for k, v in _WINDOW_CHOICES.items())
_WINDOW_MENU += "\n  • **RFC3339 range** — e.g. `2026-05-01T00:00:00Z,2026-06-01T00:00:00Z`"

_WINDOW_CLARIFICATION = f"""\
⚠️ TIME WINDOW REQUIRED — do not call get_kubecost_workload_costs yet.

Present the following options to the user and wait for their reply:

---
**Which time window would you like for this cost report?**

{_WINDOW_MENU}
---
"""

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
Present the following options to the user and wait for their reply:

---
**Which time window would you like for this container savings report?**

{_WINDOW_MENU}
1d, 3d, 7d, 15d, 30d Or enter a custom RFC3339 range.
---
"""

_PRESENTATION_RULES = """\
[PRESENTATION RULES — follow silently, do not echo back]
- Always lead with Executive Summary: summary_csv fields → render as a chart (bar for comparisons, line for time-series)
- Always precede charts with a 3 bullet insight summary
- CSV data is provided inline in structured_content for programmatic access
- Always present the "download_url" as a clickable link labeled 'Download CSV' for file access
- Do not open the csv: only provide the user with a clickable link with text "Download CSV"
"""


def register_kubecost_csv_tools(mcp: FastMCP) -> None:
    """Register kubecost cluster tool entry points."""

    # ── Gate tool: must be called before the main tool when no window is known ──

    @mcp.tool(
        description=(
            "Returns the list of valid time windows for Kubecost cost queries, "
            "formatted as a question to present to the user. "
            "⚠️ ALWAYS call this tool first when the user has not yet specified "
            "a time window in the current conversation. "
            "Do NOT call get_kubecost_workload_costs until the user has replied "
            "with their chosen window."
        )
    )
    async def kubecost_list_windows() -> list[TextContent]:
        """Return valid window options formatted for user presentation."""
        return [TextContent(type="text", text=_WINDOW_CLARIFICATION)]

    # ── Main allocation tool ───────────────────────────────────────────────────

    @mcp.tool(
        description="""
⚠️ PREREQUISITE: If the user has not explicitly stated a time window in this
conversation, call `kubecost_list_windows` FIRST and wait for their answer
before calling this tool. Do NOT infer, assume, or default the window value.

Returns Kubernetes cost allocation data from Kubecost, supporting any
aggregation level (cluster, namespace, pod, label, etc.).

The `aggregate` parameter controls grouping — pass a single dimension
(e.g. "cluster") or a comma-separated list (e.g. "cluster,namespace").
The response columns adapt automatically to whatever dimensions are present.

The `window` parameter MUST be explicitly confirmed by the user. Valid options:
- "7d" — Last 7 days
- "30d" — Last 30 days
- "90d" — Last 90 days
- "month" — This calendar month
- "lastweek" — Last calendar week
- "lastmonth" — Last calendar month
- RFC3339 range — e.g. "2026-05-01T00:00:00Z,2026-06-01T00:00:00Z"

Set `accumulate=True` (default) for a single total across the entire date range.
Set `accumulate=False` for a daily breakdown (use only for trend/time-series).

WHEN TO USE: Use when the user asks about spend by cluster,
namespace, pod, label, or any combination thereof.
        """
    )
    async def get_kubecost_workload_costs(
        window: str | None = None,
        aggregate: str = "cluster,namespace",
        accumulate: bool = True,
        limit: int = 100000,
        top_n: int = 15,
    ) -> ToolResult:
        """
        Fetch and return kubecost cluster/namespace/pod costs for selected dimensions.
        Args:
            window:      User-confirmed time window string, e.g. "7d", "30d",
                         "2026-05-01T00:00:00Z,2026-06-01T00:00:00Z".
                         Pass None (or omit) if the user has not yet chosen — the
                         tool will return a clarification prompt instead of querying.
            aggregate:   Comma-separated aggregation dimensions, e.g. "cluster",
                         "cluster,namespace", "namespace,pod".
            accumulate:  True (default) returns a single total for the window.
                         False returns daily breakdown — use only for trend analysis.
            limit:       Maximum number of allocation entries to return from the API.
            top_n:       Number of top entries to include in the inline summary.
        """
        # ── Runtime guard: bounce unconfirmed calls back as a clarification ───
        if not window:
            return ToolResult(
                content=[TextContent(type="text", text=_WINDOW_CLARIFICATION)],
                is_error=True,
            )

        try:
            response = await _fetch_allocation(aggregate=aggregate, window=window, accumulate=accumulate, limit=limit)
        except KubecostClientError as exc:
            tool_error = exc.to_tool_error()
            return ToolResult(
                content=[TextContent(type="text", text=tool_error.model_dump_json(indent=2))],
                is_error=True,
            )

        dimension_cols, rows = kubecost_csv._parse_allocation_response(response)

        if not rows:
            return ToolResult(
                content=[TextContent(type="text", text="No allocation data returned.")],
                structured_content={
                    "total_cost": 0.0,
                    "row_count": 0,
                    "dimensions": dimension_cols,
                },
            )

        full_fields = dimension_cols + ["window_start"] + COST_FIELDS
        summary_fields = dimension_cols + SUMMARY_COST_FIELDS

        full_csv = kubecost_csv._build_csv(rows, full_fields)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"allocation-{aggregate.replace(',', '-')}-{timestamp}.csv"
        csv_path = utils.write_csv(filename, full_csv)
        url = utils.report_url(filename)

        aggregated = kubecost_csv._aggregate_by_dimensions(rows, dimension_cols)
        summary_csv = kubecost_csv._build_csv(aggregated[:top_n], summary_fields)
        total = sum(float(r.get("totalCost", 0)) for r in rows)

        dims_label = ", ".join(dimension_cols) if dimension_cols else aggregate
        download_url = url or str(csv_path)

        # Convert full CSV to structured data for programmatic access
        full_structured = _csv_to_structured_data(full_csv)
        summary_structured = _csv_to_structured_data(summary_csv)

        text = f"""\
{_PRESENTATION_RULES}

[Results]
## Kubecost Allocation — aggregated by: {dims_label} | window: {window}

Total: ${total:,.2f} across {len(rows)} entries
Download full CSV: [{filename}]({download_url})

### Top {min(top_n, len(rows))} by totalCost (summary CSV):
{summary_csv}"""

        return ToolResult(
            content=[
                TextContent(type="text", text=text),
                EmbeddedResource(
                    type="resource",
                    resource={
                        "uri": f"data:text/csv;name={filename}",
                        "mimeType": "text/csv",
                        "text": full_csv,
                    },
                ),
            ],
            structured_content={
                "query": {
                    "window": window,
                    "aggregate": aggregate,
                    "accumulate": accumulate,
                    "dimensions": dimension_cols,
                },
                "summary": {
                    "total_cost": total,
                    "total_rows": len(rows),
                    "top_n_shown": min(top_n, len(rows)),
                },
                "summary_data": summary_structured,
                "full_data": full_structured,
                "download_url": download_url,
                "filename": filename,
            },
            meta={
                "execution_timestamp": datetime.now().isoformat(),
                "aggregate_dimensions": dimension_cols,
                "row_count": len(rows),
            },
        )

    # ── Container Savings / Request Sizing ────────────────────────────────────

    @mcp.tool(
        description="""
Returns container rightsizing recommendations from Kubecost, showing which
workloads are over-provisioned and how much can be saved by right-sizing them.

If the user asks HOW to rightsize properly (methodology, quantiles, CPU vs memory),
call the `container_rightsizing_guide` prompt FIRST — do not guess.

USE THIS TOOL when the user asks about:
- Kubernetes or container savings opportunities
- Over-provisioned pods, namespaces, or clusters
- Specific savings recommendations for namespaces, pods, or containers

DO NOT USE THIS TOOL when the user isn't asking about Kubecost, Kubernetes or containers

Named presets (preset param): conservative, balanced (default), aggressive.
See resource `kubecost://schema/sizing-presets` for details. Explicit params override preset.

The response includes:
- TotalMonthlySavings and Count across all recommendations
- Executive summary: top entries aggregated by containerName, namespace or cluster
- Full CSV download link with all individual container-level recommendations
- "How to read these results" interpretation block

Summary aggregation options (summary_aggregate param):
- "containerName" (default) — combines savings across all instances of the same container name.
  Note: containers sharing a name may be different workloads in different clusters/namespaces.
  The raw per-container data is always available in the download CSV.
- "namespace" — total savings per namespace
- "clusterID" — total savings per cluster
        """
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
                description="Number of top entries to include in the executive summary. Default: 15.",
                ge=1,
            ),
        ] = 15,
        include_undersized: Annotated[
            bool | None,
            Field(
                description=(
                    "Include containers where rightsizing would INCREASE cost (negative savings). "
                    "These are under-provisioned containers. Default: False (excluded)."
                )
            ),
        ] = None,
        min_monthly_savings: Annotated[
            float | None,
            Field(
                description=(
                    "Minimum monthly savings threshold in USD. Recommendations below this value "
                    "are excluded as trivial. Default: $1.00. Set to 0.0 to include all."
                ),
                ge=0.0,
            ),
        ] = None,
        summary_aggregate: Annotated[
            str,
            Field(
                description=(
                    "Dimension to group the executive summary by. "
                    "One of: 'containerName' (default), 'namespace', 'clusterID'. "
                    "'containerName' combines same-named containers across all clusters/namespaces "
                    "— raw per-container data is always in the download CSV."
                )
            ),
        ] = "containerName",
    ) -> ToolResult:
        """Fetch container rightsizing recommendations from Kubecost requestSizingV2."""

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
        resolved_window = sizing["window"]
        resolved_algorithm_cpu = sizing["algorithm_cpu"]
        resolved_algorithm_ram = sizing["algorithm_ram"]
        resolved_q_cpu = sizing["q_cpu"]
        resolved_q_ram = sizing["q_ram"]
        resolved_target_cpu = sizing["target_cpu_utilization"]
        resolved_target_ram = sizing["target_ram_utilization"]
        resolved_include_undersized = sizing["include_undersized"]
        resolved_min_monthly_savings = sizing["min_monthly_savings"]

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
                limit=1000,
            )
        except KubecostClientError as exc:
            tool_error = exc.to_tool_error()
            return ToolResult(
                content=[TextContent(type="text", text=tool_error.model_dump_json(indent=2))],
                is_error=True,
            )

        total_savings, count, all_rows = parse_request_sizing_response(response)

        if not all_rows:
            return ToolResult(
                content=[TextContent(type="text", text="No savings recommendations returned.")],
                structured_content={
                    "total_monthly_savings": 0.0,
                    "container_count": 0,
                    "filters_applied": {
                        "include_undersized": resolved_include_undersized,
                        "min_monthly_savings": resolved_min_monthly_savings,
                    },
                },
            )

        rows = list(all_rows)
        if not resolved_include_undersized:
            rows = [r for r in rows if float(r.get("monthlySavings_total", 0) or 0) > 0]
        if resolved_min_monthly_savings > 0:
            rows = [r for r in rows if float(r.get("monthlySavings_total", 0) or 0) >= resolved_min_monthly_savings]

        if not rows:
            undersized_label = "included" if resolved_include_undersized else "excluded"
            msg = (
                f"No savings recommendations matched the selected filters "
                f"(undersized containers: {undersized_label}, "
                f"minimum monthly savings: ${resolved_min_monthly_savings:,.2f})."
            )
            return ToolResult(
                content=[TextContent(type="text", text=msg)],
                structured_content={
                    "total_monthly_savings": 0.0,
                    "container_count": len(rows),
                    "filters_applied": {
                        "include_undersized": resolved_include_undersized,
                        "min_monthly_savings": resolved_min_monthly_savings,
                    },
                },
            )

        # Full CSV
        full_csv = kubecost_csv._build_csv(rows, SAVINGS_FIELDS)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"container-savings-{timestamp}.csv"
        csv_path = utils.write_csv(filename, full_csv)
        url = utils.report_url(filename)
        download_url = url or str(csv_path)

        # Summary CSV — aggregate by chosen dimension, take top N
        if summary_aggregate not in SAVINGS_AGGREGATE_OPTIONS:
            summary_aggregate = "containerName"
        aggregated_summary = aggregate_savings_by(rows, summary_aggregate)
        summary_fields = [
            summary_aggregate,
            "monthlySavings_total",
            "container_count",
            "notes",
        ]
        summary_csv = kubecost_csv._build_csv(aggregated_summary[:top_n], summary_fields)

        # Convert CSVs to structured data
        full_structured = _csv_to_structured_data(full_csv)
        summary_structured = _csv_to_structured_data(summary_csv)

        agg_caveat = (
            "\nSummary groups by containerName — same-named containers across different "
            "clusters/namespaces are combined. See the download CSV for per-container detail."
            if summary_aggregate == "containerName"
            else ""
        )

        preset_line = f"- Preset: `{preset or 'balanced (default)'}`\n" if preset else ""
        interpretation = build_result_interpretation(sizing, all_rows, filtered_rows=rows)

        text = f"""\
{_PRESENTATION_RULES}

[Results]
## Kubecost Container Savings Recommendations | window: {resolved_window}

**TotalMonthlySavings: ${total_savings:,.2f}** across {count} containers{agg_caveat}

Download full CSV: [{filename}]({download_url})

### Top {min(top_n, len(aggregated_summary))} by monthlySavings_total — grouped by {summary_aggregate} (summary CSV):
{summary_csv}
---
**Parameters used:**
{preset_line}- Window: `{resolved_window}`
- Summary grouped by: `{summary_aggregate}`
- CPU algorithm: `{resolved_algorithm_cpu}`
    (quantile: {resolved_q_cpu}) → target utilization: {int(resolved_target_cpu * 100)}%
- RAM algorithm: `{resolved_algorithm_ram}`
    (quantile: {resolved_q_ram}) → target utilization: {int(resolved_target_ram * 100)}%
- Filters applied: undersized containers {"included" if resolved_include_undersized else "excluded"},
    minimum monthly savings: ${resolved_min_monthly_savings:,.2f}
- Filter expression: `{filter_str if filter_str else "(none)"}`
{interpretation}"""

        return ToolResult(
            content=[
                TextContent(type="text", text=text),
                EmbeddedResource(
                    type="resource",
                    resource={
                        "uri": f"data:text/csv;name={filename}",
                        "mimeType": "text/csv",
                        "text": full_csv,
                    },
                ),
            ],
            structured_content={
                "query": {
                    "window": resolved_window,
                    "preset": preset,
                    "summary_aggregate": summary_aggregate,
                    "algorithms": {
                        "cpu": resolved_algorithm_cpu,
                        "ram": resolved_algorithm_ram,
                        "q_cpu": resolved_q_cpu,
                        "q_ram": resolved_q_ram,
                        "target_cpu_utilization": resolved_target_cpu,
                        "target_ram_utilization": resolved_target_ram,
                    },
                    "filters": {
                        "include_undersized": resolved_include_undersized,
                        "min_monthly_savings": resolved_min_monthly_savings,
                        "filter_expression": filter_str,
                    },
                },
                "summary": {
                    "total_monthly_savings": total_savings,
                    "container_count": count,
                    "filtered_count": len(rows),
                    "top_n_shown": min(top_n, len(aggregated_summary)),
                },
                "summary_data": summary_structured,
                "full_data": full_structured,
                "download_url": download_url,
                "filename": filename,
            },
            meta={
                "execution_timestamp": datetime.now().isoformat(),
                "preset_used": preset or "balanced",
                "container_count": count,
                "filtered_count": len(rows),
            },
        )

    # ── Resources ─────────────────────────────────────────────────────────────

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
sharedCost       — shared namespace overhead allocation
totalCost        — sum of all cost components
totalIdleCost    — sum of idle cost components (cpu + ram + gpu idle)
totalEfficiency  — utilization ratio 0 to 1 (request vs actual use)
"""

    @mcp.resource("kubecost://schema/sizing-presets")
    def sizing_presets_schema() -> str:
        """Named sizing presets for get_container_savings_recommendations."""
        return format_presets_resource()

    @mcp.resource("kubecost://guides/container-sizing")
    def container_sizing_guide() -> str:
        """Full container request sizing reference for CPU and memory reservations."""
        return CONTAINER_SIZING_REFERENCE

    # ── Prompts ────────────────────────────────────────────────────────────────

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
Present the Executive Summary with a chart, the interpretation block, and a CSV download link.
"""

    @mcp.prompt()
    def container_savings_window_help() -> str:
        """Explain the time window options for the container savings tool."""
        return _CONTAINER_SAVINGS_WINDOW_CLARIFICATION

    @mcp.prompt()
    def container_savings_filter_help() -> str:
        """Explain the filter options (undersized containers, trivial savings)
        for the container savings tool."""
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
present an Executive Summary with a chart and a CSV download link.
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
3. A 'Download CSV' link for the full report.
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
3. A 'Download CSV' link for the full data.
"""


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
    """Fetch request sizing recommendations via the Kubecost client."""
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
    kubecost_container_savings_path = get_settings().kubecost_container_savings_path
    logger.debug(
        "Kubecost request sizing request: path=%s, params=%s",
        kubecost_container_savings_path,
        params,
    )
    return await get(path=kubecost_container_savings_path, params=params)


async def _fetch_allocation(aggregate: str, window: str, accumulate: bool, limit: int) -> dict[str, Any]:
    """Fetch allocation data via the Kubecost client."""
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
    allocation_path = get_settings().kubecost_base_path
    logger.debug("Kubecost allocation request: path=%s, params=%s", allocation_path, params)
    return await get(allocation_path, params=params)
