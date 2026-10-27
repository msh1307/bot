$ErrorActionPreference = 'Stop'
$serverRoot = Join-Path $env:USERPROFILE '.config/evo-codex-server'
$settings = Get-Content -LiteralPath (Join-Path $serverRoot 'settings.json') -Raw | ConvertFrom-Json
foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js')) {
    & $settings.node --check (Join-Path $PSScriptRoot $name)
    if ($LASTEXITCODE -ne 0) { throw "Invalid JavaScript: $name" }
}
# Run after confirming bridge jobs are idle. This task owns only its headless
# backend. The running ChatGPT/Codex GUI and its app-server are not stopped.
$backup = Join-Path $serverRoot ('backups/desktop-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js')) {
    $source = Join-Path $serverRoot $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup }
}
$oldServerPid = $null
$stateFile = Join-Path $serverRoot 'state.json'
if (Test-Path -LiteralPath $stateFile) {
    $oldServerPid = (Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json).pid
}
Stop-ScheduledTask -TaskName 'EVO-Codex-Server'
try {
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $running = (Get-ScheduledTask -TaskName 'EVO-Codex-Server').State -eq 'Running'
        $oldProcess = if ($oldServerPid) { Get-Process -Id $oldServerPid -ErrorAction SilentlyContinue } else { $null }
        if (!$running -and !$oldProcess) { break }
        if ((Get-Date) -gt $deadline) { throw 'Previous bridge process has not exited' }
        Start-Sleep -Milliseconds 200
    } while ($true)
    foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $serverRoot
    }
    Start-ScheduledTask -TaskName 'EVO-Codex-Server'
} catch {
    Copy-Item -LiteralPath (Join-Path $backup 'server.js') -Destination $serverRoot
    if (Test-Path -LiteralPath (Join-Path $backup 'desktop-ipc.js')) {
        Copy-Item -LiteralPath (Join-Path $backup 'desktop-ipc.js') -Destination $serverRoot
    }
    if (Test-Path -LiteralPath (Join-Path $backup 'snapshot-tail.js')) {
        Copy-Item -LiteralPath (Join-Path $backup 'snapshot-tail.js') -Destination $serverRoot
    }
    Start-ScheduledTask -TaskName 'EVO-Codex-Server'
    throw
}
Write-Output "Updated authenticated desktop bridge; code backup: $backup"
