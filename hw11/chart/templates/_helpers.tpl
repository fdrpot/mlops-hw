{{- define "hw11.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "hw11.artifact" -}}
{{- $path := printf "files/model-%s.json" .Values.model.version -}}
{{- required "model.version must have an embedded files/model-VERSION.json artifact" (.Files.Get $path) -}}
{{- end -}}
