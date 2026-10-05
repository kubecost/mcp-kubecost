"""Render the chart's security boundary and exercise its FastMCP allowlists."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mcp_kubecost.server import KubecostMCP, health_endpoint

_CHART = Path(__file__).resolve().parents[1] / "charts" / "mcp-kubecost"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is required for chart rendering")


def _render(tmp_path, values):
    values_path = tmp_path / "values.yaml"
    values_path.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        ["helm", "template", "security", str(_CHART), "--namespace", "finops", "-f", str(values_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}


def test_policy_opt_in_and_default_deny(tmp_path):
    assert "NetworkPolicy" not in _render(tmp_path, {})
    docs = _render(tmp_path, {"networkPolicy": {"enabled": True}})
    policy = docs["NetworkPolicy"]["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["ingress"] == []
    assert policy["egress"] == []
    labels = docs["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert policy["podSelector"]["matchLabels"].items() <= labels.items()


def test_policy_preserves_scoped_rules(tmp_path):
    rules = {
        "enabled": True,
        "ingress": [
            {
                "from": [
                    {
                        "namespaceSelector": {"matchLabels": {"team": "gateway"}},
                        "podSelector": {"matchLabels": {"app": "proxy"}},
                    }
                ],
                "ports": [{"port": 3030, "protocol": "TCP"}],
            }
        ],
        "egress": [{"to": [{"ipBlock": {"cidr": "203.0.113.10/32"}}], "ports": [{"port": 443, "protocol": "TCP"}]}],
    }
    policy = _render(tmp_path, {"networkPolicy": rules})["NetworkPolicy"]["spec"]
    assert policy["ingress"] == rules["ingress"]
    assert policy["egress"] == rules["egress"]


@pytest.mark.parametrize("route", ["external", "ingress", "httpRoute"])
async def test_chart_guard_allows_public_requests_and_rejects_browser_attacks(tmp_path, route):
    values: dict[str, Any] = {"config": {"authMode": "open", "fastmcpHttpAllowedOrigins": '["http://127.0.0.1:6274"]'}}
    if route == "external":
        values["config"]["externalUrl"] = "https://mcp.example.com"
    elif route == "ingress":
        values["ingress"] = {"enabled": True, "hosts": [{"host": "mcp.example.com"}]}
    else:
        values["httpRoute"] = {"enabled": True, "hostnames": ["mcp.example.com"], "parentRefs": [{"name": "gateway"}]}
    docs = _render(tmp_path, values)
    env = docs["ConfigMap"]["data"]
    assert env["MCP_TOOL_CALL_TIMEOUT_SECONDS"] == "600"
    assert env["FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"] == "true"
    assert env["FORWARDED_ALLOW_IPS"] == ""
    hosts = json.loads(env["FASTMCP_HTTP_ALLOWED_HOSTS"])
    origins = json.loads(env["FASTMCP_HTTP_ALLOWED_ORIGINS"])
    assert "mcp.example.com" in hosts
    assert "https://mcp.example.com" in origins
    server = KubecostMCP("guard-test")
    server.custom_route("/health", methods=["GET"])(health_endpoint)
    app = server.http_app(host_origin_protection=True, allowed_hosts=hosts, allowed_origins=origins)
    # Plain HTTP models TLS termination without trusting X-Forwarded-Proto.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.example.com") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health", headers={"Origin": "https://mcp.example.com"})).status_code == 200
        assert (await client.get("/health", headers={"Origin": "http://127.0.0.1:6274"})).status_code == 200
        assert (await client.get("/health", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/mcp", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.get("/health", headers={"Host": "evil.example"})).status_code == 421
        container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container[name]["httpGet"]
            headers = {item["name"]: item["value"] for item in probe["httpHeaders"]}
            assert (await client.get(probe["path"], headers=headers)).status_code == 200


def test_update_ca_trust_is_off_by_default(tmp_path):
    """``global.updateCaTrust.caCertsSecret`` has a non-empty parent-chart default, so
    rendering must gate on ``enabled`` alone or every install would mount a Secret."""
    spec = _render(tmp_path, {})["Deployment"]["spec"]["template"]["spec"]
    assert "initContainers" not in spec
    volumes = {volume["name"] for volume in spec["volumes"]}
    assert "ssl-path" not in volumes
    assert "ca-certs" not in volumes


def test_update_ca_trust_extracts_as_non_root(tmp_path):
    """The parent chart runs this init container as ``runAsUser: 0``. ``trust extract``
    does not need it, and root here would contradict ``podSecurityContext.runAsNonRoot``
    and the OpenShift restricted-v2 SCC that ``global.platforms.openshift`` supports."""
    spec = _render(
        tmp_path,
        {
            "global": {
                "updateCaTrust": {"enabled": True, "caCertsSecret": "corporate-ca"},
                "platforms": {"cicd": {"enabled": True, "skipSanityChecks": True}},
            }
        },
    )["Deployment"]["spec"]["template"]["spec"]

    init = next(container for container in spec["initContainers"] if container["name"] == "update-ca-trust")
    assert "trust extract" in init["args"][0]
    assert "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem" in init["args"][0]
    security = init["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert "runAsUser" not in security
    assert "runAsNonRoot" not in security

    # Both containers read the extracted bundle; only the init container writes it.
    init_mounts = {mount["name"]: mount for mount in init["volumeMounts"]}
    assert init_mounts["ca-certs"]["mountPath"] == "/etc/pki/ca-trust/source/anchors"
    assert init_mounts["ssl-path"]["mountPath"] == "/etc/pki/ca-trust/extracted/pem"
    assert not init_mounts["ssl-path"].get("readOnly", False)
    main_mounts = {mount["name"]: mount for mount in spec["containers"][0]["volumeMounts"]}
    assert main_mounts["ssl-path"]["mountPath"] == "/etc/pki/ca-trust/extracted/pem"
    assert main_mounts["ssl-path"]["readOnly"] is True

    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["ca-certs"]["secret"]["secretName"] == "corporate-ca"
    # An emptyDir keeps readOnlyRootFilesystem intact and cannot go stale against
    # the image's own public anchors, which are re-extracted on every pod start.
    assert volumes["ssl-path"]["emptyDir"] == {}


def test_update_ca_trust_accepts_a_configmap(tmp_path):
    spec = _render(
        tmp_path,
        {
            "global": {
                "updateCaTrust": {"enabled": True, "caCertsSecret": "", "caCertsConfig": "corporate-ca-cm"},
                "platforms": {"cicd": {"enabled": True, "skipSanityChecks": True}},
            }
        },
    )["Deployment"]["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["ca-certs"]["configMap"]["name"] == "corporate-ca-cm"


@pytest.mark.parametrize(
    ("update_ca_trust", "expected"),
    [
        ({"enabled": True, "caCertsSecret": "a", "caCertsConfig": "b"}, "cannot both be set"),
        ({"enabled": True, "caCertsSecret": "", "caCertsConfig": ""}, "neither global.updateCaTrust.caCertsSecret"),
    ],
)
def test_update_ca_trust_rejects_ambiguous_sources(tmp_path, update_ca_trust, expected):
    values = {
        "global": {"updateCaTrust": update_ca_trust, "platforms": {"cicd": {"enabled": True, "skipSanityChecks": True}}}
    }
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render(tmp_path, values)
    assert expected in excinfo.value.stderr
