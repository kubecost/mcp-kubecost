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
