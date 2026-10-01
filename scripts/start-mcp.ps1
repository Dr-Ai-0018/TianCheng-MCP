param(
    [ValidateSet('safe', 'dev', 'grants')][string]$Mode = 'safe',
    [string]$ConfigPath,
    [switch]$AllowExec,
    [switch]$AllowPolicyHotReload,
    [string[]]$PassEnv = @()
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$localPath = if ($ConfigPath) { [System.IO.Path]::GetFullPath($ConfigPath) }
else { Join-Path $projectRoot 'config\launcher.local.json' }
$bootstrap = if ($env:TIANCHENG_PYTHON) { $env:TIANCHENG_PYTHON }
else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
    throw 'Project environment is missing. Run: uv sync --frozen --extra test'
}
$resolveArguments = @('--local', $localPath)
if ($Mode -ne 'safe') {
    $allowlistPath = Join-Path $projectRoot 'exec-env.allowlist'
    if (Test-Path -LiteralPath $allowlistPath -PathType Leaf) {
        $PassEnv += Get-Content -LiteralPath $allowlistPath -Encoding UTF8 |
            Where-Object { $_ -and -not $_.Trim().StartsWith('#') } |
            ForEach-Object { $_.Trim() }
    }
    foreach ($name in $PassEnv) { $resolveArguments += @('--pass-env', $name) }
}
$resolved = & $bootstrap (Join-Path $PSScriptRoot 'resolve_launcher.py') @resolveArguments
if ($LASTEXITCODE -ne 0) { throw 'Launcher configuration could not be resolved' }
$config = ($resolved -join "`n") | ConvertFrom-Json -AsHashtable
$python = if ($config.python) { [string]$config.python } else { $bootstrap }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Configured Python is unavailable' }

$arguments = @('-m', 'tiancheng_mcp', '--runtime-config', $localPath,
    '--runtime-project-root', $projectRoot)
if ($Mode -eq 'dev' -or $AllowExec) { $arguments += '--allow-exec' }
if ($Mode -eq 'grants') { $arguments += '--allow-external-grants' }
if ($AllowPolicyHotReload) { $arguments += '--allow-policy-hot-reload' }
if ($Mode -ne 'safe') {
    # Read values only for explicitly selected names; never import a dotenv.
    $envPath = [string]$config.envFile
    if ($envPath -and (Test-Path -LiteralPath $envPath -PathType Leaf) -and $PassEnv.Count -gt 0) {
        foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
            if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
            $name = $Matches[1]
            if ($PassEnv -notcontains $name) { continue }
            if ($null -ne [Environment]::GetEnvironmentVariable($name, 'Process')) { continue }
            $value = $Matches[2]
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            Set-Item -Path ("Env:{0}" -f $name) -Value $value
        }
    }
    foreach ($name in $PassEnv) { $arguments += @('--pass-env', $name) }
}
& $python @arguments
exit $LASTEXITCODE
