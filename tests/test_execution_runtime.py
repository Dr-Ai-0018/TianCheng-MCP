from __future__ import annotations

import json
import os
import threading
import time

import pytest

from tiancheng_mcp.access_context import AccessContext
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.policy import AccessPolicy, AccessRule
from tiancheng_mcp.service import TianChengService


def captured_service(workspace, tmp_path, **kwargs):
    return TianChengService(workspace, None, observability_config={
        "mode": "detail", "storageRoot": str(tmp_path / "records")}, **kwargs)


def events(root):
    return ExecutionQuery(root).page(detail=True)["items"]


def test_real_exit7_preserves_result_and_durable_outcome(workspace, tmp_path):
    service = captured_service(workspace, tmp_path, allow_exec=True)
    try:
        result = service.run_with_fallback("run_command", ".", lambda: service.run_command(
            "python", ["-c", "import sys; print('real-output'); print('token=sk-synthetic123456'); sys.exit(7)"]))
        assert result["exit_code"] == 7
        assert "token=sk-synthetic123456" in result["stdout"]
        assert not result["timeout"]
        root = service.execution_record_root
    finally:
        service.shutdown()
    rows = events(root)
    received = next(e for e in rows if e["phase"] == "received" and e.get("tool") == "run_command")
    report = ExecutionQuery(root).show(received["operation_id"], detail=True)
    assert report["record_complete"], report["quality"]
    assert report["operation_outcome"] == "failed"
    assert any(e["kind"] == "process" and e["phase"] == "started" for e in rows)
    assert any(e["kind"] == "process" and e["result"].get("exit_code") == 7 for e in rows if e["channel"] == "summary")
    assert any(e["invocation_outcome"] == "returned" for e in rows if e["channel"] == "summary")
    raw = b"".join(p.read_bytes() for p in root.rglob("*.jsonl"))
    assert b"sk-synthetic123456" not in raw
    assert b"real-output" in raw
    assert b"import sys;" not in raw


def test_off_has_no_files_and_observer_failure_preserves_output(workspace, tmp_path):
    off_root = tmp_path / "off"
    off = TianChengService(workspace, None, observability_config={"mode": "off", "storageRoot": str(off_root)})
    try:
        assert off.workspace_info()["execution_record_health"]["mode"] == "off"
        assert not off_root.exists()
    finally:
        off.shutdown()
    broken_root = tmp_path / "file"
    broken_root.write_text("not a directory")
    service = TianChengService(workspace, None, allow_exec=True,
        observability_config={"storageRoot": str(broken_root)})
    try:
        assert not service.workspace_info()["execution_record_health"]["storage_available"]
        result = service.run_command("python", ["-c", "print('still-works')"])
        assert result["stdout"].strip() == "still-works"
        assert result["exit_code"] == 0
    finally:
        service.shutdown()


def test_denied_and_spawn_failure_do_not_claim_process_start(workspace, tmp_path, monkeypatch):
    service = captured_service(workspace, tmp_path, allow_exec=True)
    try:
        service.allow_exec = False
        with pytest.raises(PermissionError):
            service.run_command("python", ["--version"])
        service.allow_exec = True
        def fail(*args, **kwargs):
            raise OSError("synthetic spawn failure")
        monkeypatch.setattr("tiancheng_mcp.service.subprocess.Popen", fail)
        with pytest.raises(OSError, match='synthetic spawn failure'):
            service.run_command("python", ["--version"])
    finally:
        service.shutdown()
    rows = events(tmp_path / "records")
    assert not any(e["kind"] == "process" and e["phase"] == "started" for e in rows)
    assert any(e.get("operation_outcome") == "denied" for e in rows)
    assert any(e.get("operation_outcome") == "failed" for e in rows)


def test_handoff_idempotency_and_real_process_cancel(workspace, tmp_path):
    service = captured_service(workspace, tmp_path, allow_exec=True)
    service.interactive_timeout_seconds = 0.05
    calls = []
    def operation():
        calls.append(1)
        return service.run_command("python", ["-c", "import time; print('started', flush=True); time.sleep(10)"])
    try:
        first = service.run_with_fallback("run_command", ".", operation, idempotency_key="test-once")
        second = service.run_with_fallback("run_command", ".", operation, idempotency_key="test-once")
        assert first["execution"] == second["execution"] == "background"
        assert first["job_id"] == second["job_id"]
        assert len(calls) == 1
        record = service.jobs.get(first["job_id"])
        deadline = time.monotonic() + 5
        while record.started_at is None and time.monotonic() < deadline:
            time.sleep(0.01)
        service.jobs.cancel(first["job_id"])
        assert record.done.wait(8)
        assert record.state == "cancelled"
        assert "observation_context" not in service.jobs.status(first["job_id"])
    finally:
        service.shutdown()
    rows = events(tmp_path / "records")
    summary = [e for e in rows if e["channel"] == "summary"]
    reused = next(e for e in summary if e["phase"] == "reused")
    handoffs = [e for e in summary if e["phase"] == "handoff"]
    assert len(handoffs) == 2
    assert len({e["operation_id"] for e in handoffs}) == 1
    assert reused["origin_invocation_id"] != reused["invocation_id"]
    assert ExecutionQuery(tmp_path / 'records').show(reused['resources']['job_id'])['operation_outcome']=='cancelled'
    queued = next(e for e in summary if e["kind"] == "job" and e["phase"] == "queued")
    started = next(e for e in summary if e["kind"] == "job" and e["phase"] == "started")
    assert queued["seq"] < started["seq"]
    assert any(e["phase"] == "cancel_requested" for e in summary)
    assert ExecutionQuery(tmp_path / "records").show(reused["operation_id"])["operation_outcome"] == "cancelled"


def test_record_directory_protected_with_broad_static_rule_and_grant_context(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    records = outside / "records"
    service = TianChengService(workspace, None, observability_config={"storageRoot": str(records)},
        access_policy=AccessPolicy(workspace, [AccessRule(workspace, "full"), AccessRule(outside, "full")]))
    try:
        (outside / "ordinary.txt").write_text("ordinary")
        assert service.policy_external_read_text(str(outside / "ordinary.txt"))["content"] == "ordinary"
        with pytest.raises(PermissionError, match="local administration"):
            service.policy_external_list_dir(str(outside))
        scoped = service._scoped_service(AccessContext(outside, "read", lambda: service.access_policy,
            grant_authorizer=lambda path, operation: None))
        assert scoped.execution_store is service.execution_store
        with pytest.raises(PermissionError, match="local administration"):
            scoped.read_text('records/producers/' + service.execution_store.producer_id + '/manifest.json')
        scoped.shutdown()
        assert service.execution_store.health()["storage_available"]
        assert str(records) not in json.dumps(service.workspace_info()["execution_record_health"])
    finally:
        service.shutdown()


def test_invalid_explicit_storage_rejected_before_job_start(workspace, tmp_path, monkeypatch):
    def must_not_start():
        raise AssertionError("workers must not start")
    monkeypatch.setattr("tiancheng_mcp.service.JobManager", must_not_start)
    with pytest.raises(PermissionError, match="overlap"):
        TianChengService(workspace, None, observability_config={"storageRoot": str(workspace / "records")})


def test_capture_budget_preserves_original_large_output(workspace, tmp_path):
    service = captured_service(workspace, tmp_path, allow_exec=True)
    try:
        result = service.run_command("python", ["-c", "print('x' * 80000)"])
        assert result["stdout"] == 'x' * 80000 + os.linesep
        assert not result["stdout_truncated"]
        assert result["stdout_bytes_total"] == len(result['stdout'].encode())
    finally:
        service.shutdown()
    detail = [e for e in events(tmp_path / "records") if e["channel"] == "detail"]
    assert detail
    assert sum(len(e["text"].encode()) for e in detail) < 65536
    assert any(e["capture"]["payload_truncated"] for e in detail)


def test_queued_cancel_records_terminal_without_runner_start(workspace, tmp_path):
    from tiancheng_mcp.jobs import JobManager
    service = captured_service(workspace, tmp_path)
    service.jobs.shutdown()
    service.jobs = JobManager(workers=1)
    service.interactive_timeout_seconds = 0.01
    release = threading.Event()
    service.jobs.submit('blocker', lambda cancel: release.wait(5))
    calls = []
    try:
        handle = service.run_with_fallback('slow', '.', lambda: calls.append(1))
        service.jobs.cancel(handle['job_id'])
        # A queued cancellation must not claim the runner executed.
        record = service.jobs.get(handle['job_id'])
        assert record.state == 'cancelled'
        assert not calls
    finally:
        release.set()
        service.shutdown()
    rows = events(tmp_path / 'records')
    received = next(e for e in rows if e.get('tool') == 'slow' and e['phase'] == 'received')
    assert ExecutionQuery(tmp_path / 'records').show(received['operation_id'])['operation_outcome'] == 'cancelled'
    assert not any(e.get('tool') == 'slow' and e['phase'] == 'started' for e in rows)


@pytest.mark.asyncio
async def test_stdio_real_command_is_captured(workspace, tmp_path):
    import sys
    from mcp import Client, StdioServerParameters
    root = tmp_path / 'stdio-records'
    params = StdioServerParameters(command=sys.executable, args=[
        '-m', 'tiancheng_mcp', '--workspace', str(workspace), '--allow-exec',
        '--audit-dir', str(tmp_path / 'audit'), '--observability-config',
        json.dumps({'mode': 'summary', 'storageRoot': str(root)}),
        '--access-policy', str(tmp_path / 'policy.json'),
        '--agent-sources', str(tmp_path / 'sources.json'),
        '--agent-catalog', str(tmp_path / 'catalog.sqlite3'),
        '--agent-profiles', str(tmp_path / 'profiles.json'),
        '--agent-env-file', str(tmp_path / 'agent.env'),
        '--launcher-local-config', str(tmp_path / 'launcher.json'),
        '--command-policy', str(tmp_path / 'commands.json')])
    async with Client(params) as client:
        response = await client.call_tool('run_command', {'command': 'python',
            'args': ['-c', "import sys; print('stdio-exit7'); sys.exit(7)"]})
        assert not response.is_error
        assert response.structured_content['exit_code'] == 7
        assert response.structured_content['stdout'].strip() == 'stdio-exit7'
    rows = ExecutionQuery(root).page()['items']
    received = next(e for e in rows if e.get('tool') == 'run_command' and e['phase'] == 'received')
    assert ExecutionQuery(root).show(received['operation_id'])['operation_outcome'] == 'failed'


def test_detail_progress_is_visible_before_process_exit(workspace, tmp_path):
    service = captured_service(workspace, tmp_path, allow_exec=True)
    service.interactive_timeout_seconds = 0.01
    try:
        handle = service.run_with_fallback('run_command', '.', lambda: service.run_command(
            'python', ['-c', "import time; print('live-small-marker', flush=True); time.sleep(15)"]))
        record = service.jobs.get(handle['job_id'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = ExecutionQuery(tmp_path / 'records').page(detail=True)['items']
            if any(e.get('stream') == 'stdout' and 'live-small-marker' in e.get('text', '') for e in rows):
                break
            time.sleep(0.02)
        else:
            pytest.fail('small flushed output was buffered until exit')
        assert not record.done.is_set()
        service.jobs.cancel(record.job_id)
        assert record.done.wait(8)
    finally:
        service.shutdown()
