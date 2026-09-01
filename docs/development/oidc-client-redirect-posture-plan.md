# OIDC client redirect posture implementation plan

## Goal

Move the MCP OAuth client redirect posture from the current project-maintained
vendor allowlist to **Open by default**, while giving operators an explicit
**Restricted** posture through an optional `OIDC_ALLOWED_CLIENT_REDIRECT_URIS`
setting.

This setting controls downstream MCP client callbacks. It does **not** replace
or alter the upstream identity-provider callback:
`{OIDC_BASE_URL}{OIDC_REDIRECT_PATH}`.

## Intended behavior

| Configuration | FastMCP value | Result |
| --- | --- | --- |
| Variable unset or blank | `None` | Open: accept ordinary safe client redirects under FastMCP DCR/CIMD validation. |
| JSON array of patterns | Non-empty `list[str]` | Restricted: only callbacks matching the configured patterns are allowed. |
| `[]` | Empty `list[str]` | Explicitly reject all non-null client redirects; preserve this distinction rather than treating it as Open. |

FastMCP is pinned at 3.4.7 in `uv.lock` and supports these semantics. Its
OAuth proxy documentation also recommends using `Client registered with
redirect_uri` log messages to discover real client callbacks before creating a
Restricted allowlist.

## Implementation checklist

### 1. Runtime configuration

- [x] In `src/mcp_kubecost/config/settings.py`, add
  `oidc_allowed_client_redirect_uris: list[str] | None` to `Settings` with the
  other OIDC settings.
- [x] Add an `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` parser that accepts a JSON
  array of strings.
- [x] Make an unset or whitespace-only value return `None`.
- [x] Preserve configured `[]` as an empty list, not `None`.
- [x] Reject invalid JSON, non-array values, and non-string list entries with
  an actionable `ConfigError`.
- [x] Populate the new field from `get_settings()`.
- [x] Confirm `Settings.to_loggable_dict()` can retain this non-secret setting
  without redaction.

### 2. OIDC provider behavior

- [x] In `src/mcp_kubecost/config/oidc.py`, remove
  `ALLOWED_CLIENT_REDIRECT_URIS`.
- [x] Remove the `DEFAULT_LOCALHOST_PATTERNS` import and the built-in Claude and
  ChatGPT redirect patterns.
- [x] Pass `settings.oidc_allowed_client_redirect_uris` directly as
  `allowed_client_redirect_uris` when constructing `AdaptiveOidcProxy`.
- [x] Retain the existing upstream IdP redirect-path behavior and documentation
  distinction; do not add MCP-client callback patterns to IdP configuration.
- [x] Keep consent, encrypted storage, token handling, and opaque-token
  verification behavior unchanged.

### 3. Python tests

- [x] Update direct `Settings(...)` fixtures in `tests/test_oidc.py`,
  `tests/test_client.py`, and `tests/test_auth.py` for the new field.
- [x] Add `tests/test_settings.py` coverage for unset, blank, valid JSON,
  explicit empty array, malformed JSON, scalar JSON, and non-string entries.
- [x] Update `tests/test_oidc.py` to assert that the default configuration
  forwards `None` to `AdaptiveOidcProxy`.
- [x] Add a configured-list forwarding assertion and an explicit empty-list
  forwarding assertion.
- [x] Replace tests tied to the deleted hard-coded allowlist with FastMCP
  redirect-validation coverage: Open accepts an ordinary valid HTTPS callback
  while unsafe schemes remain rejected; Restricted accepts listed patterns and
  rejects others; `[]` rejects redirects.
- [x] Retain the FastMCP constructor-signature conformance test for
  `allowed_client_redirect_uris`.

### 4. Helm chart

- [x] Add `config.oidc.allowedClientRedirectUris: ""` to
  `charts/mcp-kubecost/values.yaml`, directly after `requiredScopes`.
- [x] Document that an empty value is Open, it concerns MCP-client callbacks
  rather than the IdP callback, and a Restricted value is a JSON-array string.
- [x] Provide the former localhost/Claude/ChatGPT patterns only as a commented
  example, never as the default policy.
- [x] Add `config.oidc.allowedClientRedirectUris` as an optional string to
  `charts/mcp-kubecost/values.schema.json`.
- [x] In `charts/mcp-kubecost/templates/configmap.yaml`, emit
  `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` only when the Helm value is nonempty.
- [x] Confirm no Deployment-template change is required because it imports the
  ConfigMap using `envFrom`.
- [x] Extend `.github/workflows/helm.yml` to assert the variable is absent in
  the default/Open render and exact in a Restricted render.

### 5. Customer documentation

- [x] Add the optional setting, Open default, JSON-array format, and a
  commented Restricted example to `.env.example`.
- [x] Update `docs/auth/README.md` to remove the claim that the server
  hard-codes a localhost/Claude/ChatGPT allowlist.
- [x] Document the Open/default and Restricted/operator-controlled postures,
  including their security and compatibility tradeoff.
- [x] Add `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` /
  `config.oidc.allowedClientRedirectUris` to the environment-to-Helm table.
- [x] Add a Helm Restricted-mode values example.
- [x] Document the rollout procedure: begin Open, inspect
  `Client registered with redirect_uri` logs, form the allowlist from real
  clients, configure it, then test each supported client.
- [x] Preserve the guidance that only
  `{OIDC_BASE_URL}{OIDC_REDIRECT_PATH}` is registered at the identity provider.
- [x] Keep `docs/auth/oidc-client-sharing.md` focused on upstream IdP client
  callback registration; a cross-reference is not needed because the primary
  OIDC guide now distinguishes the two callback controls.

### 6. Release and migration

- [x] Document the intentional behavior change from a fixed vendor allowlist to
  Open-by-default DCR/CIMD compatibility in `docs/auth/README.md`.
- [x] Provide the prior patterns as a copyable Restricted-mode example for
  operators who need equivalent continuity.
- [x] Explain that `[]` is an intentional deny-all state, not an alternate
  spelling for Open.

### 7. Validation

- [x] Run `.venv/bin/ruff format .`.
- [x] Run `.venv/bin/ruff check . --fix`.
- [x] Run `.venv/bin/pyrefly check`.
- [x] Run `.venv/bin/pytest tests/test_settings.py tests/test_oidc.py tests/test_client.py tests/test_auth.py`.
- [x] Run Helm lint/render validation, including Open and Restricted render
  assertions added to `.github/workflows/helm.yml`.
- [ ] Perform an IdP-backed OIDC smoke test with an unset setting and at least
  one configured client pattern. This requires a provisioned identity provider
  and client credentials, which are not available in this workspace.

## Completion record

Update the checkboxes above in the same change that completes each item. Do not
mark an item complete solely because a command exited successfully: verify the
specific behavior described by the item.
