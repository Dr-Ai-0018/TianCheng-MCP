from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.local_runtime import powershell_path

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Console TUI")


def run_script(tmp_path: Path, body: str):
    script = tmp_path / "check-tui.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
        + ". (Join-Path $args[0] 'scripts/tui/console.ps1')\n" + body,
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell_path(ROOT), "-NoLogo", "-NoProfile", "-File", str(script), str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("page", ["main", "proxy", "profile", "key", "commands", "policy", "agents"])
@pytest.mark.parametrize("size", [(100, 30), (140, 40), (80, 24), (60, 20)])
def test_frame_scrolls_and_keeps_help_inside_window(tmp_path: Path, page: str, size):
    width, height = size
    result = run_script(tmp_path, f"""
$items=@(Get-TuiMenuItems '{page}')
$status=@('Profile 中文测试 / long path', '运行 未启动 / Key 已配置')
if ('{page}' -eq 'proxy') {{ $status=@('代理状态','http 未配置','https 未配置','noProxy 未配置','Agent off','检测 未检测','有未保存修改') }}
$checks=@(for ($index=0; $index -lt $items.Count; $index++) {{
    $frame=@(Get-TuiFrame -Page '{page}' -Items $items -Status $status -Index $index -Width {width-1} -Height {height-1})
    @{{ rows=$frame.Count; widths=@($frame | ForEach-Object {{ Get-TuiTextWidth $_.Text }});
        selected=@($frame | Where-Object Style -eq selected).Count;
        selection=($frame | Where-Object Style -eq selected).Text;
        bottom=($frame | Where-Object Text -match "Esc / 0" | Select-Object -Last 1).Text }}
}})
ConvertTo-Json -InputObject $checks -Depth 5
""")
    for frame in result:
        assert frame["rows"] == height - 1
        assert max(frame["widths"]) <= width - 1
        assert frame["selected"] == 1
        assert "›" in frame["selection"]
        assert "退出" in frame["bottom"] or "返回" in frame["bottom"]


def test_keyboard_mapping_and_text_fallback(tmp_path: Path):
    result = run_script(tmp_path, """
$items=@(Get-TuiMenuItems main)
$results=@(
    (Resolve-TuiKey UpArrow '' 0 $items),
    (Resolve-TuiKey DownArrow '' 14 $items),
    (Resolve-TuiKey End '' 0 $items),
    (Resolve-TuiKey Home '' 14 $items),
    (Resolve-TuiKey Enter '' 9 $items),
    (Resolve-TuiKey Escape '' 9 $items),
    (Resolve-TuiKey P 'p' 0 $items),
    (Resolve-TuiKey H 'h' 9 $items),
    (Resolve-TuiKey R 'r' 9 $items),
    (Resolve-TuiKey Z 'z' 9 $items),
    (Resolve-TuiKey V 'v' 9 $items)
)
@{keys=$results; rich=(Test-TuiConsole)} | ConvertTo-Json -Depth 5
""")
    assert result["rich"] is False
    keys = result["keys"]
    assert [entry["Index"] for entry in keys[:4]] == [14, 0, 14, 0]
    assert [entry["Action"] for entry in keys[4:]] == ["P", "0", "P", "H", "R", "", "V"]


def test_long_status_never_pushes_menu_or_footer_outside_window(tmp_path: Path):
    result = run_script(tmp_path, """
$status=@(1..100 | ForEach-Object { "状态 $_" })
$items=@(Get-TuiMenuItems commands)
$frame=@(Get-TuiFrame -Page commands -Items $items -Status $status -Index 6 -Width 79 -Height 23)
@{ rows=$frame.Count; selected=@($frame | Where-Object Style -eq selected).Count;
   text=($frame.Text -join "`n") } | ConvertTo-Json
""")
    assert result["rows"] == 23
    assert result["selected"] == 1
    assert "另有 96 行状态" in result["text"]
    assert "V 查看全部" in result["text"]


def test_help_and_details_reuse_status_without_refresh(tmp_path: Path):
    result = run_script(tmp_path, """
. (Join-Path $args[0] 'scripts/menu-help.ps1')
$script:keys=[Collections.Generic.Queue[string]]::new()
@('H','V','0') | ForEach-Object { $script:keys.Enqueue($_) }
$script:reads=0; $script:pauses=0
function Test-TuiConsole { $true }
function Read-TuiChoice { $script:keys.Dequeue() }
function Pause-Tq { $script:pauses++ }
$status=@(Get-TuiStatusLines { $script:reads++; Write-Host 'cached status' })
$textOnly=$false
$choice=Read-LauncherMenuChoice -Page key -Status $status -TextOnly ([ref]$textOnly) 6>$null
@{ choice=$choice.Choice; reads=$script:reads; pauses=$script:pauses } | ConvertTo-Json
""")
    assert result == {"choice": "0", "reads": 1, "pauses": 2}


def test_unicode_clipping_and_control_sequence_removal(tmp_path: Path):
    result = run_script(tmp_path, """
@{ cjk=(Get-TuiTextWidth '中文ab'); combining=(Get-TuiTextWidth "e$([char]0x301)");
   clipped=(Format-TuiText '中文abcdef' 5);
   safe=(Format-TuiText "name`e[31m`nSECRET" 80) } | ConvertTo-Json
""")
    assert result["cjk"] == 6
    assert result["combining"] == 1
    assert result["clipped"] == "中文…"
    assert result["safe"] == "name SECRET"


def test_status_failure_is_unknown_and_cached_snapshot_is_marked(tmp_path: Path):
    result = run_script(tmp_path, """
$first=@(Get-TuiStatusLines -CacheKey key { throw 'SECRET-error' })
$good=@(Get-TuiStatusLines -CacheKey profile { Write-Host 'SAFE snapshot' })
$stale=@(Get-TuiStatusLines -CacheKey profile { throw 'SECRET-error' })
@{first=$first; stale=$stale; probe=$script:TuiStatusProbe; deadline=$script:TuiProbeDeadline} | ConvertTo-Json
""")
    assert "状态未知" in "\n".join(result["first"])
    assert "缓存" in result["stale"][0]
    assert "SAFE snapshot" in result["stale"]
    assert "SECRET-error" not in str(result)
    assert result["probe"] is False and result["deadline"] == 0


@pytest.mark.parametrize("case", ["timeout", "overflow", "stderr", "success"])
def test_probe_limits_and_argv_forwarding(tmp_path: Path, case: str):
    program = tmp_path / "probe with spaces.ps1"
    bodies = {
        "timeout": "Start-Sleep -Seconds 30",
        "overflow": "[Console]::Write('x' * 20000)",
        "stderr": "[Console]::Error.Write('SECRET-error'); exit 7",
        "success": "$args | ConvertTo-Json -Compress",
    }
    program.write_text(bodies[case], encoding="utf-8")
    arguments = [str(program), "space value", "literal$(NO_COMMAND)", "中文"]
    # Use PowerShell single-quoted literals; values are data, never command text.
    quoted = lambda value: "'" + value.replace("'", "''") + "'"
    result = run_script(tmp_path, f"""
$clock=[Diagnostics.Stopwatch]::StartNew()
try {{
    $output=@(Invoke-TuiProbeProcess -FilePath {quoted(str(program))} -Arguments @('space value','literal$(NO_COMMAND)','中文') -TimeoutMilliseconds {300 if case=='timeout' else 5000} -MaxCharacters 4096)
    @{{ok=$true; text=($output -join "`n"); ms=$clock.ElapsedMilliseconds}} | ConvertTo-Json
}} catch {{ @{{ok=$false; error=$_.Exception.Message; ms=$clock.ElapsedMilliseconds}} | ConvertTo-Json }}
""")
    if case == "success":
        assert result["ok"] is True
        assert json.loads(result["text"]) == arguments[1:]
    else:
        assert result["ok"] is False
        assert "SECRET-error" not in result["error"]
        assert {"timeout": "超时", "overflow": "超过限制", "stderr": "退出码 7"}[case] in result["error"]
    if case == "timeout":
        assert result["ms"] < 2500


def test_probe_preserves_single_argument(tmp_path: Path):
    program = tmp_path / "single.ps1"
    program.write_text("ConvertTo-Json -InputObject @($args) -Compress", encoding="utf-8")
    quoted = str(program).replace("'", "''")
    result = run_script(tmp_path, f"""
$output=@(Invoke-TuiProbeProcess -FilePath '{quoted}' -Arguments @('tunnel'))
@{{text=($output -join "`n")}} | ConvertTo-Json
""")
    assert json.loads(result["text"]) == ["tunnel"]


@pytest.mark.parametrize("size", [(59, 19), (79, 23), (139, 39)])
def test_result_pages_wrap_and_keep_navigation_visible(tmp_path: Path, size):
    width, height = size
    result = run_script(tmp_path, f"""
$text=('中文路径 abc/' * 50)
$rows=@(Split-TuiResultLines @($text,'tail') {width})
$frame=@(Get-TuiResultFrame '详情' $rows 999 {width} {height})
@{{rows=$rows; frame=$frame; width=@($frame | ForEach-Object {{ Get-TuiTextWidth $_.Text }})}} | ConvertTo-Json -Depth 5
""")
    assert "".join(result["rows"]) == "中文路径 abc/" * 50 + "tail"
    assert len(result["frame"]) == height
    assert max(result["width"]) <= width
    assert "Enter/Esc" in result["frame"][-1]["Text"]
    assert any(row["Text"] == "tail" for row in result["frame"])


def test_result_navigation_clamps_and_returns(tmp_path: Path):
    result = run_script(tmp_path, """
@('UpArrow','DownArrow','PageUp','PageDown','Home','End','Escape','Enter','Z') | ForEach-Object {
    Resolve-TuiResultKey $_ 15 100 20
} | ConvertTo-Json
""")
    assert [row["Offset"] for row in result] == [14, 16, 0, 35, 0, 80, 15, 15, 15]
    assert [row["Done"] for row in result] == [False] * 6 + [True, True, False]


def test_result_text_sanitizes_without_clipping_paths(tmp_path: Path):
    result = run_script(tmp_path, """
$rows=@(Split-TuiResultLines @("abc`e[31m`n中文路径abcdef") 6)
@{rows=$rows; widths=@($rows | ForEach-Object {Get-TuiTextWidth $_})} | ConvertTo-Json
""")
    assert "".join(result["rows"]) == 'abc中文路径abcdef'
    assert max(result["widths"]) <= 6


def test_console_state_read_failure_falls_back_and_runs_cleanup(tmp_path: Path):
    result = run_script(tmp_path, """
$script:cleanups=0
function Get-TuiConsoleState { throw [IO.IOException]::new('console closed') }
function Restore-TuiConsoleState { param($State); $script:cleanups++ }
$choice=Read-TuiChoice -Page main -Status @()
@{choice=$choice; cleanups=$script:cleanups} | ConvertTo-Json
""")
    assert result == {"choice": "__text", "cleanups": 1}


@pytest.mark.parametrize("doctor_code,supervisor_code", [(7, 0), (0, 6), (0, 0)])
def test_doctor_report_keeps_exit_code_and_supervisor_gate(tmp_path: Path, doctor_code: int, supervisor_code: int):
    doctor = tmp_path / 'doctor.cmd'
    doctor.write_text(f"@echo off\necho doctor stdout\necho doctor stderr 1>&2\nexit /b {doctor_code}\n", encoding='ascii')
    supervisor = tmp_path / 'supervisor.ps1'
    supervisor.write_text(f"'supervisor stdout'; exit {supervisor_code}", encoding='utf-8')
    quote = lambda path: str(path).replace("'", "''")
    result = run_script(tmp_path, f"""
. (Join-Path $args[0] 'tc.ps1') -Action info -Json -NoUserEnvironment | Out-Null
function Test-ProfileExists {{ $true }}
function Assert-ProfileConfiguration {{ }}
function Import-ControlPlaneKey {{ @{{Configured=$true; Source='test-only'}} }}
function Get-ProfileDirectoryArguments {{ @() }}
function Test-SupervisorEnabled {{ $true }}
$report=@()
$code=Invoke-Doctor -Config @{{tunnelClient='{quote(doctor)}'; python='{quote(supervisor)}'}} -Name demo -Report ([ref]$report)
@{{code=$code; report=$report}} | ConvertTo-Json
""")
    assert result["code"] == (doctor_code or supervisor_code)
    assert "doctor stdout" in result["report"]
    assert "doctor stderr" in [line.strip() for line in result["report"]]
    assert ("supervisor stdout" in result["report"]) is (doctor_code == 0)


def test_doctor_result_renders_failure_instead_of_success(tmp_path: Path):
    result = run_script(tmp_path, """
. (Join-Path $args[0] 'tc.ps1') -Action info -Json -NoUserEnvironment | Out-Null
function Invoke-Doctor { param($Config,$Name,[ref]$Report); $Report.Value=@('diagnostic'); return 9 }
function Show-TuiResult { param($Title,$Lines); @{title=$Title; lines=$Lines} | ConvertTo-Json }
Show-DoctorResult -Config @{} -Name demo
""")
    assert "检查失败" in "\n".join(result["lines"])
    assert "退出码：9" in "\n".join(result["lines"])
    assert "diagnostic" in result["lines"]


def test_ui_developer_queries_are_bounded_and_do_not_expose_accounts(tmp_path: Path):
    result = run_script(tmp_path, """
. (Join-Path $args[0] 'tc.ps1') -Action info -Json -NoUserEnvironment | Out-Null
function Get-Command { param($Name); [pscustomobject]@{Source=$Name} }
$script:calls=@(); $script:TuiStatusProbe=$true
function Invoke-TuiProbeProcess {
    param($FilePath,$Arguments)
    $script:calls += $FilePath
    if ($FilePath -eq 'git') { throw [IO.IOException]::new('SECRET-account') }
    'SECRET-account'
}
$status=Get-DeveloperToolStatus
@{status=$status; calls=$script:calls} | ConvertTo-Json -Depth 4
""")
    assert result["calls"] == ["git", "gh"]
    assert result["status"]["gcmConfigured"] is None
    assert result["status"]["ghAuthenticated"] is True
    assert "SECRET-account" not in str(result)


def test_tall_window_keeps_context_help_next_to_menu(tmp_path: Path):
    result = run_script(tmp_path, """
$frame=@(Get-TuiFrame -Page main -Items @(Get-TuiMenuItems main) -Status @('示例状态') -Index 1 -Width 112 -Height 79)
$menuEnd=0; $help=0
for ($i=0;$i -lt $frame.Count;$i++) {
    if ($frame[$i].Text -match 'B  ') { $menuEnd=$i }
    if ($frame[$i].Text -match '^在独立 PowerShell') { $help=$i }
}
@{rows=$frame.Count; distance=$help-$menuEnd; help=$help} | ConvertTo-Json
""")
    assert result["rows"] == 79
    assert result["distance"] == 3
    assert result["help"] < 30


@pytest.mark.parametrize("page", ["main", "doctor", "status"])
def test_preview_is_explicit_sample_data_and_does_not_read_keys(tmp_path: Path, page: str):
    environment=dict(os.environ, CONTROL_PLANE_API_KEY='SECRET-preview-key')
    result=subprocess.run(
        [powershell_path(ROOT), '-NoLogo', '-NoProfile', '-File', str(ROOT/'scripts/preview-tui.ps1'), '-Page', page, '-NoPause'],
        input='2\n5\n8\n0\n' if page=='main' else None,
        capture_output=True, text=True, encoding='utf-8', env=environment, cwd=tmp_path, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '演示' in result.stdout
    assert 'SECRET-preview-key' not in result.stdout + result.stderr
    assert '未执行真实 Doctor' in result.stdout if page in ('main','doctor') else '未查询机器或网络状态' in result.stdout
    assert not list(tmp_path.iterdir())
    if page=='main':
        assert '不执行菜单操作' in result.stdout
