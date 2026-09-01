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
Controller annotations: `global.annotations` merged with
`deployment.annotations`. Chart-local keys win on conflict. Mirrors the parent
chart, where global.annotations is applied to every Deployment/StatefulSet/DaemonSet.
*/}}
{{- define "mcp-kubecost.annotations" -}}
{{- $global := ((.Values.global).annotations) | default dict -}}
{{- $local := ((.Values.deployment).annotations) | default dict -}}
{{- with merge (deepCopy $local) $global }}
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
with no kube API). The routing + authMode=none check and the replicas HA check
are not gated by skipSanityChecks: that flag only skips live Secret lookups.
*/}}
{{- define "mcp-kubecost.sanityChecks" -}}
{{- $mode := .Values.config.authMode | default "none" }}
{{- $routeEnabled := or .Values.httpRoute.enabled .Values.ingress.enabled }}
{{- if and $routeEnabled (eq $mode "none") }}
{{- fail "ERROR: \n\n authMode must be configured before enabling httproute or ingress.\n Please set authMode to a valid option.\n If anonymous access is required (for example if the ingress is behind a VPN), set  (e.g., \"authMode=open\").\n authMode = \"none\" with an exposed route is not permitted." }}
{{- end }}
{{- $replicas := ((.Values.deployment).replicas) | default 1 | int }}
{{- if and (gt $replicas 1) (or (include "mcp-kubecost.persistenceEnabled" .) (eq $mode "oidc")) }}
{{- fail "\n\nFAILURE [mcp-kubecost]: deployment.replicas is greater than 1, but this chart cannot share OAuth storage across pods.\n\nFileTreeStore is single-writer and the OAuth PVC is ReadWriteOnce. Multiple replicas would Multi-Attach the volume or split client registrations across pods.\n\nTo run more than one replica, put an MCP gateway or OAuth proxy in front of this chart, set config.authMode to none/open/api_key, and set persistence.enabled: false. Until shared OAuth storage exists, keep deployment.replicas: 1 when using OIDC.\n" }}
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
{{- $raw := .Values.config.oidc.redirectPath | default "/callback" | trim }}
{{- if or (contains "://" $raw) (contains "?" $raw) (contains "#" $raw) }}
{{- fail (printf "config.oidc.redirectPath must be a path like /callback, not a URL: %s" $raw) }}
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
Route the MCP endpoint is served on inside the container (MCP_HTTP_PATH,
passed through to `fastmcp run --path`).

Defaults to /mcp. OAuth has its own public prefix through config.oidc.baseUrl;
it does not relocate the protected-resource endpoint.
*/}}
{{- define "mcp-kubecost.httpPath" -}}
{{- $explicit := (((.Values.config).http).path) | default "" }}
{{- if $explicit }}
{{- $explicit }}
{{- else }}
{{- "/mcp" }}
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

{{/* Return the mandatory OAuth state PVC name. */}}
{{- define "mcp-kubecost.oauthStorageClaimName" -}}
{{- printf "%s-oauth" (include "mcp-kubecost.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Resolve the tri-state persistence.enabled value.
Returns a non-empty string ("true") when a PVC should be created; empty string otherwise.

  true  (explicit) → always create PVC
  false (explicit) → never create PVC
  null  (default)  → create PVC only when config.authMode is "oidc"
*/}}
{{- define "mcp-kubecost.persistenceEnabled" -}}
{{- if eq .Values.persistence.enabled true -}}true
{{- else if eq .Values.persistence.enabled false -}}
{{- else if eq (.Values.config.authMode | default "none") "oidc" -}}true
{{- end -}}
{{- end -}}

{{/*
Stable string of all ConfigMap data values used for checksum annotation.
Must stay in sync with the data block in configmap.yaml.
*/}}
{{- define "mcp-kubecost.configmap-data" -}}
KUBECOST_BASE_URL={{ printf "%s:%v" (tpl .Values.config.kubecostApiBaseUrl .) .Values.config.kubecostApiPort | quote }}
KUBECOST_API_BASE_PATH={{ .Values.config.kubecostApiBasePath | quote }}
REQUEST_TIMEOUT_SECONDS={{ .Values.config.requestTimeoutSeconds | quote }}
REQUEST_RETRY_COUNT={{ .Values.config.requestRetryCount | quote }}
MCP_RATE_LIMIT_REQUESTS_PER_SECOND={{ .Values.config.rateLimitRequestsPerSecond | quote }}
MCP_RATE_LIMIT_BURST_CAPACITY={{ .Values.config.rateLimitBurstCapacity | quote }}
MCP_MAX_CONCURRENT_TOOL_CALLS={{ .Values.config.maxConcurrentToolCalls | quote }}
DEFAULT_WINDOW={{ .Values.config.defaultWindow | quote }}
FASTMCP_TRANSPORT="streamable-http"
FASTMCP_LOG_LEVEL={{ .Values.config.logLevel | upper | quote }}
FASTMCP_ENABLE_RICH_LOGGING="false"
FASTMCP_SHOW_SERVER_BANNER="false"
USE_CAC_VIEWS={{ .Values.config.useCacViews | quote }}
REQUIRE_CLIENT_API_KEY={{ (or .Values.config.requireClientApiKey (eq .Values.config.authMode "api_key")) | quote }}
MCP_SERVER_NAME={{ .Values.config.mcpServerName | quote }}
MCP_HTTP_PATH={{ include "mcp-kubecost.httpPath" . | quote }}
FASTMCP_TELEMETRY_MODE={{ .Values.config.telemetryMode | quote }}
OTEL_SERVICE_NAME={{ .Values.config.otelServiceName | quote }}
OTEL_METRICS_EXPORTER={{ .Values.config.otelMetricsExporter | quote }}
OTEL_LOGS_EXPORTER={{ .Values.config.otelLogsExporter | quote }}
KUBECOST_SSL_VERIFY={{ .Values.config.ssl.verify | quote }}
FASTMCP_HTTP_HOST_ORIGIN_PROTECTION={{ .Values.config.fastmcpHttpHostOriginProtection | quote }}
OIDC_STORAGE_PATH="/var/lib/mcp-kubecost/oauth"
{{- if .Values.config.otelExporterOtlpEndpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT={{ .Values.config.otelExporterOtlpEndpoint | quote }}
{{- end }}
{{- if .Values.config.fastmcpHttpAllowedHosts }}
FASTMCP_HTTP_ALLOWED_HOSTS={{ .Values.config.fastmcpHttpAllowedHosts | quote }}
{{- end }}
{{- if .Values.config.fastmcpHttpAllowedOrigins }}
FASTMCP_HTTP_ALLOWED_ORIGINS={{ .Values.config.fastmcpHttpAllowedOrigins | quote }}
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
{{- if .Values.config.oidc.resourceBaseUrl }}
OIDC_RESOURCE_BASE_URL={{ .Values.config.oidc.resourceBaseUrl | quote }}
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
jwtSigningKey and storageEncryptionKey are included only when non-empty, matching
the conditional stringData entries in secret.yaml.
*/}}
{{- define "mcp-kubecost.oidc-stringdata" -}}
{{- if and .Values.config.oidc.clientId .Values.config.oidc.clientSecret (not .Values.config.oidc.existingSecret) -}}
OIDC_CLIENT_ID={{ .Values.config.oidc.clientId }}
OIDC_CLIENT_SECRET={{ .Values.config.oidc.clientSecret }}
{{- if .Values.config.oidc.jwtSigningKey }}
OIDC_JWT_SIGNING_KEY={{ .Values.config.oidc.jwtSigningKey }}
{{- end }}
{{- if .Values.config.oidc.storageEncryptionKey }}
OIDC_STORAGE_ENCRYPTION_KEY={{ .Values.config.oidc.storageEncryptionKey }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Fail when authMode is "oidc" but no OIDC credentials are configured, or when
issuerUrl / baseUrl are missing or not https:// URLs. Not gated by
skipSanityChecks — these are static value checks, not live Secret lookups.

issuerUrl accepts http:// on localhost / 127.0.0.1 to mirror the MCP SDK's own
validate_issuer_url rule; all other hosts require https://.
baseUrl and resourceBaseUrl require https:// with no localhost carve-out.
FastMCP derives secure-cookie and redirect behaviour from baseUrl.
*/}}
{{- define "mcp-kubecost.validateOIDC" -}}
{{- if eq (.Values.config.authMode | default "none") "oidc" -}}
{{- $hasInline := and .Values.config.oidc.clientId .Values.config.oidc.clientSecret -}}
{{- $hasExisting := .Values.config.oidc.existingSecret -}}
{{- if not (or $hasInline $hasExisting) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.authMode is \"oidc\" but its durable OAuth secrets are incomplete.\n\nTo fix, choose one of:\n  Option A — set clientId and clientSecret (jwtSigningKey and storageEncryptionKey are optional; ephemeral keys are auto-generated when omitted).\n\n  Option B — reference a pre-existing Secret with keys OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, and optionally OIDC_JWT_SIGNING_KEY and OIDC_STORAGE_ENCRYPTION_KEY:\n    config.oidc.existingSecret: \"<secret-name>\"\n" -}}
{{- end -}}
{{- if and $hasInline .Values.config.oidc.jwtSigningKey -}}
{{- if lt (len .Values.config.oidc.jwtSigningKey) 32 -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.jwtSigningKey must be at least 32 characters.\n" -}}
{{- end -}}
{{- end -}}
{{- if and $hasInline .Values.config.oidc.storageEncryptionKey -}}
{{- if ne (len .Values.config.oidc.storageEncryptionKey) 44 -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.storageEncryptionKey must be a 44-character URL-safe base64 Fernet key.\n" -}}
{{- end -}}
{{- end -}}
{{- $issuer := .Values.config.oidc.issuerUrl | default "" -}}
{{- $issuerHost := (urlParse $issuer).host | default "" | splitList ":" | first -}}
{{- $issuerLocal := and (hasPrefix "http://" $issuer) (or (eq $issuerHost "localhost") (hasPrefix "127.0.0.1" $issuerHost)) -}}
{{- if not (or (hasPrefix "https://" $issuer) $issuerLocal) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.issuerUrl must be an https:// URL, or http:// on localhost / 127.0.0.1.\n\nThis mirrors the MCP SDK's own issuer rule (RFC 8414 requires HTTPS; the SDK carves out localhost for testing). A plaintext issuer on any other host is rejected by the SDK at startup, so the chart refuses it here rather than letting the pod crash-loop.\n\n  Example:\n    config.oidc.issuerUrl: \"https://kubecost.example.com/.well-known/openid-configuration\"\n" -}}
{{- end -}}
{{- if not (hasPrefix "https://" (.Values.config.oidc.baseUrl | default "")) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.baseUrl must be set to an https:// authorization-server URL.\n\n  Example:\n    config.oidc.baseUrl: \"https://kubecost.example.com/oauth/mcp\"\n" -}}
{{- end -}}
{{- if not (hasPrefix "https://" (.Values.config.oidc.resourceBaseUrl | default "")) -}}
{{- fail "\n\nFAILURE [mcp-kubecost]: config.oidc.resourceBaseUrl must be set to the https:// public base URL that hosts config.http.path.\n\n  Example:\n    config.oidc.resourceBaseUrl: \"https://kubecost.example.com\"\n" -}}
{{- end -}}
{{- end -}}
{{- end -}}
