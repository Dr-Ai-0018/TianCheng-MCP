function Invoke-EnvironmentAdmin {
    param([hashtable]$Config, [string[]]$Arguments)
    $base=@('-m','tiancheng_mcp.environment_admin','--project-root',$script:ProjectRoot,
        '--local',$script:LocalConfigPath)
    $raw=if ($script:TuiStatusProbe -and $Arguments[0] -eq 'status') {
        Invoke-TuiProbeProcess -FilePath ([string]$Config.python) -Arguments ($base + $Arguments)
    } else { & ([string]$Config.python) @base @Arguments 2>$null }
    if ($LASTEXITCODE -ne 0) {
        throw '环境配置操作失败；请检查等级、变量名称和配置文件。系统强制变量及控制面密钥不可修改。'
    }
    return (($raw -join "`n") | ConvertFrom-Json -AsHashtable)
}

function Show-EnvironmentState {
    param([hashtable]$State)
    $label=switch ($State.mode) { minimal {'最小环境'}; basic {'基础环境（推荐）'}; inherit {'兼容透传'} }
    Write-Host "普通命令环境：$label / $($State.mode)（下次启动配置）" -ForegroundColor Cyan
    Write-Host "策略版本：$($State.policy_revision.Substring(0,12))；运行值未知，需核对 workspace_info.execution_environment。"
    Write-Host '保存后需重启 MCP；不会自动开启 DEV。Agent 和专用 Git 独立使用基础环境。'
    Write-Host "配置允许：$($State.configured.allow -join ', ')"
    Write-Host "旧文件允许：$($State.legacy_allow -join ', ')（非 SAFE Windows 启动入口）"
    Write-Host "禁止：$($State.deny -join ', ')；禁止优先于允许。"
    $dotenvLabel=switch ($State.dotenv_status) { unavailable {'未知（无法读取）'}; absent {'文件不存在'}; names_checked {'已检查名称'}; default {'未配置'} }
    Write-Host "当前父环境未提供：$($State.missing -join ', ')；.env：$dotenvLabel；可补值：$($State.dotenv_names -join ', ')"
    Write-Host '只显示名称，不显示值。新终端才继承新的持久环境；旧 tc 打开的子窗口也可能继承旧值。' -ForegroundColor DarkGray
}

function Show-EnvironmentMenu {
    param([hashtable]$Config)
    $textOnly=$false
    while ($true) {
        $status=@(Get-TuiStatusLines -CacheKey environment {
            Show-EnvironmentState -State (Invoke-EnvironmentAdmin -Config $Config -Arguments @('status'))
        })
        $menu=Read-LauncherMenuChoice -Page environment -Status $status -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq '0') { return }
        if ($choice -eq 'r') { continue }
        try {
            $arguments=$null
            switch ($choice) {
                '1' {
                    Write-Host 'minimal：精简基础；basic：补齐系统安装目录；inherit：继承父环境并过滤受保护密钥。'
                    $mode=(Read-Host '等级：minimal / basic / inherit').Trim().ToLowerInvariant()
                    if ($mode -eq 'inherit') {
                        Write-Host '普通命令将继承业务变量、代理认证和 PATH；未知业务凭据也可能进入子进程。' -ForegroundColor Yellow
                        if ((Read-Host '确认本机选择兼容透传，请输入 INHERIT') -cne 'INHERIT') { continue }
                    }
                    $arguments=@('mode',$mode)
                }
                '2' { $arguments=@('allow',(Read-Host '允许变量名（仅名称，不要输入值）')) }
                '3' { $arguments=@('remove-allow',(Read-Host '移除配置允许项；旧文件中的名称请用禁止项覆盖')) }
                '4' { $arguments=@('deny',(Read-Host '禁止变量名（禁止优先）')) }
                '5' { $arguments=@('remove-deny',(Read-Host '撤销禁止的变量名')) }
                '8' {
                    Write-Host '恢复 basic 并清空本页允许/禁止项；保留旧 exec-env.allowlist。'
                    if ((Read-Host '输入 RESET 确认') -ceq 'RESET') { $arguments=@('reset') }
                }
            }
            if ($null -ne $arguments) {
                [void](Invoke-EnvironmentAdmin -Config $Config -Arguments $arguments)
                Write-Host '配置已校验并保存；已有实例未改变，重启 MCP/Tunnel 后生效。' -ForegroundColor Green
            }
        } catch { Write-Host $_.Exception.Message -ForegroundColor Red }
        Pause-Tq
    }
}
