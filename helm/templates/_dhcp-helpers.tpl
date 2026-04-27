{{- define "dhcp.payload" -}}
{{- $v := .Values.dhcp_values | default dict -}}

{{- $dns := $v.dns | default dict -}}
{{- $dnsServers := $dns.servers | default (list) -}}
{{- $dnsDomain := $dns.domain | default "" -}}

{{- $useFailover := and (hasKey $v "failover") $v.failover -}}

scopeName: {{ $v.scopeName | quote }}
network: {{ $v.network | quote }}
subnetMask: {{ $v.subnetMask | quote }}
startRange: {{ $v.startRange | quote }}
endRange: {{ $v.endRange | quote }}
leaseDurationDays: {{ $v.leaseDurationDays | int }}
description: {{ $v.description | default "" | quote }}
gateway: {{ $v.gateway | quote }}
dnsServers: {{ $dnsServers | toJson }}
dnsDomain: {{ $dnsDomain | quote }}
exclusions: {{ $v.exclusions | default (list) | toJson }}
{{- if $useFailover }}
{{- $f := $v.failover }}
failover:
  partnerServer: {{ $f.partnerServer | quote }}
  relationshipName: {{ $f.relationshipName | quote }}
  mode: {{ $f.mode | quote }}
  serverRole: {{ if eq $f.mode "LoadBalance" }}"Active"{{ else }}{{ $f.serverRole | quote }}{{ end }}
  reservePercent: {{ if eq $f.mode "LoadBalance" }}0{{ else }}{{ $f.reservePercent | default 0 | int }}{{ end }}
  loadBalancePercent: {{ if eq $f.mode "HotStandby" }}0{{ else }}{{ $f.loadBalancePercent | int }}{{ end }}
  maxClientLeadTimeMinutes: {{ $f.maxClientLeadTimeMinutes | int }}
  sharedSecret: {{ if $f.sharedSecret }}{{ $f.sharedSecret | quote }}{{ else }}null{{ end }}
{{- else }}
failover: null
{{- end }}
{{- end }}
