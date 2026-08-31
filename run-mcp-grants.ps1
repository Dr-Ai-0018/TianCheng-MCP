param(
    [switch]$AllowExec,
    [switch]$AllowPolicyHotReload,
    [string[]]$PassEnv = @()
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project environment is missing. Run: uv sync --frozen --extra test"
}

$interactiveTimeout = 75
$workspace = ''
foreach ($configPath in @(
    (Join-Path $PSScriptRoot 'config\launcher.defaults.json'),
    (Join-Path $PSScriptRoot 'config\launcher.local.json')
)) {
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $config.interactiveTimeoutSeconds) {
                $candidate = [int]$config.interactiveTimeoutSeconds
                if ($candidate -ge 1 -and $candidate -le 90) { $interactiveTimeout = $candidate }
            }
            if ($config.workspace) { $workspace = [string]$config.workspace }
        } catch { }
    }
}
if ($env:TIANCHENG_WORKSPACE) { $workspace = $env:TIANCHENG_WORKSPACE }
if (-not $workspace) {
    throw ("No workspace is configured. Copy config\launcher.local.example.json to " +
        "config\launcher.local.json and set 'workspace' to the directory this server " +
        "may touch, or set the TIANCHENG_WORKSPACE environment variable. " +
        "There is deliberately no built-in default: the workspace is the security boundary.")
}

# Optional local fallback: load only the TOTP secret, never the whole .env file.
$envPath = Join-Path $PSScriptRoot '.env'
if (-not $env:TIANCHENG_TOTP_SECRET -and (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        if ($line -match '^\s*TIANCHENG_TOTP_SECRET\s*=\s*(.*)\s*$') {
            $value = $Matches[1]
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $env:TIANCHENG_TOTP_SECRET = $value
            break
        }
    }
}

$allowlistPath = Join-Path $PSScriptRoot 'exec-env.allowlist'
if (Test-Path -LiteralPath $allowlistPath -PathType Leaf) {
    $PassEnv += Get-Content -LiteralPath $allowlistPath -Encoding UTF8 |
        Where-Object { $_ -and -not $_.Trim().StartsWith('#') } |
        ForEach-Object { $_.Trim() }
}
if ((Test-Path -LiteralPath $envPath -PathType Leaf) -and $PassEnv.Count -gt 0) {
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
        $name = $Matches[1]
        if ($PassEnv -notcontains $name) { continue }
        if ([Environment]::GetEnvironmentVariable($name, 'Process')) { continue }
        $value = $Matches[2]
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path ("Env:{0}" -f $name) -Value $value
    }
}

$arguments = @(
    '-m', 'tiancheng_mcp',
    '--workspace', $workspace,
    '--audit-dir', (Join-Path $PSScriptRoot 'logs'),
    '--allow-external-grants',
    '--interactive-timeout-seconds', [string]$interactiveTimeout
)
if ($AllowExec) { $arguments += '--allow-exec' }
if ($AllowPolicyHotReload) { $arguments += '--allow-policy-hot-reload' }
foreach ($name in $PassEnv) { $arguments += @('--pass-env', $name) }

& $python @arguments
exit $LASTEXITCODE
