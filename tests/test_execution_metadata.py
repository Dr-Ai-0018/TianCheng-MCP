from __future__ import annotations

import json
import time

from tiancheng_mcp.execution_events import EventSink
from tiancheng_mcp.execution_paths import FileLock
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore, StoreLimits


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def test_shared_metadata_contention_preserves_synced_events_and_recovers(tmp_path):
    store = ExecutionStore(tmp_path / 'records', limits=StoreLimits(sync_seconds=.02))
    sink = EventSink(store)
    try:
        wait_for(lambda: store.health()['synced_seq'] >= 1)
        with FileLock(store.root / 'store.lock'):
            context = sink.begin_invocation('run_command')
            assert sink.emit(context, 'tool', 'completed', {'exit_code': 0},
                             invocation_outcome='returned', operation_outcome='succeeded') == 'accepted'
            wait_for(lambda: store.health()['metadata_deferred_count'] > 0
                     and store.health()['synced_seq'] == store.health()['observed_seq'])
            health = store.health()
            assert health['metadata_pending']
            assert health['synced_seq'] == health['observed_seq']
            assert health['write_failure_count'] == health['dropped_summary'] == 0
        wait_for(lambda: not store.health()['metadata_pending'])
    finally:
        assert store.close() == 'drained'
    result = ExecutionQuery(store.root).show(context.operation_id)
    assert result['operation_outcome'] == 'succeeded' and result['record_complete']
    rows = [json.loads(line) for p in store.directory.glob('summary-*.jsonl') for line in p.read_bytes().splitlines()]
    assert [r['seq'] for r in rows] == list(range(1, store.health()['observed_seq'] + 1))
    assert all(row['phase'] != 'loss' for row in rows)
    manifest = json.loads((store.directory / 'manifest.json').read_text())
    assert manifest['closed'] and manifest['health']['metadata_deferred_count'] > 0
    assert not manifest['health']['metadata_pending']


def test_real_manifest_failure_is_visible_without_losing_committed_event(tmp_path, monkeypatch):
    store = ExecutionStore(tmp_path / 'records', limits=StoreLimits(sync_seconds=.02))
    sink = EventSink(store)
    try:
        wait_for(lambda: store.health()['synced_seq'] >= 1)
        with monkeypatch.context() as patch:
            def fail_manifest():
                raise OSError('synthetic manifest storage failure')
            patch.setattr(store, '_write_manifest', fail_manifest)
            context = sink.begin_invocation('run_command')
            assert sink.emit(context, 'tool', 'completed', {'exit_code': 7},
                             invocation_outcome='returned', operation_outcome='failed') == 'accepted'
            wait_for(lambda: store.health()['write_failure_count'] > 0)
            wait_for(lambda: store.health()['synced_seq'] == store.health()['observed_seq'])
            assert store.health()['metadata_pending']
            assert store.health()['dropped_summary'] == 0
        wait_for(lambda: not store.health()['metadata_pending'])
    finally:
        assert store.close() == 'drained'
    result = ExecutionQuery(store.root).show(context.operation_id)
    assert result['operation_outcome'] == 'failed'
    assert not result['record_complete']
    assert any(q['reason'] == 'capture_loss' for q in result['quality'])
    assert store.health()['write_failure_count'] > 0
