# Authentication — Technical Reference<!-- omit in toc -->

This page is the technical reference for MCP OIDC, reverse-proxy routing, Helm configuration, and troubleshooting. For the auth-mode overview, see [README.md](README.md).

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

### Downstream client redirects

The default Open posture supports standards-compatible DCR and client ID metadata document clients. For an enterprise Restricted posture, set `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` to a JSON array of approved FastMCP redirect patterns. An unset value remains Open; `[]` deliberately denies every downstream redirect.

This allowlist does not change the IdP callback. Inspect `Client registered with redirect_uri` logs and test every supported MCP client before enabling it.

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
| `OIDC_CLIENT_ID` | `config.oidc.clientId` or Secret | Upstream confidential client ID |
| `OIDC_CLIENT_SECRET` | `config.oidc.clientSecret` or Secret | Upstream confidential client secret |
| `MCP_EXTERNAL_URL` | `config.externalUrl` | Public origin, no path; e.g. `https://host`. Derives the fixed `/mcp` and `/oauth/mcp` URLs |
| `OIDC_REQUIRED_SCOPES` | `config.oidc.requiredScopes` | Provider scopes; default `openid,profile` |
| `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` | `config.oidc.allowedClientRedirectUris` | Optional downstream MCP-client callback allowlist |
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

**OIDC initialization reports HTML discovery metadata**

`OIDC_ISSUER_URL` must point to the upstream provider's JSON discovery document, not a login page or this server's OAuth metadata.
