#!/usr/bin/env bash
set -euo pipefail

# Runs get_kubecost_cost_comparison twice:
#   1. Yesterday vs the day before yesterday (1-day windows)
#   2. The last 7 days (ending at UTC midnight this morning) vs the same
#      7-day range exactly one calendar month earlier
#
# Usage:
#   scripts/cost_comparison.sh [mcp_config] [aggregate]
#
# Examples:
#   scripts/cost_comparison.sh
#   scripts/cost_comparison.sh ./.bob/mcp.json namespace
#   scripts/cost_comparison.sh ./.bob/mcp.json cluster,namespace
#
# Notes:
#   - Windows are RFC3339 ranges computed in UTC so they satisfy the
#     get_kubecost_cost_comparison validation rules (no bare "Nd"/today/week/month,
#     equal-length windows, nothing reaching into today).
#   - Calendar-month arithmetic shifts the day-of-month back by one month; near
#     month-end boundaries most date implementations clamp/roll the day, which is
#     fine for a quick comparison but not guaranteed to be exactly 7 days apart
#     in every edge case.

MCP_CONFIG="${1:-./.bob/mcp.json}"
AGGREGATE="${2:-namespace}"

# --- Portable UTC date arithmetic (macOS/BSD date vs GNU date) --------------
if date -v-1d >/dev/null 2>&1; then
  # BSD date (macOS)
  _shift() { # _shift <days-back> <months-back>
    date -u -v-"$1"d -v-"$2"m +%Y-%m-%dT00:00:00Z
  }
else
  # GNU date (Linux)
  _shift() {
    date -u -d "-$1 days -$2 months" +%Y-%m-%dT00:00:00Z
  }
fi

TODAY=$(_shift 0 0)

WEEK_CURRENT_START=$(_shift 7 0)
WEEK_CURRENT_END="$TODAY"
WEEK_BASELINE_START=$(_shift 7 1)
WEEK_BASELINE_END=$(_shift 0 1)

echo
echo "== 2. Last 7 days vs same 7 days one month ago =="
echo "current:  ${WEEK_CURRENT_START},${WEEK_CURRENT_END}"
echo "baseline: ${WEEK_BASELINE_START},${WEEK_BASELINE_END}"
fastmcp call "$MCP_CONFIG" \
  get_kubecost_cost_comparison \
  --input-json "{\"current_window\": \"${WEEK_CURRENT_START},${WEEK_CURRENT_END}\", \"baseline_window\": \"${WEEK_BASELINE_START},${WEEK_BASELINE_END}\", \"aggregate\": \"${AGGREGATE}\"}"
