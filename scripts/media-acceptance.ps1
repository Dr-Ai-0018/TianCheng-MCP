[CmdletBinding()]
param([ValidateSet('start','status','stop','doctor')][string]$Action='start', [switch]$Json)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
$testRoot=Join-Path $projectRoot '.tmp/media-0.18.0'
$testConfig=Join-Path $testRoot 'launcher.local.json'
if (-not (Test-Path -LiteralPath $testConfig -PathType Leaf)) {
    throw 'Prepare an isolated media acceptance configuration before starting. No regular Profile will be used.'
}
$config=Get-Content -LiteralPath $testConfig -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
$expectedWorkspace=[System.IO.Path]::GetFullPath((Join-Path $testRoot 'functional-workspace'))
$expectedProfiles=[System.IO.Path]::GetFullPath((Join-Path $testRoot 'profiles'))
if ($config.defaultProfile -ne 'media-probe' -or
    [System.IO.Path]::GetFullPath([string]$config.workspace) -ne $expectedWorkspace -or
    [System.IO.Path]::GetFullPath([string]$config.profileDir) -ne $expectedProfiles -or
    $config.healthBaseUrl -ne 'http://127.0.0.1:18080') {
    throw 'Media acceptance requires its isolated workspace, Profile directory and health port.'
}
if ($Action -eq 'start') {
    # The test uses the same cloud Tunnel. Never start beside a regular runtime,
    # including one that listens but has not become Ready yet.
    $regularConfig=Join-Path $projectRoot 'config/launcher.local.json'
    $regularPort=8080
    if (Test-Path -LiteralPath $regularConfig -PathType Leaf) {
        $regular=Get-Content -LiteralPath $regularConfig -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
        if ($regular.ContainsKey('healthBaseUrl')) { $regularPort=([uri]$regular.healthBaseUrl).Port }
    }
    foreach ($port in @($regularPort,18080) | Select-Object -Unique) {
        $socket=[System.Net.Sockets.TcpClient]::new()
        try {
            $pending=$socket.ConnectAsync('127.0.0.1',$port)
            try { [void]$pending.Wait(1000) } catch { }
            if ($socket.Connected) { throw "A runtime already listens on port $port. It was not stopped." }
        } finally { $socket.Dispose() }
    }
    Write-Host '隔离媒体验收：持续运行，不设置会话自动停止；Ctrl+C 或显式 stop 结束。' -ForegroundColor Cyan
}
$arguments=@{Action=$Action;Profile='media-probe';ConfigPath=$testConfig;NoPause=$true}
if ($Action -eq 'stop') { $arguments.Force=$true }
if ($Json) { $arguments.Json=$true }
& (Join-Path $projectRoot 'tc.ps1') @arguments
exit $LASTEXITCODE
