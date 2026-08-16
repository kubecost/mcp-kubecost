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

{{/* Standard Kubernetes metadata labels. */}}
{{- define "mcp-kubecost.labels" -}}
helm.sh/chart: {{ include "mcp-kubecost.chart" . }}
{{ include "mcp-kubecost.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Chart.AppVersion }}
app.kubernetes.io/version: {{ . | quote }}
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
KUBECOST_BASE_URL={{ .Values.config.kubecostBaseUrl }}
KUBECOST_API_BASE_PATH={{ .Values.config.kubecostApiBasePath }}
REQUEST_TIMEOUT_SECONDS={{ .Values.config.requestTimeoutSeconds }}
REQUEST_RETRY_COUNT={{ .Values.config.requestRetryCount }}
DEFAULT_WINDOW={{ .Values.config.defaultWindow }}
FASTMCP_LOG_LEVEL={{ .Values.config.fastmcpLogLevel }}
FASTMCP_ENABLE_RICH_LOGGING={{ .Values.config.fastmcpEnableRichLogging }}
USE_CAC_VIEWS={{ .Values.config.useCacViews }}
REQUIRE_CLIENT_API_KEY={{ .Values.config.requireClientApiKey }}
MCP_SERVER_NAME={{ .Values.config.mcpServerName }}
FASTMCP_TELEMETRY_MODE={{ .Values.config.telemetryMode }}
OTEL_SERVICE_NAME={{ .Values.config.otelServiceName }}
OTEL_METRICS_EXPORTER={{ .Values.config.otelMetricsExporter }}
OTEL_LOGS_EXPORTER={{ .Values.config.otelLogsExporter }}
KUBECOST_SSL_VERIFY={{ .Values.config.ssl.verify }}
{{- if .Values.config.otelExporterOtlpEndpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT={{ .Values.config.otelExporterOtlpEndpoint }}
{{- end }}
{{- if .Values.config.fastmcpHttpAllowedHosts }}
FASTMCP_HTTP_ALLOWED_HOSTS={{ .Values.config.fastmcpHttpAllowedHosts }}
{{- end }}
{{- if .Values.config.ssl.caBundle.existingSecret }}
SSL_CA_BUNDLE={{ .Values.config.ssl.caBundle.mountPath }}
{{- end }}
{{- if ne .Values.config.oidc.authMode "none" }}
AUTH_MODE={{ .Values.config.oidc.authMode }}
{{- if .Values.config.oidc.issuerUrl }}
OIDC_ISSUER_URL={{ .Values.config.oidc.issuerUrl }}
{{- end }}
{{- if .Values.config.oidc.audience }}
OIDC_AUDIENCE={{ .Values.config.oidc.audience }}
{{- end }}
{{- if .Values.config.oidc.baseUrl }}
OIDC_BASE_URL={{ .Values.config.oidc.baseUrl }}
{{- end }}
OIDC_REDIRECT_PATH={{ include "mcp-kubecost.oidcRedirectPath" . }}
{{- if hasKey .Values.config.oidc "verifyIdToken" }}
OIDC_VERIFY_ID_TOKEN={{ .Values.config.oidc.verifyIdToken }}
{{- end }}
{{- if .Values.config.oidc.requiredScopes }}
OIDC_REQUIRED_SCOPES={{ .Values.config.oidc.requiredScopes }}
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
