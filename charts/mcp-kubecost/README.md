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

Prefer an existing Secret so credentials are not stored in a values file or Helm command history. OIDC, API-key precedence, shared-hostname OAuth routes, and pod hardening are documented in [docs/auth](https://github.com/kubecost/mcp-kubecost/blob/HEAD/docs/auth).

## Authentication

Two independent settings protect the MCP HTTP endpoint, and they compose:

| Value | Effect |
| --- | --- |
| `config.oidc.enabled` | Require a valid OIDC token via FastMCP's `OIDCProxy`. |
| `config.requireClientApiKey` | Reject requests that arrive without an `X-API-KEY` header. |

Enabling `ingress` or `httpRoute` with neither of them set is rejected. Set
`allowUnauthenticatedExposure: true` to acknowledge intentional exposure, for
example an ingress behind a VPN.

`config.oidc.enabled` also creates a `ReadWriteOnce` PVC for FastMCP's OAuth
registrations, grants, and tokens, sized by `persistence.size` and provisioned by
`persistence.storageClass`, falling back to the parent chart's
`global.defaultStorageClass` and then the cluster default. Set
`persistence.enabled: false` to use an `emptyDir` instead, which forces clients to
re-register every time the pod restarts.

## Replicas

The Deployment defaults to one replica with `Recreate`. FileTreeStore is
single-writer and cannot share OAuth registrations across pods, so Helm rejects
`replicas > 1` while `config.oidc.enabled` is true.

To scale out, put an MCP gateway or OAuth proxy that owns session state in front
of this chart, then set `deployment.replicas`, switch
`deployment.strategy.type` to `RollingUpdate`, and leave `config.oidc.enabled` off
here.

## Kubecost parent chart

The MCP, by default, is bundled with the Kubecost helm installation in v3.3+. This repo may have newer versions of the MCP available for users looking for the latest improvements. The MCP should be compatible with any version of Kubecost 3.x, though be sure to read the release notes for any dependencies.

[Kubecost Helm Chart](https://github.com/kubecost/kubecost)

When this chart is a subchart, the parent `global` values are merged in and take precedence over the defaults in this chart's `values.yaml`:

| Parent key | Effect in this chart |
| --- | --- |
| `global.imageRegistry` | Replaces `image.registry`. Defaults to `icr.io` in both charts. |
| `global.imagePullSecrets` | Unioned with `image.pullSecrets`. Accepts name strings or `{name: ...}` maps. |
| `global.defaultStorageClass` | Used for the OAuth PVC when `persistence.storageClass` is empty. |
| `global.annotations` | Merged into this chart's Deployment metadata with `deployment.annotations`; chart-local keys win on conflict. |
| `global.podAnnotations` | Merged into the pod template; this chart's `podAnnotations` win on key conflicts, and the config-reload checksums are preserved. |
| `global.additionalLabels` | Added to this chart's resources and pod template. Never added to selector labels, which must stay immutable. |
| `global.platforms.openshift.enabled` | Replaces `podSecurityContext` with `global.platforms.openshift.securityContext`, because the OpenShift restricted-v2 SCC rejects an explicit `runAsUser`/`runAsGroup`. |
| `global.platforms.cicd.enabled` + `global.platforms.cicd.skipSanityChecks` | Skip Secret existence lookups. Set both when Helm cannot see the live cluster (Argo CD) or Secrets are created in a later sync wave. |

## Tests

Template behaviour is covered by [helm-unittest](https://github.com/helm-unittest/helm-unittest) suites in `tests/`:

```bash
helm unittest charts/mcp-kubecost
```
