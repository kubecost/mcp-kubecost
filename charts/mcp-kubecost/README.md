# mcp-kubecost Helm chart

This chart deploys the read-only Kubecost FinOps MCP server.

## Installation Options

See the [readme](../../README.md) at the root of the repository for installation options.

## Standalone Install

Create a file with the values that differ from the defaults, for example
`helmValues-mcp-kubecost.yaml`. Then install with:

```bash
helm upgrade --install kubecost-mcp mcp-kubecost \
  --repo https://kubecost.github.io/mcp-kubecost mcp-kubecost \
  --namespace kubecost-mcp --create-namespace \
  -f helmValues-mcp-kubecost.yaml
```

Prefer an existing Secret so credentials are not stored in a values file or Helm command history. OIDC, API-key precedence, `REQUIRE_CLIENT_API_KEY`, shared-hostname OAuth routes, and pod hardening are documented in [docs/auth](https://github.com/kubecost/mcp-kubecost/blob/HEAD/docs/auth).

The Deployment defaults to one replica with `Recreate`.
Multiple replicas are supported when an MCP gateway or OAuth proxy owns session state in front of this chart.

Set `deployment.replicas` greater than 1, switch `deployment.strategy.type` to `RollingUpdate`, keep `config.authMode` at `none` / `open` / `api_key`, and set `persistence.enabled: false`.

Helm rejects `replicas > 1` while a PVC is mounted or `authMode` is `oidc` — FileTreeStore is single-writer and cannot share OAuth registrations across pods until shared storage exists.

`persistence.enabled` is a tri-state field:

| Value            | Behaviour                                                                                                                                                                                                        |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `null` (default) | PVC is created automatically when `config.authMode` is `oidc`; omitted for all other auth modes.                                                                                                                 |
| `true`           | PVC is always created regardless of `authMode`.                                                                                                                                                                  |
| `false`          | PVC is never created. The pod uses an `emptyDir` instead, and clients must re-register every time the pod restarts. A post-install warning is shown when `authMode=oidc` and persistence is explicitly disabled. |

When a PVC is created it defaults to `1Gi`. The StorageClass is `persistence.storageClass`, falling back to the parent chart's `global.defaultStorageClass` and then to the cluster's default StorageClass.
Set `persistence.storageClass`, `persistence.accessModes`, `persistence.size`, or `persistence.annotations` when the cluster requires different provisioning.

## Kubecost parent chart

The MCP, by default, is bundled with the Kubecost helm installation in v3.3+. This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies.

[Kubecost Helm Chart](https://github.com/kubecost/kubecost)

When this chart is a subchart, the parent `global` values are merged in and take precedence over the defaults in this chart's `values.yaml`:

| Parent key                                                                 | Effect in this chart                                                                                                                                                   |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `global.imageRegistry`                                                     | Replaces `image.registry`. Defaults to `icr.io` in both charts.                                                                                                        |
| `global.imagePullSecrets`                                                  | Unioned with `image.pullSecrets`. Accepts name strings or `{name: ...}` maps.                                                                                          |
| `global.annotations`                                                       | Merged into this chart's Deployment metadata with `deployment.annotations`; chart-local keys win on conflict.                                                          |
| `global.podAnnotations`                                                    | Merged into the pod template; this chart's `podAnnotations` win on key conflicts, and the config-reload checksums are preserved.                                       |
| `global.additionalLabels`                                                  | Added to this chart's resources and pod template. Never added to selector labels, which must stay immutable.                                                           |
| `global.platforms.openshift.enabled`                                       | Replaces `podSecurityContext` with `global.platforms.openshift.securityContext`, because the OpenShift restricted-v2 SCC rejects an explicit `runAsUser`/`runAsGroup`. |
| `global.platforms.cicd.enabled` + `global.platforms.cicd.skipSanityChecks` | Skip Secret existence lookups. Set both when Helm cannot see the live cluster (Argo CD) or Secrets are created in a later sync wave.                                   |

## Optional proxy-header trust

`config.forwardedAllowIps` defaults to `""`, disabling uvicorn's trust in forwarded client addresses and schemes. Set it only when those headers are needed, using known proxy IPs or CIDRs. Use `"*"` only when every path to the pod passes through a proxy that overwrites client-supplied headers. This setting does not authorize clients.

OAuth registration shares one process-wide budget: 120 requests/minute, burst 60. HTTP 429 includes `Retry-After: 60`. Changing forwarded-header trust does not change this budget. The limiter slows storage abuse but does not cap accumulated client files.

## HTTP Host and Origin validation

The chart enables FastMCP's strict guard (`config.fastmcpHttpHostOriginProtection: true`). It derives allowed Hosts from Service DNS names, `config.externalUrl`, and enabled Ingress/HTTPRoute hosts, and allows the public HTTPS origins. TLS may terminate at a proxy: these explicit HTTPS origins work with forwarded-header trust disabled.

For a separately managed proxy, set `config.externalUrl`. For additional native-client Host names or browser clients, set JSON array strings:

```yaml
config:
  externalUrl: https://mcp.example.com
  fastmcpHttpAllowedHosts: '["internal-mcp.example.com"]'
  fastmcpHttpAllowedOrigins: '["http://127.0.0.1:6274"]' # MCP Inspector
```

The two lists extend the derived defaults. Avoid wildcard hosts/origins unless intentionally trusting that entire scope. Native clients need no Origin header. Kubernetes HTTP probes use `Host: localhost` so strict validation does not depend on changing pod IPs. Additional cluster DNS suffixes can be supplied in `fastmcpHttpAllowedHosts`.

CORS permits browser clients to send protocol headers; FastMCP separately rejects invalid Hosts (421) and Origins (403). OAuth consent, CSRF, redirect validation and SSRF protection remain in FastMCP. NetworkPolicy does not replace these checks. Outside Helm, configure the FastMCP environment variables in `.env.example` explicitly.

## Network isolation

`networkPolicy.enabled` defaults to `false`: gateway labels, DNS topology and egress destinations are cluster-specific. Enabling it with empty rules denies both ingress and egress. Rules use the standard Kubernetes NetworkPolicy schema and are rendered verbatim, without template evaluation. Selectors identify this chart's pods even when deployed as a subchart.

Copy the [Envoy Gateway HTTPRoute example](../../docs/examples/network-policies/README.md) into your deployment values. The example folder contains a Helm values overlay, an external HTTPS egress fragment, and guidance for DNS, Kubecost, OIDC, CIMD and telemetry destinations.

NetworkPolicy requires an enforcing CNI and does not replace FastMCP's Host/Origin, OAuth or SSRF protections. Validate allowed and denied traffic on your cluster after configuring the example.
