{{/*
常用模板函数
*/}}
{{- define "quant.fullname" -}}
{{- if .Values.global.fullnameOverride -}}
{{- .Values.global.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.global.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "quant.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "quant.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "quant.selectorLabels" -}}
app.kubernetes.io/name: {{ include "quant.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "quant.namespace" -}}
{{- .Release.Namespace -}}
{{- end -}}

{{- define "quant.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets -}}
{{- range .Values.global.imagePullSecrets -}}
- name: {{ . }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "quant.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
fsGroup: 1000
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "quant.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
readOnlyRootFilesystem: true
{{- end -}}
