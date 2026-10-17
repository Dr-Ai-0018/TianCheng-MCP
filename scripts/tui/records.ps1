# Local history only. The Python query/view model supplies all displayed facts.
function Invoke-RecordsAdmin {
    param([hashtable]$Config, [string[]]$Arguments)
    $base=@('-m','tiancheng_mcp.observability_admin','--project-root',$script:ProjectRoot,'--local',$script:LocalConfigPath)
    $raw=& ([string]$Config.python) @base @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw '执行记录查询失败；请检查查询条件、记录权限或导出目标。未改变运行任务。' }
    return (($raw -join "`n") | ConvertFrom-Json -AsHashtable)
}

function Show-RecordsMenu {
    param([hashtable]$Config)
    $textOnly=$false; $page=$null; $pageArgs=@(); $operation=''
    while ($true) {
        $status=@(
            '执行记录为本机历史；读取成功不表示服务正在运行。',
            '调用返回、操作结果和记录完整性分别展示；summary 无法还原详情。',
            '查看和退出不会取消业务任务；记录不提供防篡改保证。'
        )
        $menu=Read-LauncherMenuChoice -Page records -Status $status -TextOnly ([ref]$textOnly)
        $choice=$menu.Choice
        if ($choice -eq '0') { return }
        if ($choice -eq 'r') { continue }
        try {
            $queryArgs=$null
            switch ($choice) {
                '1' { $queryArgs=@('status') }
                '2' { $queryArgs=@('recent','--limit','50') }
                '3' {
                    $queryArgs=@('recent','--limit','50')
                    foreach ($filter in @('tool','provider','status','since')) {
                        $value=(Read-Host "$filter 筛选（留空忽略；since 用 ISO UTC 时间）").Trim()
                        if ($value) { $queryArgs+=@("--$filter",$value) }
                    }
                }
                '4' {
                    $operation=(Read-Host 'operation / job / process / run ID').Trim()
                    if (-not $operation) { throw '请输入关联 ID。' }
                    $queryArgs=@('show',$operation)
                    if ((Read-Host '查看已捕获的详情？输入 detail，否则仅摘要') -ceq 'detail') { $queryArgs+='--detail' }
                }
                '5' {
                    if ($null -eq $page -or -not $page['next_cursor']) { throw '当前没有下一页；请先查询状态、列表或详情。' }
                    $queryArgs=@($pageArgs)+@('--cursor',[string]$page.next_cursor)
                }
                '6' {
                    $operation=(Read-Host '持续查看的 operation / job / process / run ID').Trim()
                    if (-not $operation) { throw '请输入关联 ID。' }
                    Write-Host 'Ctrl+C 结束查看；业务任务继续。输出包含来源、结果与质量提示。' -ForegroundColor DarkGray
                    $base=@('-m','tiancheng_mcp.observability_admin','--project-root',$script:ProjectRoot,'--local',$script:LocalConfigPath)
                    & ([string]$Config.python) @base follow $operation --text
                    if ($LASTEXITCODE -ne 0) { throw '持续查看已退出或失败；业务任务未被取消。' }
                }
                '7' {
                    $operation=(Read-Host '导出的 operation / job / process / run ID').Trim()
                    if (-not $operation) { throw '请输入关联 ID。' }
                    $exportArgs=@('export',$operation)
                    if ((Read-Host '包含已捕获详情？输入 detail，否则仅摘要') -ceq 'detail') { $exportArgs+='--detail' }
                    $preview=Invoke-RecordsAdmin -Config $Config -Arguments ($exportArgs+@('--preview','--view'))
                    Show-TuiResult -Title '导出范围与大小预览（尚未写文件）' -Lines @($preview.display_lines)
                    if ((Read-Host '输入 EXPORT 写出，否则返回') -cne 'EXPORT') { continue }
                    $output=(Read-Host '目标文件绝对路径（父目录须为现有私有目录；不覆盖文件）').Trim()
                    if (-not [IO.Path]::IsPathFullyQualified($output)) { throw '导出目标必须是绝对路径。' }
                    $receipt=Invoke-RecordsAdmin -Config $Config -Arguments ($exportArgs+@('--output',$output,'--view'))
                    Show-TuiResult -Title '导出实际回执（活跃记录可能与预览不同）' -Lines @($receipt.display_lines)
                }
            }
            if ($null -ne $queryArgs) {
                $page=Invoke-RecordsAdmin -Config $Config -Arguments ($queryArgs+@('--view'))
                # Retain the original binding, not a growing list of cursor arguments.
                if ($choice -ne '5') { $pageArgs=@($queryArgs) }
                Show-TuiResult -Title '执行记录 / 查询结果' -Lines @($page.display_lines)
            }
        } catch { Write-Host '执行记录操作失败；检查条件、权限及私有导出目录。运行任务未改变。' -ForegroundColor Red; Pause-Tq }
    }
}
