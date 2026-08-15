# mcp-kubecost Helm chart

This chart deploys the read-only Kubecost FinOps MCP server with a hardened
Deployment, ClusterIP Service, optional Gateway API HTTPRoute, optional legacy
Ingress, and a generated ConfigMap.

## Install

```bash
helm upgrade --install mcp-kubecost ./charts/mcp-kubecost \
  --namespace mcp-kubecost --create-namespace \
  --set config.kubecostBaseUrl=https://kubecost.example.com/model \
  --set config.kubecostApiKey.existingSecret=kubecost-api-key
```

Prefer an existing Secret so credentials are not stored in a values file or Helm command history. OIDC, API-key precedence, `REQUIRE_CLIENT_API_KEY`, shared-hostname OAuth routes, and pod hardening are documented in [README-auth.md](../../README-auth.md).

All application environment settings from the repository's `.env.example`
are represented under `config` in `values.yaml`. `values.schema.json` validates
the supported value types and common deployment mistakes.

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
