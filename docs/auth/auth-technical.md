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

With CIMD enabled (the default), the `mcp-kubecost` pod fetches client metadata from arbitrary `https://` origins — not only the upstream IdP. If your cluster applies a restrictive `NetworkPolicy`, ensure the pod has egress to TCP/443 for any CIMD client origin your users will present. The chart ships no `NetworkPolicy` by default; this is a note for operators who add their own.

### Downstream client redirects

The default Open posture supports standards-compatible DCR and CIMD clients. For an enterprise Restricted posture, set `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` to a JSON array of approved FastMCP redirect patterns. An unset value remains Open; `[]` deliberately denies every downstream redirect.

This allowlist applies to **both DCR and CIMD clients**. Inspect `Client registered with redirect_uri` logs and test every supported MCP client before enabling it.

To restrict which CIMD client origins the server accepts, set `OIDC_ALLOWED_CIMD_ORIGINS` to a JSON array of bare hostnames (no scheme, port, or path; matching is case-insensitive). An unset value (Open) accepts any `https://` CIMD client ID; `[]` denies all CIMD clients. Example: `["client.corp.example", "mcp-client.example"]`. Tightening the list also evicts previously stored CIMD clients from those origins on their next request, so no storage wipe is needed. Rejections are logged once per hostname at WARNING.

### DCR derived client ID

The DCR path derives a stable `client_id` from an HMAC of the registration metadata keyed on `OIDC_STORAGE_ENCRYPTION_KEY`. This means:

- The same `redirect_uris`, `client_name`, `grant_types`, `response_types`, `scope`, and `token_endpoint_auth_method` always yield the same `client_id` for a given key.
- A key rotation changes every derived ID. Clients using stored sessions must re-register.
- CIMD clients never pass through this path; their `client_id` is the metadata document URL.

Client files, DCR and CIMD alike, accumulate in `OIDC_STORAGE_PATH` for as long as the same encryption key is in use. FastMCP does not expire them automatically. With a stable key, the growth rate is bounded by the number of distinct clients — typically a few files per deployment. If you redeploy with an ephemeral key (no `OIDC_STORAGE_ENCRYPTION_KEY` set), the directory is wiped at startup automatically.

`POST /oauth/mcp/register` is unauthenticated, so the server rate-limits it: 30 requests per minute per client IP (burst 15) and 120 per minute per pod (burst 60). Excess requests receive `429` with `Retry-After: 60`. The chart disables forwarded-header trust by default (`FORWARDED_ALLOW_IPS=""`), including with Ingress or HTTPRoute enabled. No proxy IP configuration is required to install or log in. The per-IP key is the TCP peer, so clients behind the same proxy share its budget. Operators who know their proxy IPs or CIDRs can opt in through `config.forwardedAllowIps` (for example, `"10.0.1.10,10.0.2.0/24"`). This controls uvicorn's handling of both `X-Forwarded-For` and `X-Forwarded-Proto`; it does not restrict who may connect. Use `"*"` only when every route to the pod passes through a proxy that overwrites client-supplied forwarded headers.

The per-IP bucket map holds at most 1,000 entries. When full, idle entries are reclaimed; if all entries are active, new IPs receive 429 until space is available. Requests rejected by the global limit allocate no per-IP state.

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

### Consent screen branding

The OAuth consent and error pages are Kubecost-branded automatically in OIDC mode. The logo, favicon, and CSS are inline so the pages need no extra proxy routes or external network access. FastMCP continues to own the form, CSRF protection, cookies, and transaction fields.

Verify the served flow with:

```bash
just check-consent-branding
```

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
| `OIDC_ALLOWED_CIMD_ORIGINS` | `config.oidc.allowedCimdOrigins` | Optional JSON array of allowed CIMD client hostnames |
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

For a custom CA, set `config.ssl.caBundle.existingSecret` and `key`. The chart mounts it read-only and sets `SSL_CA_BUNDLE`.

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

Registration is rate-limited per TCP peer by default. Clients behind a Gateway or Ingress share its budget; honor `Retry-After: 60` if a burst exceeds it. If you know the proxy IPs or CIDRs, optionally set `config.forwardedAllowIps` (or `FORWARDED_ALLOW_IPS` outside Helm) to give forwarded client addresses independent budgets. Leave it empty when those addresses are unknown. Set a stable `OIDC_STORAGE_ENCRYPTION_KEY` to preserve registrations across restarts.

**OIDC initialization reports HTML discovery metadata**

`OIDC_ISSUER_URL` must point to the upstream provider's JSON discovery document, not a login page or this server's OAuth metadata.

## Upstream spec tracking

The items below are gaps in FastMCP `3.4.x` relative to the 2026-07-28 MCP Authorization spec. They are tracked here to avoid confusing upstream gaps with local ones. No local patch is needed — fix-it when the upstream version ships.

| Item | Spec level | Status |
| --- | --- | --- |
| RFC 9207 `iss` parameter in authorization responses | SHOULD (flagged to become MUST) | Missing from FastMCP. The callback redirect does not carry `iss` and `authorization_response_iss_parameter_supported` is absent from the AS metadata. |
| `scope` in `WWW-Authenticate` 401 challenge | SHOULD | FastMCP emits only `resource_metadata`. Once available, wire `OIDC_REQUIRED_SCOPES` into it. |

Watch [FastMCP releases](https://gofastmcp.com/updates) for these items. Bump the `>=3.4.7,<4.0` pin and re-run the full suite (including `just check-consent-branding`) against every FastMCP minor release.
