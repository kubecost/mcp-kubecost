"""Shared pytest fixtures and configuration."""

from __future__ import annotations

import os

import pytest

from mcp_kubecost.client import close_http_client

# ---------------------------------------------------------------------------
# Environment defaults — must be set before any module-level settings load
# ---------------------------------------------------------------------------

os.environ.setdefault("KUBECOST_BASE_URL", "https://demo.kubecost.xyz")


@pytest.fixture(autouse=True)
async def reset_shared_http_client():
    """Keep the process-wide client isolated across tests and event loops."""
    await close_http_client()
    yield
    await close_http_client()


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
def allocation_response_daily_buckets() -> dict:
    """Non-accumulated allocation response: 3 consecutive 1-day buckets x 2 namespaces.

    Mirrors what Kubecost returns for accumulate=false — one bucket per day, each
    entry carrying that day's own window rather than the whole queried range.
    """

    def _entry(namespace: str, day: int, total: float) -> dict:
        return {
            "name": f"cluster-one/{namespace}",
            "properties": {"cluster": "cluster-one", "namespace": namespace},
            "window": {
                "start": f"2024-01-0{day}T00:00:00Z",
                "end": f"2024-01-0{day + 1}T00:00:00Z",
            },
            "cpuCost": total / 2,
            "cpuCostIdle": total / 10,
            "ramCost": total / 2,
            "ramCostIdle": total / 10,
            "networkCost": 0.0,
            "pvCost": 0.0,
            "gpuCost": 0.0,
            "gpuCostIdle": 0.0,
            "loadBalancerCost": 0.0,
            "sharedCost": 0.0,
            "totalCost": total,
            "totalEfficiency": 0.5,
        }

    return {
        "data": [
            {
                "ns-a": _entry("ns-a", day, 10.0 * day),
                "ns-b": _entry("ns-b", day, 2.0 * day),
            }
            for day in (1, 2, 3)
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


@pytest.fixture
def savings_overview_api_response() -> dict:
    """Minimal /model/savings response with all 8 categories."""
    return {
        "code": 200,
        "data": {
            "cluster": "",
            "profile": "HighAvailability",
            "abandonedWorkloads": {"savingsPerMonth": 632.78, "lastRefresh": "2026-07-17T15:02:20Z"},
            "nodeGroupSizing": {"savingsPerMonth": 1888.08, "lastRefresh": "2026-07-17T15:02:20Z"},
            "resourceQuotaSizing": {"savingsPerMonth": 0.0, "lastRefresh": "2026-07-17T15:02:20Z"},
            "orphanedResources": {"savingsPerMonth": 0.0, "lastRefresh": "2026-07-17T15:00:00Z"},
            "underutilizedLocalDisks": {"savingsPerMonth": 118.45, "lastRefresh": "2026-07-17T15:02:10Z"},
            "persistentVolumeSizing": {"savingsPerMonth": 218.66, "lastRefresh": "2026-07-17T15:01:44Z"},
            "unclaimedVolumes": {"savingsPerMonth": 22.67, "lastRefresh": "2026-07-17T15:00:00Z"},
            "containerRequestSizing": {"savingsPerMonth": 831.94, "lastRefresh": "2026-07-17T15:02:20Z"},
        },
        "meta": {"isWarm": True},
    }


@pytest.fixture
def pv_sizing_api_response() -> dict:
    """Minimal persistentVolumeSizing API response with two recommendations."""
    return {
        "recommendations": [
            {
                "volumeName": "pvc-aaa",
                "claimName": "db-data",
                "claimNamespace": "turbonomic",
                "clusterId": "kc-demo-prod",
                "maxUsageBytes": 10984706048,
                "averageUsageBytes": 10984696758,
                "recommendedCapacityBytes": 17179869184,
                "recommendedCostMonthly": 1.28,
                "currentCapacityBytes": 536870912000,
                "currentCostMonthly": 40.0,
                "savingsMonthly": 38.72,
                "storageClass": "gp3",
            },
            {
                "volumeName": "pvc-bbb",
                "claimName": "cache-data",
                "claimNamespace": "default",
                "clusterId": "kc-demo-prod",
                "maxUsageBytes": 1073741824,
                "averageUsageBytes": 1073741824,
                "recommendedCapacityBytes": 2147483648,
                "recommendedCostMonthly": 0.20,
                "currentCapacityBytes": 107374182400,
                "currentCostMonthly": 8.0,
                "savingsMonthly": 7.80,
                "storageClass": "gp3",
            },
        ]
    }


@pytest.fixture
def local_disks_api_response() -> dict:
    """Minimal localLowDisks API response with two disks."""
    return {
        "unutilizedDisks": [
            {
                "diskName": "ip-10-0-4-95.us-west-2.compute.internal",
                "clusterId": "kc-demo-rosa",
                "utilizationPercent": 0.0,
                "currentUsageBytes": 0,
                "currentCapacityBytes": 375204589568,
                "recommendedCapacityBytes": 0,
                "currentCostMonthly": 13.98,
                "savingsMonthly": 13.98,
            },
            {
                "diskName": "ip-10-0-5-12.us-west-2.compute.internal",
                "clusterId": "kc-demo-rosa",
                "utilizationPercent": 0.05,
                "currentUsageBytes": 18760229478,
                "currentCapacityBytes": 375204589568,
                "recommendedCapacityBytes": 0,
                "currentCostMonthly": 5.00,
                "savingsMonthly": 5.00,
            },
        ]
    }


@pytest.fixture
def node_group_sizing_api_response() -> dict:
    """Minimal nodeGroupSizing API response with two recommendations including ChangeInstanceType."""
    return {
        "code": 200,
        "data": {
            "window": {"start": "2026-07-10T00:00:00Z", "end": "2026-07-17T00:00:00Z"},
            "minNodeCount": 1,
            "cpuMetric": "usage.p95",
            "cpuTargetUtilization": 0.65,
            "ramMetric": "usage.max",
            "ramTargetUtilization": 0.65,
            "totalSavingsPerMonth": 209.27,
            "warnings": [],
            "recommendations": [
                {
                    "nodeGroup": "aws-usw2-demo-ng8",
                    "recommendation": "ScaleIn",
                    "before": {
                        "instanceType": "t3a.2xlarge",
                        "nodeCount": 5,
                        "pricePerMonth": 546.37,
                        "resources": {
                            "cpu": {
                                "unit": "m",
                                "capacity": {"avg": 40000},
                                "usage": {"avg": 3071.05, "p95": 16457.02},
                                "utilization": 0.411,
                            },
                            "ram": {
                                "unit": "Mi",
                                "capacity": {"avg": 159034.32},
                                "usage": {"avg": 32495.77, "p95": 37128.29},
                                "utilization": 0.313,
                            },
                        },
                    },
                    "after": {
                        "instanceType": "t3a.2xlarge",
                        "nodeCount": 4,
                        "pricePerMonth": 437.10,
                        "resources": {
                            "cpu": {"unit": "m", "capacity": {"avg": 32000}, "utilization": 0.514},
                            "ram": {"unit": "Mi", "capacity": {"avg": 127227}, "utilization": 0.391},
                        },
                    },
                    "savingsPerMonth": 109.27,
                },
                {
                    "nodeGroup": "aws-usw2-demo-ng9",
                    "recommendation": "ChangeInstanceType",
                    "before": {
                        "instanceType": "m5.xlarge",
                        "nodeCount": 3,
                        "pricePerMonth": 300.0,
                        "resources": {
                            "cpu": {
                                "unit": "m",
                                "capacity": {"avg": 12000},
                                "usage": {"avg": 1000.0, "p95": 2000.0},
                                "utilization": 0.2,
                            },
                            "ram": {
                                "unit": "Mi",
                                "capacity": {"avg": 49000},
                                "usage": {"avg": 10000.0, "p95": 15000.0},
                                "utilization": 0.25,
                            },
                        },
                    },
                    "after": {
                        "instanceType": "t3a.xlarge",
                        "nodeCount": 3,
                        "pricePerMonth": 200.0,
                        "resources": {
                            "cpu": {"unit": "m", "capacity": {"avg": 12000}, "utilization": 0.35},
                            "ram": {"unit": "Mi", "capacity": {"avg": 24000}, "utilization": 0.5},
                        },
                    },
                    "savingsPerMonth": 100.0,
                },
            ],
        },
    }


@pytest.fixture
def unclaimed_volumes_api_response() -> dict:
    """Minimal unclaimedVolumes API response."""
    return {
        "code": 200,
        "data": {
            "count": 2,
            "monthlyCost": 15.24,
            "volumes": [
                {
                    "monthlyCost": 7.62,
                    "volumeName": "pvc-aaa",
                    "properties": {
                        "category": "Storage",
                        "provider": "GCP",
                        "service": "Kubernetes",
                        "cluster": "kc-demo-stage",
                        "name": "pvc-aaa",
                        "providerID": "pvc-aaa",
                    },
                },
                {
                    "monthlyCost": 7.62,
                    "volumeName": "pvc-bbb",
                    "properties": {
                        "category": "Storage",
                        "provider": "AWS",
                        "service": "Kubernetes",
                        "cluster": "kc-demo-prod",
                        "name": "pvc-bbb",
                        "providerID": "pvc-bbb",
                    },
                },
            ],
        },
    }


@pytest.fixture
def resource_quota_api_response() -> dict:
    """Minimal resourceQuotaSizing API response with two namespace recommendations."""
    return {
        "code": 200,
        "data": {
            "window": {"start": "2026-07-10T00:00:00Z", "end": "2026-07-17T00:00:00Z"},
            "itemCount": 2,
            "totalMonthlySavings": 0.0,
            "recommendations": [
                {
                    "isNewResourceQuota": True,
                    "cluster": "kc-demo-rosa",
                    "namespace": "openshift-image-registry",
                    "category": "compute",
                    "resources": [
                        {
                            "isNewResource": True,
                            "isDownsize": False,
                            "resourceType": "requests.cpu",
                            "category": "compute",
                            "used": "280m",
                            "recommended": "378m",
                        },
                    ],
                },
                {
                    "isNewResourceQuota": False,
                    "cluster": "kc-demo-prod",
                    "namespace": "monitoring",
                    "category": "compute",
                    "resources": [
                        {
                            "isNewResource": False,
                            "isDownsize": True,
                            "resourceType": "requests.memory",
                            "category": "compute",
                            "used": "512Mi",
                            "recommended": "256Mi",
                        },
                    ],
                },
            ],
        },
    }
