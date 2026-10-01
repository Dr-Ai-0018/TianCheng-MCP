param(
    [string]$ConfigPath,
    [switch]$AllowPolicyHotReload,
    [string[]]$PassEnv = @()
)

& (Join-Path $PSScriptRoot 'scripts/start-mcp.ps1') -Mode dev -ConfigPath $ConfigPath -AllowPolicyHotReload:$AllowPolicyHotReload -PassEnv $PassEnv
exit $LASTEXITCODE
