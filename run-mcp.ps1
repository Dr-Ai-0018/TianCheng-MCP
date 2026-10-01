param([string]$ConfigPath)

& (Join-Path $PSScriptRoot 'scripts/start-mcp.ps1') -Mode safe -ConfigPath $ConfigPath
exit $LASTEXITCODE
