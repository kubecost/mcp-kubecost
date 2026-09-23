# Connecting an AI Assistant<!-- omit in toc -->

Point a client at the server's `/mcp` endpoint. Deploying the server: [the Helm chart](../../charts/mcp-kubecost/README.md). Protecting it: [docs/auth](../auth/README.md).

| Server `AUTH_MODE` | Client sends                  |
| ------------------ | ----------------------------- |
| `none` / `open`    | Nothing                       |
| `oidc`             | OAuth sign-in                 |
| `api_key`          | `X-API-KEY` header            |

All 11 tools are read-only, so no client can change your cluster.

## Claude (web and desktop)

**Settings → Connectors → Add → Add custom connector.** Name it, paste the URL, pick **No sign-in** (`none`/`open`/`api_key`) or **Sign in now** (`oidc`). For `api_key`, add an `X-API-KEY` request header.

![Claude's "Add custom connector" dialog: the connector named "Kubecost Demo MCP", URL https://demo.kubecost.xyz/mcp, "No sign-in" selected](images/claude-add-custom-connector.png)

All 11 tools should appear. Zero tools means the URL is missing `/mcp`.

![The connected connector listing 11 read-only tools with allow/ask/deny controls](images/claude-connector-tools.png)

Now ask: _"Where are my biggest savings opportunities?"_ ([more examples](../../README.md#examples-of-what-you-can-ask))

## Claude Code

```bash
# remote
claude mcp add --transport http kubecost https://kubecost.example.com/mcp

# remote, api_key mode
claude mcp add --transport http kubecost https://kubecost.example.com/mcp \
  --header "X-API-KEY: ${KUBECOST_API_KEY}"

# local, against a port-forward
claude mcp add kubecost --env KUBECOST_BASE_URL=http://localhost:9090 \
  -- uv run --directory /path/to/mcp-kubecost mcp-kubecost

claude mcp list
```

## ChatGPT

**Settings → Connectors**, developer mode, same `/mcp` URL. OpenAI connects from its own infrastructure, so the URL must be public HTTPS with a public CA certificate — a port-forward or internal DNS name will not work. Use `oidc`.

## Other clients

```json
{
  "mcpServers": {
    "mcp-kubecost": {
      "type": "streamable-http",
      "url": "https://kubecost.example.com/mcp"
    }
  }
}
```

```json
{
  "mcpServers": {
    "mcp-kubecost": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-kubecost", "mcp-kubecost"],
      "env": { "KUBECOST_BASE_URL": "http://localhost:9090" }
    }
  }
}
```

Working copies: [`config/mcp-http.json`](../../config/mcp-http.json), [`config/mcp-http-public.json`](../../config/mcp-http-public.json).

## Troubleshooting

Reproduce the client's request first. If these fail, the server or network is at fault, not the client:

```bash
curl -fsS https://kubecost.example.com/health

npx @modelcontextprotocol/inspector \
  --cli https://kubecost.example.com/mcp --method tools/list
```

| Symptom                      | Cause                                                                                    |
| ---------------------------- | ---------------------------------------------------------------------------------------- |
| Connects, no tools           | URL missing `/mcp`                                                                       |
| `401` or repeated sign-ins   | Client auth does not match `AUTH_MODE`                                                   |
| Tool calls all fail          | Server cannot reach Kubecost — check `KUBECOST_BASE_URL`, `KUBECOST_API_KEY`             |
| Long queries time out        | Gateway timeout shorter than the query ([chart README](../../charts/mcp-kubecost/README.md)) |
| Host or origin rejected      | Add the public origin to `config.externalUrl`                                            |
