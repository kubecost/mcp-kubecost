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

The corresponding settings are:

```dotenv
AUTH_MODE=oidc
MCP_HTTP_PATH=/mcp
OIDC_BASE_URL=https://kubecost.example.com/oauth/mcp
OIDC_RESOURCE_BASE_URL=https://kubecost.example.com
OIDC_REDIRECT_PATH=/callback
```

FastMCP appends `MCP_HTTP_PATH` to `OIDC_RESOURCE_BASE_URL`, so the resource identifier is `https://kubecost.example.com/mcp`. It appends `OIDC_REDIRECT_PATH` to `OIDC_BASE_URL`, so the IdP callback is `https://kubecost.example.com/oauth/mcp/callback`.

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

The Kubecost frontend proxy must send the MCP and FastMCP OAuth surfaces to the `mcp-kubecost` Service without Kubecost's UI `auth_request`. The proxy keeps `/mcp` intact, but strips `/oauth/mcp` before forwarding OAuth operational routes because FastMCP mounts them at its application root.

```nginx
# Protected resource. Preserve /mcp for MCP_HTTP_PATH=/mcp.
location = /mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# OAuth operations. The trailing slash strips /oauth/mcp/.
location ^~ /oauth/mcp/ {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# RFC 9728 protected-resource discovery. Preserve the request URI.
location = /.well-known/oauth-protected-resource/mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# RFC 8414 path-aware discovery. FastMCP mounts the internal route at root.
location = /.well-known/oauth-authorization-server/oauth/mcp {
    proxy_pass http://mcp-kubecost.mcp-kubecost.svc.cluster.local:3030/.well-known/oauth-authorization-server;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

If the deployed FastMCP version advertises an OpenID discovery fallback, route `/.well-known/openid-configuration/oauth/mcp` to its root `/.well-known/openid-configuration` route the same way. Keep Kubecost's existing `/auth`, `/login`, and `/oidc` routes unchanged.

The exact nginx configuration belongs in the parent Kubecost chart because that chart owns the shared frontend.

If the MCP has a dedicated hostname, use the same logical separation there. For example, `OIDC_RESOURCE_BASE_URL=https://mcp.example.com`, `MCP_HTTP_PATH=/mcp`, and `OIDC_BASE_URL=https://mcp.example.com/oauth/mcp`.

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
| `OIDC_BASE_URL` | `config.oidc.baseUrl` | Public FastMCP authorization-server base; recommended `https://host/oauth/mcp` |
| `OIDC_RESOURCE_BASE_URL` | `config.oidc.resourceBaseUrl` | Public base that hosts `MCP_HTTP_PATH`; recommended `https://host` |
| `OIDC_REDIRECT_PATH` | `config.oidc.redirectPath` | Callback relative to `OIDC_BASE_URL`; default `/callback` |
| `MCP_HTTP_PATH` | `config.http.path` | Protected-resource route; default `/mcp` |
| `OIDC_REQUIRED_SCOPES` | `config.oidc.requiredScopes` | Provider scopes; default `openid,profile` |
| `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` | `config.oidc.allowedClientRedirectUris` | Optional downstream MCP-client callback allowlist |
| `OIDC_AUDIENCE` | `config.oidc.audience` | Optional upstream API audience |
| `OIDC_STORAGE_PATH` | fixed by chart | Encrypted OAuth state directory |
| `OIDC_JWT_SIGNING_KEY` | `config.oidc.jwtSigningKey` or Secret | Stable FastMCP signing key |
| `OIDC_STORAGE_ENCRYPTION_KEY` | `config.oidc.storageEncryptionKey` or Secret | Stable Fernet key for stored state |

`MCP_HTTP_PATH` is read by [`otel_entrypoint.py`](../../src/mcp_kubecost/otel_entrypoint.py) before the server process starts. Running `uv run fastmcp run config/fastmcp-http.json` directly bypasses that entrypoint; pass `--path` explicitly in that case.

### Helm example

```bash
helm upgrade --install mcp-kubecost ./charts/mcp-kubecost \
  --namespace mcp-kubecost --create-namespace \
  --set config.kubecostApiBaseUrl=https://kubecost.example.com \
  --set config.kubecostApiPort=443 \
  --set config.authMode=oidc \
  --set config.oidc.issuerUrl=https://keycloak.example.com/realms/kubecost/.well-known/openid-configuration \
  --set config.oidc.baseUrl=https://kubecost.example.com/oauth/mcp \
  --set config.oidc.resourceBaseUrl=https://kubecost.example.com \
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

The frontend routed that URL through Kubecost UI authentication. Verify all four MCP/OAuth proxy locations above bypass `auth_request` and point to the MCP Service.

**Keycloak reports `invalid_redirect_uri`**

Register `{OIDC_BASE_URL}{OIDC_REDIRECT_PATH}` exactly. With recommended defaults that is `https://host/oauth/mcp/callback`. Do not add downstream MCP-client callbacks at Keycloak.

**Client discovers an issuer at `/mcp` or requests `/mcp/register`**

The old coupled layout is still configured. Set `OIDC_BASE_URL=https://host/oauth/mcp`, `OIDC_RESOURCE_BASE_URL=https://host`, and keep `MCP_HTTP_PATH=/mcp`. Existing MCP OAuth state may reference the old issuer and should be treated as incompatible during this pre-release migration.

**Pod cannot write `/var/lib/mcp-kubecost/oauth`**

Confirm the PVC is Bound and the pod security context retains an appropriate writable `fsGroup`.

**Probes return 401**

Use `/health`, not `/mcp`.

**OIDC initialization reports HTML discovery metadata**

`OIDC_ISSUER_URL` must point to the upstream provider's JSON discovery document, not a login page or this server's OAuth metadata.
