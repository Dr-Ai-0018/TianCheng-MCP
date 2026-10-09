# Result pages consume already captured text: navigation never performs probes.
function Get-TuiConsoleState {
    return @{ Foreground=[Console]::ForegroundColor; Background=[Console]::BackgroundColor; Cursor=[Console]::CursorVisible }
}

function Restore-TuiConsoleState {
    param($State)
    if ($null -eq $State) { return }
    # Each restore is independent so a resized/closed console cannot skip cursor recovery.
    try { [Console]::ForegroundColor=$State.Foreground } catch { }
    try { [Console]::BackgroundColor=$State.Background } catch { }
    try {
        $top=[Console]::WindowTop; $width=[Math]::Max(0,[Console]::WindowWidth-1)
        for ($i=0; $i -lt [Console]::WindowHeight-1; $i++) {
            [Console]::SetCursorPosition(0,$top+$i); [Console]::Write(' ' * $width)
        }
        [Console]::SetCursorPosition(0,$top)
    } catch { }
    try { [Console]::CursorVisible=$State.Cursor } catch { }
}

function Split-TuiResultLines {
    param([string[]]$Lines, [ValidateRange(2,10000)][int]$Width)
    $rows=[Collections.Generic.List[string]]::new()
    $characters=0; $limited=$false
    foreach ($line in $Lines) {
        $line=[string]$line
        $remaining=1048576-$characters
        if ($remaining -le 0 -or $rows.Count -ge 20000) { $limited=$true; break }
        if ($line.Length -gt $remaining) { $line=$line.Substring(0,$remaining); $limited=$true }
        $characters += $line.Length
        foreach ($part in ($line -split '\r?\n')) {
            $clean=[regex]::Replace($part, '\x1b\[[0-?]*[ -/]*[@-~]', '')
            $clean=[regex]::Replace($clean, '[\p{Cc}\p{Cf}]', ' ')
            $builder=[Text.StringBuilder]::new(); $used=0
            $elements=[Globalization.StringInfo]::GetTextElementEnumerator($clean)
            while ($elements.MoveNext()) {
                $text=[string]$elements.Current; $size=Get-TuiTextWidth $text
                if ($used+$size -gt $Width) {
                    $rows.Add($builder.ToString()); [void]$builder.Clear(); $used=0
                    if ($rows.Count -ge 20000) { $limited=$true; break }
                }
                [void]$builder.Append($text); $used += $size
            }
            if ($rows.Count -ge 20000) { break }
            $rows.Add($builder.ToString())
        }
        if ($limited) { break }
    }
    if ($limited) { $rows.Add('内容过长，预览已截断；请查看原始输出。') }
    if ($rows.Count -eq 0) { $rows.Add('暂无内容。') }
    return $rows.ToArray()
}

function Resolve-TuiResultKey {
    param([string]$Key, [int]$Offset, [int]$Count, [int]$PageSize)
    $next=$Offset; $done=$false
    switch ($Key) {
        'UpArrow' { $next-- }
        'DownArrow' { $next++ }
        'PageUp' { $next -= $PageSize }
        'PageDown' { $next += $PageSize }
        'Home' { $next=0 }
        'End' { $next=$Count }
        'Escape' { $done=$true }
        'Enter' { $done=$true }
    }
    return @{ Offset=[Math]::Max(0,[Math]::Min($next,[Math]::Max(0,$Count-$PageSize))); Done=$done }
}

function Get-TuiResultFrame {
    param([string]$Title, [string[]]$Rows, [int]$Offset, [int]$Width, [int]$Height)
    $body=[Math]::Max(1,$Height-4)
    $offset=[Math]::Max(0,[Math]::Min($Offset,[Math]::Max(0,$Rows.Count-$body)))
    $frame=[Collections.Generic.List[object]]::new()
    $frame.Add(@{Text=$Title; Style='title'})
    $frame.Add(@{Text=('─' * $Width); Style='muted'})
    for ($i=0; $i -lt $body; $i++) {
        $text=if ($offset+$i -lt $Rows.Count) { $Rows[$offset+$i] } else { '' }
        $frame.Add(@{Text=$text; Style='normal'})
    }
    $frame.Add(@{Text=('─' * $Width); Style='muted'})
    $frame.Add(@{Text="↑↓ 滚动  PgUp/PgDn 翻页  Home/End  Enter/Esc 返回 · $($offset+1)-$([Math]::Min($offset+$body,$Rows.Count))/$($Rows.Count)"; Style='muted'})
    foreach ($line in $frame) { [pscustomobject]@{Text=(Format-TuiText $line.Text $Width); Style=$line.Style} }
}

function Show-TuiResult {
    param([string]$Title, [string[]]$Lines)
    $noPauseValue=Get-Variable NoPause -ValueOnly -ErrorAction SilentlyContinue
    $rich= -not $noPauseValue -and (Test-TuiConsole) -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected
    if ($rich) {
        $state=$null; $signature=''; $wrapWidth=0; $rows=@(); $offset=0; $dirty=$true
        try {
            $state=Get-TuiConsoleState; [Console]::CursorVisible=$false
            while ($true) {
                $width=[Console]::WindowWidth-1; $height=[Console]::WindowHeight-1; $top=[Console]::WindowTop
                if ($width -lt 59 -or $height -lt 19) { break }
                if ($width -ne $wrapWidth) { $rows=@(Split-TuiResultLines $Lines $width); $wrapWidth=$width }
                $offset=[Math]::Min($offset,[Math]::Max(0,$rows.Count-($height-4)))
                $size="$width/$height/$top"
                if ($dirty -or $size -ne $signature) {
                    $frame=@(Get-TuiResultFrame $Title $rows $offset $width $height)
                    Write-TuiFrame $frame $width $top $state.Foreground $state.Background
                    $signature=$size; $dirty=$false
                }
                if (-not [Console]::KeyAvailable) { [Threading.Thread]::Sleep(60); continue }
                $key=[Console]::ReadKey($true)
                $next=Resolve-TuiResultKey ([string]$key.Key) $offset $rows.Count ($height-4)
                if ($next.Done) { return }
                $dirty=$next.Offset -ne $offset; $offset=$next.Offset
            }
        } catch [System.IO.IOException] { }
          catch [System.InvalidOperationException] { }
          catch [System.ArgumentOutOfRangeException] { }
        finally { Restore-TuiConsoleState $state }
    }
    Write-Host "`n$Title" -ForegroundColor Cyan
    foreach ($line in @(Split-TuiResultLines $Lines 4096)) { Write-Host $line }
    Pause-Tq
}
