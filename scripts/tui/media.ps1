function Invoke-MediaAdmin {
    param([hashtable]$Config, [string[]]$Arguments)
    $base=@('-m','tiancheng_mcp.media_admin','--project-root',$script:ProjectRoot,'--local',$script:LocalConfigPath)
    $raw=& ([string]$Config.python) @base @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw '媒体配置操作失败；检查开关、精确主机及不含凭据的回环 HTTP 代理。配置未放宽。' }
    return (($raw -join "`n") | ConvertFrom-Json -AsHashtable)
}

function Show-MediaMenu {
    param([hashtable]$Config)
    $textOnly=$false
    while ($true) {
        $state=Invoke-MediaAdmin -Config $Config -Arguments @('status')
        $proxyLabel=[string]$state.attachments['proxy_url']
        if (-not $proxyLabel) { $proxyLabel='未配置' }
        $status=@(
            "下次启动：图片 $($state.images) · 导出 $($state.exports) · 附件 $($state.attachments.mode)",
            "附件允许主机：$($state.attachments.hosts -join ', ')",
            "专用代理（仅 clash_proxy）：$proxyLabel · 不修改系统 DNS",
            '导出/附件为实验能力；当前运行配置请核对 workspace_info.media。',
            '图片源/附件 32 MiB · 导出 4 MiB · 结果 15 分钟；保存后重启。'
        )
        $menu=Read-LauncherMenuChoice -Page media -Status $status -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq '0') { return }
        if ($choice -eq 'r') { continue }
        try {
            $arguments=$null
            switch ($choice) {
                '1' { $arguments=@('images', (Read-Host '图片工具 on / off').Trim().ToLowerInvariant()) }
                '2' {
                    Write-Host '实验导出：准备链接不代表用户已下载；每次读取重新核对权限与 hash。' -ForegroundColor DarkGray
                    $arguments=@('exports', (Read-Host '原文件导出 on / off').Trim().ToLowerInvariant())
                }
                '3' {
                    Write-Host 'direct 直连；clash_proxy 使用 Windows Clash Verge 的本机代理与只读 DNS 查询。不继承 P 菜单，不改系统 DNS。' -ForegroundColor Yellow
                    $mode=(Read-Host '附件下载 off / direct / clash_proxy').Trim().ToLowerInvariant()
                    $arguments=@('attachments', $mode)
                    if ($mode -in @('direct','clash_proxy')) {
                        $hosts=(Read-Host '允许主机（逗号分隔；不要输入 URL、签名或 API Key）') -split ','
                        if (-not ($hosts | Where-Object { $_.Trim() })) { throw '附件下载必须明确填写至少一个允许主机。' }
                        foreach ($hostName in $hosts) { if ($hostName.Trim()) { $arguments+=@('--host',$hostName.Trim()) } }
                    }
                    if ($mode -eq 'clash_proxy') {
                        $proxy=(Read-Host '附件专用代理 URL（如 http://127.0.0.1:7897；不要输入密码）').Trim()
                        $arguments+=@('--proxy-url',$proxy)
                    }
                }
            }
            if ($null -ne $arguments) {
                [void](Invoke-MediaAdmin -Config $Config -Arguments $arguments)
                Write-Host '已校验并保存 next_start 配置；已有实例未改变，重启后生效。' -ForegroundColor Green
            }
        } catch { Write-Host $_.Exception.Message -ForegroundColor Red }
        Pause-Tq
    }
}
