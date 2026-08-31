[CmdletBinding()]
param(
    [string]$ProfilePath,
    [switch]$PassThru
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$targetProfile = if ($ProfilePath) {
    [System.IO.Path]::GetFullPath($ProfilePath)
} else {
    $PROFILE.CurrentUserAllHosts
}
$launcher = Join-Path $PSScriptRoot 'tc.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher does not exist: $launcher"
}

$startMarker = '# >>> tiancheng-mcp tc >>>'
$endMarker = '# <<< tiancheng-mcp tc <<<'
$escapedLauncher = $launcher.Replace("'", "''")
$block = @"
$startMarker
function global:tc {
    & '$escapedLauncher' @args
}
$endMarker
"@

$existing = if (Test-Path -LiteralPath $targetProfile -PathType Leaf) {
    [System.IO.File]::ReadAllText($targetProfile, [System.Text.Encoding]::UTF8)
} else {
    ''
}
$pattern = '(?ms)^' + [regex]::Escape($startMarker) + '.*?^' + [regex]::Escape($endMarker) + '\s*'
$withoutOldBlock = [regex]::Replace($existing, $pattern, '').TrimEnd()
$updated = if ($withoutOldBlock) {
    $withoutOldBlock + [Environment]::NewLine + [Environment]::NewLine + $block + [Environment]::NewLine
} else {
    $block + [Environment]::NewLine
}

$parent = Split-Path -Parent $targetProfile
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}
$temporary = "$targetProfile.$PID.tmp"
try {
    [System.IO.File]::WriteAllText($temporary, $updated, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::Move($temporary, $targetProfile, $true)
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
}

if ($PassThru) {
    [pscustomobject]@{ profilePath = $targetProfile; launcher = $launcher; installed = $true }
} else {
    Write-Host "tc 已安装到：$targetProfile" -ForegroundColor Green
    Write-Host '重开 PowerShell，或执行下面命令立即加载：' -ForegroundColor DarkGray
    Write-Host ". '$targetProfile'"
}
