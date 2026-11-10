{{- define "publisher.evidenceName" -}}
{{- printf "agent-route-evidence-%s" (sha256sum (printf "%s/%s" .Release.Name (toJson .Values.release.evidence)) | trunc 16) -}}
{{- end -}}
