# Context help uses the same menu catalogue as the interactive and text menus.
function Write-MenuHint {
    param([string]$Section)
    Write-Host (Get-TuiPageInfo $Section).Note -ForegroundColor DarkGray
    Write-Host '  H. 查看每个选项的作用（只读）；完整教程：manual/quickstart-windows.md' -ForegroundColor DarkGray
}

function Show-MenuHelp {
    param([string]$Section)
    $lines=@(foreach ($item in @(Get-TuiMenuItems $Section)) { "  $($item.Key)：$($item.Help)" })
    $lines += "  0：$((Get-TuiPageInfo $Section).Exit)。$((Get-TuiPageInfo $Section).Note)"
    Show-TuiResult -Title '选项说明' -Lines $lines
}
