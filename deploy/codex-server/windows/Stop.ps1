$ErrorActionPreference='Stop'
# Ending the supervisor closes its Job and terminates this server's descendants.
Stop-ScheduledTask -TaskName 'EVO-Codex-Server'
Write-Output 'Codex remote server stopped. It remains configured to start at the next login.'
