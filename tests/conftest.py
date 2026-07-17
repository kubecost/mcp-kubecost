"""Shared pytest fixtures and configuration."""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Environment defaults — must be set before any module-level settings load
# ---------------------------------------------------------------------------

os.environ.setdefault("KUBECOST_BASE_URL", "https://demo.kubecost.xyz")


# ---------------------------------------------------------------------------
# Fixtures: minimal allocation API response
# ---------------------------------------------------------------------------


@pytest.fixture
def allocation_response_one_ns() -> dict:
    """Single-bucket allocation response with one namespace."""
    return {
        "data": [
            {
                "kubecost-system/kubecost": {
                    "name": "cluster-one/kubecost-system",
                    "properties": {"cluster": "cluster-one", "namespace": "kubecost-system"},
                    "window": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-08T00:00:00Z"},
                    "cpuCost": 10.5,
                    "cpuCostIdle": 2.5,
                    "ramCost": 5.0,
                    "ramCostIdle": 1.0,
                    "networkCost": 0.5,
                    "pvCost": 0.0,
                    "gpuCost": 0.0,
                    "gpuCostIdle": 0.0,
                    "loadBalancerCost": 0.0,
                    "sharedCost": 0.0,
                    "totalCost": 16.0,
                    "totalEfficiency": 0.6,
                }
            }
        ]
    }


@pytest.fixture
def allocation_response_multi_ns() -> dict:
    """Multi-entry allocation response with two namespaces."""
    return {
        "data": [
            {
                "ns-a": {
                    "name": "cluster-one/ns-a",
                    "properties": {"cluster": "cluster-one", "namespace": "ns-a"},
                    "window": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-08T00:00:00Z"},
                    "cpuCost": 20.0,
                    "cpuCostIdle": 5.0,
                    "ramCost": 10.0,
                    "ramCostIdle": 2.0,
                    "networkCost": 1.0,
                    "pvCost": 0.0,
                    "gpuCost": 0.0,
                    "gpuCostIdle": 0.0,
                    "loadBalancerCost": 0.0,
                    "sharedCost": 0.0,
                    "totalCost": 31.0,
                    "totalEfficiency": 0.7,
                },
                "ns-b": {
                    "name": "cluster-one/ns-b",
                    "properties": {"cluster": "cluster-one", "namespace": "ns-b"},
                    "window": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-08T00:00:00Z"},
                    "cpuCost": 5.0,
                    "cpuCostIdle": 0.5,
                    "ramCost": 2.0,
                    "ramCostIdle": 0.2,
                    "networkCost": 0.0,
                    "pvCost": 0.0,
                    "gpuCost": 0.0,
                    "gpuCostIdle": 0.0,
                    "loadBalancerCost": 0.0,
                    "sharedCost": 0.0,
                    "totalCost": 7.2,
                    "totalEfficiency": 0.8,
                },
            }
        ]
    }


@pytest.fixture
def savings_api_response() -> dict:
    """Minimal requestSizingV2 API response with two recommendations."""
    return {
        "TotalMonthlySavings": 45.50,
        "Count": 2,
        "Recommendations": [
            {
                "clusterID": "cluster-one",
                "namespace": "default",
                "controllerKind": "Deployment",
                "controllerName": "api-server",
                "containerName": "api",
                "monthlySavings": {"cpu": 20.0, "memory": 15.0, "total": 35.0},
                "normalizedRecommendedRequest": {"cpuInMilliCores": 200.0, "memoryInMiB": 256.0},
                "normalizedLatestKnownRequest": {"cpuInMilliCores": 500.0, "memoryInMiB": 512.0},
                "currentEfficiency": {"cpu": 0.3, "memory": 0.4, "total": 0.35},
                "normalizedAverageUsage": {"cpuInMilliCores": 150.0, "memoryInMiB": 200.0},
                "normalizedMaxUsage": {"cpuInMilliCores": 600.0, "memoryInMiB": 300.0},
            },
            {
                "clusterID": "cluster-one",
                "namespace": "monitoring",
                "controllerKind": "Deployment",
                "controllerName": "prometheus",
                "containerName": "prometheus",
                "monthlySavings": {"cpu": 5.0, "memory": 5.5, "total": 10.5},
                "normalizedRecommendedRequest": {"cpuInMilliCores": 100.0, "memoryInMiB": 512.0},
                "normalizedLatestKnownRequest": {"cpuInMilliCores": 300.0, "memoryInMiB": 1024.0},
                "currentEfficiency": {"cpu": 0.25, "memory": 0.5, "total": 0.37},
                "normalizedAverageUsage": {"cpuInMilliCores": 80.0, "memoryInMiB": 500.0},
                "normalizedMaxUsage": {"cpuInMilliCores": 200.0, "memoryInMiB": 600.0},
            },
        ],
    }


@pytest.fixture
def abandoned_workloads_api_response() -> list:
    """Minimal abandonedWorkloads API response — bare JSON array."""
    return [
        {
            "pod": "idle-worker-abc",
            "namespace": "batch",
            "node": "ip-10-0-1-10.us-east-1.compute.internal",
            "clusterId": "cluster-one",
            "clusterName": "",
            "owners": [{"name": "idle-worker", "kind": "deployment"}],
            "ingressBytesPerSecond": 0.0,
            "egressBytesPerSecond": 0.0,
            "allocation": {"cpuCores": 0.5, "ramBytes": 536870912.0},
            "requests": {"cpuCores": 0.5, "ramBytes": 536870912.0},
            "usage": {"cpuCores": 0.001, "ramBytes": 10485760.0},
            "monthlySavings": 42.50,
        },
        {
            "pod": "stale-job-xyz",
            "namespace": "jobs",
            "node": "ip-10-0-1-11.us-east-1.compute.internal",
            "clusterId": "cluster-one",
            "clusterName": "",
            "owners": [],
            "ingressBytesPerSecond": 1.5,
            "egressBytesPerSecond": 2.0,
            "allocation": {"cpuCores": 0.1, "ramBytes": 134217728.0},
            "requests": {"cpuCores": 0.1, "ramBytes": 134217728.0},
            "usage": {"cpuCores": 0.0005, "ramBytes": 5242880.0},
            "monthlySavings": 8.00,
        },
    ]
