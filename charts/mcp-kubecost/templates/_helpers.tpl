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
when this chart is a Kubecost subchart (IBM ships images from icr.io).
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
Fail when referenced existing Secrets are missing, unless CI/CD skip is on or
the cluster is unreachable (helm template / dry-run with no kube API).
*/}}
{{- define "mcp-kubecost.sanityChecks" -}}
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
