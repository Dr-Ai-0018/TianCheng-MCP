from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tiancheng_mcp.agents import AgentProfileRegistry
from tiancheng_mcp.protocol_stream import JsonlStream, ProtocolStreamError
from tiancheng_mcp.service import TianChengService
import tiancheng_mcp.service as module


@pytest.fixture
def runtime(workspace):
    service = TianChengService(workspace, None, allow_exec=True, enable_jobs=False,
        enable_agent_catalog=False)
    yield service
    service.shutdown()


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 1000])
def test_protocol_utf8_and_crlf_survive_every_chunk_boundary(chunk_size):
    stream = JsonlStream()
    data = '{"text":"中文🙂"}\r\n{"tail":"完成"}'.encode()
    lines = []
    for offset in range(0, len(data), chunk_size):
        lines += stream.feed(data[offset:offset + chunk_size])
    lines += stream.feed(b"", final=True)
    assert [json.loads(line) for line in lines] == [{"text": "中文🙂"}, {"tail": "完成"}]


@pytest.mark.parametrize("data,final", [(b"\xff", False), (b"\xe4", True), (b"x" * 9, False), (b"x" * 9 + b"\n", False)])
def test_protocol_invalid_or_oversized_bytes_fail_closed(data, final):
    with pytest.raises(ProtocolStreamError):
        JsonlStream(max_line_bytes=8).feed(data, final=final)


def test_protocol_gap_discards_partial_frame():
    stream = JsonlStream()
    stream.feed(b'{"lost":')
    stream.reset_after_gap()
    assert stream.feed(b'\x80tail}\n{"ok":1}\n') == ['{"ok":1}']


@pytest.mark.parametrize("second_owner", ["process", "agent"])
def test_admission_reserves_before_spawn_for_both_owners(runtime, workspace, tmp_path, monkeypatch, second_owner):
    monkeypatch.setattr(module, "MAX_MANAGED_PROCESSES", 1)
    reached = threading.Event()
    release = threading.Event()
    actual = subprocess.Popen
    spawned = []
    results = []
    failures = []

    def paused_spawn(*args, **kwargs):
        reached.set()
        assert release.wait(5)
        process = actual(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", paused_spawn)
    def first():
        try:
            results.append(runtime.start_process("python", ["-c", "import time;time.sleep(60)"], max_runtime_seconds=90))
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert reached.wait(5)
        with pytest.raises(RuntimeError, match="At most 1"):
            if second_owner == "process":
                runtime.start_process("python", ["-c", "print('second')"])
            else:
                script = tmp_path / "fake.py"
                script.write_text("print('{}')", encoding="utf-8")
                runtime._exec_commands["codex"] = [sys.executable, str(script)]
                runtime.agent_profiles = AgentProfileRegistry(["codex"])
                session = runtime.agent_session_create()
                runtime.agent_run_start(session["session_id"], "synthetic")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert len(spawned) == 1 and runtime._process_slots.active_count == 1
    runtime.stop_process(results[0]["process_id"], force=True)
    assert runtime._process_slots.active_count == 0


def test_spawn_and_environment_failure_release_reservation(runtime, monkeypatch):
    original = module.subprocess.Popen
    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failure")
    with monkeypatch.context() as patch:
        patch.setattr(module.subprocess, "Popen", fail)
        with pytest.raises(OSError, match="spawn failure"):
            runtime.start_process("python", ["-c", "print('unused')"])
    assert runtime._process_slots.active_count == 0

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_execution_environment", fail)
        with pytest.raises(OSError):
            runtime.start_process("python")
    assert runtime._process_slots.active_count == 0
    assert module.subprocess.Popen is original
    started = runtime.start_process("python", ["-c", "print('recovered')"])
    record = runtime._get_managed_process(started["process_id"])
    assert record.finished.wait(5)
    assert runtime._process_slots.active_count == 0


def test_watcher_start_failure_cleans_real_spawn(runtime, monkeypatch):
    captured = []
    actual_spawn = module.subprocess.Popen
    actual_start = threading.Thread.start
    def capture(*args, **kwargs):
        process = actual_spawn(*args, **kwargs)
        captured.append(process)
        return process
    def fail_watcher(thread):
        if getattr(thread._target, "__name__", "") == "_watch_managed_process":
            raise RuntimeError("synthetic watcher start failure")
        return actual_start(thread)
    monkeypatch.setattr(module.subprocess, "Popen", capture)
    monkeypatch.setattr(threading.Thread, "start", fail_watcher)
    with pytest.raises(RuntimeError, match="watcher start"):
        runtime.start_process("python", ["-c", "import time;time.sleep(60)"])
    assert captured[0].poll() is not None
    assert captured[0].stdout.closed and captured[0].stderr.closed
    assert not runtime._processes and runtime._process_slots.active_count == 0


@pytest.mark.parametrize("termination", ["normal", "stop", "timeout", "shutdown"])
def test_terminal_releases_handles_and_preserves_output(runtime, monkeypatch, termination):
    captured = []
    actual = module.subprocess.Popen
    def capture(*args, **kwargs):
        process = actual(*args, **kwargs)
        captured.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", capture)
    code = "print('retained-output',flush=True)"
    if termination != "normal":
        code += ";import time;time.sleep(60)"
    started = runtime.start_process("python", ["-c", code], max_runtime_seconds=1 if termination == "timeout" else 90)
    record = runtime._get_managed_process(started["process_id"])
    if termination in {"stop", "shutdown"}:
        deadline = time.monotonic() + 5
        while "retained-output" not in runtime.process_output(started["process_id"])["stdout"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if termination == "stop":
            runtime.stop_process(started["process_id"], force=True)
        else:
            runtime.shutdown()
    assert record.finished.wait(5)
    assert record.process is None and record.kill_job is None and not record.reader_threads
    assert all(stream is None or stream.closed for stream in (captured[0].stdin, captured[0].stdout, captured[0].stderr))
    if os.name == "nt":
        assert captured[0]._handle.closed
    assert runtime._process_slots.active_count == 0
    assert runtime.process_status(started["process_id"])["resources_released"]
    assert "retained-output" in runtime.process_output(started["process_id"])["stdout"]


def test_cleanup_failure_still_closes_other_resources(runtime, monkeypatch):
    started = runtime.start_process("python", ["-c", "import time;time.sleep(60)"])
    record = runtime._get_managed_process(started["process_id"])
    process = record.process
    original_close = record.kill_job.close
    def fail_close():
        original_close()
        raise OSError("synthetic job close failure")
    monkeypatch.setattr(record.kill_job, "close", fail_close)
    runtime.stop_process(started["process_id"], force=True)
    assert record.finished.wait(5)
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    if os.name == "nt":
        assert process._handle.closed
    assert not record.reader_threads and record.process is None
    assert runtime._process_slots.active_count == 0
    status = runtime.process_status(started["process_id"])
    assert not status["resources_released"] and status["resource_cleanup_error"] == "OSError"


def test_shutdown_blocks_spawn_that_was_still_preparing(runtime, monkeypatch):
    reached = threading.Event()
    release = threading.Event()
    original = runtime._execution_environment
    failures = []
    def pause(**kwargs):
        reached.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(runtime, "_execution_environment", pause)
    def start():
        try:
            runtime.start_process("python", ["-c", "import time;time.sleep(60)"])
        except RuntimeError as exc:
            failures.append(str(exc))
    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert reached.wait(5)
        runtime.shutdown()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert failures and "shutting down" in failures[0]
    assert runtime._process_slots.active_count == 0 and not runtime._processes
    with pytest.raises(RuntimeError, match="shutting down"):
        runtime.start_process("python")


def test_history_has_bounded_capacity_and_distinguishes_expiry(runtime, monkeypatch):
    monkeypatch.setattr(module, "MAX_PROCESS_HISTORY", 1)
    first = runtime.start_process("python", ["-c", "print('first')"])["process_id"]
    assert runtime._get_managed_process(first).finished.wait(5)
    second = runtime.start_process("python", ["-c", "print('second')"])["process_id"]
    record = runtime._get_managed_process(second)
    assert record.finished.wait(5)
    assert runtime.list_processes()["count"] == 1
    with pytest.raises(FileNotFoundError, match="history expired"):
        runtime.process_output(first)
    assert "second" in runtime.process_output(second)["stdout"]
    record.ended_epoch -= module.PROCESS_HISTORY_SECONDS + 1
    with pytest.raises(FileNotFoundError, match="history expired"):
        runtime.process_status(second)
    with pytest.raises(FileNotFoundError, match="not found"):
        runtime.process_status("0" * 32)


def test_real_agent_split_utf8_and_cached_history(runtime, workspace, tmp_path):
    text = "中文🙂完成"
    payload = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}, ensure_ascii=False).encode() + b"\n"
    split = payload.index("中".encode()) + 1
    script = tmp_path / "split.py"
    script.write_text(f'''import os,time,pathlib
data={payload!r}
os.write(1,data[:{split}])
pathlib.Path('partial-ready').touch()
deadline=time.monotonic()+5
while not pathlib.Path('release-partial').exists():
    if time.monotonic()>deadline: raise RuntimeError('test release timed out')
    time.sleep(.01)
os.write(1,data[{split}:])
''', encoding="utf-8")
    runtime._exec_commands["codex"] = [sys.executable, str(script)]
    runtime.agent_profiles = AgentProfileRegistry(["codex"])
    session = runtime.agent_session_create()
    started = runtime.agent_run_start(session["session_id"], "synthetic")
    _, run = runtime._get_agent_run(session["session_id"], started["run_id"])
    deadline = time.monotonic() + 5
    while run.stdout_offset != split:
        runtime.agent_run_events(session["session_id"], run.run_id)
        assert time.monotonic() < deadline
        time.sleep(.01)
    (workspace / "release-partial").touch()
    while run.final_process_status is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    result = runtime.agent_run_result(session["session_id"], run.run_id)
    assert result["state"] == "succeeded" and result["result"] == text
    assert run.process_id not in runtime._processes
    assert not runtime._process_status(run.process_id)["running"]
    assert runtime.agent_run_events(session["session_id"], run.run_id)["events"]
    assert runtime.agent_run_cancel(session["session_id"], run.run_id)["already_finished"]
    with pytest.raises(PermissionError, match="Agent run"):
        runtime.process_output(run.process_id)


@pytest.mark.parametrize("data", [b'\xff\n', b'x' * (256 * 1024 + 1)], ids=["invalid-utf8", "oversized-line"])
def test_real_agent_bad_protocol_fails_and_releases(runtime, tmp_path, data):
    script = tmp_path / "bad.py"
    script.write_text(f"import os;os.write(1,{data!r})", encoding="utf-8")
    runtime._exec_commands["codex"] = [sys.executable, str(script)]
    runtime.agent_profiles = AgentProfileRegistry(["codex"])
    session = runtime.agent_session_create()
    started = runtime.agent_run_start(session["session_id"], "synthetic")
    _, run = runtime._get_agent_run(session["session_id"], started["run_id"])
    deadline = time.monotonic() + 5
    while run.final_process_status is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    result = runtime.agent_run_result(session["session_id"], run.run_id)
    assert result["state"] == "failed" and "protocol" in result["error"]
    assert not run.final_process_status["running"]
    assert runtime._process_slots.active_count == 0


def test_terminal_agent_drains_more_than_one_page_before_cache(runtime, tmp_path):
    from dataclasses import replace
    script = tmp_path / "many.py"
    script.write_text("import json\nfor i in range(300): print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'x'*4000}}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'尾部正确'}},ensure_ascii=False),end='')\n", encoding="utf-8")
    runtime._exec_commands["codex"] = [sys.executable, str(script)]
    runtime.agent_profiles = AgentProfileRegistry(["codex"])
    profile = runtime.agent_profiles.get("codex-default")
    runtime.agent_profiles._profiles[profile.name] = replace(profile, max_output_bytes=module.MAX_MANAGED_OUTPUT_BYTES)
    session = runtime.agent_session_create()
    started = runtime.agent_run_start(session["session_id"], "synthetic")
    _, run = runtime._get_agent_run(session["session_id"], started["run_id"])
    deadline = time.monotonic() + 10
    while run.final_process_status is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    result = runtime.agent_run_result(session["session_id"], run.run_id)
    assert result["state"] == "succeeded" and result["result"] == "尾部正确"
    assert run.stdout_offset > module.MAX_COMMAND_OUTPUT_BYTES
    assert run.process_id not in runtime._processes


def test_history_total_byte_budget_evicts_oldest(runtime, monkeypatch):
    monkeypatch.setattr(module, "MAX_PROCESS_HISTORY_BYTES", 100)
    ids = []
    for _ in range(3):
        process_id = runtime.start_process("python", ["-c", "print('x'*40)"])["process_id"]
        assert runtime._get_managed_process(process_id).finished.wait(5)
        ids.append(process_id)
    assert runtime.list_processes()["count"] == 2
    with pytest.raises(FileNotFoundError, match="expired"):
        runtime.process_output(ids[0])
    assert "x" in runtime.process_output(ids[-1])["stdout"]
