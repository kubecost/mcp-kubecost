# Authentication and Security<!-- omit in toc -->

This server has two independent, optional auth layers. One protects the **MCP HTTP endpoint** (who may call tools). The other authenticates **outbound calls to Kubecost** (which Kubecost tenant/credential the server uses). They are configured separately and can be combined.

- [Authentication options](#authentication-options)
- [Protecting the MCP HTTP endpoint (OIDC)](#protecting-the-mcp-http-endpoint-oidc)
- [Kubecost API keys](#kubecost-api-keys)
- [STDIO vs HTTP](#stdio-vs-http)
- [Related docs](#related-docs)

## Authentication options

`AUTH_MODE` (Helm: `config.authMode`) controls how the MCP HTTP endpoint is protected.

| Mode      | MCP `/mcp`                                                            | Kubecost `X-API-KEY`         |
| --------- | --------------------------------------------------------------------- | ---------------------------- |
| `none`    | No auth — **not permitted when `httproute` or `ingress` is enabled**  | Optional env/header fallback |
| `open`    | No auth enforcement; explicitly acknowledged as exposed               | Optional env/header fallback |
| `oidc`    | Valid OIDC token via FastMCP `OIDCProxy`                              | Optional env/header fallback |
| `api_key` | Incoming `X-API-KEY` required (same as `REQUIRE_CLIENT_API_KEY=true`) | Header forwarded to Kubecost |

`oidc` requires `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
`OIDC_BASE_URL`, `OIDC_JWT_SIGNING_KEY`, and `OIDC_STORAGE_ENCRYPTION_KEY`.

> [!TIP]
> To require **both** OIDC identity and a per-request `X-API-KEY`, set `authMode: oidc` and `requireClientApiKey: true`.
> [!WARNING]
> If `httproute.enabled: true` or `ingress.enabled: true`, `authMode` must **not** be `"none"`.
> Set `authMode` to at least `"open"` to acknowledge the exposure, or to a stricter mode such as `"oidc"` or `"api_key"`.
> Helm will fail pre-install/pre-upgrade if this constraint is violated.
>
> ```yaml
> # values.yaml — minimum required when exposing a route
> config:
>   authMode: "open" # or oidc / api_key
> httpRoute:
>   enabled: true
> ```

## Protecting the MCP HTTP endpoint (OIDC)

When `AUTH_MODE=oidc`, the server builds a FastMCP [`OIDCProxy`](https://gofastmcp.com/servers/auth/oidc-proxy). MCP clients speak the MCP OAuth spec to **this server**. This server then talks to the upstream identity provider.

For full detail — redirect path configuration, DCR idempotency, shared Kubecost frontend hostname (nginx configuration), the full environment variable table, and Helm install examples — see [auth-technical.md](auth-technical.md#protecting-the-mcp-http-endpoint-oidc).

Sharing an OIDC client with the Kubecost UI is possible but not recommended: see [oidc-client-sharing.md](oidc-client-sharing.md) for the trade-offs.

## Kubecost API keys

By default the server calls Kubecost unauthenticated. That is fine for local port-forwards, or when another layer already authenticates to Kubecost.

When using Kubecost Enterprise with SSO enabled, Kubecost expects an API key. The key is sent as an **`X-API-KEY` request header**.

The MCP supports both per MCP client keys or a shared key assigned to the MCP server.

| Source                                  | Scope                             |
| --------------------------------------- | --------------------------------- |
| `X-API-KEY` on the incoming MCP request | Per request — HTTP transport only |
| `KUBECOST_API_KEY` environment variable | Process-wide fallback             |

Header wins when both are set. Neither is required; with no key the outbound request is unauthenticated.

`REQUIRE_CLIENT_API_KEY=true` (Helm: `config.requireClientApiKey`) rejects HTTP requests that arrive without the header. The check runs **before** the environment fallback, so a configured `KUBECOST_API_KEY` does not satisfy it. STDIO is never gated.

## STDIO vs HTTP

|                     | STDIO                  | HTTP                         |
| ------------------- | ---------------------- | ---------------------------- |
| Typical use         | Local IDE / desktop    | Shared or Kubernetes service |
| MCP OIDC            | Not used               | `AUTH_MODE=oidc`             |
| Inbound `X-API-KEY` | Cannot send headers    | Optional or required         |
| Kubecost key        | `KUBECOST_API_KEY` env | Header, then env             |

Local HTTP: `uv run fastmcp run config/fastmcp-http.json` (port 3030).

## Related docs

- [auth-technical.md](auth-technical.md) — full technical reference: OIDC setup, nginx configuration, environment variable table, Helm install examples, pod hardening, and troubleshooting
- [oidc-client-sharing.md](oidc-client-sharing.md) — whether to share an OIDC client with the Kubecost UI
- [`charts/mcp-kubecost/values.yaml`](../../charts/mcp-kubecost/values.yaml) — full Helm value reference with inline documentation
- [`.env.example`](../../.env.example) — full environment variable template
