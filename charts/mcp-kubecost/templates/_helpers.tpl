{{/*
Kubecost parent-chart globals. Helm merges the parent `global` key into every
subchart, so these helpers read `.Values.global` when present and stay no-ops
for a standalone install.
*/}}

{{/* Return true when CI/CD tools (e.g. Argo CD) should skip Secret existence lookups. */}}
{{- define "mcp-kubecost.skipSanityChecks" -}}
{{- $cicd := (((.Values.global).platforms).cicd) | default dict -}}
{{- if and $cicd.enabled $cicd.skipSanityChecks -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/*
Fully qualified container image. `global.imageRegistry` overrides `image.registry`
when this chart is a Kubecost subchart (IBM sets this to icr.io for ICR-distributed images).
*/}}
{{- define "mcp-kubecost.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- $repository := .Values.image.repository -}}
{{- $registry := .Values.image.registry | default "" -}}
{{- $globalRegistry := (.Values.global).imageRegistry | default "" -}}
{{- if $globalRegistry -}}
{{- $registry = $globalRegistry -}}
{{- end -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" ($registry | trimSuffix "/") $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end }}

{{/*
imagePullSecrets from this chart plus `global.imagePullSecrets`. Accepts either
secret-name strings or {name: ...} maps, matching Kubecost's global list.
*/}}
{{- define "mcp-kubecost.imagePullSecrets" -}}
{{- $local := .Values.image.pullSecrets | default (list) -}}
{{- $fromGlobal := ((.Values.global).imagePullSecrets) | default (list) -}}
{{- $secrets := list -}}
{{- range concat $local $fromGlobal -}}
  {{- if kindIs "string" . -}}
    {{- $secrets = append $secrets . -}}
  {{- else if and (kindIs "map" .) .name -}}
    {{- $secrets = append $secrets .name -}}
  {{- end -}}
{{- end -}}
{{- $secrets = compact $secrets | uniq -}}
{{- if $secrets }}
imagePullSecrets:
  {{- range $secrets }}
  - name: {{ . }}
  {{- end }}
{{- end }}
{{- end }}

{{/*
Controller annotations from `global.annotations`. Mirrors the parent chart, where
global.annotations is applied to every Deployment/StatefulSet/DaemonSet.
*/}}
{{- define "mcp-kubecost.annotations" -}}
{{- with ((.Values.global).annotations) }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/*
Pod template annotations: `global.podAnnotations` merged with this chart's
`podAnnotations`. Chart-local keys win on conflict.
*/}}
{{- define "mcp-kubecost.podAnnotations" -}}
{{- $global := ((.Values.global).podAnnotations) | default dict -}}
{{- $local := .Values.podAnnotations | default dict -}}
{{- with merge (deepCopy $local) $global }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/*
Pod securityContext. On OpenShift the parent chart's
`global.platforms.openshift.securityContext` replaces this chart's
`podSecurityContext`, because the restricted-v2 SCC rejects an explicit
runAsUser/runAsGroup and assigns its own IDs instead.
*/}}
{{- define "mcp-kubecost.podSecurityContext" -}}
{{- $openshift := (((.Values.global).platforms).openshift) | default dict -}}
{{- if and $openshift.enabled $openshift.securityContext -}}
{{- toYaml $openshift.securityContext -}}
{{- else -}}
{{- toYaml .Values.podSecurityContext -}}
{{- end -}}
{{- end }}

{{/*
Fail on contradictory values, and when referenced existing Secrets are missing
unless CI/CD skip is on or the cluster is unreachable (helm template / dry-run
with no kube API). The routing + authMode=none check is not gated by
skipSanityChecks: that flag only skips live Secret lookups.
*/}}
{{- define "mcp-kubecost.sanityChecks" -}}
{{- $mode := .Values.config.authMode | default "none" }}
{{- $routeEnabled := or .Values.httpRoute.enabled .Values.ingress.enabled }}
{{- if and $routeEnabled (eq $mode "none") }}
{{- fail "ERROR: \n\n authMode must be configured before enabling httproute or ingress.\n Please set authMode to a valid option.\n If anonymous access is required (for example if the ingress is behind a VPN), set  (e.g., \"authMode=open\").\n authMode = \"none\" with an exposed route is not permitted." }}
{{- end }}
{{- if ne (include "mcp-kubecost.skipSanityChecks" .) "true" }}
{{- $ns := lookup "v1" "Namespace" "" .Release.Namespace }}
{{- if $ns }}
{{- $checks := list -}}
{{- if .Values.config.kubecostApiKey.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.kubecostApiKey.existingSecret "ref" "config.kubecostApiKey.existingSecret") -}}
{{- end }}
{{- if .Values.config.oidc.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.oidc.existingSecret "ref" "config.oidc.existingSecret") -}}
{{- end }}
{{- if .Values.config.ssl.caBundle.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.ssl.caBundle.existingSecret "ref" "config.ssl.caBundle.existingSecret") -}}
{{- end }}
{{- range $checks }}
{{- $secret := lookup "v1" "Secret" $.Release.Namespace .name }}
{{- if not $secret }}
{{- fail (printf "%s %q was not found in namespace %s; create the Secret or set global.platforms.cicd.enabled and global.platforms.cicd.skipSanityChecks for Argo CD / similar tools" .ref .name $.Release.Namespace) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}

{{/* Canonical FastMCP OAuth callback path. Mirrors _get_oidc_redirect_path(). */}}
{{- define "mcp-kubecost.oidcRedirectPath" -}}
{{- $raw := .Values.config.oidc.redirectPath | default "/auth-mcp" | trim }}
{{- if or (contains "://" $raw) (contains "?" $raw) (contains "#" $raw) }}
{{- fail (printf "config.oidc.redirectPath must be a path like /auth-mcp, not a URL: %s" $raw) }}
{{- end }}
{{- if contains ".." $raw }}
{{- fail (printf "config.oidc.redirectPath must not contain '..': %s" $raw) }}
{{- end }}
{{- $path := printf "/%s" (trimAll "/" $raw) }}
{{- if eq $path "/" }}
{{- fail "config.oidc.redirectPath must be a dedicated callback path, not '/'" }}
{{- end }}
{{- $path }}
{{- end }}

{{/*
Path component of config.oidc.baseUrl, normalised without a trailing slash.
Empty when baseUrl is unset, points at the root of a host, or authMode is not
"oidc".

A non-empty value means this server is deployed behind a reverse proxy that
strips that prefix: FastMCP advertises {baseUrl}/authorize, {baseUrl}/token and
{baseUrl}/register in its OAuth metadata, so the proxy must map
{prefix}/authorize to /authorize on this Service.

Gated on authMode == "oidc" because baseUrl is only read at runtime in that
mode. Without the gate, a deployment that switched to api_key but kept its old
baseUrl would silently relocate its MCP endpoint from /mcp to /.

The scheme check lives here rather than in validateOIDC because validateOIDC is
only reachable through NOTES.txt: `helm template -s templates/configmap.yaml`
skips it, and this helper feeds that ConfigMap. Without a scheme, urlParse reads
the whole value as a path ("k.example.com/mcp" -> prefix "/k.example.com/mcp").
*/}}
{{- define "mcp-kubecost.oidcBasePathPrefix" -}}
{{- if eq (.Values.config.authMode | default "none") "oidc" }}
{{- $raw := .Values.config.oidc.baseUrl | default "" | trim }}
{{- if $raw }}
{{- if not (or (hasPrefix "https://" $raw) (hasPrefix "http://" $raw)) }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: config.oidc.baseUrl %q is missing a scheme. Set a full URL (https://kubecost.example.com or https://kubecost.example.com/mcp) — without a scheme the whole value parses as a URL path and the MCP endpoint would be relocated on the strength of a hostname.\n" $raw) }}
{{- end }}
{{- $path := (urlParse $raw).path | default "" }}
{{- $path = printf "/%s" (trimAll "/" $path) }}
{{- if ne $path "/" }}{{ $path }}{{ end }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Route the MCP endpoint is served on inside the container (MCP_HTTP_PATH,
passed through to `fastmcp run --path`).

Defaults to config.http.path. When config.oidc.baseUrl carries a path prefix the
proxy strips it, so the endpoint must be served at "/" — otherwise a stripped
request for the MCP endpoint would arrive as "/" and 404, and the RFC 9728
resource URL would double the prefix (https://host/mcp/mcp).
*/}}
{{- define "mcp-kubecost.httpPath" -}}
{{- $explicit := (((.Values.config).http).path) | default "" }}
{{- if $explicit }}
{{- $explicit }}
{{- else if (include "mcp-kubecost.oidcBasePathPrefix" .) }}
{{- "/" }}
{{- else }}
{{- "/mcp" }}
{{- end }}
{{- end }}

{{/*
Fail when a path-prefixed config.oidc.baseUrl is combined with an MCP endpoint
that is not served at "/". These two settings must agree or OAuth discovery
silently resolves to the wrong URLs.

Both checks inherit the authMode == "oidc" gate from oidcBasePathPrefix, so a
non-OIDC deployment carrying a stale baseUrl is never failed here. That matters
for the exposeAuthRoutes check in particular: ingress-oauth.yaml only renders
when authMode is "oidc", so outside that mode there is no Ingress to conflict
with and nothing to report.
*/}}
{{- define "mcp-kubecost.validateHttpPath" -}}
{{- $prefix := include "mcp-kubecost.oidcBasePathPrefix" . }}
{{- if and $prefix .Values.config.oidc.exposeAuthRoutes }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: config.oidc.exposeAuthRoutes cannot be combined with the path prefix %q in config.oidc.baseUrl. exposeAuthRoutes publishes the OAuth paths at the root of the baseUrl host for deployments that cannot change the Kubecost frontend nginx; the path prefix requires that nginx to strip the prefix. Pick one.\n" $prefix) }}
{{- end }}
{{- if and $prefix (ne (include "mcp-kubecost.httpPath" .) "/") }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: config.oidc.baseUrl has the path prefix %q, which means a reverse proxy strips that prefix before requests reach this Service. config.http.path must then be \"/\" so the MCP endpoint and the prefix-stripped OAuth paths both resolve here.\n\nTo fix, either:\n  - set config.http.path: \"/\"  (leave it empty to derive this automatically), or\n  - drop the path from config.oidc.baseUrl and serve the MCP endpoint at its own hostname.\n" $prefix) }}
{{- end }}
{{- end }}

{{/* Return the chart name, allowing a user override. */}}
{{- define "mcp-kubecost.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Return the release-scoped resource name. */}}
{{- define "mcp-kubecost.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Return the chart name and version label. */}}
{{- define "mcp-kubecost.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Standard Kubernetes metadata labels, plus the parent chart's
`global.additionalLabels`. Never used for selectors, which must stay immutable.
*/}}
{{- define "mcp-kubecost.labels" -}}
helm.sh/chart: {{ include "mcp-kubecost.chart" . }}
{{ include "mcp-kubecost.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Chart.AppVersion }}
app.kubernetes.io/version: {{ . | quote }}
{{- end }}
{{- with ((.Values.global).additionalLabels) }}
{{ toYaml . | trim }}
{{- end }}
{{- end }}

{{/* Selector labels shared by the Deployment and Service. */}}
{{- define "mcp-kubecost.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-kubecost.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Return the generated configuration ConfigMap name. */}}
{{- define "mcp-kubecost.configMapName" -}}
{{- printf "%s-config" (include "mcp-kubecost.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Return the generated API key Secret name. */}}
{{- define "mcp-kubecost.apiKeySecretName" -}}
{{- printf "%s-api-key" (include "mcp-kubecost.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Return the generated OIDC Secret name. */}}
{{- define "mcp-kubecost.oidcSecretName" -}}
{{- printf "%s-oidc" (include "mcp-kubecost.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Stable string of all ConfigMap data values used for checksum annotation.
Must stay in sync with the data block in configmap.yaml.
*/}}
{{- define "mcp-kubecost.configmap-data" -}}
KUBECOST_BASE_URL={{ printf "%s:%v" (tpl .Values.config.kubecostApiBaseUrl .) .Values.config.kubecostApiPort | quote }}
KUBECOST_API_BASE_PATH={{ .Values.config.kubecostApiBasePath | quote }}
REQUEST_TIMEOUT_SECONDS={{ .Values.config.requestTimeoutSeconds | quote }}
REQUEST_RETRY_COUNT={{ .Values.config.requestRetryCount | quote }}
DEFAULT_WINDOW={{ .Values.config.defaultWindow | quote }}
FASTMCP_LOG_LEVEL={{ .Values.config.logLevel | upper | quote }}
USE_CAC_VIEWS={{ .Values.config.useCacViews | quote }}
REQUIRE_CLIENT_API_KEY={{ (or .Values.config.requireClientApiKey (eq .Values.config.authMode "api_key")) | quote }}
MCP_SERVER_NAME={{ .Values.config.mcpServerName | quote }}
FASTMCP_TELEMETRY_MODE={{ .Values.config.telemetryMode | quote }}
OTEL_SERVICE_NAME={{ .Values.config.otelServiceName | quote }}
OTEL_METRICS_EXPORTER={{ .Values.config.otelMetricsExporter | quote }}
OTEL_LOGS_EXPORTER={{ .Values.config.otelLogsExporter | quote }}
KUBECOST_SSL_VERIFY={{ .Values.config.ssl.verify | quote }}
{{- if .Values.config.otelExporterOtlpEndpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT={{ .Values.config.otelExporterOtlpEndpoint | quote }}
{{- end }}
{{- if .Values.config.fastmcpHttpAllowedHosts }}
FASTMCP_HTTP_ALLOWED_HOSTS={{ .Values.config.fastmcpHttpAllowedHosts | quote }}
{{- end }}
{{- if .Values.config.ssl.caBundle.existingSecret }}
SSL_CA_BUNDLE={{ .Values.config.ssl.caBundle.mountPath | quote }}
{{- end }}
{{- if ne .Values.config.authMode "none" }}
AUTH_MODE={{ .Values.config.authMode | quote }}
{{- if .Values.config.oidc.issuerUrl }}
OIDC_ISSUER_URL={{ .Values.config.oidc.issuerUrl | quote }}
{{- end }}
{{- if .Values.config.oidc.audience }}
OIDC_AUDIENCE={{ .Values.config.oidc.audience | quote }}
{{- end }}
{{- if .Values.config.oidc.baseUrl }}
OIDC_BASE_URL={{ .Values.config.oidc.baseUrl | quote }}
{{- end }}
OIDC_REDIRECT_PATH={{ include "mcp-kubecost.oidcRedirectPath" . | quote }}
{{- if .Values.config.oidc.requiredScopes }}
OIDC_REQUIRED_SCOPES={{ .Values.config.oidc.requiredScopes | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Stable string of the API key Secret stringData used for checksum annotation.
Returns an empty string when the secret would not be created, so no annotation is emitted.
Must stay in sync with the first Secret block in secret.yaml.
*/}}
{{- define "mcp-kubecost.apikey-stringdata" -}}
{{- if .Values.config.kubecostApiKey.value -}}
{{ .Values.config.kubecostApiKey.key }}={{ .Values.config.kubecostApiKey.value }}
{{- end }}
{{- end }}

{{/*
Stable string of the OIDC Secret stringData used for checksum annotation.
Returns an empty string when the secret would not be created, so no annotation is emitted.
Must stay in sync with the second Secret block in secret.yaml.
*/}}
{{- define "mcp-kubecost.oidc-stringdata" -}}
{{- if and .Values.config.oidc.clientId .Values.config.oidc.clientSecret (not .Values.config.oidc.existingSecret) -}}
OIDC_CLIENT_ID={{ .Values.config.oidc.clientId }}
OIDC_CLIENT_SECRET={{ .Values.config.oidc.clientSecret }}
{{- end }}
{{- end }}

{{/*
Fail when authMode is "oidc" but no OIDC credentials are configured, or when
issuerUrl / baseUrl are missing or not https:// URLs.

Valid credential state = either (clientId AND clientSecret both non-empty)
OR existingSecret non-empty. These checks are NOT gated by skipSanityChecks:
credential presence and URL format are hard logical requirements, not live
cluster lookups, so bypassing them for CI/CD would silently produce a broken
deployment.

CONCERNS:
- No mutual-exclusivity guard: if the user sets both existingSecret AND inline
  clientId/clientSecret, this check passes. At runtime the deployment favours
  existingSecret, but secret.yaml also renders an inline Secret — unused plaintext
  credentials are committed to cluster state (secret sprawl). This is intentional:
  enforcing mutual exclusivity is not implemented here.
- Presence != validity: a clientSecret of a single space passes this check.
  The real gate is the OIDC token exchange at runtime.
- skipSanityChecks is intentionally absent: that flag documents a bypass for live
  Secret lookups (kube API unavailable). Static value checks have no lookup cost;
  bypassing them would silently break OIDC auth.
- The two URL scheme rules are deliberately asymmetric, each matching what the
  runtime actually enforces:
  * issuerUrl — https, or http on localhost/127.0.0.1. This is the MCP SDK's own
    rule (mcp/server/auth/routes.py validate_issuer_url), so a plaintext issuer
    on any other host would be rejected at startup anyway. An in-cluster
    plaintext IdP is therefore not supported, by the SDK rather than by us.
  * baseUrl — https only, no localhost carve-out. FastMCP derives its _is_https
    flag from this value, which drives secure-cookie and redirect behaviour.
*/}}
{{- define "mcp-kubecost.validateOIDC" -}}
{{- if eq (.Values.config.authMode | default "none") "oidc" -}}
{{- $hasInline := and .Values.config.oidc.clientId .Values.config.oidc.clientSecret -}}
{{- $hasExisting := .Values.config.oidc.existingSecret -}}
{{- if not (or $hasInline $hasExisting) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.authMode is \"oidc\" but no OIDC credentials are configured.\n\nTo fix, choose one of:\n  Option A — inline credentials:\n    config.oidc.clientId: \"<your-client-id>\"\n    config.oidc.clientSecret: \"<your-client-secret>\"\n\n  Option B — reference a pre-existing Secret (required keys: OIDC_CLIENT_ID, OIDC_CLIENT_SECRET):\n    config.oidc.existingSecret: \"<secret-name>\"\n" -}}
{{- end -}}
{{- $issuer := .Values.config.oidc.issuerUrl | default "" -}}
{{- $issuerHost := (urlParse $issuer).host | default "" | splitList ":" | first -}}
{{- $issuerLocal := and (hasPrefix "http://" $issuer) (or (eq $issuerHost "localhost") (hasPrefix "127.0.0.1" $issuerHost)) -}}
{{- if not (or (hasPrefix "https://" $issuer) $issuerLocal) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.issuerUrl must be an https:// URL, or http:// on localhost / 127.0.0.1.\n\nThis mirrors the MCP SDK's own issuer rule (RFC 8414 requires HTTPS; the SDK carves out localhost for testing). A plaintext issuer on any other host is rejected by the SDK at startup, so the chart refuses it here rather than letting the pod crash-loop.\n\n  Example:\n    config.oidc.issuerUrl: \"https://kubecost.example.com/.well-known/openid-configuration\"\n" -}}
{{- end -}}
{{- if not (hasPrefix "https://" (.Values.config.oidc.baseUrl | default "")) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.baseUrl must be set to an https:// URL.\n\n  Example:\n    config.oidc.baseUrl: \"https://kubecost.example.com\"\n" -}}
{{- end -}}
{{- end -}}
{{- end -}}
