param([string]$PcIp = '127.0.0.1', [string]$EvoIp = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$serverRoot = Join-Path $env:USERPROFILE '.config/evo-codex-server'
$serverWorkspace = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex-Remote'
New-Item -ItemType Directory -Path $serverRoot,$serverWorkspace -Force | Out-Null
# Credentials are kept outside every agent workspace, with a user/SYSTEM ACL.
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
& icacls.exe $serverRoot /inheritance:r /grant:r "*$($userSid.Value):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the credential directory' }
function New-RandomSecret {
    $bytes = [byte[]]::new(48)
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}
foreach ($name in @('token','pfx-password')) {
    $p=Join-Path $serverRoot $name
    if (!(Test-Path -LiteralPath $p)) { [IO.File]::WriteAllText($p,(New-RandomSecret)) }
}
if (!(Test-Path -LiteralPath (Join-Path $serverRoot 'server.pfx'))) {
    $cert=New-SelfSignedCertificate -Subject 'CN=EVO Codex PC' -Type SSLServerAuthentication `
        -TextExtension @("2.5.29.17={text}IPAddress=$PcIp&DNS=localhost") `
        -CertStoreLocation Cert:\CurrentUser\My -KeyAlgorithm RSA -KeyLength 3072 `
        -NotAfter (Get-Date).AddYears(3) -KeyExportPolicy Exportable
    $password=ConvertTo-SecureString ([IO.File]::ReadAllText((Join-Path $serverRoot 'pfx-password'))) -AsPlainText -Force
    Export-PfxCertificate -Cert $cert -FilePath (Join-Path $serverRoot 'server.pfx') -Password $password `
        -CryptoAlgorithmOption AES256_SHA256 | Out-Null
    $pem="-----BEGIN CERTIFICATE-----`n"+[Convert]::ToBase64String($cert.RawData,[Base64FormattingOptions]::InsertLineBreaks)+"`n-----END CERTIFICATE-----`n"
    [IO.File]::WriteAllText((Join-Path $serverRoot 'server.pem'),$pem)
}
if (!(Test-Path -LiteralPath (Join-Path $serverRoot 'settings.json'))) {
    $settings=[ordered]@{port=4500;backendPort=4501;pcIp=$PcIp;evoIp=$EvoIp;
        node=(Get-Command node.exe).Source;codex=(Get-Command codex.exe).Source;workspace=$serverWorkspace}
    $settings | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $serverRoot 'settings.json') -Encoding utf8
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'server.js') -Destination $serverRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'desktop-ipc.js') -Destination $serverRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'snapshot-tail.js') -Destination $serverRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'text-file.js') -Destination $serverRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Firewall.ps1') -Destination $serverRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Stop.ps1') -Destination $serverRoot
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Web.Extensions.dll `
    "/out:$(Join-Path $serverRoot 'EvoCodexServer.exe')" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Server launcher compilation failed' }
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay='PT45S'
$action=New-ScheduledTaskAction -Execute (Join-Path $serverRoot 'EvoCodexServer.exe')
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$taskSettings=New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'EVO-Codex-Server' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description 'Authenticated TLS Codex app-server for the EVO owner Discord bot.' -Force | Out-Null
Start-ScheduledTask -TaskName 'EVO-Codex-Server'
Write-Output 'Installed quiet login server; TLS port 4500, authenticated loopback backend 4501.'
Write-Output 'Credential files are private and were not printed.'
