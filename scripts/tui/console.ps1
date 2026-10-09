# Pure frame construction is separate from Console IO and business actions.
. (Join-Path $PSScriptRoot 'menus.ps1')
. (Join-Path $PSScriptRoot 'status.ps1')
. (Join-Path $PSScriptRoot 'results.ps1')
$script:TuiSelection = @{}

function Test-TuiConsole {
    try {
        return (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected -and
            [Console]::WindowWidth -ge 60 -and [Console]::WindowHeight -ge 20 -and
            $Host.Name -eq 'ConsoleHost')
    } catch { return $false }
}

function Get-TuiTextWidth {
    param([string]$Text)
    $width = 0
    $elements = [Globalization.StringInfo]::GetTextElementEnumerator($Text)
    while ($elements.MoveNext()) {
        $code = [char]::ConvertToUtf32([string]$elements.Current, 0)
        $category = [Globalization.CharUnicodeInfo]::GetUnicodeCategory([string]$elements.Current, 0)
        if ($category -in @('NonSpacingMark','EnclosingMark','Format','Control')) { continue }
        if (($code -ge 0x1100 -and $code -le 0x115f) -or
            ($code -ge 0x2e80 -and $code -le 0xa4cf) -or
            ($code -ge 0xac00 -and $code -le 0xd7a3) -or
            ($code -ge 0xf900 -and $code -le 0xfaff) -or
            ($code -ge 0xfe10 -and $code -le 0xfe6f) -or
            ($code -ge 0xff01 -and $code -le 0xff60) -or
            ($code -ge 0xffe0 -and $code -le 0xffe6) -or
            ($code -ge 0x1f300 -and $code -le 0x1faff) -or $code -ge 0x20000) { $width += 2 }
        else { $width++ }
    }
    return $width
}

function Format-TuiText {
    param([string]$Text, [int]$Width)
    if ($Width -le 0) { return '' }
    # Untrusted names/paths must not introduce terminal control sequences.
    $clean = [regex]::Replace($Text, '\x1b\[[0-?]*[ -/]*[@-~]', '')
    $clean = [regex]::Replace($clean, '[\p{Cc}\p{Cf}]', ' ')
    if ((Get-TuiTextWidth $clean) -le $Width) { return $clean }
    $result = [Text.StringBuilder]::new()
    $used = 0
    $elements = [Globalization.StringInfo]::GetTextElementEnumerator($clean)
    while ($elements.MoveNext()) {
        $part = [string]$elements.Current
        $size = Get-TuiTextWidth $part
        if ($used + $size -gt $Width - 1) { break }
        [void]$result.Append($part); $used += $size
    }
    return $result.ToString() + '…'
}

function Resolve-TuiKey {
    param([string]$Key, [string]$Character, [int]$Index, [object[]]$Items)
    $next = $Index
    $action = ''
    switch ($Key) {
        'UpArrow' { $next = ($Index + $Items.Count - 1) % $Items.Count }
        'DownArrow' { $next = ($Index + 1) % $Items.Count }
        'Home' { $next = 0 }
        'End' { $next = $Items.Count - 1 }
        'Enter' { $action = $Items[$Index].Key }
        'Escape' { $action = '0' }
        default {
            $candidate = $Character.ToUpperInvariant()
            if ($candidate -in @('0','H','R','V')) { $action = $candidate }
            else {
                for ($i = 0; $i -lt $Items.Count; $i++) {
                    if ($Items[$i].Key -eq $candidate) { $next=$i; $action=$candidate; break }
                }
            }
        }
    }
    return @{ Index=$next; Action=$action }
}

function Get-TuiStatusLines {
    param([scriptblock]$Reader, [string]$CacheKey='default')
    $previous=$script:TuiStatusProbe; $deadline=$script:TuiProbeDeadline
    $script:TuiStatusProbe=$true; $script:TuiProbeDeadline=[Environment]::TickCount64 + 8000
    $clock=[Diagnostics.Stopwatch]::StartNew()
    try {
        $lines=@(& $Reader 6>&1 | Out-String -Stream | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $at=Get-Date -Format 'HH:mm:ss'
        $script:TuiStatusCache[$CacheKey]=@{ Lines=$lines; At=$at }
        return @($lines) + "读取 $at · $($clock.ElapsedMilliseconds) ms"
    } catch {
        $reason=if ($_.Exception -is [TimeoutException]) { '超时' } else { '失败' }
        if ($script:TuiStatusCache.ContainsKey($CacheKey)) {
            $cached=$script:TuiStatusCache[$CacheKey]
            return @("状态读取$reason · 缓存（上次成功 $($cached.At)）· R 重试") + @($cached.Lines)
        }
        # Raw exception messages may contain secret values from helper output.
        return @("状态读取$reason · 状态未知 · R 重试", '读取失败不代表服务已停止；请检查配置与客户端。')
    } finally {
        $script:TuiStatusProbe=$previous; $script:TuiProbeDeadline=$deadline
    }
}

function Read-LauncherMenuChoice {
    param([string]$Page, [string[]]$Status, [ref]$TextOnly)
    while ($true) {
        $rich = (-not $TextOnly.Value) -and (Test-TuiConsole)
        if ($rich) {
            $choice=Read-TuiChoice -Page $Page -Status $Status
            if ($choice -eq '__text') { $TextOnly.Value=$true; continue }
        } else {
            $info=Get-TuiPageInfo $Page
            Write-Host "`n天澄 Local MCP / $($info.Title)" -ForegroundColor Cyan
            foreach ($line in $Status) { Write-Host (Format-TuiText $line 4096) }
            foreach ($item in @(Get-TuiMenuItems $Page)) { Write-Host "  $($item.Key). $($item.Label)" }
            Write-Host "  0. $($info.Exit)"
            Write-MenuHint -Section $Page
            Write-Host '  V. 查看全部状态；R. 刷新状态' -ForegroundColor DarkGray
            $choice=Read-Host '选择'
        }
        if ($choice -eq 'h') { Show-MenuHelp -Section $Page; continue }
        if ($choice -eq 'v') {
            Show-TuiResult -Title "$((Get-TuiPageInfo $Page).Title) / 状态详情" -Lines $Status
            continue
        }
        return @{ Choice=$choice; Rich=$rich }
    }
}

function Get-TuiFrame {
    param([string]$Page, [object[]]$Items, [string[]]$Status, [int]$Index, [int]$Width, [int]$Height)
    $lines = [Collections.Generic.List[object]]::new()
    $pageInfo = Get-TuiPageInfo $Page
    $title = '天澄 Local MCP / ' + $pageInfo.Title
    $lines.Add(@{Text=$title; Style='title'})
    $lines.Add(@{Text=('─' * $Width); Style='muted'})
    foreach ($line in @($Status | Select-Object -First 4)) { $lines.Add(@{Text=$line; Style='muted'}) }
    if ($Status.Count -gt 4) { $lines.Add(@{Text="另有 $($Status.Count - 4) 行状态 · V 查看全部"; Style='muted'}) }
    $lines.Add(@{Text=''; Style='normal'})
    $body = [Collections.Generic.List[object]]::new()
    $group = ''; $selectedLine = 0
    for ($i=0; $i -lt $Items.Count; $i++) {
        $item = $Items[$i]
        if ($group -ne $item.Group) {
            $group=$item.Group; $body.Add(@{Text=$group; Style='group'})
        }
        $prefix = if ($i -eq $Index) { '›' } else { ' ' }
        if ($i -eq $Index) { $selectedLine=$body.Count }
        $style = if ($i -eq $Index) { 'selected' } else { 'normal' }
        $body.Add(@{Text="$prefix $($item.Key)  $($item.Label)"; Style=$style})
    }
    $exit = 'Esc / 0 ' + $pageInfo.Exit
    $navigation = '↑↓ 选择  Enter 执行  H 帮助  V 详情'
    $right = "R 刷新  $exit"
    $gap = $Width - (Get-TuiTextWidth $navigation) - (Get-TuiTextWidth $right)
    $footerCount = if ($gap -lt 2) { 6 } else { 5 }
    $available = [Math]::Max(1, $Height - $lines.Count - $footerCount)
    $start = [Math]::Max(0, [Math]::Min($selectedLine - $available + 1, $body.Count - $available))
    $end = [Math]::Min($body.Count, $start + $available)
    for ($i=$start; $i -lt $end; $i++) { $lines.Add($body[$i]) }
    # Keep context help adjacent to the menu, even in a very tall window.
    if ($lines.Count -lt $Height - $footerCount) { $lines.Add(@{Text=''; Style='normal'}) }
    $lines.Add(@{Text=('─' * $Width); Style='muted'})
    $lines.Add(@{Text=$Items[$Index].Help; Style='muted'})
    $note = $pageInfo.Note
    if ($body.Count -gt $available) { $note = "菜单可滚动 · $($Index + 1)/$($Items.Count)  $note" }
    $lines.Add(@{Text=$note; Style='muted'})
    $lines.Add(@{Text=''; Style='normal'})
    if ($gap -ge 2) {
        $lines.Add(@{Text=($navigation + (' ' * $gap) + $right); Style='muted'})
    } else {
        $lines.Add(@{Text=$navigation; Style='muted'})
        $lines.Add(@{Text=$right; Style='muted'})
    }
    while ($lines.Count -lt $Height) { $lines.Add(@{Text=''; Style='normal'}) }
    foreach ($line in $lines) {
        [pscustomobject]@{ Text=(Format-TuiText $line.Text $Width); Style=$line.Style }
    }
}

function Write-TuiFrame {
    param([object[]]$Lines, [int]$Width, [int]$Top, [ConsoleColor]$Foreground, [ConsoleColor]$Background)
    for ($i=0; $i -lt $Lines.Count; $i++) {
        [Console]::SetCursorPosition(0, $Top + $i)
        [Console]::BackgroundColor=$Background
        [Console]::ForegroundColor=$Foreground
        switch ($Lines[$i].Style) {
            'muted' { [Console]::ForegroundColor=[ConsoleColor]::DarkGray }
            'group' { [Console]::ForegroundColor=[ConsoleColor]::Cyan }
            'title' { [Console]::ForegroundColor=$Foreground }
            'selected' { [Console]::BackgroundColor=[ConsoleColor]::DarkCyan; [Console]::ForegroundColor=[ConsoleColor]::White }
        }
        $text=$Lines[$i].Text
        [Console]::Write($text + (' ' * [Math]::Max(0, $Width - (Get-TuiTextWidth $text))))
        # Clear the rest of a wide/resized window without stretching selection.
        [Console]::BackgroundColor=$Background
        [Console]::Write(' ' * [Math]::Max(0, [Console]::WindowWidth - 1 - $Width))
    }
}

function Read-TuiChoice {
    param([ValidateSet('main','proxy','profile','key','commands','policy','agents')][string]$Page, [string[]]$Status)
    $items=@(Get-TuiMenuItems $Page)
    $index = if ($script:TuiSelection.ContainsKey($Page)) { [int]$script:TuiSelection[$Page] } else { 0 }
    $state=$null
    $signature=''; $dirty=$true
    try {
        $state=Get-TuiConsoleState
        $foreground=$state.Foreground; $background=$state.Background
        [Console]::CursorVisible=$false
        while ($true) {
            $width=[Math]::Min(112,[Console]::WindowWidth - 1); $height=[Console]::WindowHeight - 1
            $top=[Console]::WindowTop
            if ($width -lt 59 -or $height -lt 19) { return '__text' }
            $size="$width/$height/$top"
            if ($dirty -or $size -ne $signature) {
                $frame=@(Get-TuiFrame -Page $Page -Items $items -Status $Status -Index $index -Width $width -Height $height)
                Write-TuiFrame $frame $width $top $foreground $background
                $signature=$size; $dirty=$false
            }
            if (-not [Console]::KeyAvailable) { [Threading.Thread]::Sleep(60); continue }
            $key=[Console]::ReadKey($true)
            $result=Resolve-TuiKey -Key ([string]$key.Key) -Character ([string]$key.KeyChar) -Index $index -Items $items
            $dirty=$result.Index -ne $index; $index=$result.Index
            $script:TuiSelection[$Page]=$index
            if ($result.Action) { return [string]$result.Action }
        }
    } catch [System.IO.IOException] { return '__text' }
      catch [System.InvalidOperationException] { return '__text' }
      catch [System.ArgumentOutOfRangeException] { return '__text' }
    finally {
        Restore-TuiConsoleState $state
    }
}
