"""Supervisor observation with real isolated children and loopback HTTP only."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from tiancheng_mcp.execution_config import CaptureSettings
from tiancheng_mcp.execution_events import safe_result
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore, StoreLimits
from tiancheng_mcp.execution_supervisor import SupervisorObserver, open_supervisor_observer
from tiancheng_mcp.tunnel_supervisor import LauncherConfig, SupervisorSettings, TunnelSupervisor


def rows(root):
    result = []
    cursor = None
    for _ in range(100):
        page = ExecutionQuery(root).page(detail=True, cursor=cursor)
        result.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            return result
    raise AssertionError('Query did not finish')


def make_supervisor(tmp_path, monkeypatch, url='http://127.0.0.1:1', **settings):
    monkeypatch.setattr('tiancheng_mcp.tunnel_supervisor.signal.signal', lambda *args: None)
    root = tmp_path / 'records'
    store = ExecutionStore(root, CaptureSettings(mode='detail'), producer_origin='supervisor')
    observer = SupervisorObserver(store, 'fixture-profile')
    supervisor = TunnelSupervisor(config=LauncherConfig(
        tunnel_client=Path(sys.executable), profile_dir=None, health_base_url=url,
        settings=SupervisorSettings.from_mapping({
            'startupGraceSeconds': 1, 'healthPollSeconds': 1,
            'restartBackoffSeconds': [0],
            'restartBudget': {'maxAttempts': 1, 'windowSeconds': 10}, **settings})),
        profile='fixture-profile', state_path=tmp_path / 'state.json', observation=observer)
    return supervisor, observer, root


@contextmanager
def readiness_server(status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/readyz'
            self.send_response(status)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_restart_readiness_stop_and_private_tunnel_output(tmp_path, monkeypatch):
    with readiness_server() as url:
        supervisor, observer, root = make_supervisor(tmp_path, monkeypatch, url)
        code = "import sys,time; print('private-tunnel-request-body', flush=True); "
        monkeypatch.setattr(supervisor, '_command', lambda: [sys.executable, '-c', code +
            ('sys.exit(7)' if supervisor.generation == 1 else 'time.sleep(30)')])
        original_ready = supervisor._ready
        def stop_after_probe():
            ready = original_ready()
            supervisor.request_stop()
            return ready
        monkeypatch.setattr(supervisor, '_ready', stop_after_probe)
        assert supervisor.run() == 0
    history = rows(root)
    assert {r['producer_id'] for r in history} == {observer.store.producer_id}
    events = [r for r in history if r['kind'] != 'producer']
    assert all(r['origin']['channel'] == 'supervisor' for r in events)
    assert all(not ({'job_id', 'run_id', 'process_id', 'native_item_id'} & r['resources'].keys())
               for r in events)
    processes = [r for r in events if r['kind'] == 'process' and r['phase'] == 'completed']
    assert len(processes) == 2
    assert processes[0]['result']['exit_code'] == 7
    assert processes[0]['operation_outcome'] == 'failed'
    assert processes[1]['operation_outcome'] == 'cancelled'
    recovery = next(r for r in events if r['phase'] == 'recovery')
    assert recovery['result']['generation'] == 1 and recovery['result']['next_generation'] == 2
    assert recovery['result']['restart_count'] == 1
    assert 'backoff_until' in recovery['result']
    ready = next(r for r in events if r['result'].get('scope') == 'tunnel_readiness')
    assert ready['result']['tunnel_ready'] and ready['evidence'] == 'observed'
    healthy = next(r for r in events if r['result'].get('state') == 'healthy')
    assert healthy['result']['mcp_transport'] == 'inferred' and healthy['evidence'] == 'inferred'
    assert events[-1]['result']['state'] == 'stopped'
    assert observer.health()['queue_depth'] == 0 and not observer.health()['shutdown_incomplete']
    assert not observer.health()['write_failure_count']
    assert all(r['channel'] == 'summary' for r in history)
    assert 'private-tunnel-request-body' not in json.dumps(history)


@pytest.mark.parametrize('status', [200, 503])
def test_readyz_actual_http_has_own_evidence_and_bypasses_proxy(tmp_path, monkeypatch, status):
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    with readiness_server(status) as url:
        supervisor, observer, root = make_supervisor(tmp_path, monkeypatch, url)
        try:
            for _ in range(3):
                assert supervisor._ready() is (status == 200)
        finally:
            observer.close()
    probes = [r for r in rows(root) if r['kind'] == 'supervisor']
    assert len(probes) == 1
    assert probes[0]['result']['health_source'] == 'readyz_http'
    assert probes[0]['result']['mcp_transport'] == 'unavailable'


def test_observer_fault_preserves_actual_child_stop_and_exit(tmp_path, monkeypatch):
    supervisor, observer, root = make_supervisor(tmp_path, monkeypatch)
    monkeypatch.setattr(supervisor, '_command', lambda: [sys.executable, '-c', 'import time; time.sleep(30)'])
    def broken(*args):
        raise OSError('private-observer-error')
    monkeypatch.setattr(observer.sink, 'emit', broken)
    original_spawn = supervisor._spawn
    def spawn_then_stop():
        original_spawn()
        supervisor.request_stop()
    monkeypatch.setattr(supervisor, '_spawn', spawn_then_stop)
    assert supervisor.run() == 0
    assert supervisor.process is None and supervisor.kill_job is None
    assert observer.health()['write_failure_count'] > 0
    assert observer.health()['queue_depth'] == 0
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['state'] == 'stopped' and state['execution_records']['write_failure_count'] > 0


def test_dedupe_and_failure_classification_do_not_copy_raw_line(tmp_path, monkeypatch):
    supervisor, observer, root = make_supervisor(tmp_path, monkeypatch)
    try:
        supervisor.generation = 3
        for _ in range(50):
            supervisor._write_state('degraded', tunnel_ready=False)
        supervisor._tee(io.StringIO('failure_source=client_internal upstream_response_received=false '
            'request_id=private-native-id token=private-key\n'), io.StringIO(), 3)
        assert supervisor.recover_event.is_set()
        supervisor.last_recovery_reason = supervisor.failure_reason
        supervisor._append_recovery_event(reason=supervisor.failure_reason, backoff_until=None)
    finally:
        observer.close()
    history = rows(root)
    assert len([r for r in history if r['result'].get('scope') == 'supervisor_state']) == 1
    assert next(r for r in history if r['phase'] == 'recovery')['result']['reason_code'] == 'client_internal_without_upstream'
    assert 'private-native-id' not in json.dumps(history) and 'private-key' not in json.dumps(history)


def write_config(tmp_path, capture):
    defaults = tmp_path / 'defaults.json'
    local = tmp_path / 'local.json'
    defaults.write_text(json.dumps({'tunnelClient': sys.executable,
        'observability': {'mode': 'detail', 'summaryMaxMiB': 32},
        'supervisor': {'restartBackoffSeconds': [0],
            'restartBudget': {'maxAttempts': 1, 'windowSeconds': 10}}}), encoding='utf-8')
    local.write_text(json.dumps({'observability': capture}), encoding='utf-8')
    return defaults, local


def test_off_invalid_configuration_and_unsafe_overlap_do_not_start_capture(tmp_path, capsys):
    root = tmp_path / 'records'
    defaults, local = write_config(tmp_path, {'mode': 'off', 'storageRoot': str(root)})
    assert open_supervisor_observer(defaults, local, 'fixture') is None
    assert not root.exists()
    local.write_text(json.dumps({'observability': {'mode': 'private-invalid-config'}}))
    assert open_supervisor_observer(defaults, local, 'fixture') is None
    assert 'private-invalid-config' not in capsys.readouterr().err
    local.write_text(json.dumps({'workspace': str(root), 'observability': {'storageRoot': str(root)}}))
    assert open_supervisor_observer(defaults, local, 'fixture') is None
    assert not root.exists()


def test_independent_cli_producer_shares_record_root_and_closes_after_budget_failure(tmp_path):
    root = tmp_path / 'records'
    defaults, local = write_config(tmp_path, {'storageRoot': str(root)})
    parent = open_supervisor_observer(defaults, local, 'parent-fixture')
    assert parent is not None and parent.store.settings.summary_max_mib == 32
    try:
        result = subprocess.run([sys.executable, '-m', 'tiancheng_mcp.tunnel_supervisor',
            '--defaults', str(defaults), '--local-config', str(local), '--profile', 'cli-fixture',
            '--state-dir', str(tmp_path / 'state')], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=20, shell=False)
        # Python acts as the fake Tunnel executable and exits because "run" is not a script.
        assert result.returncode == 2, result.stderr
    finally:
        parent.close()
    history = rows(root)
    producers = {r['producer_id'] for r in history}
    assert len(producers) == 2
    assert all(json.loads(p.read_text())['closed'] for p in root.glob('producers/*/manifest.json'))
    assert all(not json.loads(p.read_text())['health']['shutdown_incomplete']
               for p in root.glob('producers/*/manifest.json'))
    assert all(r['origin']['channel'] == 'supervisor' for r in history)
    cli = [r for r in history if r['producer_id'] != parent.store.producer_id]
    assert next(r for r in cli if r['result'].get('scope') == 'supervisor_lifecycle' and
                r['phase'] == 'completed')['operation_outcome'] == 'failed'
    assert any(r['result'].get('reason_code') == 'restart_budget_exhausted' for r in cli)
    assert not list((tmp_path / 'state').glob('*.lock'))


def test_backoff_time_projection_accepts_only_bounded_utc_timestamp():
    assert safe_result({'backoff_until': '2026-10-15T12:34:56+00:00'})['backoff_until'].endswith('+00:00')
    for value in ('private-payload', '2026-10-15', '2026-10-15T12:34:56+08:00', 'x' * 100):
        assert 'backoff_until' not in safe_result({'backoff_until': value})


def test_spawn_failure_does_not_invent_child_started(tmp_path, monkeypatch):
    supervisor, observer, root = make_supervisor(tmp_path, monkeypatch)
    def fail_spawn(*args, **kwargs):
        raise OSError('private-spawn-error-body')
    monkeypatch.setattr('tiancheng_mcp.tunnel_supervisor.subprocess.Popen', fail_spawn)
    assert supervisor.run() == 2
    history = rows(root)
    assert not [r for r in history if r['kind'] == 'process']
    assert any(r['result'].get('reason_code') == 'tunnel_client_spawn_failed' for r in history)
    assert 'private-spawn-error-body' not in json.dumps(history)


def test_check_does_not_open_capture(tmp_path, capsys):
    from tiancheng_mcp.tunnel_supervisor import main
    root = tmp_path / 'records'
    defaults, local = write_config(tmp_path, {'storageRoot': str(root)})
    assert main(['--defaults', str(defaults), '--local-config', str(local),
                 '--profile', 'check-fixture', '--check']) == 0
    assert json.loads(capsys.readouterr().out)['valid']
    assert not root.exists()


def test_incomplete_close_is_reported_without_claiming_drain(tmp_path, monkeypatch, capsys):
    supervisor, observer, root = make_supervisor(tmp_path, monkeypatch)
    original_close = observer.store.close
    try:
        monkeypatch.setattr(observer.store, 'close', lambda: 'incomplete')
        assert observer.close() == 'incomplete'
        assert 'shutdown incomplete' in capsys.readouterr().err
    finally:
        original_close()


def test_other_process_cannot_bypass_shared_quota_or_evict_active_producer(tmp_path):
    root = tmp_path / 'records'
    limits = StoreLimits(segment_bytes=1500, summary_bytes=2000)
    parent = ExecutionStore(root, limits=limits, producer_origin='supervisor')
    try:
        deadline = time.monotonic() + 5
        while parent.health()['synced_seq'] < 1:
            assert time.monotonic() < deadline, parent.health()
            time.sleep(.02)
        original = {p: p.read_bytes() for p in parent.directory.glob('summary-*.jsonl')}
        code = '''
import json,sys,time
from pathlib import Path
from tiancheng_mcp.execution_store import ExecutionStore,StoreLimits
store=ExecutionStore(Path(sys.argv[1]), limits=StoreLimits(segment_bytes=1500,summary_bytes=2000),
                     producer_origin='supervisor')
deadline=time.monotonic()+5
while not store.health()['write_failure_count'] and time.monotonic()<deadline: time.sleep(.02)
store.close()
print(json.dumps({'original_business_result':42,'health':store.health()}))
'''
        result = subprocess.run([sys.executable, '-c', code, str(root)], capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=15, shell=False)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload['original_business_result'] == 42
        assert payload['health']['write_failure_count'] > 0
        assert all(p.read_bytes() == content for p, content in original.items())
        assert sum(p.stat().st_size for p in root.glob('producers/*/summary-*.jsonl')) <= 2000
    finally:
        parent.close()
