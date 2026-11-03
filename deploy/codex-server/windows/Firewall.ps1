$ErrorActionPreference = 'Stop'
$name = 'EVO-Codex-Server'
$existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($existing) { Remove-NetFirewallRule -Name $name }
New-NetFirewallRule -Name $name -DisplayName 'EVO owner Discord to Codex TLS server' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 4500 `
    -RemoteAddress 192.168.219.200 -Profile Any | Out-Null
Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.config/evo-codex-server/firewall-ready') -Value 'ready'
