[CmdletBinding()]
param(
    [ValidateSet(
        'menu', 'start', 'start-new', 'doctor', 'profiles', 'configure-profile',
        'select-profile', 'edit-profile', 'key', 'key-status', 'status',
        'set-mode', 'stop', 'restart', 'open-ui', 'settings', 'info', 'install-alias', 'totp-setup', 'policy', 'agents'
    )]
    [string]$Action = 'menu',
    [string]$Profile,
    [string]$ConfigPath,
    [ValidateSet('safe', 'dev')]
    [string]$Mode,
    [switch]$Json,
    [switch]$SkipDoctor,
    [switch]$AllowExecProfile,
    [switch]$Force,
    [switch]$NoUserEnvironment,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:ProjectRoot = $PSScriptRoot
$script:WorkspaceCache = ''

function Get-Workspace {
    <#
        The workspace is the security boundary, so there is no built-in
        default: a wrong guess would silently point the server at somebody
        else's directory.  It comes from launcher.local.json or the
        TIANCHENG_WORKSPACE environment variable, and its absence is an error.
    #>
    if (-not [string]::IsNullOrWhiteSpace($script:WorkspaceCache)) {
        return $script:WorkspaceCache
    }
    $value = ''
    try {
        $config = Get-LauncherConfig
        if ($config.ContainsKey('workspace')) { $value = [string]$config['workspace'] }
    } catch { }
    if ($env:TIANCHENG_WORKSPACE) { $value = [string]$env:TIANCHENG_WORKSPACE }
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw ("No workspace is configured. Copy config\launcher.local.example.json to " +
            "config\launcher.local.json and set 'workspace', or set the " +
            "TIANCHENG_WORKSPACE environment variable.")
    }
    $script:WorkspaceCache = [System.IO.Path]::GetFullPath($value)
    return $script:WorkspaceCache
}
$script:DefaultsPath = Join-Path $PSScriptRoot 'config\launcher.defaults.json'
$script:LocalConfigPath = if ($ConfigPath) {
    [System.IO.Path]::GetFullPath($ConfigPath)
} else {
    Join-Path $PSScriptRoot 'config\launcher.local.json'
}

function Read-JsonHashtable {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @{}
    }
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return @{}
    }
    return $raw | ConvertFrom-Json -AsHashtable
}

# Paths the shipped defaults express relative to the project root, so a clone
# works from any directory without editing a tracked file.
$script:ProjectRelativeKeys = @(
    'mcpScript', 'mcpExecScript', 'mcpGrantsScript',
    'accessPolicyPath', 'agentSourcesPath', 'agentCatalogPath', 'envFile'
)

function Resolve-LauncherConfig {
    param([Parameter(Mandatory)][hashtable]$Config)

    foreach ($key in $script:ProjectRelativeKeys) {
        if (-not $Config.ContainsKey($key)) { continue }
        $value = [string]$Config[$key]
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        if (-not [System.IO.Path]::IsPathRooted($value)) {
            $Config[$key] = [System.IO.Path]::GetFullPath((Join-Path $script:ProjectRoot $value))
        }
    }
    # External tools are discovered on PATH when the local config leaves them
    # blank.  Nothing here may fall back to one particular machine's layout.
    foreach ($pair in @(
        @{ Key = 'tunnelClient'; Command = 'tunnel-client' },
        @{ Key = 'powerShell';   Command = 'pwsh' }
    )) {
        $key = $pair.Key
        if ($Config.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace([string]$Config[$key])) {
            continue
        }
        $found = Get-Command $pair.Command -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $Config[$key] = [string]$found.Source }
    }
    return $Config
}

function Get-LauncherConfig {
    $defaults = Read-JsonHashtable -Path $script:DefaultsPath
    if ($defaults.Count -eq 0) {
        throw "Launcher defaults are missing: $script:DefaultsPath"
    }
    $overrides = Read-JsonHashtable -Path $script:LocalConfigPath
    foreach ($key in $overrides.Keys) {
        $defaults[$key] = $overrides[$key]
    }
    return (Resolve-LauncherConfig -Config $defaults)
}

function Save-LauncherOverrides {
    param([Parameter(Mandatory)][hashtable]$Changes)

    $current = Read-JsonHashtable -Path $script:LocalConfigPath
    foreach ($key in $Changes.Keys) {
        $current[$key] = $Changes[$key]
    }
    $parent = Split-Path -Parent $script:LocalConfigPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $jsonText = $current | ConvertTo-Json -Depth 8
    $temporary = "$script:LocalConfigPath.$PID.tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            $jsonText + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::Move($temporary, $script:LocalConfigPath, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Assert-FileExists {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Path, [Parameter(Mandatory)][string]$Label)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw ("$Label is not configured and was not found on PATH. Copy " +
            "config\launcher.local.example.json to config\launcher.local.json and set it, " +
            "or run: tc -Action settings")
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist: $Path"
    }
}

function Assert-ProfileName {
    param([Parameter(Mandatory)][string]$Name)

    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
        throw 'Profile name may contain only letters, digits, dot, underscore, and dash (max 64).'
    }
}

function Resolve-SelectedProfile {
    param([hashtable]$Config, [string]$Requested)

    $selected = if ([string]::IsNullOrWhiteSpace($Requested)) {
        [string]$Config.defaultProfile
    } else {
        $Requested
    }
    Assert-ProfileName -Name $selected
    return $selected
}

function Get-ProfileNames {
    param([hashtable]$Config)

    Assert-FileExists -Path ([string]$Config.tunnelClient) -Label 'tunnel-client'
    $profileArguments = Get-ProfileDirectoryArguments -Config $Config
    $raw = & ([string]$Config.tunnelClient) profiles list --json @profileArguments 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'tunnel-client could not list profiles.'
    }
    if ([string]::IsNullOrWhiteSpace(($raw -join "`n"))) {
        return @()
    }
    $parsed = ($raw -join "`n") | ConvertFrom-Json
    $items = if ($null -ne $parsed.PSObject.Properties['profiles']) {
        @($parsed.profiles)
    } else {
        @($parsed)
    }
    $names = foreach ($item in $items) {
        if ($item -is [string]) {
            $item
        } elseif ($null -ne $item.PSObject.Properties['name']) {
            [string]$item.name
        } elseif ($null -ne $item.PSObject.Properties['profile']) {
            [string]$item.profile
        }
    }
    return @($names | Where-Object { $_ } | Sort-Object -Unique)
}

function Get-ProfileRecords {
    param([hashtable]$Config)

    Assert-FileExists -Path ([string]$Config.tunnelClient) -Label 'tunnel-client'
    $profileArguments = Get-ProfileDirectoryArguments -Config $Config
    $raw = & ([string]$Config.tunnelClient) profiles list --json @profileArguments 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'tunnel-client could not list profiles.'
    }
    if ([string]::IsNullOrWhiteSpace(($raw -join "`n"))) { return @() }
    $parsed = ($raw -join "`n") | ConvertFrom-Json
    $items = if ($null -ne $parsed.PSObject.Properties['profiles']) {
        @($parsed.profiles)
    } else {
        @($parsed)
    }
    return @(
        foreach ($item in $items) {
            if ($item -is [string]) {
                [PSCustomObject]@{ Name = [string]$item; Path = $null }
            } else {
                $name = if ($null -ne $item.PSObject.Properties['name']) {
                    [string]$item.name
                } elseif ($null -ne $item.PSObject.Properties['profile']) {
                    [string]$item.profile
                }
                $path = if ($null -ne $item.PSObject.Properties['path']) { [string]$item.path } else { $null }
                if ($name) { [PSCustomObject]@{ Name = $name; Path = $path } }
            }
        }
    )
}

function Get-ProfileRecord {
    param([hashtable]$Config, [string]$Name)

    $records = @(Get-ProfileRecords -Config $Config | Where-Object Name -eq $Name | Select-Object -First 1)
    if ($records.Count -eq 0) { return $null }
    return $records[0]
}

function Get-ProfileMode {
    param([hashtable]$Config, [string]$Name)

    $record = Get-ProfileRecord -Config $Config -Name $Name
    if ($null -eq $record -or [string]::IsNullOrWhiteSpace([string]$record.Path) -or
        -not (Test-Path -LiteralPath ([string]$record.Path) -PathType Leaf)) {
        return 'UNKNOWN'
    }
    $text = [System.IO.File]::ReadAllText([string]$record.Path, [System.Text.Encoding]::UTF8)
    $hot = if ($text -match '(?i)(?:^|\s)-AllowPolicyHotReload(?:\s|"|$)') { '+HOT' } else { '' }
    if ($text -match '(?i)(?:^|[/\\])run-mcp-grants\.ps1(?:\s|"|$)') {
        if ($text -match '(?i)(?:^|\s)-AllowExec(?:\s|"|$)') { return "GRANTS+EXEC$hot" }
        return "GRANTS$hot"
    }
    if ($text -match '(?i)(?:^|[/\\])run-mcp-exec\.ps1(?:\s|"|$)') { return "DEV$hot" }
    if ($text -match '(?i)(?:^|[/\\])run-mcp\.ps1(?:\s|"|$)') { return 'SAFE' }
    return 'UNKNOWN'
}

function Test-ProfileHotReload {
    param([hashtable]$Config, [string]$Name)

    return (Get-ProfileMode -Config $Config -Name $Name).EndsWith('+HOT')
}

function Get-ProfileDirectoryArguments {
    param([hashtable]$Config)

    if ($Config.ContainsKey('profileDir') -and -not [string]::IsNullOrWhiteSpace([string]$Config.profileDir)) {
        return @('--profile-dir', [string]$Config.profileDir)
    }
    return @()
}

function Test-ProfileExists {
    param([hashtable]$Config, [string]$Name)

    return (Get-ProfileNames -Config $Config) -contains $Name
}

function Get-DotEnvKey {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*CONTROL_PLANE_API_KEY\s*=\s*(.*)\s*$') {
            $value = $Matches[1].Trim()
            if (
                $value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))
            ) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                return $value
            }
        }
    }
    return $null
}

function Get-KeyRecord {
    param([hashtable]$Config)

    $processValue = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'Process')
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return @{ Configured = $true; Source = 'process environment'; Value = $processValue }
    }
    if (-not $NoUserEnvironment) {
        $userValue = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'User')
        if (-not [string]::IsNullOrWhiteSpace($userValue)) {
            return @{ Configured = $true; Source = 'Windows user environment'; Value = $userValue }
        }
    }
    $fileValue = Get-DotEnvKey -Path ([string]$Config.envFile)
    if (-not [string]::IsNullOrWhiteSpace($fileValue)) {
        return @{ Configured = $true; Source = '.env file'; Value = $fileValue }
    }
    return @{ Configured = $false; Source = 'not configured'; Value = $null }
}

function Import-ControlPlaneKey {
    param([hashtable]$Config)

    $record = Get-KeyRecord -Config $Config
    if (-not $record.Configured) {
        return $record
    }
    $env:CONTROL_PLANE_API_KEY = [string]$record.Value
    return $record
}

function Read-SecretText {
    param([string]$Prompt = '请输入 CONTROL_PLANE_API_KEY')

    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

function Assert-KeyValue {
    param([Parameter(Mandatory)][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw 'API key cannot be empty or contain line breaks.'
    }
}

function Assert-EnvFileOutsideWorkspace {
    param([Parameter(Mandatory)][string]$Path)

    $workspace = Get-Workspace
    $candidate = [System.IO.Path]::GetFullPath($Path)
    $relative = [System.IO.Path]::GetRelativePath($workspace, $candidate)
    if ($relative -ne '..' -and -not $relative.StartsWith("..$([System.IO.Path]::DirectorySeparatorChar)")) {
        throw ".env must remain outside the workspace: $workspace"
    }
}

function Set-DotEnvKey {
    param([hashtable]$Config, [Parameter(Mandatory)][string]$Value)

    Assert-KeyValue -Value $Value
    $envPath = [string]$Config.envFile
    Assert-EnvFileOutsideWorkspace -Path $envPath
    $parent = Split-Path -Parent $envPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = "$envPath.$PID.tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            "CONTROL_PLANE_API_KEY=$Value$([Environment]::NewLine)",
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::Move($temporary, $envPath, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    if ($IsWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $envPath '/inheritance:r' '/grant:r' "${identity}:(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'The .env file was written, but its Windows ACL could not be tightened.'
        }
    }
    $env:CONTROL_PLANE_API_KEY = $Value
}

function Set-ProcessKeyInteractive {
    $plain = Read-SecretText
    try {
        Assert-KeyValue -Value $plain
        $env:CONTROL_PLANE_API_KEY = $plain
        Write-Host '已写入当前 PowerShell 进程；关闭窗口后失效。' -ForegroundColor Green
    } finally {
        $plain = $null
    }
}

function Set-UserKeyInteractive {
    $plain = Read-SecretText
    try {
        Assert-KeyValue -Value $plain
        [Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', $plain, 'User')
        $env:CONTROL_PLANE_API_KEY = $plain
        Write-Host '已写入 Windows 用户环境变量；新进程会自动继承。' -ForegroundColor Green
    } finally {
        $plain = $null
    }
}

function Set-DotEnvKeyInteractive {
    param([hashtable]$Config)

    Write-Warning '.env 是本机明文文件，只应用于 tc 启动器，并非加密保险箱。'
    $plain = Read-SecretText
    try {
        Set-DotEnvKey -Config $Config -Value $plain
        Write-Host "已写入并收紧 ACL：$($Config.envFile)" -ForegroundColor Green
    } finally {
        $plain = $null
    }
}

function Quote-CommandPart {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-McpCommand {
    param([hashtable]$Config, [bool]$ExecMode, [bool]$ExternalGrants = $false, [bool]$HotReload = $false)

    $scriptPath = if ($ExternalGrants) { [string]$Config.mcpGrantsScript }
    elseif ($ExecMode) { [string]$Config.mcpExecScript }
    else { [string]$Config.mcpScript }
    Assert-FileExists -Path ([string]$Config.powerShell) -Label 'PowerShell 7'
    Assert-FileExists -Path $scriptPath -Label 'MCP startup script'
    $powerShellCommandPath = ([string]$Config.powerShell).Replace('\', '/')
    $mcpCommandPath = $scriptPath.Replace('\', '/')
    $parts = @(
        (Quote-CommandPart -Value $powerShellCommandPath),
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-CommandPart -Value $mcpCommandPath)
    )
    if ($ExternalGrants -and $ExecMode) { $parts += '-AllowExec' }
    # Hot reload is a separate high-risk switch: it lets an approved chat
    # request widen the access policy without a restart. Both the grants and
    # exec launchers accept it; the plain SAFE launcher does not.
    if ($HotReload -and ($ExternalGrants -or $ExecMode)) { $parts += '-AllowPolicyHotReload' }
    return $parts -join ' '
}

function Configure-ProfileInteractive {
    param([hashtable]$Config)

    $suggested = [string]$Config.defaultProfile
    $name = Read-Host "Profile 名称 [$suggested]"
    if ([string]::IsNullOrWhiteSpace($name)) { $name = $suggested }
    Assert-ProfileName -Name $name
    $tunnelId = Read-Host 'Tunnel ID（tunnel_...）'
    if ($tunnelId -cnotmatch '^tunnel_[a-z0-9]{32}$') {
        throw 'Tunnel ID must match tunnel_ followed by 32 lowercase letters or digits.'
    }

    $mode = Read-Host 'MCP 模式：1=安全默认，2=聊天外部授权，3=外部授权+Exec [1]'
    $externalGrants = $mode -in @('2', '3')
    $execMode = $mode -eq '3'
    if ($execMode) {
        Write-Warning 'Exec 模式不是 OS sandbox，代码可能访问工作区之外。'
        if ((Read-Host '请输入 ENABLE EXEC 确认') -cne 'ENABLE EXEC') {
            throw 'Exec profile creation cancelled.'
        }
    }
    if ($externalGrants) {
        Assert-FileExists -Path ([string]$Config.mcpGrantsScript) -Label 'External grants MCP startup script'
        Write-Warning '聊天外部授权会允许 ChatGPT 在临时 TOTP 授权后访问工作区之外的目录。'
    }
    $openUi = (Read-Host 'Tunnel 启动时自动打开管理 UI？y/N') -match '^(?i)y(?:es)?$'
    $exists = Test-ProfileExists -Config $Config -Name $name
    if ($exists -and (Read-Host "Profile '$name' 已存在，覆盖？输入 YES") -cne 'YES') {
        throw 'Profile update cancelled.'
    }

    $arguments = @(
        'init',
        '--sample', 'sample_mcp_stdio_local',
        '--profile', $name,
        '--tunnel-id', $tunnelId,
        '--mcp-command', (Get-McpCommand -Config $Config -ExecMode $execMode -ExternalGrants $externalGrants),
        '--health-listen-addr', '127.0.0.1:8080'
    )
    if ($openUi) { $arguments += '--open-web-ui' }
    if ($exists) { $arguments += '--force' }
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'tunnel-client profile creation failed.'
    }
    Save-LauncherOverrides -Changes @{ defaultProfile = $name }
    Write-Host "Profile '$name' 已保存并设为默认。" -ForegroundColor Green
}

function Set-ProfileMode {
    param(
        [hashtable]$Config,
        [string]$Name,
        [bool]$ExecMode,
        [bool]$AlreadyConfirmed
    )

    $record = Get-ProfileRecord -Config $Config -Name $Name
    if ($null -eq $record -or [string]::IsNullOrWhiteSpace([string]$record.Path) -or
        -not (Test-Path -LiteralPath ([string]$record.Path) -PathType Leaf)) {
        throw "Profile '$Name' could not be read."
    }
    if ($ExecMode -and -not $AlreadyConfirmed) {
        Write-Warning 'DEV 模式允许任意开发代码访问网络和工作区外资源，不是 OS sandbox。'
        if ((Read-Host '输入 ENABLE DEV 确认') -cne 'ENABLE DEV') {
            throw 'DEV mode switch cancelled.'
        }
    }
    $text = [System.IO.File]::ReadAllText([string]$record.Path, [System.Text.Encoding]::UTF8)
    $tunnelMatch = [regex]::Match($text, '(?m)^\s*tunnel_id:\s*"?([^"#\s]+)"?\s*$')
    if (-not $tunnelMatch.Success -or $tunnelMatch.Groups[1].Value -cnotmatch '^tunnel_[a-z0-9]{32}$') {
        throw 'Existing profile tunnel_id could not be validated.'
    }
    $listenMatch = [regex]::Match($text, '(?m)^\s*listen_addr:\s*"?([^"#\s]+)"?\s*$')
    $listenAddress = if ($listenMatch.Success) { $listenMatch.Groups[1].Value } else { '127.0.0.1:8080' }
    $openUi = [regex]::IsMatch($text, '(?m)^\s*open_browser:\s*true\s*$')
    $arguments = @(
        'init',
        '--sample', 'sample_mcp_stdio_local',
        '--profile', $Name,
        '--tunnel-id', $tunnelMatch.Groups[1].Value,
        '--mcp-command', (Get-McpCommand -Config $Config -ExecMode $ExecMode),
        '--health-listen-addr', $listenAddress,
        '--force'
    )
    if ($openUi) { $arguments += '--open-web-ui' }
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments
    if ($LASTEXITCODE -ne 0) { throw 'tunnel-client profile mode update failed.' }
    $label = if ($ExecMode) { 'DEV' } else { 'SAFE' }
    Write-Host "Profile '$Name' 已切换为 $label。" -ForegroundColor Green
}

function Set-ProfileExternalGrants {
    param([hashtable]$Config, [string]$Name, [bool]$ExecMode = $false, [bool]$HotReload = $false)

    $record = Get-ProfileRecord -Config $Config -Name $Name
    if ($null -eq $record -or -not (Test-Path -LiteralPath ([string]$record.Path) -PathType Leaf)) {
        throw "Profile '$Name' could not be read."
    }
    if ($HotReload) {
        Write-Warning '策略热重载：经你在对话中批准后，ChatGPT 可以把新目录写入白名单并立即生效，无需重启。'
        Write-Warning '一次性验证码会返回给模型，因此"必须你批准"是对话层面的约定，不是密码学强制。'
        Write-Warning '服务端自身目录、盘符根、系统目录和敏感名称路径始终被拒绝。'
        if ((Read-Host '输入 ENABLE HOT RELOAD 确认') -cne 'ENABLE HOT RELOAD') {
            throw 'Policy hot reload switch cancelled.'
        }
    }
    if ($ExecMode) {
        Write-Warning '外部授权 + Exec 允许 ChatGPT 在临时授权后运行开发命令；不是 OS sandbox。'
        if ((Read-Host '输入 ENABLE EXTERNAL EXEC 确认') -cne 'ENABLE EXTERNAL EXEC') {
            throw 'External exec profile switch cancelled.'
        }
    }
    $text = [System.IO.File]::ReadAllText([string]$record.Path, [System.Text.Encoding]::UTF8)
    $tunnelMatch = [regex]::Match($text, '(?m)^\s*tunnel_id:\s*"?([^"#\s]+)"?\s*$')
    if (-not $tunnelMatch.Success) { throw 'Existing profile tunnel_id could not be validated.' }
    $listenMatch = [regex]::Match($text, '(?m)^\s*listen_addr:\s*"?([^"#\s]+)"?\s*$')
    $listenAddress = if ($listenMatch.Success) { $listenMatch.Groups[1].Value } else { '127.0.0.1:8080' }
    $openUi = [regex]::IsMatch($text, '(?m)^\s*open_browser:\s*true\s*$')
    $arguments = @('init', '--sample', 'sample_mcp_stdio_local', '--profile', $Name,
        '--tunnel-id', $tunnelMatch.Groups[1].Value,
        '--mcp-command', (Get-McpCommand -Config $Config -ExecMode $ExecMode -ExternalGrants $true -HotReload $HotReload),
        '--health-listen-addr', $listenAddress, '--force')
    if ($openUi) { $arguments += '--open-web-ui' }
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments
    if ($LASTEXITCODE -ne 0) { throw 'External grants profile update failed.' }
    $label = if ($ExecMode) { '外部授权+Exec' } else { '聊天外部授权' }
    if ($HotReload) { $label += ' + 策略热重载' }
    Write-Host "Profile '$Name' 已切换为 $label。" -ForegroundColor Green
}

function Select-ProfileInteractive {
    param([hashtable]$Config)

    $names = @(Get-ProfileNames -Config $Config)
    if ($names.Count -eq 0) {
        Write-Host '还没有 profile，请先创建。' -ForegroundColor Yellow
        return
    }
    for ($index = 0; $index -lt $names.Count; $index++) {
        Write-Host "  $($index + 1). $($names[$index])"
    }
    $choice = Read-Host '选择序号'
    $number = 0
    if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $names.Count) {
        throw 'Invalid profile selection.'
    }
    Save-LauncherOverrides -Changes @{ defaultProfile = $names[$number - 1] }
    Write-Host "默认 profile：$($names[$number - 1])" -ForegroundColor Green
}

function Invoke-Doctor {
    param([hashtable]$Config, [string]$Name)

    if (-not (Test-ProfileExists -Config $Config -Name $Name)) {
        throw "Profile '$Name' does not exist."
    }
    $key = Import-ControlPlaneKey -Config $Config
    if (-not $key.Configured) {
        throw 'CONTROL_PLANE_API_KEY is not configured. Use the key menu first.'
    }
    Write-Host "使用密钥来源：$($key.Source)（值不会显示）" -ForegroundColor DarkGray
    $arguments = @('doctor', '--profile', $Name, '--explain')
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments | Out-Host
    $exitCode = $LASTEXITCODE
    return $exitCode
}

function Confirm-ExecProfile {
    param([hashtable]$Config, [string]$Name, [bool]$AlreadyAllowed)

    $isExec = (Get-ProfileMode -Config $Config -Name $Name) -eq 'DEV'
    if (-not $isExec -or $AlreadyAllowed) {
        return
    }
    Write-Warning "Profile '$Name' 会启用 run_command；它不是 OS sandbox。"
    if ((Read-Host '输入 RUN EXEC 继续') -cne 'RUN EXEC') {
        throw 'Exec profile start cancelled.'
    }
}

function Start-TunnelForeground {
    param(
        [hashtable]$Config,
        [string]$Name,
        [bool]$SkipDoctorCheck,
        [bool]$ExecAlreadyAllowed
    )

    if (-not (Test-ProfileExists -Config $Config -Name $Name)) {
        throw "Profile '$Name' does not exist. Create it from the profile menu first."
    }
    Confirm-ExecProfile -Config $Config -Name $Name -AlreadyAllowed $ExecAlreadyAllowed
    $key = Import-ControlPlaneKey -Config $Config
    if (-not $key.Configured) {
        throw 'CONTROL_PLANE_API_KEY is not configured. Use the key menu first.'
    }
    if (-not $SkipDoctorCheck -and [bool]$Config.doctorBeforeStart) {
        Write-Host "`n先检查 profile '$Name'..." -ForegroundColor Cyan
        $doctorExit = Invoke-Doctor -Config $Config -Name $Name
        if ($doctorExit -ne 0) {
            throw 'Doctor failed; Tunnel was not started.'
        }
    }
    Write-Host "`n正在启动 Tunnel；它会自动拉起 TianCheng MCP。按 Ctrl+C 停止。" -ForegroundColor Green
    $arguments = @('run', '--profile', $Name)
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "tunnel-client exited with code $LASTEXITCODE."
    }
}

function Start-TunnelWindow {
    param([hashtable]$Config, [string]$Name, [bool]$ExecAlreadyAllowed)

    Confirm-ExecProfile -Config $Config -Name $Name -AlreadyAllowed $ExecAlreadyAllowed
    $key = Import-ControlPlaneKey -Config $Config
    if (-not $key.Configured) {
        throw 'CONTROL_PLANE_API_KEY is not configured. Use the key menu first.'
    }
    Assert-FileExists -Path ([string]$Config.powerShell) -Label 'PowerShell 7'
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NoExit', '-File', $PSCommandPath,
        '-Action', 'start', '-Profile', $Name
    )
    if ($SkipDoctor) { $arguments += '-SkipDoctor' }
    if ((Get-ProfileMode -Config $Config -Name $Name) -eq 'DEV' -or $ExecAlreadyAllowed) {
        $arguments += '-AllowExecProfile'
    }
    Start-Process -FilePath ([string]$Config.powerShell) -ArgumentList $arguments -WindowStyle Normal
    Write-Host "已在新窗口启动 '$Name'。" -ForegroundColor Green
}

function Get-HealthStatus {
    param([hashtable]$Config)

    $base = ([string]$Config.healthBaseUrl).TrimEnd('/')
    try {
        $response = Invoke-WebRequest -Uri "$base/readyz" -TimeoutSec 2 -NoProxy -UseBasicParsing
        return @{ Reachable = $true; Ready = $response.StatusCode -eq 200; StatusCode = $response.StatusCode }
    } catch {
        return @{ Reachable = $false; Ready = $false; StatusCode = $null }
    }
}

function Get-RunningTunnelRecords {
    param([hashtable]$Config)

    if (-not $IsWindows) { return @() }
    try {
        $expected = [System.IO.Path]::GetFullPath([string]$Config.tunnelClient)
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'tunnel-client.exe'" -ErrorAction Stop |
                ForEach-Object {
                    if ([string]::IsNullOrWhiteSpace([string]$_.ExecutablePath) -or
                        -not [System.IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
                            $expected, [StringComparison]::OrdinalIgnoreCase
                        )) { return }
                    $match = [regex]::Match(
                        [string]$_.CommandLine,
                        '(?i)(?:^|\s)run(?:\s|$).*?(?:^|\s)--profile(?:=|\s+)["'']?([A-Za-z0-9._-]+)'
                    )
                    if ($match.Success) {
                        [PSCustomObject]@{ Profile = $match.Groups[1].Value; ProcessId = [int]$_.ProcessId }
                    }
                }
        )
    } catch {
        return @()
    }
}

function Get-DeveloperToolStatus {
    $git = Get-Command git -ErrorAction SilentlyContinue
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    $gcmConfigured = $false
    $ghAuthenticated = $false
    if ($null -ne $git) {
        $accounts = & git credential-manager github list --no-ui 2>$null
        $gcmConfigured = $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($accounts -join "`n"))
    }
    if ($null -ne $gh) {
        & gh auth status *> $null
        $ghAuthenticated = $LASTEXITCODE -eq 0
    }
    return [ordered]@{
        gitAvailable = $null -ne $git
        gcmConfigured = $gcmConfigured
        ghAvailable = $null -ne $gh
        ghAuthenticated = $ghAuthenticated
    }
}

function Stop-TunnelProfile {
    param([hashtable]$Config, [string]$Name, [bool]$Confirmed)

    $records = @(Get-RunningTunnelRecords -Config $Config | Where-Object Profile -eq $Name)
    if ($records.Count -eq 0) {
        Write-Host "Profile '$Name' 当前没有运行中的 Tunnel。" -ForegroundColor Yellow
        return $false
    }
    if (-not $Confirmed -and (Read-Host "停止 '$Name'？输入 STOP") -cne 'STOP') {
        throw 'Tunnel stop cancelled.'
    }
    foreach ($record in $records) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $record.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not stop tunnel-client PID $($record.ProcessId)." }
    }
    Write-Host "已停止 '$Name' 的 $($records.Count) 个 Tunnel 进程。" -ForegroundColor Green
    return $true
}

function Restart-TunnelProfile {
    param([hashtable]$Config, [string]$Name, [bool]$Confirmed)

    [void](Stop-TunnelProfile -Config $Config -Name $Name -Confirmed $Confirmed)
    Start-TunnelWindow -Config $Config -Name $Name -ExecAlreadyAllowed:$AllowExecProfile
}

function Show-Status {
    param([hashtable]$Config)

    $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
    $profiles = @(Get-ProfileNames -Config $Config)
    $key = Get-KeyRecord -Config $Config
    $health = Get-HealthStatus -Config $Config
    $running = @(Get-RunningTunnelRecords -Config $Config)
    $developer = Get-DeveloperToolStatus
    $status = [ordered]@{
        selectedProfile = $selected
        profileExists = $profiles -contains $selected
        selectedMode = Get-ProfileMode -Config $Config -Name $selected
        configuredProfiles = $profiles
        runningProfiles = @($running | ForEach-Object Profile | Sort-Object -Unique)
        keyConfigured = [bool]$key.Configured
        keySource = [string]$key.Source
        tunnelReachable = [bool]$health.Reachable
        tunnelReady = [bool]$health.Ready
        healthBaseUrl = [string]$Config.healthBaseUrl
        developerTools = $developer
    }
    if ($Json) {
        $status | ConvertTo-Json -Depth 5
        return
    }
    Write-Host "默认 Profile : $selected" -ForegroundColor Cyan
    Write-Host "Profile 存在 : $($status.profileExists)"
    Write-Host "实际 MCP 模式 : $($status.selectedMode)"
    Write-Host "运行中 Profile : $($status.runningProfiles -join ', ')"
    Write-Host "密钥已配置   : $($status.keyConfigured)"
    Write-Host "密钥来源     : $($status.keySource)"
    Write-Host "Tunnel Ready : $($status.tunnelReady)"
    Write-Host "管理地址     : $($status.healthBaseUrl)/ui"
    Write-Host "Git / GCM    : $($developer.gitAvailable) / $($developer.gcmConfigured)"
    Write-Host "gh / 已登录  : $($developer.ghAvailable) / $($developer.ghAuthenticated)"
}

function Show-Profiles {
    param([hashtable]$Config)

    $names = @(Get-ProfileNames -Config $Config)
    if ($Json) {
        $modes = [ordered]@{}
        foreach ($profileName in $names) {
            $modes[$profileName] = Get-ProfileMode -Config $Config -Name $profileName
        }
        @{
            profiles = $names
            profileModes = $modes
            defaultProfile = [string]$Config.defaultProfile
        } | ConvertTo-Json -Depth 4
        return
    }
    if ($names.Count -eq 0) {
        Write-Host '没有已配置的 tunnel-client profile。' -ForegroundColor Yellow
        return
    }
    foreach ($name in $names) {
        $marker = if ($name -eq [string]$Config.defaultProfile) { '*' } else { ' ' }
        $modeLabel = Get-ProfileMode -Config $Config -Name $name
        Write-Host "$marker $name [$modeLabel]"
    }
}

function Show-KeyStatus {
    param([hashtable]$Config)

    $record = Get-KeyRecord -Config $Config
    $safe = [ordered]@{ configured = [bool]$record.Configured; source = [string]$record.Source }
    if ($Json) {
        $safe | ConvertTo-Json
    } else {
        Write-Host "密钥已配置：$($safe.configured)"
        Write-Host "来源：$($safe.source)（值永远不显示）"
    }
}

function Show-Info {
    param([hashtable]$Config)

    $info = [ordered]@{
        launcherVersion = 1
        projectRoot = $script:ProjectRoot
        defaultsPath = $script:DefaultsPath
        localConfigPath = $script:LocalConfigPath
        defaultProfile = [string]$Config.defaultProfile
        tunnelClientExists = Test-Path -LiteralPath ([string]$Config.tunnelClient) -PathType Leaf
        mcpScriptExists = Test-Path -LiteralPath ([string]$Config.mcpScript) -PathType Leaf
        execScriptExists = Test-Path -LiteralPath ([string]$Config.mcpExecScript) -PathType Leaf
        grantsScriptExists = Test-Path -LiteralPath ([string]$Config.mcpGrantsScript) -PathType Leaf
        envFileExists = Test-Path -LiteralPath ([string]$Config.envFile) -PathType Leaf
    }
    if ($Json) { $info | ConvertTo-Json -Depth 4 } else { $info.GetEnumerator() | Format-Table -AutoSize }
}

function Open-AdminUi {
    param([hashtable]$Config)

    $url = ([string]$Config.healthBaseUrl).TrimEnd('/') + '/ui'
    Start-Process $url
}

function Edit-SettingsInteractive {
    param([hashtable]$Config)

    Write-Host '直接回车表示保持现值。配置不包含 API key。' -ForegroundColor DarkGray
    $interactiveTimeout = if ($Config.ContainsKey('interactiveTimeoutSeconds')) {
        [int]$Config.interactiveTimeoutSeconds
    } else { 75 }
    $tunnel = Read-Host "tunnel-client [$($Config.tunnelClient)]"
    $powershell = Read-Host "PowerShell 7 [$($Config.powerShell)]"
    $health = Read-Host "Health URL [$($Config.healthBaseUrl)]"
    $profileDir = Read-Host "Profile 目录（留空=系统默认）[$($Config.profileDir)]"
    $timeout = Read-Host "MCP 自动转后台等待秒数（1-90） [$interactiveTimeout]"
    $doctor = Read-Host "启动前运行 doctor？Y/n [$($Config.doctorBeforeStart)]"
    $changes = @{}
    if ($tunnel) { Assert-FileExists -Path $tunnel -Label 'tunnel-client'; $changes.tunnelClient = $tunnel }
    if ($powershell) { Assert-FileExists -Path $powershell -Label 'PowerShell 7'; $changes.powerShell = $powershell }
    if ($health) { $changes.healthBaseUrl = $health.TrimEnd('/') }
    if ($profileDir) { $changes.profileDir = [System.IO.Path]::GetFullPath($profileDir) }
    if ($timeout) {
        [int]$parsedTimeout = 0
        if (-not [int]::TryParse($timeout, [ref]$parsedTimeout) -or $parsedTimeout -lt 1 -or $parsedTimeout -gt 90) {
            throw 'MCP 自动转后台等待秒数必须是 1-90 的整数。'
        }
        $changes.interactiveTimeoutSeconds = $parsedTimeout
    }
    if ($doctor -match '^(?i)n(?:o)?$') { $changes.doctorBeforeStart = $false }
    elseif ($doctor -match '^(?i)y(?:es)?$') { $changes.doctorBeforeStart = $true }
    if ($changes.Count -gt 0) {
        Save-LauncherOverrides -Changes $changes
        Write-Host '启动器设置已保存。' -ForegroundColor Green
    }
}

function Get-AccessPolicyPath {
    $config = Get-LauncherConfig
    if ($config.ContainsKey('accessPolicyPath') -and -not [string]::IsNullOrWhiteSpace([string]$config.accessPolicyPath)) {
        return [System.IO.Path]::GetFullPath([string]$config.accessPolicyPath)
    }
    return Join-Path $script:ProjectRoot 'config\access-policy.json'
}

function Get-AccessPolicyData {
    $path = Get-AccessPolicyPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [ordered]@{ rules = @([ordered]@{
            path = (Get-Workspace); mode = 'full'; require_approval = $false
            enabled = $true; allow_exec = $false; note = '固定工作区'
        }) }
    }
    $data = Read-JsonHashtable -Path $path
    if (-not $data.ContainsKey('rules') -or -not ($data.rules -is [System.Collections.IEnumerable])) {
        throw "访问策略格式无效：缺少 rules 数组。"
    }
    return $data
}

function Save-AccessPolicyData {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Data)

    $path = Get-AccessPolicyPath
    $parent = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = "$path.$PID.tmp"
    $backup = "$path.bak"
    $hadExisting = Test-Path -LiteralPath $path -PathType Leaf
    if ($hadExisting) {
        $existingItem = Get-Item -LiteralPath $path -Force
        if ($existingItem.LinkType) {
            throw '策略文件不能是符号链接或其他链接类型。'
        }
    }
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            ($Data | ConvertTo-Json -Depth 8) + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        # Keep one recoverable previous snapshot before replacing the live
        # policy.  The backup is never loaded automatically and is safe to
        # remove manually after inspection.
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [System.IO.File]::Copy($path, $backup, $true)
        }
        [System.IO.File]::Move($temporary, $path, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
    if ($IsWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        foreach ($protectedPath in @($path, $backup)) {
            if (-not (Test-Path -LiteralPath $protectedPath -PathType Leaf)) { continue }
            & icacls.exe $protectedPath '/inheritance:r' '/grant:r' "${identity}:(F)" | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "策略文件已写入，但 ACL 未能收紧：$protectedPath"
            }
        }
    }
    try {
        # Parse and canonicalize with the exact Python loader before telling
        # the user the edit is usable.  If validation fails, restore the last
        # known-good snapshot so a malformed TUI edit cannot brick reload.
        [void](Invoke-AccessPolicyValidation -Path (Get-Workspace) -Operation 'read')
    } catch {
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            [System.IO.File]::Copy($backup, $path, $true)
        } elseif (-not $hadExisting -and (Test-Path -LiteralPath $path -PathType Leaf)) {
            Remove-Item -LiteralPath $path -Force
        }
        throw "策略已恢复到上一个有效版本：$($_.Exception.Message)"
    }
}

function Invoke-AccessPolicyValidation {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Operation)

    $config = Get-LauncherConfig
    $python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    Assert-FileExists -Path $python -Label 'Python environment'
    $helper = Join-Path $script:ProjectRoot 'scripts\policy_explain.py'
    Assert-FileExists -Path $helper -Label 'Policy explanation helper'
    $workspace = Get-Workspace
    $raw = & $python $helper --workspace $workspace --policy (Get-AccessPolicyPath) --path $Path --operation $Operation 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw (($raw -join [Environment]::NewLine).Trim())
    }
    return ($raw -join [Environment]::NewLine)
}

function Explain-AccessPolicyInteractive {
    $path = Read-Host '绝对路径（留空取消）'
    if ([string]::IsNullOrWhiteSpace($path)) { return }
    $operation = (Read-Host '操作 read/write/delete/exec/git_read/git_write [read]').Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($operation)) { $operation = 'read' }
    $result = Invoke-AccessPolicyValidation -Path $path -Operation $operation
    Write-Host $result
}

function Validate-AccessPolicyInteractive {
    $path = Get-AccessPolicyPath
    # Validate the complete file against the same loader used by MCP.  A
    # harmless root explanation forces parsing and canonical/reparse checks.
    $result = Invoke-AccessPolicyValidation -Path (Get-Workspace) -Operation 'read'
    Write-Host '策略文件验证通过；当前运行中的 MCP 需要调用 access_policy_reload 或重启后才会采用新快照。' -ForegroundColor Green
    if ($result) { Write-Host $result }
}

function Show-AccessPolicy {
    param([switch]$Interactive)

    $data = Get-AccessPolicyData
    $rows = @()
    $index = 0
    foreach ($rule in @($data.rules)) {
        $index++
        $rows += [PSCustomObject]@{
            Index = $index
            Path = [string]$rule.path
            Mode = [string]$rule.mode
            Approval = [bool]$rule.require_approval
            Exec = [bool]$rule.allow_exec
            Enabled = [bool]$rule.enabled
            Note = [string]$rule.note
        }
    }
    if ($Json) { @{ policyPath = Get-AccessPolicyPath; rules = $rows } | ConvertTo-Json -Depth 6; return }
    Write-Host "策略文件：$(Get-AccessPolicyPath)" -ForegroundColor DarkGray
    if ($rows.Count -eq 0) { Write-Host '当前没有规则。' -ForegroundColor Yellow }
    else { $rows | Format-Table -AutoSize }
}

function Add-AccessPolicyRuleInteractive {
    $data = Get-AccessPolicyData
    $path = Read-Host '绝对目录路径（留空取消）'
    if ([string]::IsNullOrWhiteSpace($path)) { return }
    if (-not [System.IO.Path]::IsPathRooted($path) -or $path -match '[*?]') {
        throw '白名单路径必须是绝对目录路径，且不能包含通配符。'
    }
    if ([System.IO.Path]::GetFullPath($path) -ieq (Get-Workspace)) {
        throw '固定工作区规则已内置，不能重复添加。'
    }
    $mode = (Read-Host '权限 read/write/full/deny [read]').Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = 'read' }
    if ($mode -notin @('read', 'write', 'full', 'deny')) { throw '权限必须是 read、write、full 或 deny。' }
    $approval = (Read-Host '是否每次需要聊天确认？Y/n [n]').Trim()
    $exec = (Read-Host '是否允许命令执行？y/N [N]').Trim()
    $note = Read-Host '备注（可选）'
    $rule = [ordered]@{
        path = [System.IO.Path]::GetFullPath($path)
        mode = $mode
        require_approval = ($approval -match '^(?i)y(?:es)?$')
        enabled = $true
        allow_exec = ($exec -match '^(?i)y(?:es)?$')
        note = $note
    }
    $data.rules = @($data.rules) + $rule
    Save-AccessPolicyData -Data $data
    Write-Host '白名单规则已保存；重启 MCP 后生效。' -ForegroundColor Green
}

function Remove-AccessPolicyRuleInteractive {
    $data = Get-AccessPolicyData
    Show-AccessPolicy
    $raw = Read-Host '输入要删除的规则编号（留空取消）'
    if ([string]::IsNullOrWhiteSpace($raw)) { return }
    [int]$number = 0
    if (-not [int]::TryParse($raw, [ref]$number) -or $number -lt 1 -or $number -gt @($data.rules).Count) {
        throw '规则编号无效。'
    }
    if ([string]$data.rules[$number - 1].path -ieq (Get-Workspace)) {
        throw '不能删除固定工作区规则。'
    }
    if ((Read-Host '输入 DELETE 确认') -cne 'DELETE') { return }
    $remaining = @()
    for ($i = 0; $i -lt @($data.rules).Count; $i++) {
        if ($i -ne ($number - 1)) { $remaining += $data.rules[$i] }
    }
    $data.rules = $remaining
    Save-AccessPolicyData -Data $data
    Write-Host '白名单规则已删除；重启 MCP 后生效。' -ForegroundColor Green
}

function Edit-AccessPolicyRuleInteractive {
    $data = Get-AccessPolicyData
    Show-AccessPolicy
    $raw = Read-Host '输入要编辑的规则编号（留空取消）'
    if ([string]::IsNullOrWhiteSpace($raw)) { return }
    [int]$number = 0
    if (-not [int]::TryParse($raw, [ref]$number) -or $number -lt 1 -or $number -gt @($data.rules).Count) {
        throw '规则编号无效。'
    }
    $rule = $data.rules[$number - 1]
    $mode = (Read-Host "权限 read/write/full/deny [$($rule.mode)]").Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = [string]$rule.mode }
    if ($mode -notin @('read', 'write', 'full', 'deny')) { throw '权限必须是 read、write、full 或 deny。' }
    if ([string]$rule.path -ieq (Get-Workspace) -and $mode -ne 'full') {
        throw '固定工作区规则必须保持 full 权限。'
    }
    $approval = Read-Host "每次需要聊天确认？Y/n [$([bool]$rule.require_approval)]"
    $exec = Read-Host "允许命令执行？y/N [$([bool]$rule.allow_exec)]"
    $enabled = Read-Host "启用规则？Y/n [$([bool]$rule.enabled)]"
    $note = Read-Host "备注 [$([string]$rule.note)]"
    $rule.mode = $mode
    if ([string]$rule.path -ieq (Get-Workspace)) {
        $rule.require_approval = $false
        $rule.allow_exec = $false
        $rule.enabled = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($approval)) { $rule.require_approval = $approval -match '^(?i)y(?:es)?$' }
    if (-not [string]::IsNullOrWhiteSpace($exec)) { $rule.allow_exec = $exec -match '^(?i)y(?:es)?$' }
    if (-not [string]::IsNullOrWhiteSpace($enabled)) { $rule.enabled = $enabled -match '^(?i)y(?:es)?$' }
    if ($null -ne $note) { $rule.note = $note }
    Save-AccessPolicyData -Data $data
    Write-Host '白名单规则已更新；重启 MCP 后生效。' -ForegroundColor Green
}

function Show-AccessPolicyMenu {
    while ($true) {
        Clear-Host
        Write-Host "`n访问策略 / 外部路径白名单" -ForegroundColor Cyan
        Show-AccessPolicy
        Write-Host '  1. 新增规则'
        Write-Host '  2. 删除规则'
        Write-Host '  3. 编辑 / 启用 / 禁用规则'
        Write-Host '  4. 测试路径权限（只读解释）'
        Write-Host '  5. 验证策略并提示 reload'
        Write-Host '  0. 返回'
        switch (Read-Host '选择') {
            '1' { Add-AccessPolicyRuleInteractive; Pause-Tq }
            '2' { Remove-AccessPolicyRuleInteractive; Pause-Tq }
            '3' { Edit-AccessPolicyRuleInteractive; Pause-Tq }
            '4' { Explain-AccessPolicyInteractive; Pause-Tq }
            '5' { Validate-AccessPolicyInteractive; Pause-Tq }
            '0' { return }
            default { Write-Host '无效选择。' -ForegroundColor Yellow; Pause-Tq }
        }
    }
}

function Get-AgentSourceConfigPath {
    param([hashtable]$Config)

    if ($Config.ContainsKey('agentSourcesPath') -and -not [string]::IsNullOrWhiteSpace([string]$Config.agentSourcesPath)) {
        return [System.IO.Path]::GetFullPath([string]$Config.agentSourcesPath)
    }
    return Join-Path $script:ProjectRoot 'config\agent-sources.json'
}

function Get-AgentCatalogPath {
    param([hashtable]$Config)

    if ($Config.ContainsKey('agentCatalogPath') -and -not [string]::IsNullOrWhiteSpace([string]$Config.agentCatalogPath)) {
        return [System.IO.Path]::GetFullPath([string]$Config.agentCatalogPath)
    }
    return Join-Path $script:ProjectRoot 'state\agent-catalog.sqlite3'
}

function Invoke-AgentSourceAdmin {
    param([hashtable]$Config, [Parameter(Mandatory)][string[]]$Arguments)

    $python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    Assert-FileExists -Path $python -Label 'Python environment'
    $base = @(
        '-m', 'tiancheng_mcp.agent_admin',
        '--config', (Get-AgentSourceConfigPath -Config $Config),
        '--catalog', (Get-AgentCatalogPath -Config $Config),
        '--workspace', (Get-Workspace)
    )
    $raw = & $python @base @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw (($raw -join [Environment]::NewLine).Trim())
    }
    $text = ($raw -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw 'Agent source helper returned no result.' }
    return $text | ConvertFrom-Json
}

function Get-AgentSourceState {
    param([hashtable]$Config)

    return [PSCustomObject]@{
        Discovery = Invoke-AgentSourceAdmin -Config $Config -Arguments @('discover')
        Status = Invoke-AgentSourceAdmin -Config $Config -Arguments @('status')
    }
}

function Show-AgentSourceState {
    param([hashtable]$Config)

    $state = Get-AgentSourceState -Config $Config
    if ($Json) {
        [ordered]@{
            sourcePolicyPath = Get-AgentSourceConfigPath -Config $Config
            catalogPath = Get-AgentCatalogPath -Config $Config
            providers = @($state.Discovery.providers)
            sources = @($state.Status.sources)
        } | ConvertTo-Json -Depth 10
        return
    }
    Write-Host "Source policy：$(Get-AgentSourceConfigPath -Config $Config)" -ForegroundColor DarkGray
    Write-Host "Catalog：$(Get-AgentCatalogPath -Config $Config)" -ForegroundColor DarkGray
    $providers = @($state.Discovery.providers | ForEach-Object {
        [PSCustomObject]@{
            Provider = $_.provider
            CLI = $(if ($_.cli_available) { $_.cli_version } else { 'not found' })
            SuggestedRoot = $_.suggested_root
            RootExists = [bool]$_.source_exists
        }
    })
    if ($providers.Count) { $providers | Format-Table -AutoSize }
    $sources = @($state.Status.sources)
    if (-not $sources.Count) {
        Write-Host '尚未授权任何会话源；MCP 不会扫描真实 Codex/Claude 历史。' -ForegroundColor Yellow
    } else {
        $rows = @($sources | ForEach-Object {
            $refresh = $_.last_refresh
            $counts = $_.record_status_counts
            $countParts = @()
            foreach ($name in @('ready', 'unsupported', 'corrupt', 'active-writing')) {
                if ($null -ne $counts -and $null -ne $counts.PSObject.Properties[$name]) {
                    $countParts += "${name}=$($counts.$name)"
                }
            }
            [PSCustomObject]@{
                SourceId = $_.source_id
                Provider = $_.provider
                Enabled = [bool]$_.enabled
                Root = $_.root
                LastRefresh = $(if ($null -eq $refresh) { 'never' } else { $refresh.refreshed_at })
                Parsed = $(if ($null -eq $refresh) { 0 } else { $refresh.parsed_files })
                Errors = $(if ($null -eq $refresh) { 0 } else { $refresh.error_files })
                Records = $(if ($countParts.Count) { $countParts -join ',' } else { 'none' })
            }
        })
        $rows | Format-Table -AutoSize
    }
    return $state
}

function Select-AgentSourceInteractive {
    param([hashtable]$Config)

    $state = Get-AgentSourceState -Config $Config
    $sources = @($state.Status.sources)
    if (-not $sources.Count) { throw '当前没有已配置的 Agent source。' }
    for ($index = 0; $index -lt $sources.Count; $index++) {
        Write-Host "  $($index + 1). $($sources[$index].source_id) [$($sources[$index].provider)] enabled=$($sources[$index].enabled)"
    }
    $raw = Read-Host '选择 source 编号（留空取消）'
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    [int]$number = 0
    if (-not [int]::TryParse($raw, [ref]$number) -or $number -lt 1 -or $number -gt $sources.Count) {
        throw 'Source 编号无效。'
    }
    return $sources[$number - 1]
}

function Add-AgentSourceInteractive {
    param([hashtable]$Config)

    $discovery = Invoke-AgentSourceAdmin -Config $Config -Arguments @('discover')
    Write-Host '  1. Codex (.codex\sessions)'
    Write-Host '  2. Claude Code (.claude\projects)'
    $choice = Read-Host '选择 provider（留空取消）'
    if ([string]::IsNullOrWhiteSpace($choice)) { return }
    $provider = if ($choice -eq '1') { 'codex' } elseif ($choice -eq '2') { 'claude-code' } else { throw 'Provider 选择无效。' }
    $candidate = @($discovery.providers | Where-Object provider -eq $provider | Select-Object -First 1)[0]
    $suggestedId = if ($provider -eq 'codex') { 'src_codex_local' } else { 'src_claude_local' }
    $sourceId = Read-Host "Source ID [$suggestedId]"
    if ([string]::IsNullOrWhiteSpace($sourceId)) { $sourceId = $suggestedId }
    $root = Read-Host "会话根目录 [$($candidate.suggested_root)]"
    if ([string]::IsNullOrWhiteSpace($root)) { $root = [string]$candidate.suggested_root }
    Write-Warning '该授权仅允许 metadata Catalog/attach，不会把目录变成普通文件工具白名单。'
    if ((Read-Host '输入 ADD 确认') -cne 'ADD') { return }
    [void](Invoke-AgentSourceAdmin -Config $Config -Arguments @(
        'add', '--source-id', $sourceId, '--provider', $provider, '--root', $root
    ))
    Write-Host 'Agent source 已保存并收紧 ACL；重启 MCP/Tunnel 后加载新 policy。' -ForegroundColor Green
}

function Toggle-AgentSourceInteractive {
    param([hashtable]$Config)

    $source = Select-AgentSourceInteractive -Config $Config
    if ($null -eq $source) { return }
    $next = if ([bool]$source.enabled) { 'false' } else { 'true' }
    [void](Invoke-AgentSourceAdmin -Config $Config -Arguments @(
        'set-enabled', '--source-id', [string]$source.source_id, '--enabled', $next
    ))
    Write-Host "Source 已$(if ($next -eq 'true') { '启用' } else { '禁用' })；重启 MCP/Tunnel 后生效。" -ForegroundColor Green
}

function Remove-AgentSourceInteractive {
    param([hashtable]$Config)

    $source = Select-AgentSourceInteractive -Config $Config
    if ($null -eq $source) { return }
    if ((Read-Host "输入 DELETE 删除 $($source.source_id)") -cne 'DELETE') { return }
    [void](Invoke-AgentSourceAdmin -Config $Config -Arguments @(
        'remove', '--source-id', [string]$source.source_id
    ))
    Write-Host 'Agent source 已删除且不会再暴露；如需清除磁盘上的旧 metadata rows，可停止服务后重建 Catalog。' -ForegroundColor Green
}

function Refresh-AgentSourceInteractive {
    param([hashtable]$Config)

    $source = Select-AgentSourceInteractive -Config $Config
    if ($null -eq $source) { return }
    if (-not [bool]$source.enabled) { throw '请先启用该 source。' }
    Write-Host '只会有界读取 provider 会话 metadata，不输出 transcript 正文。' -ForegroundColor DarkGray
    $result = Invoke-AgentSourceAdmin -Config $Config -Arguments @(
        'refresh', '--source-id', [string]$source.source_id
    )
    $result | Format-List
}

function Rebuild-AgentCatalogInteractive {
    param([hashtable]$Config)

    $running = @(Get-RunningTunnelRecords -Config $Config)
    if ($running.Count) { throw '请先停止所有正在运行的 Tunnel/MCP，再重建 Catalog。' }
    Write-Warning '重建会把现有 SQLite/WAL/SHM 移为带时间戳的可恢复备份，然后重新索引所有已启用 source。'
    if ((Read-Host '输入 REBUILD 确认') -cne 'REBUILD') { return }
    $result = Invoke-AgentSourceAdmin -Config $Config -Arguments @('rebuild')
    Write-Host "Catalog 重建完成；备份文件数：$(@($result.backup_files).Count)" -ForegroundColor Green
}

function Import-AgentSmokeEnvironment {
    $allowlist = Join-Path $script:ProjectRoot 'exec-env.allowlist'
    $names = @()
    if (Test-Path -LiteralPath $allowlist -PathType Leaf) {
        $names = @(Get-Content -LiteralPath $allowlist -Encoding UTF8 |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith('#') })
    }
    foreach ($name in $names) {
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') {
            throw "exec-env.allowlist 包含无效变量名：$name"
        }
    }
    $envPath = Join-Path $script:ProjectRoot '.env'
    if ((Test-Path -LiteralPath $envPath -PathType Leaf) -and $names.Count) {
        $selected = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($name in $names) { [void]$selected.Add($name) }
        foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
            if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
            $name = $Matches[1]
            if (-not $selected.Contains($name)) { continue }
            if ($null -ne [Environment]::GetEnvironmentVariable($name, 'Process')) { continue }
            $value = $Matches[2]
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            Set-Item -Path ("Env:{0}" -f $name) -Value $value
        }
    }
    return $names
}

function Invoke-AgentSmokeInteractive {
    param([hashtable]$Config)

    if (@(Get-RunningTunnelRecords -Config $Config).Count) {
        throw '请先停止所有 Tunnel/MCP，再运行独立 smoke，避免同时操作工作区。'
    }
    $discovery = Invoke-AgentSourceAdmin -Config $Config -Arguments @('discover')
    $providers = @($discovery.providers)
    Write-Host '  1. Codex (codex-default)'
    Write-Host '  2. Claude Code (claude-default)'
    $choice = Read-Host '选择要真实测试的 provider（留空取消）'
    if ([string]::IsNullOrWhiteSpace($choice)) { return }
    if ($choice -eq '1') {
        $provider = 'codex'
        $profileName = 'codex-default'
        $confirmation = 'RUN CODEX'
    } elseif ($choice -eq '2') {
        $provider = 'claude-code'
        $profileName = 'claude-default'
        $confirmation = 'RUN CLAUDE'
    } else { throw 'Provider 选择无效。' }
    $record = @($providers | Where-Object provider -eq $provider | Select-Object -First 1)
    if (-not $record.Count -or -not [bool]$record[0].cli_available) {
        throw "$provider CLI 当前不可用。"
    }
    Write-Warning '这会发送一次真实模型请求、消耗对应账号额度，并在 provider 原生目录创建一条最小会话记录。'
    Write-Host '请求固定为 read-only，要求不使用工具且只回复 TIANCHENG_SMOKE_OK；不会输出回复正文或 key/token。' -ForegroundColor DarkGray
    if ((Read-Host "输入 $confirmation 确认") -cne $confirmation) { return }
    $arguments = @('smoke', '--profile', $profileName, '--timeout-seconds', '180')
    if ($provider -eq 'codex') {
        foreach ($name in @(Import-AgentSmokeEnvironment)) {
            $arguments += @('--pass-env', $name)
        }
    }
    $result = Invoke-AgentSourceAdmin -Config $Config -Arguments $arguments
    if (-not [bool]$result.marker_verified) { throw 'Smoke marker 未通过验证。' }
    Write-Host "$profileName smoke 通过，耗时 $($result.duration_seconds)s。" -ForegroundColor Green
}

function Show-AgentSourceMenu {
    param([hashtable]$Config)

    while ($true) {
        if (-not [Console]::IsOutputRedirected) { Clear-Host }
        Write-Host "`n本地 Agent / 会话源管理" -ForegroundColor Cyan
        [void](Show-AgentSourceState -Config $Config)
        Write-Host '  1. 添加固定 provider 会话源'
        Write-Host '  2. 启用 / 禁用 source'
        Write-Host '  3. 删除 source'
        Write-Host '  4. 验证配置'
        Write-Host '  5. 刷新一个 source 的 metadata Catalog'
        Write-Host '  6. 重建 Catalog（需先停止 Tunnel/MCP）'
        Write-Host '  7. 真实最小 Agent smoke（消耗额度，二次确认）'
        Write-Host '  0. 返回'
        try {
            switch (Read-Host '选择') {
                '1' { Add-AgentSourceInteractive -Config $Config; Pause-Tq }
                '2' { Toggle-AgentSourceInteractive -Config $Config; Pause-Tq }
                '3' { Remove-AgentSourceInteractive -Config $Config; Pause-Tq }
                '4' { [void](Invoke-AgentSourceAdmin -Config $Config -Arguments @('validate')); Write-Host 'Agent source policy 验证通过。' -ForegroundColor Green; Pause-Tq }
                '5' { Refresh-AgentSourceInteractive -Config $Config; Pause-Tq }
                '6' { Rebuild-AgentCatalogInteractive -Config $Config; Pause-Tq }
                '7' { Invoke-AgentSmokeInteractive -Config $Config; Pause-Tq }
                '0' { return }
                default { Write-Host '无效选择。' -ForegroundColor Yellow; Pause-Tq }
            }
        } catch {
            Write-Host "操作失败：$($_.Exception.Message)" -ForegroundColor Red
            Pause-Tq
        }
    }
}

function Pause-Tq {
    if (-not $NoPause) { [void](Read-Host '按回车继续') }
}

function Show-KeyMenu {
    param([hashtable]$Config)

    while ($true) {
        Write-Host "`n密钥管理（永远不会显示密钥值）" -ForegroundColor Cyan
        Show-KeyStatus -Config $Config
        Write-Host '  1. 写入当前 PowerShell 环境'
        Write-Host '  2. 写入 Windows 用户环境变量'
        Write-Host '  3. 写入项目 .env（明文 + 收紧 ACL）'
        Write-Host '  4. 删除 Windows 用户环境变量'
        Write-Host '  5. 删除项目 .env'
        Write-Host '  0. 返回'
        switch (Read-Host '选择') {
            '1' { Set-ProcessKeyInteractive }
            '2' { Set-UserKeyInteractive }
            '3' { Set-DotEnvKeyInteractive -Config $Config }
            '4' {
                if ((Read-Host '输入 DELETE 确认') -ceq 'DELETE') {
                    [Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', $null, 'User')
                    Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
                    Write-Host '已删除用户环境变量。' -ForegroundColor Green
                }
            }
            '5' {
                if ((Test-Path -LiteralPath ([string]$Config.envFile)) -and (Read-Host '输入 DELETE 确认') -ceq 'DELETE') {
                    Remove-Item -LiteralPath ([string]$Config.envFile) -Force
                    Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
                    Write-Host '已删除 .env。' -ForegroundColor Green
                }
            }
            '0' { return }
            default { Write-Host '无效选择。' -ForegroundColor Yellow }
        }
    }
}

function Show-ProfileMenu {
    param([hashtable]$Config)

    while ($true) {
        Write-Host "`nProfile 管理" -ForegroundColor Cyan
        Show-Profiles -Config $Config
        Write-Host '  1. 创建或重建 Profile'
        Write-Host '  2. 选择默认 Profile'
        Write-Host '  3. 使用 tunnel-client 编辑 Profile'
        Write-Host '  4. 一键切换当前 Profile 为 SAFE'
        Write-Host '  5. 一键切换当前 Profile 为 DEV（工作区命令 + 远程 Git）'
        Write-Host '  6. 启用聊天外部授权（TOTP）'
        Write-Host '  7. 启用聊天外部授权 + Exec（外部命令也可用）'
        Write-Host '  8. 开启策略热重载（高危：批准后可当场扩大白名单，无需重启）'
        Write-Host '  9. 关闭策略热重载（回到冷重载：改白名单必须重启）'
        Write-Host '  0. 返回'
        switch (Read-Host '选择') {
            '1' { Configure-ProfileInteractive -Config (Get-LauncherConfig); $Config = Get-LauncherConfig }
            '2' { Select-ProfileInteractive -Config $Config; $Config = Get-LauncherConfig }
            '3' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                $arguments = @('profiles', 'edit', $selected)
                $arguments += Get-ProfileDirectoryArguments -Config $Config
                & ([string]$Config.tunnelClient) @arguments
            }
            '4' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                Set-ProfileMode -Config $Config -Name $selected -ExecMode:$false -AlreadyConfirmed:$true
                $Config = Get-LauncherConfig
            }
            '5' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                Set-ProfileMode -Config $Config -Name $selected -ExecMode:$true -AlreadyConfirmed:$false
                $Config = Get-LauncherConfig
            }
            '6' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                Set-ProfileExternalGrants -Config $Config -Name $selected -ExecMode:$false
                $Config = Get-LauncherConfig
            }
            '7' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                Set-ProfileExternalGrants -Config $Config -Name $selected -ExecMode:$true
                $Config = Get-LauncherConfig
            }
            '8' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                $mode = Get-ProfileMode -Config $Config -Name $selected
                if ($mode -notlike 'GRANTS*' -and $mode -notlike 'DEV*') {
                    Write-Host '热重载需要先切换到聊天外部授权或 DEV Profile（第 5/6/7 项）。' -ForegroundColor Yellow
                } else {
                    Set-ProfileExternalGrants -Config $Config -Name $selected `
                        -ExecMode:($mode -like '*EXEC*') -HotReload:$true
                    $Config = Get-LauncherConfig
                }
            }
            '9' {
                $selected = Resolve-SelectedProfile -Config $Config -Requested $Profile
                $mode = Get-ProfileMode -Config $Config -Name $selected
                if (-not $mode.EndsWith('+HOT')) {
                    Write-Host '当前 Profile 已经是冷重载。' -ForegroundColor Yellow
                } else {
                    Set-ProfileExternalGrants -Config $Config -Name $selected `
                        -ExecMode:($mode -like '*EXEC*') -HotReload:$false
                    Write-Host '已回到冷重载：白名单改动需要重启 MCP/Tunnel 才生效。' -ForegroundColor Green
                    $Config = Get-LauncherConfig
                }
            }
            '0' { return }
            default { Write-Host '无效选择。' -ForegroundColor Yellow }
        }
    }
}

function Install-Alias {
    $installer = Join-Path $script:ProjectRoot 'install-tc.ps1'
    Assert-FileExists -Path $installer -Label 'Alias installer'
    & $installer
}

function Setup-Totp {
    $python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    Assert-FileExists -Path $python -Label 'Python environment'
    $setup = Join-Path $script:ProjectRoot 'scripts\setup_totp.py'
    Assert-FileExists -Path $setup -Label 'TOTP setup script'
    & $python $setup
    if ($LASTEXITCODE -ne 0) { throw "TOTP setup failed with exit code $LASTEXITCODE." }
}

function Show-MainMenu {
    while ($true) {
        $config = Get-LauncherConfig
        $selected = Resolve-SelectedProfile -Config $config -Requested $Profile
        $key = Get-KeyRecord -Config $config
        $modeLabel = Get-ProfileMode -Config $config -Name $selected
        $runningProfiles = @(Get-RunningTunnelRecords -Config $config | ForEach-Object Profile | Sort-Object -Unique)
        Clear-Host
        Write-Host '╔══════════════════════════════════════╗' -ForegroundColor Cyan
        Write-Host '║       天成 Local MCP 控制台          ║' -ForegroundColor Cyan
        Write-Host '╚══════════════════════════════════════╝' -ForegroundColor Cyan
        Write-Host "Profile: $selected [$modeLabel]   Key: $($key.Configured)" -ForegroundColor DarkGray
        if ($modeLabel -eq 'GRANTS') {
            Write-Host '能力提示：聊天外部授权已开；工作区 run_command/远程 Git 未开。到“6→7”可开启外部 Exec。' -ForegroundColor Yellow
        } elseif ($modeLabel -eq 'GRANTS+EXEC') {
            Write-Host '能力提示：外部授权、工作区命令、远程 Git 和外部 Exec 均已开。' -ForegroundColor Yellow
        } elseif ($modeLabel -eq 'DEV') {
            Write-Host '能力提示：工作区命令与远程 Git 已开；外部路径仍需单独启用聊天授权。' -ForegroundColor Yellow
        } elseif ($modeLabel -eq 'SAFE') {
            Write-Host '能力提示：仅工作区文件与本地 Git，命令执行关闭。' -ForegroundColor DarkGray
        }
        Write-Host "MCP 自动转后台等待: $($config.interactiveTimeoutSeconds)s" -ForegroundColor DarkGray
        Write-Host "Running: $(if ($runningProfiles.Count) { $runningProfiles -join ', ' } else { 'none' })" -ForegroundColor DarkGray
        Write-Host
        Write-Host '  1. 一键检查并启动 MCP + Tunnel'
        Write-Host '  2. 在新窗口启动 MCP + Tunnel'
        Write-Host '  3. 停止当前 Profile'
        Write-Host '  4. 重启当前 Profile（新窗口）'
        Write-Host '  5. Doctor 检查'
        Write-Host '  6. Profile 管理 / SAFE-DEV 切换'
        Write-Host '  7. API Key 管理'
        Write-Host '  8. 状态（含 Git/GCM/gh）'
        Write-Host '  9. 打开 Tunnel 管理 UI'
        Write-Host '  A. 启动器设置'
        Write-Host '  B. 安装/修复 tc 快捷命令'
        Write-Host '  C. TOTP 二维码初始化'
        Write-Host '  D. 外部路径白名单 / 访问策略'
        Write-Host '  E. 本地 Agent / 会话源管理'
        Write-Host '  0. 退出'
        try {
            switch (Read-Host '选择') {
                '1' { Start-TunnelForeground -Config $config -Name $selected -SkipDoctorCheck:$false -ExecAlreadyAllowed:$false }
                '2' { Start-TunnelWindow -Config $config -Name $selected -ExecAlreadyAllowed:$false; Pause-Tq }
                '3' { [void](Stop-TunnelProfile -Config $config -Name $selected -Confirmed:$false); Pause-Tq }
                '4' { Restart-TunnelProfile -Config $config -Name $selected -Confirmed:$false; Pause-Tq }
                '5' { [void](Invoke-Doctor -Config $config -Name $selected); Pause-Tq }
                '6' { Show-ProfileMenu -Config $config }
                '7' { Show-KeyMenu -Config $config }
                '8' { Show-Status -Config $config; Pause-Tq }
                '9' { Open-AdminUi -Config $config; Pause-Tq }
                { $_ -match '^(?i)a$' } { Edit-SettingsInteractive -Config $config; Pause-Tq }
                { $_ -match '^(?i)b$' } { Install-Alias; Pause-Tq }
                { $_ -match '^(?i)c$' } { Setup-Totp; Pause-Tq }
                { $_ -match '^(?i)d$' } { Show-AccessPolicyMenu }
                { $_ -match '^(?i)e$' } { Show-AgentSourceMenu -Config $config }
                '0' { return }
                default { Write-Host '无效选择。' -ForegroundColor Yellow; Pause-Tq }
            }
        } catch {
            Write-Host "操作失败：$($_.Exception.Message)" -ForegroundColor Red
            Pause-Tq
        }
    }
}

$config = Get-LauncherConfig
$selectedProfile = Resolve-SelectedProfile -Config $config -Requested $Profile

switch ($Action) {
    'menu' { Show-MainMenu }
    'start' {
        Start-TunnelForeground -Config $config -Name $selectedProfile -SkipDoctorCheck:$SkipDoctor -ExecAlreadyAllowed:$AllowExecProfile
    }
    'start-new' {
        Start-TunnelWindow -Config $config -Name $selectedProfile -ExecAlreadyAllowed:$AllowExecProfile
    }
    'doctor' { exit (Invoke-Doctor -Config $config -Name $selectedProfile) }
    'profiles' { Show-Profiles -Config $config }
    'configure-profile' { Configure-ProfileInteractive -Config $config }
    'select-profile' { Select-ProfileInteractive -Config $config }
    'edit-profile' {
        $arguments = @('profiles', 'edit', $selectedProfile)
        $arguments += Get-ProfileDirectoryArguments -Config $config
        & ([string]$config.tunnelClient) @arguments
    }
    'set-mode' {
        if ([string]::IsNullOrWhiteSpace($Mode)) { throw '-Mode safe|dev is required.' }
        Set-ProfileMode -Config $config -Name $selectedProfile -ExecMode:($Mode -eq 'dev') -AlreadyConfirmed:$AllowExecProfile
    }
    'stop' { [void](Stop-TunnelProfile -Config $config -Name $selectedProfile -Confirmed:$Force) }
    'restart' { Restart-TunnelProfile -Config $config -Name $selectedProfile -Confirmed:$Force }
    'key' { Show-KeyMenu -Config $config }
    'key-status' { Show-KeyStatus -Config $config }
    'status' { Show-Status -Config $config }
    'open-ui' { Open-AdminUi -Config $config }
    'settings' { Edit-SettingsInteractive -Config $config }
    'info' { Show-Info -Config $config }
    'install-alias' { Install-Alias }
    'totp-setup' { Setup-Totp }
    'policy' { Show-AccessPolicy }
    'agents' {
        if ($Json) { Show-AgentSourceState -Config $config }
        else { Show-AgentSourceMenu -Config $config }
    }
}
