# Limits apply only to read-only UI probes, never to managed service operations.
$script:TuiStatusProbe = $false
$script:TuiProbeDeadline = 0L
$script:TuiStatusCache = @{}

function Invoke-TuiProbeProcess {
    param([string]$FilePath, [string[]]$Arguments,
        [ValidateRange(100,60000)][int]$TimeoutMilliseconds=8000,
        [ValidateRange(1024,1048576)][int]$MaxCharacters=1048576)
    if ($script:TuiProbeDeadline -gt 0) {
        $TimeoutMilliseconds=[int][Math]::Min($TimeoutMilliseconds, $script:TuiProbeDeadline - [Environment]::TickCount64)
        if ($TimeoutMilliseconds -lt 1) { throw [TimeoutException]::new('状态探测超时。') }
    }
    $info=[Diagnostics.ProcessStartInfo]::new()
    $info.FileName=$FilePath; $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $info.StandardOutputEncoding=[Text.Encoding]::UTF8; $info.StandardErrorEncoding=[Text.Encoding]::UTF8
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    if ([IO.Path]::GetExtension($FilePath) -in @('.ps1','.cmd','.bat')) {
        $info.FileName=(Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $info.ArgumentList.Clear()
        foreach ($argument in @('-NoLogo','-NoProfile','-NonInteractive','-File',
            (Join-Path $PSScriptRoot 'probe-command.ps1'),$FilePath) + $Arguments) { $info.ArgumentList.Add($argument) }
    }
    $process=[Diagnostics.Process]::new(); $process.StartInfo=$info; $started=$false
    $clock=[Diagnostics.Stopwatch]::StartNew()
    try {
        $started=$process.Start()
        $output=[Text.StringBuilder]::new(); $count=0
        $outBuffer=[char[]]::new(4096); $errBuffer=[char[]]::new(4096)
        $outTask=$process.StandardOutput.ReadAsync($outBuffer,0,4096)
        $errTask=$process.StandardError.ReadAsync($errBuffer,0,4096)
        $outDone=$false; $errDone=$false
        while (-not ($outDone -and $errDone -and $process.HasExited)) {
            if ($clock.ElapsedMilliseconds -ge $TimeoutMilliseconds) { throw [TimeoutException]::new('状态探测超时。') }
            if (-not $outDone -and $outTask.IsCompleted) {
                $length=$outTask.GetAwaiter().GetResult(); $count+=$length
                if ($count -gt $MaxCharacters) { throw [IO.InvalidDataException]::new('状态输出超过限制。') }
                if ($length -eq 0) { $outDone=$true } else {
                    [void]$output.Append($outBuffer,0,$length)
                    $outTask=$process.StandardOutput.ReadAsync($outBuffer,0,4096)
                }
            }
            if (-not $errDone -and $errTask.IsCompleted) {
                $length=$errTask.GetAwaiter().GetResult(); $count+=$length
                if ($count -gt $MaxCharacters) { throw [IO.InvalidDataException]::new('状态输出超过限制。') }
                # Drain stderr concurrently, but never expose its credentials/argv.
                if ($length -eq 0) { $errDone=$true } else { $errTask=$process.StandardError.ReadAsync($errBuffer,0,4096) }
            }
            [Threading.Thread]::Sleep(10)
        }
        if ($process.ExitCode -ne 0) { throw [InvalidOperationException]::new("状态探测失败（退出码 $($process.ExitCode)）。") }
        $global:LASTEXITCODE=0
        return @($output.ToString() -split '\r?\n' | Where-Object { $_ -ne '' })
    } finally {
        if ($started) {
            try { if (-not $process.HasExited) { $process.Kill($true); [void]$process.WaitForExit(500) } } catch { }
        }
        $process.Dispose()
    }
}

function Get-TuiProcessRecords {
    param([ValidateSet('tunnel','supervisor')][string]$Kind)
    $raw=Invoke-TuiProbeProcess -FilePath (Join-Path $PSScriptRoot 'probe-processes.ps1') -Arguments @($Kind)
    return @(($raw -join "`n") | ConvertFrom-Json)
}
