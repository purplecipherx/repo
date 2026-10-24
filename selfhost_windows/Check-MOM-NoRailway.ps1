#Requires -Version 5.1
[CmdletBinding()]
param([string]$Root = (Join-Path $env:USERPROFILE 'Desktop\M0J1M0J1_MOM'))
$local = Test-Path -LiteralPath (Join-Path $Root 'scripts\START_MOM_CONTROL_MCP_WINDOWS.ps1')
$tunnel = Test-Path -LiteralPath (Join-Path $Root 'scripts\SETUP_MOM_SECURE_TUNNEL_WINDOWS.ps1')
$c = New-Object System.Net.Sockets.TcpClient
$connected = $false
try {
    $a=$c.BeginConnect('127.0.0.1',8765,$null,$null)
    if ($a.AsyncWaitHandle.WaitOne(1000)) {try {$c.EndConnect($a);$connected=$true}catch{}}
} finally {$c.Close()}
$agents = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match 'mom_relay_agent\.py'
})
[ordered]@{
    local_launcher_present=$local
    tunnel_launcher_present=$tunnel
    port_8765_listening=$connected
    legacy_relay_agent_count=$agents.Count
    chatgpt_connection_tested=$false
} | ConvertTo-Json
if (-not ($local -and $connected)) {exit 2}
