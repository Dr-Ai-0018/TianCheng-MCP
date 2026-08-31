from __future__ import annotations

import time
import threading
from pathlib import Path

import pytest

from tiancheng_mcp.jobs import JobCancelled, JobManager, current_cancel_event
from tiancheng_mcp.service import TianChengService


def test_job_manager_waits_for_fast_work() -> None:
    manager = JobManager(workers=1)
    try:
        record, completed, result = manager.submit_and_wait(
            "fast", lambda _cancel: {"ok": True}, interactive_timeout=1
        )
        assert completed is True
        assert result == {"ok": True}
        assert manager.status(record.job_id)["state"] == "succeeded"
    finally:
        manager.shutdown()


def test_job_manager_returns_handle_before_slow_work_finishes() -> None:
    manager = JobManager(workers=1)
    try:
        record, completed, response = manager.submit_and_wait(
            "slow",
            lambda _cancel: (time.sleep(0.25), {"done": True})[1],
            interactive_timeout=0.03,
        )
        assert completed is False
        assert response["execution"] == "background"
        assert response["job_id"] == record.job_id
        assert manager.status(record.job_id)["state"] in {"queued", "running"}
        assert record.done.wait(2)
        result = manager.result(record.job_id)
        assert result["ready"] is True
        assert result["result"] == {"done": True}
    finally:
        manager.shutdown()


def test_job_cancel_cooperative_runner() -> None:
    manager = JobManager(workers=1)
    try:
        def runner(cancel):
            while not cancel.wait(0.01):
                pass
            raise JobCancelled("stopped")

        record, completed, response = manager.submit_and_wait(
            "cancellable", runner, interactive_timeout=0.03
        )
        assert completed is False
        assert response["job_id"] == record.job_id
        cancelled = manager.cancel(record.job_id, "test")
        assert cancelled["accepted"] is True
        assert record.done.wait(2)
        assert manager.status(record.job_id)["state"] == "cancelled"
    finally:
        manager.shutdown()


def test_job_runner_receives_context_cancellation_token() -> None:
    manager = JobManager(workers=1)
    try:
        seen: list[bool] = []

        def runner(_cancel):
            event = current_cancel_event()
            seen.append(event is not None)
            raise JobCancelled("stop")

        record = manager.submit("context", runner)
        assert record.done.wait(2)
        assert seen == [True]
        assert manager.status(record.job_id)["state"] == "cancelled"
    finally:
        manager.shutdown()


def test_idempotency_key_reuses_single_job_and_rejects_conflict() -> None:
    manager = JobManager(workers=1)
    try:
        first = manager.submit(
            "write_text", lambda _cancel: {"ok": True},
            idempotency_key="op-1", idempotency_fingerprint="fingerprint-a",
        )
        second = manager.submit(
            "write_text", lambda _cancel: {"unexpected": True},
            idempotency_key="op-1", idempotency_fingerprint="fingerprint-a",
        )
        assert second is first
        assert first.done.wait(2)
        with pytest.raises(ValueError, match="different operation"):
            manager.submit(
                "write_text", lambda _cancel: None,
                idempotency_key="op-1", idempotency_fingerprint="fingerprint-b",
            )
    finally:
        manager.shutdown()


def test_shutdown_finishes_queued_jobs_and_waits_for_workers() -> None:
    manager = JobManager(workers=1)
    started = threading.Event()
    try:
        running = manager.submit("running", lambda cancel: (started.set(), cancel.wait(5))[1])
        queued = manager.submit("queued", lambda _cancel: None)
        assert started.wait(2)
        manager.shutdown(wait_seconds=2)
        assert queued.done.is_set()
        assert manager.status(queued.job_id)["state"] == "expired"
        assert running.done.is_set()
    finally:
        manager.shutdown()


def test_service_auto_fallback_exposes_job_tools(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        interactive_timeout_seconds=1,
    )
    try:
        response = service.run_with_fallback(
            "synthetic_slow",
            ".",
            lambda: (time.sleep(1.2), {"done": True})[1],
        )
        assert response["execution"] == "background"
        job_id = response["job_id"]
        assert service.job_status(job_id)["state"] in {"queued", "running"}
        assert service.jobs is not None
        assert service.jobs.get(job_id).done.wait(3)
        result = service.job_result(job_id)
        assert result["ready"] is True
        assert result["result"] == {"done": True}
        audit_text = (tmp_path / "audit" / "tiancheng-mcp-audit.jsonl").read_text(
            encoding="utf-8"
        )
        assert f'"job_id":"{job_id}"' in audit_text
        assert '"state":"succeeded"' in audit_text

        record = service.jobs.submit("list_result", lambda _cancel: [0, 1, 2])
        assert record.done.wait(2)
        page = service.job_result(record.job_id, cursor=1, max_items=1)
        assert page["result"] == [1]
        assert page["next_cursor"] == 2
    finally:
        service.shutdown()


def test_run_command_job_cancel_terminates_child(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        interactive_timeout_seconds=1,
    )
    try:
        response = service.run_with_fallback(
            "run_command",
            ".",
            lambda: service.run_command(
                "python", ["-c", "import time; time.sleep(60)"], timeout_seconds=120
            ),
        )
        assert response["execution"] == "background"
        job_id = str(response["job_id"])
        service.job_cancel(job_id, "test child cancellation")
        assert service.jobs is not None
        assert service.jobs.get(job_id).done.wait(10)
        assert service.job_status(job_id)["state"] == "cancelled"
    finally:
        service.shutdown()


def test_default_interactive_budget_and_lightweight_recovery(workspace: Path, tmp_path: Path) -> None:
    service = TianChengService(workspace, tmp_path / "audit")
    try:
        assert service.interactive_timeout_seconds == 75
        (workspace / "probe.txt").write_text("ok", encoding="utf-8")
        assert service.jobs is not None
        blockers = [
            service.jobs.submit(
                "blocker", lambda cancel: (cancel.wait(5), None)[1]
            )
            for _ in range(4)
        ]
        # stat is intentionally inline and must remain responsive while every
        # background worker is occupied.
        started = time.monotonic()
        result = service.run_with_fallback("stat", "probe.txt", lambda: service.stat("probe.txt"))
        elapsed = time.monotonic() - started
        assert result["type"] == "file"
        assert elapsed < 1
        for record in blockers:
            service.jobs.cancel(record.job_id, "cleanup")
    finally:
        service.shutdown()


def test_finished_records_are_reclaimed_instead_of_bricking_the_queue() -> None:
    manager = JobManager(workers=2, max_records=8, retention_seconds=3600)
    try:
        for index in range(40):
            record, completed, _ = manager.submit_and_wait(
                "poll", lambda _cancel: {"index": index}, interactive_timeout=5
            )
            assert completed is True, f"submission {index} did not finish"
        # Retention is an hour, so nothing expired; capacity must be reclaimed
        # from the oldest finished records rather than refusing new work.
        assert len(manager._records) <= 8
        record, completed, _ = manager.submit_and_wait(
            "after-overflow", lambda _cancel: {"ok": True}, interactive_timeout=5
        )
        assert completed is True
    finally:
        manager.shutdown()


def test_capacity_reclaim_never_evicts_unfinished_records() -> None:
    # Two workers: one stays blocked for the whole test, the other drains the
    # fast churn that overflows the record table.
    manager = JobManager(workers=2, max_records=4, retention_seconds=3600)
    try:
        blocker = manager.submit("blocker", lambda cancel: (cancel.wait(30), None)[1])
        for _ in range(10):
            manager.submit_and_wait(
                "poll", lambda _cancel: {"ok": True}, interactive_timeout=5
            )
        # The unfinished record must still be addressable after the churn.
        assert manager.status(blocker.job_id)["state"] in {"queued", "running"}
        manager.cancel(blocker.job_id, "cleanup")
    finally:
        manager.shutdown()


def test_polling_surfaces_do_not_consume_job_records(
    workspace: Path, tmp_path: Path
) -> None:
    service = TianChengService(workspace, tmp_path / "audit")
    try:
        assert service.jobs is not None
        before = len(service.jobs._records)
        for tool in (
            "process_status",
            "process_output",
            "list_processes",
            "agent_run_events",
            "agent_run_inspect",
            "agent_run_result",
            "agent_run_cancel",
            "agent_session_list",
            "agent_session_inspect",
            "agent_session_close",
        ):
            service.run_with_fallback(tool, "<agent-run>", lambda: {"polled": True})
        assert len(service.jobs._records) == before
    finally:
        service.shutdown()
