from __future__ import annotations

import base64
import json

import pytest

from tiancheng_mcp.execution_events import EventSink
from tiancheng_mcp.execution_paths import private_directory
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore
from tiancheng_mcp import execution_query as query_module


def populated(tmp_path, producers=6):
    root = tmp_path / 'records'
    expected = set()
    for i in range(producers):
        store = ExecutionStore(root)
        sink = EventSink(store)
        context = sink.begin_invocation('query_fixture')
        sink.emit(context, 'tool', 'started')
        sink.emit(context, 'tool', 'completed', {'count': i}, operation_outcome='succeeded')
        expected.add(context.operation_id)
        assert store.close() == 'drained', store.health()
    return root, expected


def delayed_metadata(monkeypatch):
    clock = [0.0]
    original = query_module.validate_private
    def checked(*args, **kwargs):
        clock[0] += 0.006
        return original(*args, **kwargs)
    monkeypatch.setattr(query_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(query_module, 'validate_private', checked)
    return clock


def test_metadata_budget_resumes_without_missing_or_repeating_events(tmp_path, monkeypatch):
    root, expected = populated(tmp_path)
    delayed_metadata(monkeypatch)
    query = ExecutionQuery(root)
    cursor = None
    identities = []
    operations = set()
    for _ in range(20):
        page = query.page(cursor=cursor, seconds=0.05)
        identities.extend(e['event_id'] for e in page['items'])
        operations.update(e['operation_id'] for e in page['items'] if e['operation_id'])
        assert not page['cursor_gap']
        if not page['next_cursor']:
            assert not page['scan_incomplete']
            break
        assert page['scan_incomplete']
        assert page['next_cursor'] != cursor
        cursor = page['next_cursor']
    else:
        pytest.fail('metadata cursor did not finish')
    assert operations == expected
    assert len(identities) == len(set(identities)) == 6 * 5


def test_inventory_only_timeout_has_cursor_and_cannot_claim_complete(tmp_path, monkeypatch):
    root, _ = populated(tmp_path, 1)
    clock = delayed_metadata(monkeypatch)
    query = ExecutionQuery(root)
    page = query.page(seconds=0.01)
    assert page['items'] == [] and page['scan_incomplete']
    state = json.loads(base64.urlsafe_b64decode(page['next_cursor']))
    assert state['segment'] is None and state['offset'] == 0
    clock[0] = 0
    resumed = query.page(cursor=page['next_cursor'], seconds=0.5)
    assert len(resumed['items']) == 5 and not resumed['scan_incomplete']
    with pytest.raises(ValueError, match='binding'):
        query.page({'tool': 'another'}, page['next_cursor'])


def test_status_is_paged_and_bound_separately_from_history(tmp_path, monkeypatch):
    root, _ = populated(tmp_path)
    delayed_metadata(monkeypatch)
    query = ExecutionQuery(root)
    cursor = None
    found = 0
    total = 0
    for _ in range(20):
        status = query.status(cursor=cursor, seconds=0.05)
        found += status['segment_count']
        total += status['bytes']['summary']
        assert status['counts_scope'] == 'scanned_page'
        if not status['next_cursor']:
            assert not status['scan_incomplete']
            break
        assert status['scan_incomplete']
        with pytest.raises(ValueError):
            query.page(cursor=status['next_cursor'])
        cursor = status['next_cursor']
    assert found == 6
    assert total == sum(p.stat().st_size for p in root.rglob('summary-*.jsonl'))


def test_export_refuses_unfinished_inventory_scan(tmp_path, monkeypatch):
    root, expected = populated(tmp_path, 1)
    query = ExecutionQuery(root)
    output = private_directory(tmp_path / 'export') / 'evidence.json'
    monkeypatch.setattr(query, 'page', lambda *a, **kw: {'items': [], 'quality': [],
        'scanned_bytes': 0, 'cursor_gap': False, 'scan_incomplete': True, 'next_cursor': None})
    with pytest.raises(ValueError, match='incomplete'):
        query.export(next(iter(expected)), output)
    assert not output.exists()


def test_inventory_cursor_rejects_offsets_and_foreign_store(tmp_path):
    root, _ = populated(tmp_path, 1)
    query = ExecutionQuery(root)
    producer = next((root / 'producers').iterdir())
    cursor = query._cursor(producer, 0, 0, {}, False, inventory=True)
    state = json.loads(base64.urlsafe_b64decode(cursor))
    state['offset'] = 1
    with pytest.raises(ValueError):
        query.page(cursor=base64.urlsafe_b64encode(json.dumps(state).encode()).decode())
    with pytest.raises(ValueError):
        ExecutionQuery(tmp_path / 'foreign').page(cursor=cursor)


def test_job_resource_resolves_original_operation_and_collisions_are_explicit(tmp_path):
    from dataclasses import replace
    root=tmp_path/'records'
    identities=[]
    for code in (0,7):
        store=ExecutionStore(root); sink=EventSink(store)
        context=replace(sink.begin_invocation('run_command'), resources={'job_id':'job_synthetic_collision'})
        sink.emit(context,'job','started')
        sink.emit(context,'job','completed',{'exit_code':code},operation_outcome='failed' if code else 'succeeded')
        sink.emit(sink.child(context),'process','completed',{'exit_code':code},operation_outcome='failed' if code else 'succeeded')
        assert store.close()=='drained'
        identities.append(context.operation_id)
    report=ExecutionQuery(root).show('job_synthetic_collision')
    assert report['ambiguous_matches'] and report['operation_outcome']=='unknown'
    assert {e['operation_id'] for e in report['resolved_operations']}==set(identities)
    assert {e['operation_outcome'] for e in report['resolved_operations']}=={'succeeded','failed'}
    assert ExecutionQuery(root).show(identities[1])['operation_outcome']=='failed'


def test_since_date_is_utc_and_offset_comparisons_work(tmp_path):
    root,_=populated(tmp_path,1)
    assert len(ExecutionQuery(root).page({'since':'2000-01-01'})['items'])==5
    assert len(ExecutionQuery(root).page({'since':'2000-01-01T00:00:00-10:00'})['items'])==5
    assert not ExecutionQuery(root).page({'since':'3000-01-01'})['items']
