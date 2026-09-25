# Authentication — Technical Reference<!-- omit in toc -->

This page is the technical reference for MCP OIDC, reverse-proxy routing, Helm configuration, and troubleshooting. For the auth-mode overview, see [README.md](README.md).

**The MCP Server authentication has limited testing and is considered beta. Consider using multiple layers of security for production deployments.**

## Protecting the MCP HTTP endpoint (OIDC)

With `AUTH_MODE=oidc`, FastMCP acts as an OAuth authorization server to MCP clients and as an OIDC client to the upstream identity provider. Those are two different protocol relationships.

The recommended public layout keeps the protected resource stable and gives OAuth operations a distinct namespace:

| Surface | Public URL |
| --- | --- |
| MCP protected resource | `https://kubecost.example.com/mcp` |
| OAuth authorization server | `https://kubecost.example.com/oauth/mcp` |
| Upstream IdP callback | `https://kubecost.example.com/oauth/mcp/callback` |
| Protected-resource metadata | `https://kubecost.example.com/.well-known/oauth-protected-resource/mcp` |
| Authorization-server metadata | `https://kubecost.example.com/.well-known/oauth-authorization-server/oauth/mcp` |

The server also answers the three bare root paths — `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, and `/.well-known/openid-configuration` — with the same documents. These are not advertised anywhere. They exist for clients that never learn the path-aware URL, typically because the `WWW-Authenticate: Bearer resource_metadata=...` header on the 401 was dropped by an intermediary. Without them such a client probes the root, gets a 404, and falls back to the MCP SDK's default endpoints (`/authorize`, `/token`, `/register`) — paths this server does not mount, so the flow dead-ends on a 404 at `/authorize`.

This follows the path-aware discovery forms in [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728.html) and [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414.html), as required by the [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

The corresponding setting is:

```dotenv
AUTH_MODE=oidc
MCP_EXTERNAL_URL=https://kubecost.example.com
```

`MCP_EXTERNAL_URL` is the public origin only — scheme, hostname, and optional port, no path. The MCP endpoint (`/mcp`), the OAuth authorization-server prefix (`/oauth/mcp`), and the IdP callback (`/oauth/mcp/callback`) are fixed and derived from it; they are not independently configurable. The server also mounts the OAuth operational routes (`/authorize`, `/token`, `/register`, `/revoke`, `/consent`, `/callback`) directly under `/oauth/mcp` itself, so a reverse proxy forwards these paths verbatim with no prefix stripping or rewriting.

### Identity provider setup

Create a confidential OIDC client for `mcp-kubecost` and register exactly:

```text
https://kubecost.example.com/oauth/mcp/callback
```

Do not register MCP-client callbacks such as loopback, Claude, or ChatGPT URLs at the IdP. MCP clients register those with FastMCP through DCR or client ID metadata documents. The IdP only redirects to the server-side callback above.

`OIDC_ISSUER_URL` is the provider discovery document, for example `https://keycloak.example.com/realms/kubecost/.well-known/openid-configuration`. `OIDC_REQUIRED_SCOPES` defaults to `openid,profile`. `OIDC_AUDIENCE` is optional and should only be set when the provider issues access tokens for that API audience.

FastMCP stores registrations, grants, and tokens through an encrypted FileTreeStore. Keep `OIDC_JWT_SIGNING_KEY` and `OIDC_STORAGE_ENCRYPTION_KEY` stable in production. The Helm chart automatically creates a single-writer PVC in OIDC mode unless persistence is explicitly disabled.

### Client registration — priority order and mechanisms

MCP clients use one of three registration paths, tried in priority order:

1. **Pre-registration** — The client already holds credentials issued out of band and skips registration. Under this proxy the only such path is a client that presents the upstream OAuth app's own `client_id` directly; FastMCP synthesizes a client record for it on the fly. There is no operator-facing way to seed other pre-registered clients.
2. **CIMD — Client ID Metadata Document** — The client presents an `https://` URL as its `client_id`. FastMCP fetches the CIMD document from that URL, validates `redirect_uris`, and stores the resolved client in the same encrypted FileTreeStore DCR uses, refreshing it on later requests according to the document's HTTP cache headers.
3. **DCR — Dynamic Client Registration, RFC 7591** — The client sends a `POST /oauth/mcp/register` request. The server derives a deterministic `client_id` from the registration metadata (see below), writes an encrypted file to `OIDC_STORAGE_PATH`, and returns 201. This path is deprecated in the spec but supported for clients that do not yet implement CIMD.

Which path a given MCP client takes is decided by the client. Check the server logs during a live handshake rather than assuming; `Client registered with redirect_uri` marks DCR, and a `client_id` that is an `https://` URL marks CIMD.

#### CIMD egress requirement

With CIMD enabled (the default), the `mcp-kubecost` pod fetches client metadata from arbitrary `https://` origins — not only the upstream IdP. If your cluster applies a restrictive `NetworkPolicy`, ensure the pod has egress to TCP/443 for any CIMD client origin your users will present. The chart provides opt-in `networkPolicy` rules; see [the Envoy Gateway NetworkPolicy examples](../examples/network-policies/README.md).

### Downstream client redirects

The default Open posture supports standards-compatible DCR and CIMD clients. For an enterprise Restricted posture, set `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` to a JSON array of approved FastMCP redirect patterns. An unset value remains Open; `[]` deliberately denies every downstream redirect.

This allowlist applies to **both DCR and CIMD clients**. Inspect `Client registered with redirect_uri` logs and test every supported MCP client before enabling it.

CIMD metadata validation and SSRF protection belong to FastMCP. The server accepts any valid CIMD client URL; there is no custom hostname allowlist. OAuth consent and redirect validation still apply.

### DCR derived client ID

The DCR path derives a stable `client_id` from an HMAC of the registration metadata keyed on `OIDC_STORAGE_ENCRYPTION_KEY`. This means:

- The same `redirect_uris`, `client_name`, `grant_types`, `response_types`, `scope`, and `token_endpoint_auth_method` always yield the same `client_id` for a given key.
- A key rotation changes every derived ID. Clients using stored sessions must re-register.
- CIMD clients never pass through this path; their `client_id` is the metadata document URL.

Client files, DCR and CIMD alike, accumulate in `OIDC_STORAGE_PATH` for as long as the same encryption key is in use. FastMCP does not expire them automatically. With a stable key, each distinct registration can add storage; rate limiting slows abuse but does not cap total disk usage. Monitor volume capacity. If you redeploy with an ephemeral key (no `OIDC_STORAGE_ENCRYPTION_KEY` set), the directory is wiped at startup automatically.

`POST /oauth/mcp/register` is unauthenticated, so one process-wide token bucket limits it to 120 requests per minute (burst 60). All clients share this budget regardless of source IP or forwarded headers. Excess requests receive `429` with `Retry-After: 60`. The normal single-process pod has one bucket; additional processes or replicas have independent budgets. This limits throughput, not cumulative storage growth or per-client fairness. NetworkPolicy cannot replace this HTTP limit.

The chart disables forwarded-header trust by default (`FORWARDED_ALLOW_IPS=""`). Optional `config.forwardedAllowIps` controls uvicorn's interpretation of forwarded client addresses and schemes; it neither authorizes clients nor changes registration budgets. Trust only proxies that overwrite client-supplied headers.

The chart enables strict FastMCP Host/Origin validation, deriving public allowlists from `config.externalUrl` and enabled route hostnames. Additional browser clients can be admitted with `config.fastmcpHttpAllowedOrigins`. Native clients without an Origin header remain supported. See [the chart HTTP guard configuration](../../charts/mcp-kubecost/README.md#http-host-and-origin-validation). Outside Helm, configure `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=true` and the public Host/Origin allowlists explicitly as shown in `.env.example`.

### Shared Kubecost frontend hostname

The Kubecost frontend proxy must send the MCP and FastMCP OAuth surfaces to the `mcp-kubecost` Service without Kubecost's UI `auth_request`. Every path below is forwarded verbatim — no prefix stripping or rewriting — because the server itself serves `/mcp`, `/oauth/mcp/*`, and the well-known metadata paths at those exact public paths.

```nginx
# Protected resource.
location = /mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# OAuth operations (/authorize, /token, /register, /revoke, /consent, /callback).
location ^~ /oauth/mcp/ {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# RFC 9728 protected-resource discovery.
location = /.well-known/oauth-protected-resource/mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# RFC 8414 path-aware authorization-server discovery.
location = /.well-known/oauth-authorization-server/oauth/mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# OIDC-style discovery alias, same metadata.
location = /.well-known/openid-configuration/oauth/mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Root-probe fallbacks for clients that lost the resource_metadata hint.
# Optional on a shared hostname, and only safe while nothing else on that
# hostname serves these paths — forwarding them would shadow it.
location = /.well-known/oauth-protected-resource {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /.well-known/oauth-authorization-server {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /.well-known/openid-configuration {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Keep Kubecost's existing `/auth`, `/login`, and `/oidc` routes unchanged.

The exact nginx configuration belongs in the parent Kubecost chart because that chart owns the shared frontend.

If the MCP has a dedicated hostname, set `MCP_EXTERNAL_URL=https://mcp.example.com` there — the same fixed `/mcp` and `/oauth/mcp` paths apply.

### Unauthenticated HTTP paths

These custom routes are not wrapped in OAuth middleware:

| Path | Purpose |
| --- | --- |
| `GET /health` | Liveness and readiness probes |
| `GET /version` | Package version |
| `GET /favicon.ico` | Browser favicon fallback |

Do not point Kubernetes probes at `/mcp`.

## Configuration

Templates: [`.env.example`](../../.env.example) and [`charts/mcp-kubecost/values.yaml`](../../charts/mcp-kubecost/values.yaml).

| Environment variable | Helm value | Role |
| --- | --- | --- |
| `AUTH_MODE` | `config.authMode` | `none`, `open`, `oidc`, or `api_key` |
| `OIDC_ISSUER_URL` | `config.oidc.issuerUrl` | Upstream provider discovery URL |
| `OIDC_CLIENT_ID` | `config.oidc.clientID` or Secret | Upstream confidential client ID |
| `OIDC_CLIENT_SECRET` | `config.oidc.clientSecret` or Secret | Upstream confidential client secret |
| `MCP_EXTERNAL_URL` | `config.externalUrl` | Public origin, no path; e.g. `https://host`. Derives the fixed `/mcp` and `/oauth/mcp` URLs |
| `OIDC_REQUIRED_SCOPES` | `config.oidc.requiredScopes` | Provider scopes; default `openid,profile` |
| `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` | `config.oidc.allowedClientRedirectUris` | Optional downstream MCP-client callback allowlist (DCR and CIMD) |
| `FORWARDED_ALLOW_IPS` | `config.forwardedAllowIps` (default `""`) | Optional trusted proxy IPs/CIDRs; empty disables forwarded-header trust |
| `OIDC_AUDIENCE` | `config.oidc.audience` | Optional upstream API audience |
| `OIDC_STORAGE_PATH` | fixed by chart | Encrypted OAuth state directory |
| `OIDC_JWT_SIGNING_KEY` | `config.oidc.jwtSigningKey` or Secret | Stable FastMCP signing key |
| `OIDC_STORAGE_ENCRYPTION_KEY` | `config.oidc.storageEncryptionKey` or Secret | Stable Fernet key for stored state |

The `/mcp` route is fixed by [`otel_entrypoint.py`](../../src/mcp_kubecost/otel_entrypoint.py), which always launches `fastmcp run` with `--path /mcp`. Running `uv run fastmcp run config/fastmcp-http.json` directly bypasses that entrypoint but defaults to the same `/mcp` path.

### Helm example

```bash
helm upgrade --install mcp-kubecost ./charts/mcp-kubecost \
  --namespace mcp-kubecost --create-namespace \
  --set config.kubecostApiBaseUrl=https://kubecost.example.com \
  --set config.kubecostApiPort=443 \
  --set config.authMode=oidc \
  --set config.oidc.issuerUrl=https://keycloak.example.com/realms/kubecost/.well-known/openid-configuration \
  --set config.externalUrl=https://kubecost.example.com \
  --set config.oidc.existingSecret=mcp-oidc
```

The referenced Secret must contain `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. Production deployments should also provide `OIDC_JWT_SIGNING_KEY` and `OIDC_STORAGE_ENCRYPTION_KEY`.

## Pod hardening and TLS

The chart defaults to a non-root UID/GID, RuntimeDefault seccomp, a read-only root filesystem, dropped capabilities, disabled privilege escalation, and no service-account token. OAuth state is written only to its dedicated volume.

### Custom CAs

There are two mechanisms, and they reach different traffic. This server makes two kinds of outbound TLS connection — FastMCP's own client fetches the OIDC discovery document, and the Kubecost client calls the cost APIs — so which one you need depends on what is privately signed.

**`global.updateCaTrust` — a private CA that must cover everything.** An init container merges the certificates you supply with the image's public roots and writes the result to the trust store both clients verify against, so the CA is trusted *in addition to* the public roots, for the identity provider and Kubecost alike. The keys mirror the Kubecost umbrella chart, so an umbrella install that already sets `global.updateCaTrust` covers this pod with no MCP-specific value.

```bash
kubectl create secret generic corporate-ca --from-file=ca.crt=corporate-root.pem -n mcp-kubecost
helm upgrade --install mcp-kubecost kubecost/mcp-kubecost \
  --set global.updateCaTrust.enabled=true \
  --set global.updateCaTrust.caCertsSecret=corporate-ca
```

Set exactly one of `caCertsSecret` and `caCertsConfig` (a ConfigMap); the chart fails the render otherwise. Either may hold more than one PEM certificate. Unlike the parent chart, the init container runs as the pod's non-root UID — `trust extract` needs no root — so `global.updateCaTrust.securityContext` is deliberately ignored and the OpenShift restricted-v2 path keeps working.

**`config.ssl.caBundle` — a narrow Kubecost-only override.** The chart mounts the Secret read-only and sets `SSL_CA_BUNDLE`, which makes the Kubecost client trust that bundle *instead of* the public roots. It has no effect on OIDC discovery. Use it only when Kubecost alone is privately signed and you want nothing else trusted.

```bash
helm upgrade --install mcp-kubecost kubecost/mcp-kubecost \
  --set config.ssl.caBundle.existingSecret=kubecost-ca \
  --set config.ssl.caBundle.key=ca.crt
```

Without either, both clients verify against the operating system trust store in the image, which carries the public roots.

## STDIO vs HTTP

OIDC and inbound headers apply only to HTTP transport. STDIO clients cannot send an inbound `X-API-KEY`; they can still use the process-wide `KUBECOST_API_KEY` for outbound Kubecost calls.

## Troubleshooting

**Browser or client receives Kubecost HTML from an OAuth or discovery URL**

The frontend routed that URL through Kubecost UI authentication. Verify all the MCP/OAuth proxy locations above bypass `auth_request` and point to the MCP Service.

**Keycloak reports `invalid_redirect_uri`**

Register `{MCP_EXTERNAL_URL}/oauth/mcp/callback` exactly, e.g. `https://host/oauth/mcp/callback`. Do not add downstream MCP-client callbacks at Keycloak.

**Client discovers an issuer at `/mcp` or requests `/mcp/register`**

The client is talking to the wrong issuer. Confirm `MCP_EXTERNAL_URL` is set and that discovery responses advertise `{MCP_EXTERNAL_URL}/oauth/mcp` as the authorization server, not `{MCP_EXTERNAL_URL}/mcp`. OAuth state created under a previous issuer layout is incompatible and should be discarded — this chart does not migrate it automatically.

**Pod cannot write `/var/lib/mcp-kubecost/oauth`**

Confirm the PVC is Bound and the pod security context retains an appropriate writable `fsGroup`.

**Probes return 401**

Use `/health`, not `/mcp`.

**Clients receive `429` from `/oauth/mcp/register` after a restart**

Registration shares one process-wide budget; honor `Retry-After: 60` after HTTP 429. Proxy-header trust does not change the budget. Set a stable `OIDC_STORAGE_ENCRYPTION_KEY` to preserve registrations across restarts.

**OIDC initialization reports HTML discovery metadata**

`OIDC_ISSUER_URL` must point to the upstream provider's JSON discovery document, not a login page or this server's OAuth metadata.

## Upstream spec tracking

The items below are gaps in FastMCP `3.4.x` relative to the 2026-07-28 MCP Authorization spec. They are tracked here to avoid confusing upstream gaps with local ones. No local patch is needed — fix-it when the upstream version ships.

| Item | Spec level | Status |
| --- | --- | --- |
| RFC 9207 `iss` parameter in authorization responses | SHOULD (flagged to become MUST) | Missing from FastMCP. The callback redirect does not carry `iss` and `authorization_response_iss_parameter_supported` is absent from the AS metadata. |
| `scope` in `WWW-Authenticate` 401 challenge | SHOULD | FastMCP emits only `resource_metadata`. Once available, wire `OIDC_REQUIRED_SCOPES` into it. |

Watch [FastMCP releases](https://gofastmcp.com/updates) for these items. Bump the `>=4.0.9,<5.0` pin and re-run the full suite (including `just check-consent-branding`) against every FastMCP minor release.
