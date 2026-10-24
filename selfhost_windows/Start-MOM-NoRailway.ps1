#Requires -Version 5.1
# Run on the authorized Windows PC. Existing local MOM scripts remain source of truth.
[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:USERPROFILE 'Desktop\M0J1M0J1_MOM'),
    [switch]$LocalOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$launch = Join-Path $Root 'scripts\START_MOM_CONTROL_MCP_WINDOWS.ps1'
$tunnel = Join-Path $Root 'scripts\SETUP_MOM_SECURE_TUNNEL_WINDOWS.ps1'
if (-not (Test-Path -LiteralPath $launch -PathType Leaf)) { throw "Missing $launch" }
if (-not $LocalOnly -and -not (Test-Path -LiteralPath $tunnel -PathType Leaf)) { throw "Missing $tunnel" }
function Test-Port {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $a = $c.BeginConnect('127.0.0.1', 8765, $null, $null)
        if (-not $a.AsyncWaitHandle.WaitOne(1000)) { return $false }
        try { $c.EndConnect($a); return $true } catch { return $false }
    } finally { $c.Close() }
}
if (-not (Test-Port)) {
    $argsLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $launch
    $proc = Start-Process -FilePath powershell.exe -ArgumentList $argsLine -WindowStyle Hidden -PassThru
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port) { $ok = $true; break }
        if ($proc.HasExited) { break }
    }
    if (-not $ok) { throw 'Local MOM failed to listen. No legacy agent was stopped.' }
}
Write-Host 'MOM MCP listening locally on http://127.0.0.1:8765/mcp'
if (-not $LocalOnly) {
    & $tunnel
    if (-not $?) { throw 'Tunnel bootstrap failed. Legacy agent not stopped.' }
}
$old = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match 'mom_relay_agent\.py'
})
foreach ($p in $old) {
    Write-Host "Stopping obsolete relay agent PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId
}
Write-Host 'MOM local startup complete. ChatGPT plugin/tunnel authorization NOT verified.'
Write-Host 'Reconnect MOM as a Tunnel-based MCP plugin in ChatGPT web and test mom_status.'
