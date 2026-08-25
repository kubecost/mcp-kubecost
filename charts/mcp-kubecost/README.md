# mcp-kubecost Helm chart

This chart deploys the read-only Kubecost FinOps MCP server with a hardened
Deployment, ClusterIP Service, optional Gateway API HTTPRoute, optional legacy
Ingress, and a generated ConfigMap.

## Installation Options

The MCP, by default, is bundled with the Kubecost helm installation. This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies.

[kubecost chart](https://github.com/kubecost/kubecost)

## Standalone Install

Create a file with the values that differ from the defaults, example `helmValues-mcp-kubecost.yaml. Then install with:

```bash
helm upgrade --install kubecost-mcp mcp-kubecost \
  --repo https://kubecost.github.io/mcp-kubecost mcp-kubecost \
  --namespace kubecost-mcp --create-namespace \
  -f helmValues-mcp-kubecost.yaml
```

Prefer an existing Secret so credentials are not stored in a values file or Helm command history. OIDC, API-key precedence, `REQUIRE_CLIENT_API_KEY`, shared-hostname OAuth routes, and pod hardening are documented in [docs/auth](https://github.com/kubecost/mcp-kubecost/blob/HEAD/docs/auth).

All application environment settings from the repository's `.env.example`
are represented under `config` in `values.yaml`, except
`FASTMCP_ENABLE_RICH_LOGGING` — this chart only runs the HTTP transport, where
rich logging is forced off. `values.schema.json` validates the supported value
types and common deployment mistakes.

## Gateway API HTTPRoute (preferred)

Gateway API is the preferred way to expose this chart. The cluster must
already have a Gateway API implementation and a compatible Gateway resource;
this chart intentionally does not install cluster-scoped Gateway resources.

Enable the route and point it at an existing Gateway listener:

```bash
helm upgrade --install mcp-kubecost ./charts/mcp-kubecost \
  --namespace mcp-kubecost --create-namespace \
  --set httpRoute.enabled=true \
  --set 'httpRoute.parentRefs[0].name=mcp-gateway' \
  --set 'httpRoute.parentRefs[0].sectionName=https'
```

`httpRoute.rules` accepts Gateway API rule fields such as `matches`,
`filters`, `timeouts`, and `backendRefs`. If a rule omits `backendRefs`, the
chart routes it to the Service created by this release.

## Ingress fallback and custom CA

Ingress is retained for clusters that do not yet provide Gateway API. Set
`ingress.enabled=true`, then provide the host, path, annotations, and TLS
Secret appropriate for the cluster. Do not enable both exposure resources
unless you intentionally want both routes.

For a custom Kubecost CA, put the certificate in an existing Secret and set
`config.ssl.caBundle.existingSecret` and `config.ssl.caBundle.key`. The chart
mounts it read-only and sets `SSL_CA_BUNDLE` to the configured mount path.

The debug Caddy proxy is intentionally not part of this production chart.

## Kubecost parent chart

The MCP, by default, is bundled with the Kubecost helm installation. This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies.

[kubecost chart](https://github.com/kubecost/kubecost)

When this chart is a subchart, parent `global` values are merged in and take
precedence over the defaults in this chart's `values.yaml`:

| Parent key                                                                 | Effect in this chart                                                                                                                                                   |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `global.imageRegistry`                                                     | Replaces `image.registry`. Defaults to `icr.io` in both charts.                                                                                                        |
| `global.imagePullSecrets`                                                  | Unioned with `image.pullSecrets`. Accepts name strings or `{name: ...}` maps.                                                                                          |
| `global.annotations`                                                       | Added to this chart's Deployment metadata.                                                                                                                             |
| `global.podAnnotations`                                                    | Merged into the pod template; this chart's `podAnnotations` win on key conflicts, and the config-reload checksums are preserved.                                       |
| `global.additionalLabels`                                                  | Added to this chart's resources and pod template. Never added to selector labels, which must stay immutable.                                                           |
| `global.platforms.openshift.enabled`                                       | Replaces `podSecurityContext` with `global.platforms.openshift.securityContext`, because the OpenShift restricted-v2 SCC rejects an explicit `runAsUser`/`runAsGroup`. |
| `global.platforms.cicd.enabled` + `global.platforms.cicd.skipSanityChecks` | Skip Secret existence lookups. Set both when Helm cannot see the live cluster (Argo CD) or Secrets are created in a later sync wave.                                   |

Standalone installs get the same defaults, so `helm install` of this chart on
its own renders the same image (`icr.io/kubecost/mcp-kubecost`) as a subchart
install. Other parent `global` keys (for example `clusterId`) are accepted and
ignored.

The `enabled` key exists only so the parent's `condition: mcp-kubecost.enabled`
can gate the subchart. This chart's templates do not read it, so setting it on a
standalone install has no effect.
