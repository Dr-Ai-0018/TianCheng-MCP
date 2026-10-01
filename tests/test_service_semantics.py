from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import queue
import subprocess
import threading

import pytest

from tiancheng_mcp.jobs import JobManager, JobCancelled
from tiancheng_mcp.search_runtime import SearchCandidates
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp.tunnel_supervisor import LauncherConfig, SupervisorSettings, TunnelSupervisor


def test_finished_idempotent_write_survives_capacity_and_conflict(tmp_path: Path) -> None:
    manager = JobManager(workers=1, max_records=1)
    marker = tmp_path / "marker"

    def write(_cancel):
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("once\n")
        return "written"

    try:
        original = manager.submit("write", write, idempotency_key="same")
        assert original.done.wait(3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: manager.submit("write", write, idempotency_key="same"), range(16)))
        assert all(record is original for record in results)
        with pytest.raises(ValueError, match="different operation"):
            manager.submit("other-write", write, idempotency_key="same")
        assert manager.get(original.job_id) is original
        assert marker.read_text(encoding="utf-8") == "once\n"
        # A genuinely new request may reclaim a finished record.
        fresh = manager.submit("write", write, idempotency_key="new")
        assert fresh.done.wait(3)
        assert marker.read_text(encoding="utf-8") == "once\nonce\n"
    finally:
        manager.shutdown()


def test_expired_idempotency_key_may_start_new_work() -> None:
    manager = JobManager(workers=1, max_records=1, retention_seconds=1)
    try:
        first = manager.submit("write", lambda _: True, idempotency_key="same")
        assert first.done.wait(3)
        first.finished_at -= 2
        second = manager.submit("write", lambda _: True, idempotency_key="same")
        assert second is not first and second.done.wait(3)
    finally:
        manager.shutdown()


def test_failed_enqueue_does_not_publish_phantom_idempotency(tmp_path, monkeypatch):
    manager = JobManager(workers=1)
    entered, release, retrying = threading.Event(), threading.Event(), threading.Event()
    original = manager._queue.put_nowait
    calls = 0
    marker = tmp_path / "queue-marker"

    def put(record):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
            raise queue.Full
        original(record)

    monkeypatch.setattr(manager._queue, "put_nowait", put)

    def submit(retry=False):
        if retry:
            retrying.set()
        return manager.submit("write", lambda _: marker.write_text("once", encoding="utf-8"),
                              idempotency_key="same")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit)
            try:
                assert entered.wait(3)
                second = pool.submit(submit, True)
                assert retrying.wait(3)
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="queue is full"):
                first.result(timeout=3)
            record = second.result(timeout=3)
            assert record.done.wait(3)
            assert marker.read_text(encoding="utf-8") == "once"
            assert manager.get(record.job_id) is record
    finally:
        release.set()
        manager.shutdown()


def test_broken_audit_preserves_handoff_cancel_and_real_write(
    service: TianChengService, workspace: Path, monkeypatch, capsys,
) -> None:
    def fail(**_event):
        raise OSError("synthetic-secret-must-not-leak")

    monkeypatch.setattr(service.audit, "record", fail)
    service.interactive_timeout_seconds = 0.01
    release = threading.Event()

    def operation():
        assert release.wait(3)
        (workspace / "actual-write.txt").write_text("done", encoding="utf-8")
        return {"written": True}

    try:
        response = service.run_with_fallback("write_text", "actual-write.txt", operation)
        assert response["execution"] == "background"
        record = service.jobs.get(response["job_id"])
        release.set()
        assert record.done.wait(3)
        assert service.job_result(record.job_id)["result"] == {"written": True}
        assert (workspace / "actual-write.txt").read_text(encoding="utf-8") == "done"
        entered = threading.Event()

        def cancellable(cancel):
            entered.set()
            assert cancel.wait(3)
            raise JobCancelled("cancelled")

        other = service.jobs.submit("cancel-test", cancellable)
        assert entered.wait(3)
        service.job_cancel(other.job_id)
        assert other.done.wait(3)
        assert service.job_status(other.job_id)["state"] == "cancelled"
        health = service.workspace_info()["audit_health"]
        assert health == {"failure_count": 3, "last_error_type": "OSError"}
        assert "synthetic-secret" not in capsys.readouterr().err
    finally:
        release.set()
        service.shutdown()


@pytest.fixture(params=["python", "ripgrep"])
def search_service(service: TianChengService, request):
    if request.param == "ripgrep" and not service.rg_executable:
        pytest.skip("ripgrep is unavailable")
    if request.param == "python":
        service.rg_executable = None
    return service


def test_search_shared_filters_nested_ignores_and_native_oracle(search_service, workspace):
    files = {
        "visible.txt", "root-only.txt", "sub/root-only.txt", "skip.tmp", "keep.tmp",
        "sub/skip.tmp", "sub/keep.tmp", "ignored/child.txt", "tree/drop.txt", "tree/keep.txt",
        "sub/deep/no.txt", "sub/deep/yes.txt", "#literal.txt", "!literal.txt", "space .txt",
        ".hidden/x.txt", "node_modules/internal.txt", "override.txt", "sub/override.txt",
    }
    for name in files:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TQ_CONTENT\n", encoding="utf-8")
    (workspace / ".gitignore").write_text(
        "/root-only.txt\n*.tmp\n!keep.tmp\nignored/\n!ignored/child.txt\n"
        "tree/**\n!tree/keep.txt\n\\#literal.txt\n\\!literal.txt\nspace\\ .txt\noverride.txt\n",
        encoding="utf-8",
    )
    (workspace / "sub/.gitignore").write_text("deep/*\n!deep/yes.txt\n", encoding="utf-8")
    (workspace / ".ignore").write_text("!override.txt\n", encoding="utf-8")
    (workspace / "sub/.rgignore").write_text("override.txt\n", encoding="utf-8")
    expected = {"visible.txt", "sub/root-only.txt", "keep.tmp", "sub/keep.tmp", "tree/keep.txt",
                "sub/deep/yes.txt", ".hidden/x.txt", "override.txt"}
    result = search_service.search_text("TQ_CONTENT", max_results=100)
    assert {item["path"] for item in result["results"]} == expected
    assert result["respect_gitignore"] is True
    visible = search_service.search_text("TQ_CONTENT", max_results=100, include_hidden=False)
    assert {item["path"] for item in visible["results"]} == expected - {".hidden/x.txt"}
    all_files = search_service.search_text("TQ_CONTENT", max_results=100, respect_gitignore=False, include_internal=True)
    assert {item["path"] for item in all_files["results"]} == files
    assert not search_service.search_text("TQ_CONTENT", base_path="ignored")["results"]
    assert not search_service.search_text("TQ_CONTENT", base_path=".hidden", include_hidden=False)["results"]
    if search_service.rg_executable:
        native = subprocess.run(
            [search_service.rg_executable, "--files", "--hidden", "--no-require-git", "--glob", "!**/node_modules/**"],
            cwd=workspace, capture_output=True, text=True, check=True,
        )
        observed = {line.replace("\\", "/") for line in native.stdout.splitlines()} & files
        assert observed == expected


def test_scan_truncation_keeps_all_selected_candidates(search_service, workspace):
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_bytes(b"needle\n")
    result = search_service.search_text("needle", max_scan_bytes=15, max_results=20)
    assert [item["path"] for item in result["results"]] == ["a.txt", "b.txt"]
    assert result["truncated"] is True and result["scanned_bytes"] == 14


def test_python_keeps_utf16(service, workspace):
    service.rg_executable = None
    (workspace / "utf16.txt").write_text("中文 needle\n", encoding="utf-16")
    assert service.search_text("中文")["results"][0]["context"] == "中文 needle"


def test_both_search_engines_enforce_content_deadline(search_service, workspace, monkeypatch):
    (workspace / "probe.txt").write_text("needle", encoding="utf-8")
    original = SearchCandidates.collect

    def overdue(self):
        result = original(self)
        result.started -= 121
        return result

    monkeypatch.setattr(SearchCandidates, "collect", overdue)
    with pytest.raises(TimeoutError):
        search_service.search_text("needle")


@pytest.mark.parametrize("cancelled", [False, True])
def test_search_candidate_budget_and_cancellation(service, workspace, cancelled):
    for index in range(4):
        (workspace / f"{index}.txt").write_text("needle", encoding="utf-8")
    event = threading.Event()
    selection = SearchCandidates(service.jail, workspace, "**/*", True, False, False, 1000, 1000, 2, 30, event)
    if cancelled:
        event.set()
        with pytest.raises(JobCancelled):
            selection.collect()
    else:
        assert len(selection.collect().paths) == 2 and selection.truncated


@pytest.mark.parametrize("samples, cleared, recover_at", [
    ([(10, False), (100, False)], None, None),
    ([(10, True), (25, True), (26, False), (50, True), (69, True), (70, True)], 70, None),
    ([(10, True), (30, True)], 30, None),
    ([(10, True), (30, True)], None, 30),
])
def test_supervisor_resets_only_after_continuous_ready(tmp_path, monkeypatch, samples, cleared, recover_at):
    settings = replace(SupervisorSettings.from_mapping(None), startup_grace_seconds=1,
                       stable_reset_seconds=20, restart_budget_max_attempts=1)
    config = LauncherConfig(Path("unused-client"), None, "http://127.0.0.1:8080", settings)
    index = 0
    supervisor = TunnelSupervisor(config=config, profile="test", state_path=tmp_path / "state.json",
                                  monotonic=lambda: samples[index][0])
    states = []

    class Process:
        def poll(self):
            return None

    def spawn():
        supervisor.process = Process()
        supervisor.started_at = 0.0
        return True

    def wait(_seconds):
        nonlocal index
        index += 1
        if index == len(samples):
            supervisor.stop_event.set()

    ready_calls = 0

    def ready():
        nonlocal ready_calls
        ready_calls += 1
        if samples[index][0] == recover_at:
            supervisor.recover_event.set()
        return samples[index][1]

    monkeypatch.setattr("tiancheng_mcp.tunnel_supervisor.signal.signal", lambda *_: None)
    monkeypatch.setattr(supervisor, "_spawn_with_recovery", spawn)
    monkeypatch.setattr(supervisor, "_ready", ready)
    monkeypatch.setattr(supervisor, "_stop_child", lambda: None)
    monkeypatch.setattr(supervisor.stop_event, "wait", wait)
    monkeypatch.setattr(supervisor, "_write_state", lambda state, **extra: states.append(
        (samples[min(index, len(samples)-1)][0], state, supervisor.restart_budget.count, extra)))
    assert supervisor.restart_budget.allow(0)
    assert supervisor.run() == (2 if recover_at is not None else 0)
    assert ready_calls == len(samples)
    resets = [stamp for stamp, state, count, _ in states if state == "healthy" and count == 0]
    assert resets == ([] if cleared is None else [cleared])
    assert all(extra["tunnel_ready"] is True for _, state, _, extra in states if state == "healthy")
