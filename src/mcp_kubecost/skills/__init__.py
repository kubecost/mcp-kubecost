"""MCP Skills (prompts) for the mcp-kubecost server.

Skills are exposed as MCP prompts — IDE-agnostic guidance that any MCP client
can discover and use to navigate the server's tools effectively.
"""

from mcp_kubecost.skills.container_cost_allocation import register_container_cost_allocation_skill
from mcp_kubecost.skills.optimization import register_optimization_skill


def register_all_skills(mcp) -> None:
    """Register all skills (prompts) with the MCP server."""
    register_optimization_skill(mcp)
    register_container_cost_allocation_skill(mcp)
