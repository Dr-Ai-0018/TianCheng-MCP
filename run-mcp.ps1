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

& $python -m tiancheng_mcp `
    --workspace $workspace `
    --audit-dir (Join-Path $PSScriptRoot 'logs') `
    --interactive-timeout-seconds $interactiveTimeout
exit $LASTEXITCODE
