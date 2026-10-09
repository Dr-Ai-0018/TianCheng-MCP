[CmdletBinding()]
param(
    [ValidateSet(
        'menu', 'start', 'start-new', 'doctor', 'profiles', 'configure-profile',
        'select-profile', 'edit-profile', 'key', 'key-status', 'status',
        'set-mode', 'stop', 'restart', 'open-ui', 'settings', 'proxy', 'info', 'install-alias', 'policy', 'agents', 'commands'
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
. (Join-Path $script:ProjectRoot 'scripts\menu-help.ps1')
. (Join-Path $script:ProjectRoot 'scripts\tui\console.ps1')
$script:WorkspaceCache = ''
$script:ProxyProbeResult = $null
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

function Resolve-LauncherConfig {
    param([Parameter(Mandatory)][hashtable]$Config)

    foreach ($pair in @(
        @{ Key = 'tunnelClient'; Command = 'tunnel-client' },
        @{ Key = 'powerShell'; Command = 'pwsh' }
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

function Get-Workspace {
    if (-not [string]::IsNullOrWhiteSpace($script:WorkspaceCache)) {
        return $script:WorkspaceCache
    }
    $config = Get-LauncherConfig
    $value = if ($config.ContainsKey('workspace')) { [string]$config.workspace } else { '' }
    if ($env:TIANCHENG_WORKSPACE) { $value = [string]$env:TIANCHENG_WORKSPACE }
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw ("No workspace is configured. Set 'workspace' in " +
            "config\launcher.local.json or set TIANCHENG_WORKSPACE.")
    }
    $script:WorkspaceCache = [System.IO.Path]::GetFullPath($value)
    return $script:WorkspaceCache
}

function Get-LauncherConfig {
    $bootstrap = if ($env:TIANCHENG_PYTHON) { $env:TIANCHENG_PYTHON }
    else { Join-Path $script:ProjectRoot '.venv\Scripts\python.exe' }
    $resolved = if ($script:TuiStatusProbe -or $Action -eq 'menu') {
        Invoke-TuiProbeProcess -FilePath $bootstrap -Arguments @((Join-Path $script:ProjectRoot 'scripts\resolve_launcher.py'),'--local',$script:LocalConfigPath)
    } else {
        & $bootstrap (Join-Path $script:ProjectRoot 'scripts\resolve_launcher.py') --local $script:LocalConfigPath
    }
    if ($LASTEXITCODE -ne 0) { throw 'Launcher configuration could not be resolved' }
    $config = ($resolved -join "`n") | ConvertFrom-Json -AsHashtable
    return (Resolve-LauncherConfig -Config $config)
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
        throw "$Label is not configured and was not found on PATH."
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
    $raw = if ($script:TuiStatusProbe) {
        Invoke-TuiProbeProcess -FilePath ([string]$Config.tunnelClient) -Arguments (@('profiles','list','--json') + $profileArguments)
    } else { & ([string]$Config.tunnelClient) profiles list --json @profileArguments 2>$null }
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
    $raw = if ($script:TuiStatusProbe) {
        Invoke-TuiProbeProcess -FilePath ([string]$Config.tunnelClient) -Arguments (@('profiles','list','--json') + $profileArguments)
    } else { & ([string]$Config.tunnelClient) profiles list --json @profileArguments 2>$null }
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

function Assert-ProfileConfiguration {
    param([hashtable]$Config, [string]$Name)

    $record = Get-ProfileRecord -Config $Config -Name $Name
    if ($null -eq $record -or -not (Test-Path -LiteralPath ([string]$record.Path) -PathType Leaf)) {
        throw 'Profile configuration cannot be inspected before launch.'
    }
    $text = [System.IO.File]::ReadAllText([string]$record.Path, [System.Text.Encoding]::UTF8).Replace('\"', '"')
    $match = [regex]::Match($text, '(?i)(?:^|\s)-ConfigPath\s+(?:"([^"]+)"|([^\s"'']+))')
    $selected = [System.IO.Path]::GetFullPath($script:LocalConfigPath)
    if ($match.Success) {
        $stored = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        if ([System.IO.Path]::GetFullPath($stored) -eq $selected) { return }
    } elseif ($selected -eq [System.IO.Path]::GetFullPath((Join-Path $script:ProjectRoot 'config\launcher.local.json'))) {
        return  # Older default profiles still select the same default file.
    }
    throw 'Profile uses a different launcher configuration. Run set-mode or edit-profile with this -ConfigPath to update the existing profile before starting.'
}

function Get-ProfileDirectoryArguments {
    param([hashtable]$Config)

    if ($Config.ContainsKey('profileDir') -and -not [string]::IsNullOrWhiteSpace([string]$Config.profileDir)) {
        return @('--profile-dir', [string]$Config.profileDir)
    }
    return @()
}

function Test-SupervisorEnabled {
    param([hashtable]$Config)

    return -not $Config.ContainsKey('supervisor') -or
        -not $Config.supervisor.ContainsKey('enabled') -or [bool]$Config.supervisor.enabled
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
        throw '.env must remain outside the configured workspace.'
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
        (Quote-CommandPart -Value $mcpCommandPath),
        '-ConfigPath',
        (Quote-CommandPart -Value $script:LocalConfigPath.Replace('\', '/'))
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
        Write-Warning 'Exec 模式不是 OS sandbox，代码可能访问配置工作区之外。'
        if ((Read-Host '请输入 ENABLE EXEC 确认') -cne 'ENABLE EXEC') {
            throw 'Exec profile creation cancelled.'
        }
    }
    if ($externalGrants) {
        Assert-FileExists -Path ([string]$Config.mcpGrantsScript) -Label 'External grants MCP startup script'
        Write-Warning '聊天外部授权会允许 ChatGPT 在用户明确确认一次性 challenge 后访问配置工作区之外的目录。'
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
    param([hashtable]$Config, [string]$Name, [ref]$Report)

    if (-not (Test-ProfileExists -Config $Config -Name $Name)) {
        throw "Profile '$Name' does not exist."
    }
    Assert-ProfileConfiguration -Config $Config -Name $Name
    $key = Import-ControlPlaneKey -Config $Config
    if (-not $key.Configured) {
        throw 'CONTROL_PLANE_API_KEY is not configured. Use the key menu first.'
    }
    $sourceLine="使用密钥来源：$($key.Source)（值不会显示）"
    if ($null -ne $Report) { $Report.Value=@($sourceLine) }
    else { Write-Host $sourceLine -ForegroundColor DarkGray }
    $arguments = @('doctor', '--profile', $Name, '--explain')
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    if ($null -ne $Report) {
        $output=@(& ([string]$Config.tunnelClient) @arguments 2>&1)
        $exitCode=$LASTEXITCODE
        $Report.Value += @($output | ForEach-Object { [string]$_ })
    } else {
        & ([string]$Config.tunnelClient) @arguments | Out-Host
        $exitCode=$LASTEXITCODE
    }
    if ($exitCode -eq 0 -and (Test-SupervisorEnabled -Config $Config)) {
        Assert-FileExists -Path ([string]$Config.python) -Label 'TianCheng Python environment'
        $supervisorArguments = @(
            '-m', 'tiancheng_mcp.tunnel_supervisor',
            '--defaults', $script:DefaultsPath,
            '--local-config', $script:LocalConfigPath,
            '--profile', $Name,
            '--check'
        )
        if ($null -ne $Report) {
            $output=@(& ([string]$Config.python) @supervisorArguments 2>&1)
            $exitCode=$LASTEXITCODE
            $Report.Value += @($output | ForEach-Object { [string]$_ })
        } else {
            & ([string]$Config.python) @supervisorArguments | Out-Host
            $exitCode=$LASTEXITCODE
        }
    }
    return $exitCode
}

function Show-DoctorResult {
    param([hashtable]$Config, [string]$Name)
    $report=@()
    $exitCode=Invoke-Doctor -Config $Config -Name $Name -Report ([ref]$report)
    $label=if ($exitCode -eq 0) { '检查通过' } else { '检查失败；请按上面的诊断处理后重试' }
    $report += "Doctor 退出码：$exitCode · $label"
    $report += '检查通过不等于 MCP 文件工具 roundtrip 已验收。'
    Show-TuiResult -Title '连接准备检查 / Doctor' -Lines $report
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
    Assert-TunnelCanStart -Config $Config -Name $Name
    Assert-ProfileConfiguration -Config $Config -Name $Name
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
    $supervisorEnabled = Test-SupervisorEnabled -Config $Config
    if ($supervisorEnabled) {
        Assert-FileExists -Path ([string]$Config.python) -Label 'TianCheng Python environment'
        $supervisorArguments = @(
            '-m', 'tiancheng_mcp.tunnel_supervisor',
            '--defaults', $script:DefaultsPath,
            '--local-config', $script:LocalConfigPath,
            '--profile', $Name,
            '--state-dir', (Join-Path $script:ProjectRoot 'state')
        )
        & ([string]$Config.python) @supervisorArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Tunnel supervisor exited with code $LASTEXITCODE."
        }
        return
    }
    $arguments = @('run', '--profile', $Name)
    $arguments += Get-ProfileDirectoryArguments -Config $Config
    & ([string]$Config.tunnelClient) @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "tunnel-client exited with code $LASTEXITCODE."
    }
}

function Start-TunnelWindow {
    param([hashtable]$Config, [string]$Name, [bool]$ExecAlreadyAllowed)

    if (-not (Test-ProfileExists -Config $Config -Name $Name)) {
        throw "Profile '$Name' does not exist. Create it from the profile menu first."
    }
    Assert-TunnelCanStart -Config $Config -Name $Name
    Assert-ProfileConfiguration -Config $Config -Name $Name
    Confirm-ExecProfile -Config $Config -Name $Name -AlreadyAllowed $ExecAlreadyAllowed
    $key = Import-ControlPlaneKey -Config $Config
    if (-not $key.Configured) {
        throw 'CONTROL_PLANE_API_KEY is not configured. Use the key menu first.'
    }
    Assert-FileExists -Path ([string]$Config.powerShell) -Label 'PowerShell 7'
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NoExit', '-File', $PSCommandPath,
        '-Action', 'start', '-Profile', $Name,
        '-ConfigPath', (Quote-CommandPart -Value $script:LocalConfigPath.Replace('\', '/'))
    )
    if ($SkipDoctor) { $arguments += '-SkipDoctor' }
    if ((Get-ProfileMode -Config $Config -Name $Name) -eq 'DEV' -or $ExecAlreadyAllowed) {
        $arguments += '-AllowExecProfile'
    }
    Start-Process -FilePath ([string]$Config.powerShell) -ArgumentList $arguments -WindowStyle Normal
    Write-Host "已在新窗口发起 '$Name' 启动；请在新窗口确认 Doctor 和运行结果。" -ForegroundColor Green
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

function Get-HealthListenerConflict {
    param([hashtable]$Config)

    $uri = [Uri][string]$Config.healthBaseUrl
    $address = $null
    if ($uri.Host -eq 'localhost') {
        $address = [System.Net.IPAddress]::Loopback
    } elseif (-not [System.Net.IPAddress]::TryParse($uri.Host, [ref]$address)) {
        return $null
    }
    if (-not [System.Net.IPAddress]::IsLoopback($address)) { return $null }

    $listener = [System.Net.Sockets.TcpListener]::new($address, [int]$uri.Port)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $null
    } catch [System.Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -ne [System.Net.Sockets.SocketError]::AddressAlreadyInUse) {
            throw
        }
        $owner = $null
        try {
            $owner = if (-not $script:TuiStatusProbe) { Get-NetTCPConnection -LocalPort $uri.Port -State Listen -ErrorAction Stop |
                Where-Object { $_.LocalAddress -in @($uri.Host, '0.0.0.0', '::') } |
                Select-Object -First 1 }
        } catch { }
        return [PSCustomObject]@{
            Endpoint = '{0}:{1}' -f $uri.Host, $uri.Port
            Port = [int]$uri.Port
            ProcessId = if ($null -ne $owner) { [int]$owner.OwningProcess } else { $null }
        }
    } finally {
        $listener.Stop()
    }
}

function Assert-TunnelCanStart {
    param([hashtable]$Config, [string]$Name)

    $existing = @(Get-RunningTunnelRecords -Config $Config | Where-Object Profile -eq $Name)
    $supervisors = @(Get-RunningSupervisorRecords -Config $Config | Where-Object Profile -eq $Name)
    if ($existing.Count -gt 0 -or $supervisors.Count -gt 0) {
        throw "Profile '$Name' 已在运行；请先检查状态，不要重复启动。"
    }
    $conflict = Get-HealthListenerConflict -Config $Config
    if ($null -ne $conflict) {
        $ownerText = if ($null -ne $conflict.ProcessId) { "（PID $($conflict.ProcessId)）" } else { '' }
        throw ("健康端口 $($conflict.Endpoint) 已被占用$ownerText。请检查冲突进程，" +
            "例如运行 netstat -ano -p tcp | findstr :$($conflict.Port)；" +
            "确认是否已有 Tunnel 在运行，不要重复启动或直接结束未知进程。")
    }
}

function Get-RunningTunnelRecords {
    param([hashtable]$Config)

    if (-not $IsWindows) { return @() }
    try {
        $expected = [System.IO.Path]::GetFullPath([string]$Config.tunnelClient)
        return @(
            $(if ($script:TuiStatusProbe) { Get-TuiProcessRecords -Kind tunnel }
                else { Get-CimInstance Win32_Process -Filter "Name = 'tunnel-client.exe'" -ErrorAction Stop }) |
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
        if ($script:TuiStatusProbe) { throw }
        return @()
    }
}

function Get-RunningSupervisorRecords {
    param([hashtable]$Config)

    if (-not $IsWindows -or -not $Config.ContainsKey('python')) { return @() }
    try {
        $expected = [System.IO.Path]::GetFullPath([string]$Config.python)
        $expectedPrefix = '^\s*"?' + [regex]::Escape($expected) +
            '"?\s+-m\s+tiancheng_mcp\.tunnel_supervisor(?:\s|$)'
        return @(
            $(if ($script:TuiStatusProbe) { Get-TuiProcessRecords -Kind supervisor }
                else { Get-CimInstance Win32_Process -ErrorAction Stop }) |
                Where-Object { $_.Name -in @('python.exe', 'pythonw.exe') } |
                ForEach-Object {
                    $commandLine = [string]$_.CommandLine
                    # A Windows venv launcher may report its base uv-managed
                    # interpreter as ExecutablePath.  Validate the exact
                    # configured launcher at argv[0] instead of rejecting it.
                    if ($commandLine -notmatch "(?i)$expectedPrefix") { return }
                    $match = [regex]::Match(
                        $commandLine,
                        '(?i)(?:^|\s)--profile(?:=|\s+)["'']?([A-Za-z0-9._-]+)'
                    )
                    if ($match.Success) {
                        [PSCustomObject]@{ Profile = $match.Groups[1].Value; ProcessId = [int]$_.ProcessId }
                    }
                }
        )
    } catch {
        if ($script:TuiStatusProbe) { throw }
        return @()
    }
}

function Get-SupervisorState {
    param([string]$Name)

    $path = Join-Path $script:ProjectRoot "state\tunnel-supervisor-$Name.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        return Read-JsonHashtable -Path $path
    } catch {
        return @{ state = 'invalid'; mcp_transport = 'unknown'; probe_mode = 'none' }
    }
}

function Get-DeveloperToolStatus {
    $git = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
    $gh = Get-Command gh -ErrorAction SilentlyContinue | Select-Object -First 1
    $gcmConfigured = $false
    $ghAuthenticated = $false
    if ($null -ne $git) {
        if ($script:TuiStatusProbe) {
            try {
                $accounts=Invoke-TuiProbeProcess -FilePath $git.Source -Arguments @('credential-manager','github','list','--no-ui')
                $gcmConfigured=-not [string]::IsNullOrWhiteSpace(($accounts -join "`n"))
            } catch [TimeoutException] { throw }
              catch { $gcmConfigured=$null }
        } else {
            $accounts = & git credential-manager github list --no-ui 2>$null
            $gcmConfigured = $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($accounts -join "`n"))
        }
    }
    if ($null -ne $gh) {
        if ($script:TuiStatusProbe) {
            try {
                [void](Invoke-TuiProbeProcess -FilePath $gh.Source -Arguments @('auth','status'))
                $ghAuthenticated=$true
            } catch [TimeoutException] { throw }
              catch { $ghAuthenticated=$null }
        } else {
            & gh auth status *> $null
            $ghAuthenticated = $LASTEXITCODE -eq 0
        }
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

    $supervisors = @(Get-RunningSupervisorRecords -Config $Config | Where-Object Profile -eq $Name)
    $records = @(Get-RunningTunnelRecords -Config $Config | Where-Object Profile -eq $Name)
    if ($records.Count -eq 0 -and $supervisors.Count -eq 0) {
        Write-Host "Profile '$Name' 当前没有运行中的 Tunnel。" -ForegroundColor Yellow
        return $false
    }
    if (-not $Confirmed -and (Read-Host "停止 '$Name'？输入 STOP") -cne 'STOP') {
        throw 'Tunnel stop cancelled.'
    }
    $stateDirectory = Join-Path $script:ProjectRoot 'state'
    $stopPath = Join-Path $stateDirectory "tunnel-supervisor-$Name.stop"
    $lockPath = Join-Path $stateDirectory "tunnel-supervisor-$Name.lock"
    if ($supervisors.Count -gt 0) {
        if (-not (Test-Path -LiteralPath $stateDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
        }
        [System.IO.File]::WriteAllText(
            $stopPath,
            "stop$([Environment]::NewLine)",
            [System.Text.UTF8Encoding]::new($false)
        )
        $stopDeadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 250
            $supervisors = @(Get-RunningSupervisorRecords -Config $Config | Where-Object Profile -eq $Name)
        } while ($supervisors.Count -gt 0 -and [DateTime]::UtcNow -lt $stopDeadline)
    }
    foreach ($record in $supervisors) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $record.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not force-stop supervisor PID $($record.ProcessId)." }
    }
    Start-Sleep -Milliseconds 300
    $records = @(Get-RunningTunnelRecords -Config $Config | Where-Object Profile -eq $Name)
    foreach ($record in $records) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $record.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not stop tunnel-client PID $($record.ProcessId)." }
    }
    foreach ($runtimePath in @($stopPath, $lockPath)) {
        if (Test-Path -LiteralPath $runtimePath -PathType Leaf) {
            Remove-Item -LiteralPath $runtimePath -Force
        }
    }
    $remainingSupervisors = @(Get-RunningSupervisorRecords -Config $Config | Where-Object Profile -eq $Name)
    $remainingTunnels = @(Get-RunningTunnelRecords -Config $Config | Where-Object Profile -eq $Name)
    if ($remainingSupervisors.Count -gt 0 -or $remainingTunnels.Count -gt 0) {
        throw "Profile '$Name' still has managed processes after stop."
    }
    Write-Host "已停止 '$Name' 的 Supervisor 和 Tunnel 进程树。" -ForegroundColor Green
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
    $supervisors = @(Get-RunningSupervisorRecords -Config $Config)
    $supervisorState = Get-SupervisorState -Name $selected
    $supervisorAlive = @($supervisors | Where-Object Profile -eq $selected).Count -gt 0
    $recordedTerminal = $null -ne $supervisorState -and
        [string]$supervisorState.state -in @('stopped', 'failed')
    $supervisorLabel = if ($supervisorAlive -and $null -ne $supervisorState) {
        [string]$supervisorState.state
    } elseif ($supervisorAlive) { 'starting' }
    elseif ($recordedTerminal) { [string]$supervisorState.state }
    elseif ($null -ne $supervisorState) { 'stale' }
    else { 'not-started' }
    $mcpTransport = if ($supervisorAlive -and $null -ne $supervisorState) {
        [string]$supervisorState.mcp_transport
    } elseif ($recordedTerminal) { [string]$supervisorState.mcp_transport }
    else { 'unverified' }
    $probeMode = if ($supervisorAlive -and $null -ne $supervisorState) {
        [string]$supervisorState.probe_mode
    } elseif ($recordedTerminal) { [string]$supervisorState.probe_mode }
    else { 'none' }
    $mcpInferred = if ($supervisorAlive -and $null -ne $supervisorState) {
        [string]$supervisorState.state
    } elseif ($recordedTerminal) { [string]$supervisorState.state }
    else { 'unverified' }
    # tunnel-client v0.0.12 skips a real MCP probe for stdio.  Keep this
    # separate from inferred health so /readyz can never masquerade as an
    # end-to-end tool roundtrip.
    $mcpVerified = 'not-available'
    $configuredTtl = if ($Config.ContainsKey('supervisor') -and
        $Config.supervisor.ContainsKey('mcpConnectionMaxTtl')) {
        [string]$Config.supervisor.mcpConnectionMaxTtl
    } else { '24h' }
    $developer = Get-DeveloperToolStatus
    $status = [ordered]@{
        selectedProfile = $selected
        profileExists = $profiles -contains $selected
        selectedMode = Get-ProfileMode -Config $Config -Name $selected
        configuredProfiles = $profiles
        runningProfiles = @(
            @($running | ForEach-Object Profile) + @($supervisors | ForEach-Object Profile) |
                Sort-Object -Unique
        )
        supervisorState = $supervisorLabel
        mcpTransport = $mcpTransport
        mcpProbeMode = $probeMode
        mcpInferred = $mcpInferred
        mcpVerified = $mcpVerified
        mcpConnectionMaxTtl = $configuredTtl
        lastMcpHealthyAt = if ($null -ne $supervisorState) { $supervisorState.last_healthy_at } else { $null }
        lastRecoveryReason = if ($null -ne $supervisorState) { $supervisorState.last_recovery_reason } else { $null }
        supervisorGeneration = if ($null -ne $supervisorState) { $supervisorState.generation } else { $null }
        restartCountWindow = if ($null -ne $supervisorState) { $supervisorState.restart_count_window } else { 0 }
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
    Write-Host "Supervisor 状态 : $($status.supervisorState)"
    Write-Host "MCP Inferred     : $($status.mcpInferred) ($($status.mcpProbeMode))"
    Write-Host "MCP Verified     : $($status.mcpVerified)"
    Write-Host "MCP Connection TTL: $($status.mcpConnectionMaxTtl)（不是 Agent 运行上限）"
    Write-Host "Generation / 恢复数: $($status.supervisorGeneration) / $($status.restartCountWindow)"
    Write-Host "密钥已配置   : $($status.keyConfigured)"
    Write-Host "密钥来源     : $($status.keySource)"
    Write-Host "Tunnel Ready     : $($status.tunnelReady)（不等于 MCP roundtrip 已验证）"
    Write-Host "管理地址     : $($status.healthBaseUrl)/ui"
    $gcmLabel=if ($null -eq $developer.gcmConfigured) { '未知（查询失败）' } else { [string]$developer.gcmConfigured }
    $ghLabel=if ($null -eq $developer.ghAuthenticated) { '未知（查询失败）' } else { [string]$developer.ghAuthenticated }
    Write-Host "Git / GCM    : $($developer.gitAvailable) / $gcmLabel"
    Write-Host "gh / 已登录  : $($developer.ghAvailable) / $ghLabel"
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
        workspace = [string]$Config.workspace
        python = [string]$Config.python
        accessPolicyPath = [string]$Config.accessPolicyPath
        commandPolicyPath = [string]$Config.commandPolicyPath
        agentSourcesPath = [string]$Config.agentSourcesPath
        agentCatalogPath = [string]$Config.agentCatalogPath
        mcpCommand = if ((Test-Path -LiteralPath ([string]$Config.powerShell) -PathType Leaf) -and
            (Test-Path -LiteralPath ([string]$Config.mcpScript) -PathType Leaf)) {
            Get-McpCommand -Config $Config -ExecMode $false
        } else { $null }
        defaultProfile = [string]$Config.defaultProfile
        tunnelClientExists = Test-Path -LiteralPath ([string]$Config.tunnelClient) -PathType Leaf
        supervisorPythonExists = Test-Path -LiteralPath ([string]$Config.python) -PathType Leaf
        supervisorEnabled = Test-SupervisorEnabled -Config $Config
        tunnelLaunchMode = if (Test-SupervisorEnabled -Config $Config) { 'supervised' } else { 'direct' }
        mcpConnectionMaxTtl = if ($Config.ContainsKey('supervisor') -and
            $Config.supervisor.ContainsKey('mcpConnectionMaxTtl')) {
            [string]$Config.supervisor.mcpConnectionMaxTtl
        } else { '24h' }
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

function Get-ProxyOverrides {
    $local = Read-JsonHashtable -Path $script:LocalConfigPath
    $proxy = @{}
    if ($local.ContainsKey('proxy') -and $local.proxy -is [System.Collections.IDictionary]) {
        foreach ($entry in $local.proxy.GetEnumerator()) {
            $proxy[$entry.Key] = $entry.Value
        }
    }
    return $proxy
}

function Show-ProxySummary {
    param([System.Collections.IDictionary]$Overrides, [switch]$Pending)

    $defaults = Read-JsonHashtable -Path $script:DefaultsPath
    $local = if ($null -eq $Overrides) { Get-ProxyOverrides } else { $Overrides }
    $defaultProxy = if ($defaults.ContainsKey('proxy') -and
        $defaults.proxy -is [System.Collections.IDictionary]) { $defaults.proxy } else { @{} }
    Write-Host '出站代理（显示主机/端口；隐藏用户名和密码）：' -ForegroundColor Cyan
    $hasProxy = $false
    foreach ($field in @('http', 'https', 'noProxy')) {
        $names = switch ($field) {
            'http' { @('HTTP_PROXY', 'http_proxy') }
            'https' { @('HTTPS_PROXY', 'https_proxy') }
            'noProxy' { @('NO_PROXY', 'no_proxy') }
        }
        $value = $null
        $source = '默认'
        if ($local.ContainsKey($field)) {
            $value = [string]$local[$field]
            $source = '本机配置'
        } else {
            foreach ($name in $names) {
                $candidate = [Environment]::GetEnvironmentVariable($name, 'Process')
                if ($null -ne $candidate) {
                    $value = $candidate
                    $source = '进程环境'
                    break
                }
            }
        }
        if ($null -eq $value -and $defaultProxy.ContainsKey($field)) {
            $value = [string]$defaultProxy[$field]
        }
        if ($field -ne 'noProxy' -and -not [string]::IsNullOrEmpty($value)) {
            $hasProxy = $true
        }
        $label = if ([string]::IsNullOrEmpty($value)) { '未配置' }
            elseif ($field -eq 'noProxy') {
                $count = @($value.Split(',') | Where-Object { $_.Trim() }).Count
                "已配置（$count 条直连规则）"
            }
            else {
                $uri = $null
                if ([uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri)) {
                    $auth = if ($uri.UserInfo) { '，含认证' } else { '' }
                    $port = if ($uri.IsDefaultPort) { '' } else { ":$($uri.Port)" }
                    "已配置 $($uri.Scheme)://$($uri.Host)$port$auth"
                } else { 'URL 格式无效' }
            }
        Write-Host "  $field : $label [$source]"
    }
    $agent = if ($local.ContainsKey('agent')) { [string]$local.agent }
        elseif ($defaultProxy.ContainsKey('agent')) { [string]$defaultProxy.agent }
        else { 'off' }
    if ($agent -eq 'inherit') { $agent = 'always' }
    $agentLabel = switch ($agent) {
        'off' { '全局 Agent 透传网络：保持现有隔离环境，不注入项目代理' }
        'selective' { '自选 Agent 代理注入：创建/附加 session 时可选 use_proxy=true' }
        'always' { '全局 Agent 代理启用：所有新 session 自动注入项目代理' }
        default { 'Agent 模式配置无效' }
    }
    Write-Host "  Agent 模式: $agentLabel"
    if ($Pending) {
        Write-Host '  当前显示待保存配置；选 0 保存。' -ForegroundColor Yellow
    }
    if (-not $hasProxy -and $agent -ne 'off') {
        Write-Host '  当前没有可注入的代理；Agent 模式仅在配置代理后起作用。' -ForegroundColor Yellow
    }
    if ($null -ne $script:ProxyProbeResult) {
        foreach ($field in @('http', 'https')) {
            $result = $script:ProxyProbeResult[$field]
            if ($null -eq $result) { continue }
            $status = switch ($result.status) {
                'ok' { "连通，出口 IP $($result.egress_ip)" }
                'failed' { "失败（$($result.reason)）" }
                default { '未配置' }
            }
            Write-Host "  $field 检测: $status"
        }
    } else {
        Write-Host '  连通性: 未检测（选 8 手动检测，向 api.ipify.org 查询出口 IP）'
    }
}

function Test-ConfiguredProxy {
    $config = Get-LauncherConfig
    $python = [string]$config.python
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host 'Python 不可用，无法检测代理。' -ForegroundColor Yellow
        return
    }
    try {
        $output = & $python -m tiancheng_mcp.proxy_probe `
            --defaults $script:DefaultsPath --local $script:LocalConfigPath 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'probe failed' }
        $result = $output | ConvertFrom-Json -AsHashtable
        if ($result.ContainsKey('error')) { throw 'invalid proxy config' }
        $script:ProxyProbeResult = $result
    } catch {
        Write-Host '代理检测无法完成；请检查 Python 环境和代理配置。' -ForegroundColor Yellow
    }
}

function Assert-ProxyUrl {
    param([Parameter(Mandatory)][string]$Value)

    $uri = $null
    if ($Value.Length -gt 4096 -or $Value -match '\s' -or
        -not [uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -notin @('http', 'https', 'socks5', 'socks5h') -or
        [string]::IsNullOrWhiteSpace($uri.Host) -or
        $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment) {
        throw '代理 URL 无效；请使用 http://、https://、socks5:// 或 socks5h://，并对凭据特殊字符做 URL 编码。'
    }
}

function Read-ProxyUrl {
    param([Parameter(Mandatory)][string]$Prompt)

    if ([Console]::IsInputRedirected) {
        # Read-Host echoes redirected input, while -AsSecureString waits for
        # an interactive terminal. Read only the next line from the supplied
        # stream and never write its contents to the host.
        Write-Host "$Prompt（从重定向输入读取）"
        return [Console]::In.ReadLine()
    }
    return (Read-SecretText -Prompt $Prompt)
}

function Save-ProxySettings {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Overrides)

    Save-LauncherOverrides -Changes @{ proxy = $Overrides }
    Write-Host '代理设置已保存；下次检测/启动读取新配置，已运行的 MCP/Tunnel 需重启；无需重开 PowerShell。' -ForegroundColor Green
}

function Edit-ProxySettingsInteractive {
    $proxy = Get-ProxyOverrides
    $changed = $false
    $script:ProxyProbeResult = $null
    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'proxy' { Show-ProxySummary -Overrides $proxy -Pending:$changed })
        $menu=Read-LauncherMenuChoice -Page proxy -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice; $rich=$menu.Rich
        if ($choice -eq 'r') { continue }
        switch ($choice) {
            '1' {
                $value = Read-ProxyUrl -Prompt 'HTTP 目标代理 URL'
                try {
                    Assert-ProxyUrl -Value $value; $proxy.http = $value; $changed = $true
                    Write-Host 'HTTP 代理已设置，选 0 保存。' -ForegroundColor Green
                }
                catch { Write-Host '代理 URL 无效，请检查协议、主机、端口与凭据的 URL 编码。' -ForegroundColor Yellow }
                finally { $value = $null }
            }
            '2' {
                $value = Read-ProxyUrl -Prompt 'HTTPS 目标代理 URL'
                try {
                    Assert-ProxyUrl -Value $value; $proxy.https = $value; $changed = $true
                    Write-Host 'HTTPS 代理已设置，选 0 保存。' -ForegroundColor Green
                }
                catch { Write-Host '代理 URL 无效，请检查协议、主机、端口与凭据的 URL 编码。' -ForegroundColor Yellow }
                finally { $value = $null }
            }
            '3' {
                $value = Read-Host 'NO_PROXY（逗号分隔，输入空行=清除）'
                if ($null -eq $value -or $value.Length -gt 4096 -or $value -match '[\x00-\x1f]') {
                    Write-Host 'NO_PROXY 必须是最多 4096 字符的单行文本。' -ForegroundColor Yellow
                    continue
                }
                $proxy.noProxy = $value
                $changed = $true
                Write-Host 'NO_PROXY 已设置，选 0 保存。' -ForegroundColor Green
            }
            '4' {
                Write-Host 'off = 全局 Agent 透传网络：保持原有环境，不注入项目代理'
                Write-Host 'selective = 自选：仅 use_proxy=true 的新 session 注入代理'
                Write-Host 'always = 全局启用：所有新 session 注入代理，不能逐个关闭'
                $value = Read-Host 'Agent 模式：off / selective / always'
                if ($value -notin @('off', 'selective', 'always')) {
                    Write-Host '只能输入 off、selective 或 always。' -ForegroundColor Yellow
                    continue
                }
                $proxy.agent = $value
                $changed = $true
                Write-Host 'Agent 模式已设置，选 0 保存。' -ForegroundColor Green
            }
            '5' { $proxy.http = ''; $changed = $true; Write-Host 'HTTP 代理已清除，选 0 保存。' }
            '6' { $proxy.https = ''; $changed = $true; Write-Host 'HTTPS 代理已清除，选 0 保存。' }
            '7' { $proxy.noProxy = ''; $changed = $true; Write-Host 'NO_PROXY 已清除，选 0 保存。' }
            '8' {
                if ($changed) {
                    Save-ProxySettings -Overrides $proxy
                    $changed = $false
                    $script:ProxyProbeResult = $null
                }
                Test-ConfiguredProxy
            }
            'r' { continue }
            'h' { Show-MenuHelp -Section 'proxy' }
            '0' {
                if ($changed) {
                    Save-ProxySettings -Overrides $proxy
                } else {
                    Write-Host '代理设置没有更改，已返回。' -ForegroundColor DarkGray
                }
                return
            }
            default { Write-Host '无效选择。' -ForegroundColor Yellow }
        }
        if ($changed) { $script:ProxyProbeResult = $null }
        if ($rich -and $choice -ne 'h') { Pause-Tq }
    }
}

function Invoke-CommandPolicyAdmin {
    param([hashtable]$Config, [string[]]$Arguments, [string]$RuleInput)

    $python = [string]$Config.python
    Assert-FileExists -Path $python -Label 'Python environment'
    $policyPath = if ($Config.ContainsKey('commandPolicyPath')) { [string]$Config.commandPolicyPath }
        else { Join-Path $script:ProjectRoot 'config\command-policy.local.json' }
    $base = @('-m', 'tiancheng_mcp.command_policy_admin', '--policy', $policyPath,
        '--workspace', [string]$Config.workspace)
    $raw = if ($script:TuiStatusProbe -and $Arguments[0] -eq 'status') {
        Invoke-TuiProbeProcess -FilePath $python -Arguments ($base + $Arguments)
    } elseif ($PSBoundParameters.ContainsKey('RuleInput')) { $RuleInput | & $python @base @Arguments 2>$null }
        else { & $python @base @Arguments 2>$null }
    if ($LASTEXITCODE -ne 0) {
        $failure = ($raw -join "`n") | ConvertFrom-Json -AsHashtable -ErrorAction SilentlyContinue
        if ($failure -and $failure.ContainsKey('reason')) {
            throw "命令策略校验失败：$($failure.reason)。非法配置未保存。"
        }
        throw '命令策略操作失败；请检查格式、可信程序路径及工作区外配置位置。'
    }
    return (($raw -join "`n") | ConvertFrom-Json -AsHashtable)
}

function Show-CommandPolicyState {
    param([hashtable]$State)

    Write-Host "命令策略：$($State.preset)（下次启动配置）" -ForegroundColor Cyan
    Write-Host '这里读取保存配置，不代表当前运行值；运行快照请看 workspace_info.command_policy。'
    if ($State.policy_revision) {
        Write-Host "配置版本：$($State.policy_revision.Substring(0, 12))；与运行快照 policy_revision 不同则需重启 MCP。"
    }
    Write-Host '仅管理普通命令；SAFE/DEV、固定模板 Agent、专用 Git 和路径授权独立。'
    Write-Host '开发代码和 Shell 使用 MCP 宿主账户权限；elevated 不提权。修改后需重启 MCP。' -ForegroundColor Yellow
    if ($State.mode -eq 'unrestricted') {
        Write-Host '命令不设限：支持 PATH 程序名/绝对程序路径；下列只列配置规则。禁用按直接调用文件名生效。' -ForegroundColor Yellow
    }
    foreach ($entry in $State.commands) {
        $source = if ($entry.source -eq 'local') { '本机添加' } else { '默认预设' }
        $status = switch ($entry.status) { 'available' { '可用' }; 'disabled' { '已禁用' }; default { '未安装/不可用' } }
        $arguments = if ($entry.arguments -eq 'exact') { '精确参数模板' } else { '任意开发参数' }
        Write-Host "  $($entry.name): $status / $source / $arguments"
    }
    if ($State.disabled.Count) { Write-Host "  本机禁用：$($State.disabled -join ', ')" }
}

function Show-CommandPolicyMenu {
    param([hashtable]$Config)

    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'commands' {
                $state=Invoke-CommandPolicyAdmin -Config $Config -Arguments @('status')
                Show-CommandPolicyState -State $state
        })
        $menu=Read-LauncherMenuChoice -Page commands -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq '0') { return }
        if ($choice -eq 'r') { continue }
        try {
            $cancelled = $false
            switch ($choice) {
                '1' {
                    Write-Host '切换预设会保留本机添加和禁用；要清空覆盖请先选 8 恢复默认。' -ForegroundColor Yellow
                    $preset = Read-Host '预设：minimal / balanced / elevated / unrestricted'
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('preset', $preset))
                }
                '2' {
                    $name = Read-Host '命令别名（如 python 或 git-version）'
                    $builtin = Read-Host '内置工具：python/pytest/py/uv/git/gh/node/rg/npm/npx/codex/pwsh/powershell/cmd/bash/sh'
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('add-builtin', $name, $builtin))
                }
                '3' {
                    $name = Read-Host '命令别名'
                    Write-Host '规则字段：builtin 或 argv（二选一）；arguments 为 any 或 {"exact":[["--version"]]}。'
                    Write-Host 'argv 首项须为工作区外可信程序绝对路径；固定参数不会显示在状态中。'
                    $rule = Read-ProxyUrl -Prompt '单行规则 JSON'
                    try { [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('add-rule', $name) -RuleInput $rule) }
                    finally { $rule = $null }
                }
                '4' {
                    $name = Read-Host '禁用的命令别名'
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('disable', $name))
                }
                '5' {
                    $name = Read-Host '撤销禁用的命令别名'
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('enable', $name))
                }
                '6' {
                    $name = Read-Host '移除的本机命令别名'
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('remove', $name))
                }
                '8' {
                    if ((Read-Host '输入 RESET 恢复默认') -cne 'RESET') { $cancelled=$true; Write-Host '已取消恢复默认。'; continue }
                    [void](Invoke-CommandPolicyAdmin -Config $Config -Arguments @('reset'))
                }
                default { $cancelled=$true; Write-Host '无效选择。' -ForegroundColor Yellow; continue }
            }
            if (-not $cancelled) { Write-Host '命令策略已保存；重启 MCP 后生效，SAFE/DEV 总开关不变。' -ForegroundColor Green }
        } catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
        if ($menu.Rich) { Pause-Tq }
    }
}

function Edit-SettingsInteractive {
    param([hashtable]$Config)

    Write-Host 'A 设置会依次询问程序路径、健康地址、等待预算、Doctor、自动恢复和连接 TTL。' -ForegroundColor DarkGray
    Write-Host '直接回车表示保持现值；保存后需重启已运行实例。TTL 不改变 Agent 生命周期。' -ForegroundColor DarkGray
    $interactiveTimeout = if ($Config.ContainsKey('interactiveTimeoutSeconds')) {
        [int]$Config.interactiveTimeoutSeconds
    } else { 75 }
    $tunnel = Read-Host "tunnel-client [$($Config.tunnelClient)]"
    $powershell = Read-Host "PowerShell 7 [$($Config.powerShell)]"
    $health = Read-Host "Health URL [$($Config.healthBaseUrl)]"
    $profileDir = Read-Host "Profile 目录（留空=系统默认）[$($Config.profileDir)]"
    $timeout = Read-Host "MCP 自动转后台等待秒数（1-90） [$interactiveTimeout]"
    $doctor = Read-Host "启动前运行 doctor？Y/n [$($Config.doctorBeforeStart)]"
    $supervisorEnabled = Test-SupervisorEnabled -Config $Config
    $currentTtl = if ($Config.ContainsKey('supervisor') -and
        $Config.supervisor.ContainsKey('mcpConnectionMaxTtl')) {
        [string]$Config.supervisor.mcpConnectionMaxTtl
    } else { '24h' }
    $supervisorChoice = Read-Host "启用 Tunnel 自动恢复 Supervisor？Y/n [$supervisorEnabled]"
    $ttlChoice = Read-Host "MCP 传输连接 TTL（不是 Agent 运行上限）[$currentTtl]"
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
    $supervisorChanges = @{}
    if ($Config.ContainsKey('supervisor')) {
        foreach ($entry in $Config.supervisor.GetEnumerator()) {
            $supervisorChanges[$entry.Key] = $entry.Value
        }
    }
    $supervisorChanged = $false
    if ($supervisorChoice -match '^(?i)n(?:o)?$') {
        $supervisorChanges.enabled = $false
        $supervisorChanged = $true
    } elseif ($supervisorChoice -match '^(?i)y(?:es)?$') {
        $supervisorChanges.enabled = $true
        $supervisorChanged = $true
    }
    if ($ttlChoice) {
        if ($ttlChoice -notmatch '^([1-9][0-9]*)(s|m|h)$') {
            throw 'MCP 传输连接 TTL 必须是正整数加 s/m/h，例如 30m、2h、24h。'
        }
        $amount = [long]$Matches[1]
        $seconds = switch ($Matches[2]) { 's' { $amount }; 'm' { $amount * 60 }; 'h' { $amount * 3600 } }
        if ($seconds -gt 604800) { throw 'MCP 传输连接 TTL 不能超过 168h。' }
        $supervisorChanges.mcpConnectionMaxTtl = $ttlChoice
        $supervisorChanged = $true
    }
    if ($supervisorChanged) { $changes.supervisor = $supervisorChanges }
    if ($changes.Count -gt 0) {
        Save-LauncherOverrides -Changes $changes
        Write-Host '启动器设置已保存。' -ForegroundColor Green
    }
    Show-ProxySummary
    $proxyChoice = Read-Host '管理出站代理？y/N'
    if ($proxyChoice -match '^(?i)y(?:es)?$') {
        Edit-ProxySettingsInteractive
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
    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'policy' { Show-AccessPolicy })
        $menu=Read-LauncherMenuChoice -Page policy -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq 'r') { continue }
        switch ($choice) {
            '1' { Add-AccessPolicyRuleInteractive; Pause-Tq }
            '2' { Remove-AccessPolicyRuleInteractive; Pause-Tq }
            '3' { Edit-AccessPolicyRuleInteractive; Pause-Tq }
            '4' { Explain-AccessPolicyInteractive; Pause-Tq }
            '5' { Validate-AccessPolicyInteractive; Pause-Tq }
            'h' { Show-MenuHelp -Section 'policy' }
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
    $raw = if ($script:TuiStatusProbe -and $Arguments[0] -in @('discover','status')) {
        Invoke-TuiProbeProcess -FilePath $python -Arguments ($base + $Arguments)
    } else { & $python @base @Arguments 2>&1 }
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

    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'agents' { Write-Host 'Claude 命令档位由本机 profile 决定；workspace_info 显示当前加载值。'; Write-Host 'trusted-shell 可运行宿主命令；Windows 原生环境不保证项目路径隔离。'; Show-AgentSourceState -Config $Config })
        $menu=Read-LauncherMenuChoice -Page agents -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq 'r') { continue }
        try {
            switch ($choice) {
                '1' { Add-AgentSourceInteractive -Config $Config; Pause-Tq }
                '2' { Toggle-AgentSourceInteractive -Config $Config; Pause-Tq }
                '3' { Remove-AgentSourceInteractive -Config $Config; Pause-Tq }
                '4' { [void](Invoke-AgentSourceAdmin -Config $Config -Arguments @('validate')); Write-Host 'Agent source policy 验证通过。' -ForegroundColor Green; Pause-Tq }
                '5' { Refresh-AgentSourceInteractive -Config $Config; Pause-Tq }
                '6' { Rebuild-AgentCatalogInteractive -Config $Config; Pause-Tq }
                '7' { Invoke-AgentSmokeInteractive -Config $Config; Pause-Tq }
                'h' { Show-MenuHelp -Section 'agents' }
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

    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'key' { Show-KeyStatus -Config $Config })
        $menu=Read-LauncherMenuChoice -Page key -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq 'r') { continue }
        switch ($choice) {
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
            'h' { Show-MenuHelp -Section 'key' }
            '0' { return }
            default { Write-Host '无效选择。' -ForegroundColor Yellow }
        }
        if ($menu.Rich -and $choice -ne 'h') { Pause-Tq }
    }
}

function Show-ProfileMenu {
    param([hashtable]$Config)

    $textOnly = $false
    while ($true) {
        $statusLines=@(Get-TuiStatusLines -CacheKey 'profile' { Show-Profiles -Config $Config })
        $menu=Read-LauncherMenuChoice -Page profile -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq 'r') { continue }
        switch ($choice) {
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
            'h' { Show-MenuHelp -Section 'profile' }
            '0' { return }
            default { Write-Host '无效选择。' -ForegroundColor Yellow }
        }
        if ($menu.Rich -and $choice -ne 'h') { Pause-Tq }
    }
}

function Install-Alias {
    $installer = Join-Path $script:ProjectRoot 'install-tc.ps1'
    Assert-FileExists -Path $installer -Label 'Alias installer'
    & $installer
}

function Show-MainMenu {
    $textOnly = $false
    while ($true) {
        try {
            $config=Get-LauncherConfig
            $selected=Resolve-SelectedProfile -Config $config -Requested $Profile
        } catch {
            $menu=Read-LauncherMenuChoice -Page main -Status @('启动配置读取失败 · 操作不可用 · R 重试 / 0 退出') -TextOnly ([ref]$textOnly)
            if ($menu.Choice -eq '0') { return }
            if ($menu.Choice -ne 'r') { Write-Host '请先修复本机启动配置。'; Pause-Tq }
            continue
        }
        $statusLines=@(Get-TuiStatusLines -CacheKey 'main' {
            $key=Get-KeyRecord -Config $config
            $modeLabel = Get-ProfileMode -Config $config -Name $selected
            $runningProfiles = @(
                @(Get-RunningTunnelRecords -Config $config | ForEach-Object Profile) +
                @(Get-RunningSupervisorRecords -Config $config | ForEach-Object Profile) |
                    Sort-Object -Unique
            )
            $listenerConflict = if ($runningProfiles.Count -eq 0) {
                Get-HealthListenerConflict -Config $config
            } else { $null }
            $runningLabel = if ($runningProfiles.Count) { $runningProfiles -join ', ' }
                elseif ($null -ne $listenerConflict) { '未知（健康端口已占用）' } else { '未启动' }
            $keyLabel = if ($key.Configured) { '已配置' } else { '未配置' }
            "Profile  $selected    模式  $modeLabel    Key  $keyLabel",
            "受管运行  $runningLabel    云端连接  未检测    刷新  $(Get-Date -Format 'HH:mm:ss')"
        })
        $menu=Read-LauncherMenuChoice -Page main -Status $statusLines -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        try {
            switch ($choice) {
                '1' { Start-TunnelForeground -Config $config -Name $selected -SkipDoctorCheck:$false -ExecAlreadyAllowed:$false }
                '2' { Start-TunnelWindow -Config $config -Name $selected -ExecAlreadyAllowed:$false; Pause-Tq }
                '3' { [void](Stop-TunnelProfile -Config $config -Name $selected -Confirmed:$false); Pause-Tq }
                '4' { Restart-TunnelProfile -Config $config -Name $selected -Confirmed:$false; Pause-Tq }
                '5' { Show-DoctorResult -Config $config -Name $selected }
                '6' { Show-ProfileMenu -Config $config }
                '7' { Show-KeyMenu -Config $config }
                '8' { Show-TuiResult -Title '完整状态' -Lines @(Get-TuiStatusLines -CacheKey full-status { Show-Status -Config $config }) }
                '9' { Open-AdminUi -Config $config; Pause-Tq }
                { $_ -match '^(?i)a$' } { Edit-SettingsInteractive -Config $config; Pause-Tq }
                { $_ -match '^(?i)p$' } { Edit-ProxySettingsInteractive; Pause-Tq }
                { $_ -match '^(?i)b$' } { Install-Alias; Pause-Tq }
                { $_ -match '^(?i)d$' } { Show-AccessPolicyMenu }
                { $_ -match '^(?i)e$' } { Show-AgentSourceMenu -Config $config }
                { $_ -match '^(?i)c$' } { Show-CommandPolicyMenu -Config $config }
                'r' { continue }
                'h' { Show-MenuHelp -Section 'main' }
                '0' { return }
                default { Write-Host '无效选择。' -ForegroundColor Yellow; Pause-Tq }
            }
        } catch {
            Write-Host "操作失败：$($_.Exception.Message)" -ForegroundColor Red
            Pause-Tq
        }
    }
}

if ($Action -ne 'menu') {
    $config = Get-LauncherConfig
    $selectedProfile = Resolve-SelectedProfile -Config $config -Requested $Profile
}

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
    'proxy' { Edit-ProxySettingsInteractive }
    'commands' {
        if ($Json) { Invoke-CommandPolicyAdmin -Config $config -Arguments @('status') | ConvertTo-Json -Depth 8 }
        else { Show-CommandPolicyMenu -Config $config }
    }
    'info' { Show-Info -Config $config }
    'install-alias' { Install-Alias }
    'policy' { Show-AccessPolicy }
    'agents' {
        if ($Json) { Show-AgentSourceState -Config $config }
        else { Show-AgentSourceMenu -Config $config }
    }
}
