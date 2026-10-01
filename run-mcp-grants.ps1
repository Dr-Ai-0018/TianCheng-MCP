param(
    [string]$ConfigPath,
    [switch]$AllowExec,
    [switch]$AllowPolicyHotReload,
    [string[]]$PassEnv = @()
)

& (Join-Path $PSScriptRoot 'scripts/start-mcp.ps1') -Mode grants -ConfigPath $ConfigPath -AllowExec:$AllowExec -AllowPolicyHotReload:$AllowPolicyHotReload -PassEnv $PassEnv
exit $LASTEXITCODE
