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
StorageClass for the OAuth state PVC: `persistence.storageClass`, falling back to
the parent chart's `global.defaultStorageClass`, and finally to "" so the cluster's
default StorageClass claims the volume. Matches the parent chart, where every
component's storageClass defers to global.defaultStorageClass when unset.
*/}}
{{- define "mcp-kubecost.storageClass" -}}
{{- .Values.persistence.storageClass | default ((.Values.global).defaultStorageClass) | default "" -}}
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
{{- $p := include "mcp-kubecost.valuePrefix" . }}
{{- $mode := .Values.config.authMode | default "none" }}
{{- $routeEnabled := or .Values.httpRoute.enabled .Values.ingress.enabled }}
{{- if and $routeEnabled (eq $mode "none") }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.authMode must be configured before enabling %shttpRoute or %singress.\n\n%sconfig.authMode \"none\" with an exposed route is not permitted. Set it to \"oidc\" or \"api_key\" to enforce authentication, or to \"open\" to acknowledge intentional unauthenticated exposure (for example an ingress behind a VPN).\n" $p $p $p $p) }}
{{- end }}
{{- /* An enabled route must carry its own hostnames. The chart ships these empty
     so a placeholder host is never published, and so config.externalUrl is never
     inferred from an example value. */}}
{{- if and .Values.ingress.enabled (empty .Values.ingress.hosts) }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %singress.enabled is true but %singress.hosts is empty.\n\n  Example:\n    %singress.hosts:\n      - host: mcp.example.com\n" $p $p $p) }}
{{- end }}
{{- if .Values.httpRoute.enabled }}
{{- if empty .Values.httpRoute.parentRefs }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %shttpRoute.enabled is true but %shttpRoute.parentRefs is empty.\n\nReference the Gateway this route attaches to.\n\n  Example:\n    %shttpRoute.parentRefs:\n      - name: mcp-gateway\n        sectionName: https\n" $p $p $p) }}
{{- end }}
{{- if empty .Values.httpRoute.hostnames }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %shttpRoute.enabled is true but %shttpRoute.hostnames is empty.\n\n  Example:\n    %shttpRoute.hostnames:\n      - mcp.example.com\n" $p $p $p) }}
{{- end }}
{{- end }}
{{- $replicas := ((.Values.deployment).replicas) | default 1 | int }}
{{- if and (gt $replicas 1) (or (include "mcp-kubecost.persistenceEnabled" .) (eq $mode "oidc")) }}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sdeployment.replicas is greater than 1, but this chart cannot share OAuth storage across pods.\n\nFileTreeStore is single-writer and the OAuth PVC is ReadWriteOnce. Multiple replicas would Multi-Attach the volume or split client registrations across pods.\n\nTo run more than one replica, put an MCP gateway or OAuth proxy in front of this chart, set %sconfig.authMode to none/open/api_key, and set %spersistence.enabled: false. Until shared OAuth storage exists, keep %sdeployment.replicas: 1 when using OIDC.\n" $p $p $p $p) }}
{{- end }}
{{- if ne (include "mcp-kubecost.skipSanityChecks" .) "true" }}
{{- $ns := lookup "v1" "Namespace" "" .Release.Namespace }}
{{- if $ns }}
{{- $checks := list -}}
{{- if .Values.config.kubecostApiKey.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.kubecostApiKey.existingSecret "ref" (printf "%sconfig.kubecostApiKey.existingSecret" $p)) -}}
{{- end }}
{{- if .Values.config.oidc.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.oidc.existingSecret "ref" (printf "%sconfig.oidc.existingSecret" $p)) -}}
{{- end }}
{{- if .Values.config.ssl.caBundle.existingSecret }}
{{- $checks = append $checks (dict "name" .Values.config.ssl.caBundle.existingSecret "ref" (printf "%sconfig.ssl.caBundle.existingSecret" $p)) -}}
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

{{/*
Prefix for value paths quoted in error messages. Under the Kubecost parent chart
every value of this chart is set beneath the `mcp` alias, so naming a bare
`config.externalUrl` there would send the operator to a key that does not exist
in their values file. Standalone installs get no prefix.
*/}}
{{- define "mcp-kubecost.valuePrefix" -}}
{{- if .Chart.IsRoot -}}{{- else -}}mcp.{{- end -}}
{{- end -}}

{{/* Fully-qualified path of config.externalUrl for the current install shape. */}}
{{- define "mcp-kubecost.externalUrlRef" -}}
{{- printf "%sconfig.externalUrl" (include "mcp-kubecost.valuePrefix" .) -}}
{{- end -}}

{{/*
Extra guidance for the subchart case. The Kubecost frontend proxies /mcp and
/oauth/mcp on the Kubecost hostname, and the parent chart cannot compute this
value for us: only `global` crosses the chart boundary and values.yaml is not
templated. So say which host to use rather than leaving it to be guessed.
*/}}
{{- define "mcp-kubecost.externalUrlHint" -}}
{{- if not .Chart.IsRoot }}
When installed as a subchart of the Kubecost chart, MCP is proxied through the
Kubecost frontend, so this must be the Kubecost hostname -- the same host as
ingress.hosts or httpRoute.hostnames in the parent chart's values.
{{ end -}}
{{- end -}}

{{/*
Resolve the public MCP origin (MCP_EXTERNAL_URL): config.externalUrl if set
(validated as an https:// origin with no path/query/fragment), otherwise
inferred as https://<host> when exactly one of httpRoute/ingress is enabled
with exactly one non-wildcard hostname. Mirrors _get_external_url() in
src/mcp_kubecost/config/settings.py.

Fails when both routes are enabled simultaneously, when an explicit value's
host is not in the enabled route's hostnames, or when config.authMode is
"oidc" and no origin can be resolved. Otherwise returns "" for chart installs
that don't need it (no OIDC, no route, or an ambiguous route left unset).
*/}}
{{- define "mcp-kubecost.externalUrl" -}}
{{- $p := include "mcp-kubecost.valuePrefix" . -}}
{{- $httpRouteOn := .Values.httpRoute.enabled -}}
{{- $ingressOn := .Values.ingress.enabled -}}
{{- if and $httpRouteOn $ingressOn -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %shttpRoute.enabled and %singress.enabled cannot both be true. Choose one chart-managed route.\n" $p $p) -}}
{{- end -}}
{{- $routeHosts := list -}}
{{- if $httpRouteOn -}}
{{- $routeHosts = .Values.httpRoute.hostnames | default (list) -}}
{{- else if $ingressOn -}}
{{- range .Values.ingress.hosts | default (list) -}}
{{- $routeHosts = append $routeHosts .host -}}
{{- end -}}
{{- end -}}
{{- $explicit := .Values.config.externalUrl | default "" | trim | trimSuffix "/" -}}
{{- if $explicit -}}
{{- if not (hasPrefix "https://" $explicit) -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.externalUrl must be an https:// origin with no path: %s\n" $p $explicit) -}}
{{- end -}}
{{- $explicitHost := trimPrefix "https://" $explicit -}}
{{- if and (gt (len $routeHosts) 0) (not (has $explicitHost $routeHosts)) -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.externalUrl host %q is not one of the enabled route's hostnames %v.\n" $p $explicitHost $routeHosts) -}}
{{- end -}}
{{- $explicit -}}
{{- else -}}
{{- $nonWildcard := list -}}
{{- range $routeHosts -}}
{{- if not (hasPrefix "*" .) -}}
{{- $nonWildcard = append $nonWildcard . -}}
{{- end -}}
{{- end -}}
{{- if eq (len $nonWildcard) 1 -}}
{{- printf "https://%s" (first $nonWildcard) -}}
{{- else if eq (.Values.config.authMode | default "none") "oidc" -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %s could not be inferred.\n\nSet %s explicitly, or enable exactly one of httpRoute/ingress with exactly one non-wildcard hostname.\n%s\n  Example:\n    %s: \"https://kubecost.example.com\"\n" (include "mcp-kubecost.externalUrlRef" .) (include "mcp-kubecost.externalUrlRef" .) (include "mcp-kubecost.externalUrlHint" .) (include "mcp-kubecost.externalUrlRef" .)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

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
MCP_LEGACY_TEXT_CONTENT={{ .Values.config.legacyTextContent | quote }}
REQUIRE_CLIENT_API_KEY={{ (or .Values.config.requireClientApiKey (eq .Values.config.authMode "api_key")) | quote }}
MCP_SERVER_NAME={{ .Values.config.mcpServerName | quote }}
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
MCP_EXTERNAL_URL={{ include "mcp-kubecost.externalUrl" . | quote }}
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
{{- if and .Values.config.oidc.clientID .Values.config.oidc.clientSecret (not .Values.config.oidc.existingSecret) -}}
OIDC_CLIENT_ID={{ .Values.config.oidc.clientID }}
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
Validate the standalone chart's OIDC configuration. Credentials must come from
exactly one source: a complete inline clientID/clientSecret pair or an
existingSecret. Whitespace-only credential values are treated as unset.

issuerUrl accepts http:// on localhost / 127.0.0.1 to mirror the MCP SDK's own
validate_issuer_url rule; all other hosts require https://.
config.externalUrl requires https:// with no localhost carve-out (see
mcp-kubecost.externalUrl). FastMCP derives secure-cookie and redirect
behaviour from it.
*/}}
{{- define "mcp-kubecost.validateOIDC" -}}
{{- $p := include "mcp-kubecost.valuePrefix" . -}}
{{- if eq (.Values.config.authMode | default "none") "oidc" -}}
{{- $clientID := trim (.Values.config.oidc.clientID | default "") -}}
{{- $clientSecret := trim (.Values.config.oidc.clientSecret | default "") -}}
{{- $hasAnyInline := or (not (empty $clientID)) (not (empty $clientSecret)) -}}
{{- $hasInline := and (not (empty $clientID)) (not (empty $clientSecret)) -}}
{{- $hasExisting := not (empty (trim (.Values.config.oidc.existingSecret | default ""))) -}}
{{- if not (or $hasInline $hasExisting) -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.authMode is \"oidc\" but its durable OAuth secrets are incomplete.\n\nTo fix, choose one of:\n  Option A — set %sconfig.oidc.clientID and %sconfig.oidc.clientSecret (jwtSigningKey and storageEncryptionKey are optional; ephemeral keys are auto-generated when omitted).\n\n  Option B — reference a pre-existing Secret with keys OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, and optionally OIDC_JWT_SIGNING_KEY and OIDC_STORAGE_ENCRYPTION_KEY:\n    %sconfig.oidc.existingSecret: \"<secret-name>\"\n" $p $p $p $p) -}}
{{- end -}}
{{- if and $hasAnyInline $hasExisting -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.oidc.existingSecret cannot be combined with inline clientID or clientSecret values. Supply exactly one credential source so the active credentials are unambiguous.\n" $p) -}}
{{- end -}}
{{- if and $hasInline .Values.config.oidc.jwtSigningKey -}}
{{- if lt (len .Values.config.oidc.jwtSigningKey) 32 -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.oidc.jwtSigningKey must be at least 32 characters.\n" $p) -}}
{{- end -}}
{{- end -}}
{{- if and $hasInline .Values.config.oidc.storageEncryptionKey -}}
{{- if ne (len .Values.config.oidc.storageEncryptionKey) 44 -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.oidc.storageEncryptionKey must be a 44-character URL-safe base64 Fernet key.\n" $p) -}}
{{- end -}}
{{- end -}}
{{- $issuer := .Values.config.oidc.issuerUrl | default "" -}}
{{- $issuerHost := (urlParse $issuer).host | default "" | splitList ":" | first -}}
{{- $issuerLocal := and (hasPrefix "http://" $issuer) (or (eq $issuerHost "localhost") (hasPrefix "127.0.0.1" $issuerHost)) -}}
{{- if not (or (hasPrefix "https://" $issuer) $issuerLocal) -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %sconfig.oidc.issuerUrl must be an https:// URL, or http:// on localhost / 127.0.0.1.\n\nThis mirrors the MCP SDK's own issuer rule (RFC 8414 requires HTTPS; the SDK carves out localhost for testing). A plaintext issuer on any other host is rejected by the SDK at startup, so the chart refuses it here rather than letting the pod crash-loop.\n\n  Example:\n    %sconfig.oidc.issuerUrl: \"https://kubecost.example.com/.well-known/openid-configuration\"\n" $p $p) -}}
{{- end -}}
{{- if not (include "mcp-kubecost.externalUrl" .) -}}
{{- fail (printf "\n\nFAILURE [mcp-kubecost]: %s could not be resolved.\n\nSet %s explicitly, or enable exactly one of httpRoute/ingress with exactly one non-wildcard hostname.\n%s\n  Example:\n    %s: \"https://kubecost.example.com\"\n" (include "mcp-kubecost.externalUrlRef" .) (include "mcp-kubecost.externalUrlRef" .) (include "mcp-kubecost.externalUrlHint" .) (include "mcp-kubecost.externalUrlRef" .)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
