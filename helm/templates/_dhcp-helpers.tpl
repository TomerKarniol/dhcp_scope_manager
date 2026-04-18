{{- define "dhcp.payload" -}}
{{- $v := .Values.dhcp_values | default dict -}}

{{- $dns := $v.dns | default dict -}}
{{- $dnsServers := $dns.servers | default (list) -}}
{{- $dnsDomain := $dns.domain | default "" -}}

{{- $useFailover := and (hasKey $v "failover") $v.failover -}}

scopeName: {{ required "dhcp_values.scopeName is required" $v.scopeName | quote }}
network: {{ required "dhcp_values.network is required" $v.network | quote }}
subnetMask: {{ required "dhcp_values.subnetMask is required" $v.subnetMask | quote }}
startRange: {{ required "dhcp_values.startRange is required" $v.startRange | quote }}
endRange: {{ required "dhcp_values.endRange is required" $v.endRange | quote }}
leaseDurationDays: {{ required "dhcp_values.leaseDurationDays is required" $v.leaseDurationDays | int }}
description: {{ $v.description | default "" | quote }}
gateway: {{ required "dhcp_values.gateway is required" $v.gateway | quote }}
dnsServers: {{ required "dhcp_values.dns.servers is required" $dnsServers | toJson }}
dnsDomain: {{ required "dhcp_values.dns.domain is required" $dnsDomain | quote }}
exclusions: {{ $v.exclusions | default (list) | toJson }}
{{- if $useFailover }}
{{- $f := $v.failover }}
failover:
  partnerServer: {{ required "failover.partnerServer is required" $f.partnerServer | quote }}
  relationshipName: {{ required "failover.relationshipName is required" $f.relationshipName | quote }}
  mode: {{ required "failover.mode is required" $f.mode | quote }}
  serverRole: {{ if eq $f.mode "LoadBalance" }}"Active"{{ else }}{{ required "failover.serverRole is required for HotStandby mode" $f.serverRole | quote }}{{ end }}
  reservePercent: {{ if eq $f.mode "LoadBalance" }}0{{ else }}{{ $f.reservePercent | default 0 | int }}{{ end }}
  loadBalancePercent: {{ if eq $f.mode "HotStandby" }}0{{ else }}{{ required "failover.loadBalancePercent is required for LoadBalance mode" ($f.loadBalancePercent | toString) | int }}{{ end }}
  maxClientLeadTimeMinutes: {{ required "failover.maxClientLeadTimeMinutes is required" $f.maxClientLeadTimeMinutes | int }}
  sharedSecret: {{ if $f.sharedSecret }}{{ $f.sharedSecret | quote }}{{ else }}null{{ end }}
{{- else }}
failover: null
{{- end }}
{{- end }}
