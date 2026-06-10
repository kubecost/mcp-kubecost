"""Optimization skill — rightsizing and Reserved Instance workflows."""

from fastmcp import FastMCP

SKILL_CONTENT = """\
# Optimization

## When to Use
Use this skill when investigating savings opportunities, rightsizing resources, \
planning Reserved Instance purchases, or reviewing RI utilization.

## Available Tools

### Container request sizing (Kubecost)
- `get_container_savings_recommendations` — Quantile-based container CPU/RAM rightsizing from Kubecost requestSizingV2.
   Supports named presets (conservative, balanced, aggressive).
- `container_rightsizing_guide` prompt — Methodology for CPU vs memory sizing (call when user asks HOW to rightsize).
- `explore_container_savings` prompt — Guided workflow for container savings exploration.
- Resource `kubecost://guides/container-sizing` — Full sizing reference.
- Resource `kubecost://schema/sizing-presets` — Preset parameter bundles.

## Common Workflows

### Kubernetes/Container rightsizing investigation
1. If user asks about methodology, invoke `container_rightsizing_guide` prompt first
2. `get_container_savings_recommendations` with `preset="balanced"` for a first pass
3. Check interpretation block for undersized memory (negative memory savings) — never downsize those
4. Re-run with `preset="conservative"` for production-critical workloads
5. Use `include_undersized=True` to surface containers that need MORE resources
"""


def register_optimization_skill(mcp: FastMCP) -> None:
    """Register the optimization skill as an MCP prompt."""

    @mcp.prompt()
    def optimization() -> str:
        """Guidance for rightsizing resources and planning Reserved Instance purchases.

        Use this when investigating savings opportunities, checking RI utilization,
        or planning commitment purchases. Always check rightsizing before RI planning.
        """
        return SKILL_CONTENT
