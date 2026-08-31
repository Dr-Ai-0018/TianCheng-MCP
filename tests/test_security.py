from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from tiancheng_mcp.security import WorkspaceSecurityError
from tiancheng_mcp.service import TianChengService


@pytest.mark.parametrize(
    "attack",
    [
        r"..\outside.txt",
        r"folder\..\outside.txt",
        r"E:\outside.txt",
        r"C:\Windows\win.ini",
        r"Z:\other-drive.txt",
        r"\\server\share\secret.txt",
        r"//server/share/secret.txt",
        r"\\?\C:\Windows\win.ini",
    ],
)
def test_path_escape_variants_are_rejected(
    service: TianChengService, attack: str
) -> None:
    with pytest.raises(WorkspaceSecurityError):
        service.jail.resolve(attack, must_exist=False)


def test_symlink_escape_is_rejected(
    service: TianChengService, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-symlink"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")
    with pytest.raises(WorkspaceSecurityError, match="reparse point|Symlink"):
        service.read_text("link/secret.txt")
    with pytest.raises(WorkspaceSecurityError, match="reparse point|Symlink"):
        service.write_text("link/new.txt", "blocked")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_junction_escape_is_rejected(
    service: TianChengService, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-junction"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    junction = workspace / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        creationflags=0x08000000,
    )
    if result.returncode != 0 or not junction.exists():
        pytest.skip("Junction creation is unavailable in this test environment")
    with pytest.raises(WorkspaceSecurityError, match="reparse point"):
        service.read_text("junction/secret.txt")


def test_audit_directory_must_be_outside_workspace(workspace: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        TianChengService(workspace, workspace / "logs")
    assert not (workspace / "logs").exists()


def test_rejected_absolute_path_is_sanitized_in_audit_log(
    service: TianChengService, tmp_path: Path
) -> None:
    from tiancheng_mcp.server import _audit_path

    label = _audit_path(r"C:\Users\someone\secret.txt")
    assert label == "<rejected-path>"


def test_windows_ambiguous_components_are_rejected(service: TianChengService) -> None:
    for path in ("name. ", "NUL.txt", "file.txt:secret", "bad?.txt"):
        with pytest.raises(WorkspaceSecurityError):
            service.write_text(path, "blocked")


def test_exec_environment_does_not_inherit_control_plane_key(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "must-not-leak")
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    result = enabled.run_command(
        "python",
        ["-c", "import os; print('CONTROL_PLANE_API_KEY' in os.environ)"],
        timeout_seconds=10,
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "False"
    assert "must-not-leak" not in result["stdout"] + result["stderr"]


def test_exec_environment_passes_only_explicit_named_variable(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXAMPLE_SERVICE_KEY", "fixture-not-a-real-key")
    enabled = TianChengService(
        workspace,
        tmp_path / "exec-audit",
        allow_exec=True,
        passthrough_env=("EXAMPLE_SERVICE_KEY",),
    )
    result = enabled.run_command(
        "python",
        ["-c", "import os; print(os.environ.get('EXAMPLE_SERVICE_KEY', 'missing'))"],
        timeout_seconds=10,
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "fixture-not-a-real-key"


def test_exec_environment_never_allows_protected_names(
    workspace: Path, tmp_path: Path
) -> None:
    with pytest.raises(PermissionError, match="never passed"):
        TianChengService(
            workspace,
            tmp_path / "exec-audit",
            allow_exec=True,
            passthrough_env=("CONTROL_PLANE_API_KEY",),
        )


def test_exec_rejects_unlisted_shells_but_allows_developer_arguments(
    workspace: Path, tmp_path: Path
) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    for command in ("cmd", "del", "powershell", "pwsh", "rm"):
        with pytest.raises(PermissionError, match="allowlisted"):
            enabled.run_command(command, ["unused"])
    prepared = enabled._prepare_exec_command("python", ["-c", "print('developer mode')"])
    assert prepared[-2:] == ["-c", "print('developer mode')"]


def test_exec_git_is_open_but_credential_secret_commands_are_blocked(
    workspace: Path, tmp_path: Path
) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    if "git" not in enabled._exec_commands:
        pytest.skip("Git is unavailable")
    assert enabled._prepare_exec_command("git", ["push"])[-1] == "push"
    assert enabled._prepare_exec_command("git", ["reset", "--hard"])[-2:] == [
        "reset",
        "--hard",
    ]
    with pytest.raises(PermissionError, match="keyring secrets"):
        enabled.run_command("git", ["credential", "fill"])
    if "gh" in enabled._exec_commands:
        with pytest.raises(PermissionError, match="prints a secret"):
            enabled.run_command("gh", ["auth", "token"])


def test_exec_git_policy_allows_bounded_read_only_status(
    workspace: Path, tmp_path: Path
) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    if "git" not in enabled._exec_commands:
        pytest.skip("Git is unavailable")
    enabled.git_init(".")
    result = enabled.run_command("git", ["status", "--short"], timeout_seconds=10)
    assert result["exit_code"] == 0
    assert result["policy"] == "guarded-development"


def test_exec_codex_is_discoverable_when_installed(
    workspace: Path, tmp_path: Path
) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    if "codex" not in enabled._exec_commands:
        pytest.skip("Codex CLI is unavailable")
    prepared = enabled._prepare_exec_command("codex", ["--version"])
    assert prepared[0].casefold().endswith("codex.exe") or prepared[0].casefold().endswith("node.exe")


def test_exec_output_is_bounded(workspace: Path, tmp_path: Path) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    result = enabled.run_command(
        "python",
        ["-c", "print('x' * 10000)"],
        timeout_seconds=10,
        max_output_bytes=1024,
    )
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= 1024
    assert result["stdout_bytes_total"] > 1024


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
def test_exec_timeout_kills_child_process_tree(workspace: Path, tmp_path: Path) -> None:
    enabled = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    child_code = (
        "import pathlib,time; time.sleep(2); "
        "pathlib.Path('child-survived.txt').write_text('bad', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    result = enabled.run_command(
        "python", ["-c", parent_code], timeout_seconds=1, max_output_bytes=4096
    )
    assert result["timeout"] is True
    time.sleep(3)
    assert not (workspace / "child-survived.txt").exists()
