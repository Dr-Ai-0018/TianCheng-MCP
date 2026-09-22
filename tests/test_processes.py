from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from tiancheng_mcp.service import TianChengService


def _wait_for_exit(service: TianChengService, process_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.process_status(process_id)
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("managed process did not exit")


def test_managed_process_output_and_status(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    started = service.start_process(
        "python",
        ["-c", "import time; print('ready', flush=True); time.sleep(0.2); print('done')"],
        max_runtime_seconds=10,
    )
    process_id = started["process_id"]
    status = _wait_for_exit(service, process_id)
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    output = service.process_output(process_id)
    assert "ready" in output["stdout"]
    assert "done" in output["stdout"]
    assert service.list_processes()["count"] == 1


def test_managed_process_can_be_stopped(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    started = service.start_process(
        "python", ["-c", "import time; time.sleep(60)"], max_runtime_seconds=120
    )
    stopped = service.stop_process(started["process_id"], force=True)
    assert stopped["running"] is False
    assert stopped["state"] == "stopped"


def test_managed_process_hard_runtime_limit(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    started = service.start_process(
        "python", ["-c", "import time; time.sleep(60)"], max_runtime_seconds=1
    )
    status = _wait_for_exit(service, started["process_id"], timeout=10)
    assert status["state"] == "timed_out"
    assert status["timed_out"] is True


def test_managed_process_accepts_stdin_and_reports_session_id(
    workspace: Path, tmp_path: Path
) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    started = service.start_process(
        "python",
        ["-c", "import sys; print(sys.stdin.read(), end='', flush=True)"],
        max_runtime_seconds=10,
    )
    assert started["session_id"].startswith("sess_")
    sent = service.process_input(started["process_id"], "中文输入\n", close_stdin=True)
    assert sent["bytes_sent"] == len("中文输入\n".encode("utf-8"))
    status = _wait_for_exit(service, started["process_id"])
    assert status["state"] == "exited"
    assert status["stdin_closed"] is True
    assert service.process_output(started["process_id"])["stdout"] == "中文输入\r\n"


def test_managed_process_output_cursor_is_incremental(
    workspace: Path, tmp_path: Path
) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    started = service.start_process(
        "python",
        ["-c", "print('one', flush=True); print('two', flush=True)"],
        max_runtime_seconds=10,
    )
    status = _wait_for_exit(service, started["process_id"])
    assert status["state"] == "exited"
    first = service.process_output(started["process_id"], stream="stdout", max_bytes=5)
    assert first["stdout"] == "one\r\n"
    assert first["stdout_next_offset_bytes"] == 5
    second = service.process_output(
        started["process_id"], stream="stdout", max_bytes=16, after_bytes=5
    )
    assert second["stdout"] == "two\r\n"
    assert second["stdout_cursor_gap"] is False
    assert second["stdout_next_offset_bytes"] == 10


def test_managed_output_is_readable_before_the_process_exits(
    workspace: Path, tmp_path: Path
) -> None:
    """Incremental output must not wait for the pipe to close.

    A blocking read of a fixed size holds everything back until the process
    exits, which makes every long-poll and cursor API look correct while
    delivering nothing until the very end.
    """

    script = workspace / "emit.py"
    script.write_text(
        "import sys, time\n"
        "print('FIRST', flush=True)\n"
        "time.sleep(5)\n"
        "print('SECOND', flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    try:
        started = service.start_process("python", ["emit.py"], ".")
        process_id = started["process_id"]
        deadline = time.monotonic() + 4
        seen = ""
        while time.monotonic() < deadline:
            seen = service.process_output(process_id, stream="stdout")["stdout"]
            if "FIRST" in seen:
                break
            time.sleep(0.1)
        assert service.process_status(process_id)["running"] is True, (
            "process already exited; the test no longer proves streaming"
        )
        assert "FIRST" in seen, "first line was not readable while still running"
        assert "SECOND" not in seen
        service.stop_process(process_id, force=True)
    finally:
        service.shutdown()


def _fake_which(mapping: dict[str, str]):
    def which(name: str, *args, **kwargs):
        return mapping.get(name)

    return which


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_codex_discovery_uses_the_npm_managed_launcher(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path
) -> None:
    launcher = _touch(tmp_path / "npm/codex.cmd")
    _touch(tmp_path / "npm/node_modules/@openai/codex/bin/codex.js")
    node = _touch(tmp_path / "node.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(
        shutil, "which", _fake_which({"node": str(node), "codex": str(launcher)})
    )

    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)

    codex = service._exec_commands["codex"]
    assert codex[0] == str(node.resolve())
    assert codex[1].endswith("codex.js")


def test_codex_discovery_refuses_the_desktop_build(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path
) -> None:
    local_app_data = tmp_path / "AppData/Local"
    desktop = _touch(local_app_data / "OpenAI/Codex/bin/247581e40ee272fb/codex.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(shutil, "which", _fake_which({"codex": str(desktop)}))

    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)

    # Mixing the Desktop build with the npm release makes each one invalidate
    # the other's elevated-sandbox marker, so every sandboxed call re-runs the
    # elevated provisioning helper and raises a UAC prompt.
    assert "codex" not in service._exec_commands


def test_codex_discovery_refuses_a_bare_windows_executable(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path
) -> None:
    stray = _touch(tmp_path / "elsewhere/codex.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(shutil, "which", _fake_which({"codex": str(stray)}))
    monkeypatch.setattr(os, "name", "nt")

    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)

    assert "codex" not in service._exec_commands
