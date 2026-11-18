param([switch]$Detached)
$ErrorActionPreference = 'Stop'
$serverRoot = Join-Path $env:USERPROFILE '.config/evo-codex-server'
$settings = Get-Content -LiteralPath (Join-Path $serverRoot 'settings.json') -Raw | ConvertFrom-Json
foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js', 'text-file.js')) {
    & $settings.node --check (Join-Path $PSScriptRoot $name)
    if ($LASTEXITCODE -ne 0) { throw "Invalid JavaScript: $name" }
}
# A command launched through this bridge belongs to its kill-on-close Job.
# Run its update outside that Job before stopping it, or the update dies too.
$stateFile = Join-Path $serverRoot 'state.json'
$oldServerPid = if (Test-Path -LiteralPath $stateFile) {
    (Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json).pid
} else { $null }
$ancestor = $PID
while ($oldServerPid -and $ancestor) {
    if ($ancestor -eq $oldServerPid) {
        if ($Detached) { throw 'Updater still belongs to the bridge process tree' }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe') -Argument (
            '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Detached')
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(5)
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        $taskSettings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName 'EVO-Codex-Bridge-Update' -Action $action -Trigger $trigger `
            -Principal $principal -Settings $taskSettings -Force | Out-Null
        Write-Output 'Update queued outside the bridge Job. Check update-result.json after reconnecting; not yet applied.'
        return
    }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ancestor"
    if (!$parent -or $parent.ParentProcessId -eq $ancestor) { break }
    $ancestor = $parent.ParentProcessId
}
# Build before stopping anything. Replace the launcher only after its Job exits.
$launcher = Join-Path $serverRoot 'EvoCodexServer.new.exe'
& (Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe') /nologo /target:winexe /platform:x64 `
    /reference:System.Web.Extensions.dll "/out:$launcher" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Server launcher compilation failed' }
# Run after confirming bridge jobs are idle. This task owns only its headless
# backend. The running ChatGPT/Codex GUI and its app-server are not stopped.
$backup = Join-Path $serverRoot ('backups/desktop-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js', 'text-file.js', 'EvoCodexServer.exe')) {
    $source = Join-Path $serverRoot $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup }
}
# Hold the periodic recovery trigger during the short replacement window.
Disable-ScheduledTask -TaskName 'EVO-Codex-Server' | Out-Null
try {
    Stop-ScheduledTask -TaskName 'EVO-Codex-Server'
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $running = (Get-ScheduledTask -TaskName 'EVO-Codex-Server').State -eq 'Running'
        $oldProcess = if ($oldServerPid) { Get-Process -Id $oldServerPid -ErrorAction SilentlyContinue } else { $null }
        if (!$running -and !$oldProcess) { break }
        if ((Get-Date) -gt $deadline) { throw 'Previous bridge process has not exited' }
        Start-Sleep -Milliseconds 200
    } while ($true)
    foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js', 'text-file.js')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $serverRoot
    }
    Move-Item -LiteralPath $launcher -Destination (Join-Path $serverRoot 'EvoCodexServer.exe') -Force
    $task = Get-ScheduledTask -TaskName 'EVO-Codex-Server'
    if (!@($task.Triggers | Where-Object { $_.Repetition.Interval -eq 'PT1M' }).Count) {
        $recovery = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        Set-ScheduledTask -TaskName 'EVO-Codex-Server' -Trigger @(@($task.Triggers) + $recovery) | Out-Null
    }
    Enable-ScheduledTask -TaskName 'EVO-Codex-Server' | Out-Null
    Start-ScheduledTask -TaskName 'EVO-Codex-Server'
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $updated = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $newProcess = Get-Process -Id $updated.pid -ErrorAction SilentlyContinue
        if ($newProcess -and $updated.pid -ne $oldServerPid -and
            (Get-ScheduledTask -TaskName 'EVO-Codex-Server').State -eq 'Running') { break }
        if ((Get-Date) -gt $deadline) { throw 'Updated bridge did not become ready' }
    } while ($true)
    foreach ($name in @('server.js', 'desktop-ipc.js', 'snapshot-tail.js', 'text-file.js')) {
        if ((Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $name)).Hash -ne
            (Get-FileHash -LiteralPath (Join-Path $serverRoot $name)).Hash) { throw "Deployment hash differs: $name" }
    }
    @{applied=$true; at=(Get-Date -Format o); pid=$updated.pid; backup=$backup} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $serverRoot 'update-result.json') -Encoding UTF8
} catch {
    Stop-ScheduledTask -TaskName 'EVO-Codex-Server'
    Copy-Item -LiteralPath (Join-Path $backup 'server.js') -Destination $serverRoot
    if (Test-Path -LiteralPath (Join-Path $backup 'desktop-ipc.js')) {
        Copy-Item -LiteralPath (Join-Path $backup 'desktop-ipc.js') -Destination $serverRoot
    }
    if (Test-Path -LiteralPath (Join-Path $backup 'snapshot-tail.js')) {
        Copy-Item -LiteralPath (Join-Path $backup 'snapshot-tail.js') -Destination $serverRoot
    }
    if (Test-Path -LiteralPath (Join-Path $backup 'text-file.js')) {
        Copy-Item -LiteralPath (Join-Path $backup 'text-file.js') -Destination $serverRoot
    }
    Copy-Item -LiteralPath (Join-Path $backup 'EvoCodexServer.exe') -Destination $serverRoot
    Enable-ScheduledTask -TaskName 'EVO-Codex-Server' | Out-Null
    Start-ScheduledTask -TaskName 'EVO-Codex-Server'
    @{applied=$false; at=(Get-Date -Format o); rolled_back=$true} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $serverRoot 'update-result.json') -Encoding UTF8
    throw
} finally {
    Enable-ScheduledTask -TaskName 'EVO-Codex-Server' | Out-Null
    if ($Detached) { Unregister-ScheduledTask -TaskName 'EVO-Codex-Bridge-Update' -Confirm:$false }
}
Write-Output "Updated authenticated desktop bridge; code backup: $backup"
