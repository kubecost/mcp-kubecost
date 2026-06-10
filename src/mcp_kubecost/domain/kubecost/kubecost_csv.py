import csv
import io
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Cost fields to extract from each allocation entry (in display order)
COST_FIELDS = [
    "cpuCost",
    "cpuCostIdle",
    "ramCost",
    "ramCostIdle",
    "networkCost",
    "pvCost",
    "gpuCost",
    "gpuCostIdleloadBalancerCost",
    "sharedCost",
    "totalCost",
    "totalEfficiency",
]

# Compact summary uses only these cost fields (aggregated, no efficiency)
SUMMARY_COST_FIELDS = [
    "totalCost",
    "totalIdlePct",
    "cpuCost",
    "cpuIdlePct",
    "ramCost",
    "ramIdlePct",
    "gpuCost",
    "gpuIdlePct",
    "networkCost",
    "pvCost",
]


def _format_number(value: float) -> str | float:
    """Format number: 2 decimals if fractional, no decimal if whole."""
    if value == int(value):
        return int(value)
    return round(value, 2)


def _format_date(iso_string: str) -> str:
    """Convert ISO datetime string to YYYY-MM-DD format."""
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
    """
    Parse a Kubecost allocation API response into (dimension_columns, rows).

    Works for any aggregation level (cluster, cluster+namespace,
    cluster+namespace+pod, etc.) by inspecting the `properties` of the
    first entry to discover dimension keys.

    Returns:
        dimension_columns: ordered list of dimension column names (e.g. ["cluster", "namespace"])
        rows: list of flat dicts ready for csv.DictWriter
    """
    data_list: list[dict] = response.get("data", [])
    if not data_list:
        return [], []

    # Merge all time-window buckets into one flat list
    all_entries: list[dict] = []
    for bucket in data_list:
        all_entries.extend(bucket.values())

    if not all_entries:
        return [], []

    # Discover dimension columns from the first entry's `properties`
    # Standard Kubecost property keys that represent aggregation dimensions
    KNOWN_DIMENSIONS = [
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
    first_props: dict = all_entries[0].get("properties", {})
    dimension_cols = [k for k in KNOWN_DIMENSIONS if k in first_props]

    # Fallback: if no known dimensions found, split the `name` field by "/"
    if not dimension_cols:
        sample_name: str = all_entries[0].get("name", "")
        parts = sample_name.split("/")
        dimension_cols = [f"dim_{i}" for i in range(len(parts))]

    rows: list[dict] = []
    for entry in all_entries:
        row: dict = {}

        props = entry.get("properties", {})
        if dimension_cols and not dimension_cols[0].startswith("dim_"):
            for col in dimension_cols:
                val = props.get(col, "")
                # Some properties are lists (e.g. services); join them
                if isinstance(val, list):
                    val = "|".join(val)
                row[col] = val
        else:
            # Fallback: split name
            parts = entry.get("name", "").split("/")
            for i, col in enumerate(dimension_cols):
                row[col] = parts[i] if i < len(parts) else ""

        # Window
        window = entry.get("window", {})
        row["window_start"] = _format_date(window.get("start", ""))

        # Cost fields - format numbers
        for field in COST_FIELDS:
            value = entry.get(field, 0.0)
            row[field] = _format_number(float(value))

        rows.append(row)

    return dimension_cols, rows


def _aggregate_by_dimensions(rows: list[dict], dimension_cols: list[str]) -> list[dict]:
    """
    Aggregate rows by dimension columns, summing all cost fields.
    Returns aggregated rows sorted by totalCost (highest first).
    """
    from collections import defaultdict

    # Group by dimension values (using Any to allow both float and str values)
    groups: dict[tuple, dict[str, Any]] = defaultdict(lambda: defaultdict(float))

    for row in rows:
        # Create key from dimension values
        key = tuple(row.get(dim, "") for dim in dimension_cols)

        # Sum all numeric cost fields (including idle costs for percentage calculation)
        for field in COST_FIELDS:
            if field != "totalEfficiency":
                value = row.get(field, 0)
                if isinstance(value, (int, float)):
                    groups[key][field] += float(value)

        # Store dimension values
        for dim in dimension_cols:
            groups[key][dim] = row.get(dim, "")

    # Convert to list of dicts and calculate idle percentages
    aggregated = []
    for values in groups.values():
        row = dict(values)

        # Calculate idle percentages for each category
        cpu_total = row.get("cpuCost", 0)
        ram_total = row.get("ramCost", 0)
        gpu_total = row.get("gpuCost", 0)

        # CPU idle %
        if cpu_total > 0:
            row["cpuIdlePct"] = f"{(row.get('cpuCostIdle', 0) / cpu_total * 100):.1f}%"
        else:
            row["cpuIdlePct"] = "0%"

        # RAM idle %
        if ram_total > 0:
            row["ramIdlePct"] = f"{(row.get('ramCostIdle', 0) / ram_total * 100):.1f}%"
        else:
            row["ramIdlePct"] = "0%"

        # GPU idle %
        if gpu_total > 0:
            row["gpuIdlePct"] = f"{(row.get('gpuCostIdle', 0) / gpu_total * 100):.1f}%"
        else:
            row["gpuIdlePct"] = "0%"

        # Total idle % (weighted average across all resources)
        total_cost = row.get("totalCost", 0)
        if total_cost > 0:
            total_idle = row.get("cpuCostIdle", 0) + row.get("ramCostIdle", 0) + row.get("gpuCostIdle", 0)
            row["totalIdlePct"] = f"{(total_idle / total_cost * 100):.1f}%"
        else:
            row["totalIdlePct"] = "0%"

        aggregated.append(row)

    # Sort by totalCost descending
    aggregated.sort(key=lambda r: float(r.get("totalCost", 0)), reverse=True)

    # Format numbers (excluding idle percentages which are already formatted)
    for row in aggregated:
        for field in COST_FIELDS:
            if field in row and field != "totalEfficiency":
                row[field] = _format_number(row[field])

    return aggregated


#: Valid aggregation keys for the savings summary
SAVINGS_AGGREGATE_OPTIONS = ("containerName", "namespace", "clusterID")


def aggregate_savings_by(rows: list[dict], group_key: str) -> list[dict]:
    """
    Aggregate savings rows by a single dimension key, summing monthlySavings_total
    and counting the number of individual containers in each group.

    Returns rows sorted by monthlySavings_total descending.
    Each output row has: {group_key, monthlySavings_total, container_count}.

    Note: containers with the same name may represent different workloads (like a container named "agent")
    — the caller should surface this caveat to the user.
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

    aggregated = []
    for values in groups.values():
        values["monthlySavings_total"] = _format_number(values["monthlySavings_total"])
        values["notes"] = ";".join(sorted(values.pop("notes_set")))
        aggregated.append(dict(values))

    aggregated.sort(key=lambda r: float(r.get("monthlySavings_total", 0) or 0), reverse=True)
    return aggregated


def _build_csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ── Request Sizing / Container Savings ────────────────────────────────────────

# Metadata columns for each recommendation row
SAVINGS_METADATA_FIELDS = [
    "clusterID",
    "namespace",
    "controllerKind",
    "controllerName",
    "containerName",
]

# All flattened data columns (metadata + nested objects)
SAVINGS_FIELDS = SAVINGS_METADATA_FIELDS + [
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
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_savings_notes(row: dict) -> str:
    """Build semicolon-separated notes for a savings recommendation row."""
    notes: list[str] = []
    if _float_field(row, "Recommended_memory") < _float_field(row, "MaxUsage_memory"):
        notes.append(NOTE_MEM_RECOMMENDATION_LESS_THAN_MAX)
    return ";".join(notes)


# Compact summary columns for top-N inline display
SAVINGS_SUMMARY_FIELDS = [
    "clusterID",
    "namespace",
    "controllerName",
    "containerName",
    "monthlySavings_total",
    "currentEfficiency_total",
]


def parse_request_sizing_response(
    response: dict,
) -> tuple[float, int, list[dict]]:
    """
    Parse a Kubecost requestSizingV2 API response into flat rows.

    Returns:
        total_monthly_savings: top-level TotalMonthlySavings value
        count: top-level Count value
        rows: list of flat dicts (SAVINGS_FIELDS columns), sorted by
              monthlySavings_total descending
    """
    total_monthly_savings: float = float(response.get("TotalMonthlySavings", 0.0))
    count: int = int(response.get("Count", 0))
    recommendations: list[dict] = response.get("Recommendations", [])

    rows: list[dict] = []
    for rec in recommendations:
        row: dict = {}

        # Metadata
        for field in SAVINGS_METADATA_FIELDS:
            row[field] = rec.get(field, "")

        # Flatten nested objects - inline to avoid closure issues
        # monthlySavings
        ms_obj = rec.get("monthlySavings", {}) or {}
        row["monthlySavings_cpu"] = _format_number(float(ms_obj.get("cpu", 0.0)))
        row["monthlySavings_memory"] = _format_number(float(ms_obj.get("memory", 0.0)))
        row["monthlySavings_total"] = _format_number(float(ms_obj.get("total", 0.0)))

        # normalizedRecommendedRequest
        rec_req = rec.get("normalizedRecommendedRequest", {}) or {}
        row["Recommended_cpu"] = _format_number(float(rec_req.get("cpuInMilliCores", 0.0)))
        row["Recommended_memory"] = _format_number(float(rec_req.get("memoryInMiB", 0.0)))

        # normalizedLatestKnownRequest
        curr_req = rec.get("normalizedLatestKnownRequest", {}) or {}
        row["current_cpu"] = _format_number(float(curr_req.get("cpuInMilliCores", 0.0)))
        row["current_memory"] = _format_number(float(curr_req.get("memoryInMiB", 0.0)))

        # currentEfficiency
        eff = rec.get("currentEfficiency", {}) or {}
        row["currentEfficiency_cpu"] = _format_number(float(eff.get("cpu", 0.0)))
        row["currentEfficiency_memory"] = _format_number(float(eff.get("memory", 0.0)))
        row["currentEfficiency"] = _format_number(float(eff.get("total", 0.0)))

        # normalizedAverageUsage
        avg_usage = rec.get("normalizedAverageUsage", {}) or {}
        row["AvgUsage_cpu"] = _format_number(float(avg_usage.get("cpuInMilliCores", 0.0)))
        row["AvgUsage_memory"] = _format_number(float(avg_usage.get("memoryInMiB", 0.0)))

        # normalizedMaxUsage
        max_usage = rec.get("normalizedMaxUsage", {}) or {}
        row["MaxUsage_cpu"] = _format_number(float(max_usage.get("cpuInMilliCores", 0.0)))
        row["MaxUsage_memory"] = _format_number(float(max_usage.get("memoryInMiB", 0.0)))

        row["notes"] = compute_savings_notes(row)
        rows.append(row)

    # Sort by monthlySavings_total descending
    rows.sort(
        key=lambda r: float(r.get("monthlySavings_total", 0) or 0),
        reverse=True,
    )

    return total_monthly_savings, count, rows
