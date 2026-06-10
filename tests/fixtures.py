"""Test fixtures and constants for MCP contract tests."""

from __future__ import annotations

EXPECTED_TOOLS = frozenset(
    {
        "kubecost_list_windows",
        "get_kubecost_workload_costs",
        "get_container_savings_recommendations",
    }
)

EXPECTED_PROMPTS = frozenset(
    {
        "container_rightsizing_guide",
        "container_savings_filter_help",
        "container_savings_window_help",
        "cost_trend",
        "explore_container_savings",
        "explore_costs",
        "kubecost_cost_allocation",
        "optimization",
        "top_spenders",
    }
)

EXPECTED_RESOURCES = frozenset(
    {
        "kubecost://guides/container-sizing",
        "kubecost://schema/allocation-params",
        "kubecost://schema/cost-fields",
        "kubecost://schema/sizing-presets",
    }
)

SAMPLE_ALLOCATION_RESPONSE = {
    "data": [
        {
            "entry1": {
                "name": "cluster1/namespace1",
                "properties": {"cluster": "cluster1", "namespace": "namespace1"},
                "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-08T00:00:00Z"},
                "cpuCost": 10.0,
                "cpuCostIdle": 2.0,
                "ramCost": 5.0,
                "ramCostIdle": 1.0,
                "networkCost": 0.5,
                "pvCost": 0.2,
                "gpuCost": 0.0,
                "gpuCostIdle": 0.0,
                "gpuCostIdleloadBalancerCost": 0.0,
                "sharedCost": 0.1,
                "totalCost": 18.8,
                "totalEfficiency": 0.75,
            }
        }
    ]
}

SAMPLE_SAVINGS_RESPONSE = {
    "TotalMonthlySavings": 42.5,
    "Count": 1,
    "Recommendations": [
        {
            "clusterID": "cluster1",
            "namespace": "app",
            "containerName": "web",
            "monthlySavings": {"cpu": 20.0, "memory": 22.5, "total": 42.5},
            "normalizedRecommendedRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
            "normalizedLatestKnownRequest": {"cpuInMilliCores": 1000.0, "memoryInMiB": 1024.0},
            "currentEfficiency": {"cpu": 0.5, "memory": 0.5, "total": 0.5},
            "normalizedAverageUsage": {"cpuInMilliCores": 400.0, "memoryInMiB": 400.0},
            "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 600.0},
        }
    ],
}
