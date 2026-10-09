# Uses production TUI components with explicit sample data; no service/config imports.
[CmdletBinding()]
param(
    [ValidateSet('main','doctor','status')][string]$Page='main',
    [switch]$NoPause
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'tui/console.ps1')
. (Join-Path $PSScriptRoot 'menu-help.ps1')
function Pause-Tq { if (-not $NoPause) { [void](Read-Host '按回车继续') } }

function Show-PreviewDoctor {
    Show-TuiResult -Title '连接准备检查 / Doctor · 演示' -Lines @(
        '截图预览 · 以下为示例说明，未执行真实 Doctor。',
        'Profile：demo-local / SAFE',
        '运行 Key：未配置',
        '',
        '首次使用：7 保存 Key → 6 创建 SAFE → 5 检查 → 2 启动。',
        '实际检查会显示客户端诊断与真实退出码。',
        '退出码为 0 才表示检查通过；非零时按诊断处理后重试。',
        '检查通过后仍需完成 Tunnel 连接确认和真实文件写读验收。'
    )
}
function Show-PreviewStatus {
    Show-TuiResult -Title '完整状态 · 演示' -Lines @(
        '截图预览 · 以下均为示例数据，未查询机器或网络状态。',
        'Profile：demo-local',
        '模式：SAFE',
        '示例受管运行：未启动',
        '示例 Key：未配置',
        '示例云端连接：未检测',
        '',
        '真实页面另有 Supervisor、MCP Inferred / Verified、TTL 和 Git/gh 信息。',
        'Tunnel Ready 不等于 MCP 文件工具 roundtrip 已验证。',
        '查询失败时显示未知或注明时间的缓存，不能据此断言服务已停止。'
    )
}
if ($Page -eq 'doctor') { Show-PreviewDoctor; return }
if ($Page -eq 'status') { Show-PreviewStatus; return }
$textOnly=$false
$script:TuiSelection.main=1
$status=@(
    '截图预览 · 演示数据 · 不执行服务或配置操作',
    'Profile  demo-local    模式  SAFE    Key  未配置',
    '示例受管运行  未启动    示例云端连接  未检测'
)
while ($true) {
    $menu=Read-LauncherMenuChoice -Page main -Status $status -TextOnly ([ref]$textOnly)
    switch ($menu.Choice) {
        '0' { return }
        'R' { continue }
        '5' { Show-PreviewDoctor }
        '8' { Show-PreviewStatus }
        default {
            Show-TuiResult -Title '截图预览' -Lines @(
                '本入口只预览界面，不执行菜单操作。',
                '正常使用请退出预览，再运行 tc。',
                '5 查看 Doctor 示例，8 查看状态示例，H/V 查看帮助与详情。'
            )
        }
    }
}
