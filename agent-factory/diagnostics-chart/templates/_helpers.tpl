{{- define "diagnostics.incidentName" -}}
{{- printf "foundry-incident-%s" (sha256sum (printf "%s/%s" .Release.Name (toJson .Values.incident.payload)) | trunc 16) -}}
{{- end -}}
