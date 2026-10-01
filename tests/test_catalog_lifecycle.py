from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from mcp import Client, StdioServerParameters

from tiancheng_mcp import agent_catalog, agent_admin, catalog_storage
from tiancheng_mcp.agent_catalog import AgentCatalog
from tiancheng_mcp.agent_sources import AgentSourcePolicy
from tiancheng_mcp.catalog_storage import CatalogStorageError, catalog_lock, move_database_group


@pytest.fixture
def setup_catalog(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    root = tmp_path / '.codex' / 'sessions'
    root.mkdir(parents=True)
    config = tmp_path / 'sources.json'
    payload = {'schema_version': 1, 'sources': [{'source_id': 'src_budget',
        'provider': 'codex', 'root': str(root), 'mode': 'catalog-read',
        'max_scan_bytes': 1024}]}
    database = tmp_path / 'state' / 'catalog.sqlite3'
    def configure(**limits):
        payload['sources'][0].update(limits)
        config.write_text(json.dumps(payload), encoding='utf-8')
        return AgentSourcePolicy.load(config)
    def write(relative, identifier):
        path = root / '2026' / '09' / '30' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        header = json.dumps({'type': 'session_meta', 'payload': {'id': identifier, 'cwd': str(workspace)}})
        path.write_bytes(header.encode() + b'\n' + b' ' * (788 - len(header.encode())) + b'\n')
        assert path.stat().st_size == 790
        return path
    return SimpleNamespace(workspace=workspace, root=root, database=database,
        config=config, configure=configure, write=write)


def test_byte_budget_retries_deferred_until_complete(setup_catalog):
    f = setup_catalog
    for i in range(3):
        f.write(f'rollout-{i}.jsonl', f'session_{i}')
    policy = f.configure()
    results = []
    for _ in range(3):
        catalog = AgentCatalog(f.database, f.workspace)
        results.append(catalog.refresh(policy, 'src_budget'))
        assert results[-1]['scanned_bytes'] <= 1024
    assert [r['state'] for r in results] == ['partial', 'partial', 'complete']
    assert results[-1]['retryable_files'] == 0
    assert catalog.list_records(policy)['count'] == 3


@pytest.mark.asyncio
async def test_safe_stdio_catalog_budget_progress_and_boundaries(setup_catalog):
    f = setup_catalog
    for i in range(3):
        f.write(f'rollout-{i}.jsonl', f'session_{i}')
    f.configure()
    parameters = StdioServerParameters(command=sys.executable, args=[
        '-m', 'tiancheng_mcp', '--workspace', str(f.workspace), '--agent-sources', str(f.config),
        '--agent-catalog', str(f.database), '--access-policy', str(f.config.parent / 'no-policy.json'),
        '--agent-profiles', str(f.config.parent / 'no-profiles.json'),
        '--agent-env-file', str(f.config.parent / 'no-agent.env'),
        '--launcher-local-config', str(f.config.parent / 'no-launcher.json'),
        '--audit-dir', str(f.config.parent / 'audit')],
        cwd=str(Path(__file__).resolve().parents[1]), encoding='utf-8')
    async with Client(parameters, mode='legacy', raise_exceptions=False) as client:
        info = await client.call_tool('workspace_info', {})
        assert not info.structured_content['command_execution_enabled']
        results = []
        for _ in range(3):
            result = await client.call_tool('agent_catalog', {'action': 'refresh', 'source_id': 'src_budget'})
            assert not result.is_error
            results.append(result.structured_content)
        assert [r['state'] for r in results] == ['partial', 'partial', 'complete']
        listed = await client.call_tool('agent_catalog', {'action': 'list'})
        assert listed.structured_content['count'] == 3
        sources = await client.call_tool('agent_catalog', {'action': 'sources'})
        source = sources.structured_content['sources'][0]
        assert 'root' not in source and source['last_refresh']['retryable_files'] == 0
        denied = await client.call_tool('read_text', {'path': str(f.root / '2026/09/30/rollout-0.jsonl')})
        assert denied.is_error


def test_file_budget_checkpoint_survives_instances_and_directory_order(setup_catalog):
    f = setup_catalog
    for i, relative in enumerate(['rollout-root.jsonl', 'a/rollout-1.jsonl',
            'a/nested/rollout-2.jsonl', 'z/rollout-3.jsonl']):
        f.write(relative, f'session_{i}')
    policy = f.configure(max_files=1)
    results = [AgentCatalog(f.database, f.workspace).refresh(policy, 'src_budget') for _ in range(4)]
    assert all(r['scanned_files'] <= 1 for r in results)
    assert [r['state'] for r in results] == ['partial'] * 3 + ['complete']
    catalog = AgentCatalog(f.database, f.workspace)
    assert catalog.list_records(policy)['count'] == 4
    # A later partial cycle must preserve rows outside that round's prefix.
    assert catalog.refresh(policy, 'src_budget')['removed_files'] == 0
    assert catalog.list_records(policy)['count'] == 4


def test_transient_prefix_error_does_not_starve_later_files(setup_catalog, monkeypatch):
    f = setup_catalog
    failing = f.write('rollout-a.jsonl', 'session_a')
    f.write('rollout-b.jsonl', 'session_b')
    policy = f.configure()
    actual = agent_catalog._read_metadata_bytes
    def read(path, size):
        if path == failing:
            raise PermissionError('synthetic temporary read failure')
        return actual(path, size)
    with monkeypatch.context() as patch:
        patch.setattr(agent_catalog, '_read_metadata_bytes', read)
        catalog = AgentCatalog(f.database, f.workspace)
        first = catalog.refresh(policy, 'src_budget')
        second = catalog.refresh(policy, 'src_budget')
        assert first['state'] == second['state'] == 'partial'
        assert [x['native_session_id'] for x in catalog.list_records(policy)['conversations']] == ['session_b']
        assert second['retryable_files'] == 1
    recovered = catalog.refresh(policy, 'src_budget')
    assert recovered['state'] == 'complete' and recovered['parsed_files'] == 1
    assert catalog.list_records(policy)['count'] == 2


def test_single_file_larger_than_scan_budget_is_explicit_terminal_skip(setup_catalog):
    f = setup_catalog
    path = f.write('rollout-big.jsonl', 'session_big')
    path.write_bytes(path.read_bytes() + b' ' * 500)
    policy = f.configure(max_file_bytes=2048)
    catalog = AgentCatalog(f.database, f.workspace)
    result = catalog.refresh(policy, 'src_budget')
    assert result['state'] == 'complete' and result['skipped_files'] == 1
    assert catalog.source_summaries(policy)[0]['record_status_counts'] == {'scan-oversized': 1}
    policy = f.configure(max_scan_bytes=2048)
    assert catalog.refresh(policy, 'src_budget')['parsed_files'] == 1


def test_deleted_checkpoint_and_full_cycle_remove_only_stale_rows(setup_catalog):
    f = setup_catalog
    first = f.write('rollout-a.jsonl', 'session_a')
    f.write('rollout-b.jsonl', 'session_b')
    policy = f.configure(max_files=1)
    catalog = AgentCatalog(f.database, f.workspace)
    assert catalog.refresh(policy, 'src_budget')['state'] == 'partial'
    first.unlink()
    assert catalog.refresh(policy, 'src_budget')['state'] == 'complete'
    # Deleted behind the cursor was seen earlier in that cycle, then is pruned
    # by the next complete observation, without deleting surviving suffix rows.
    result = catalog.refresh(policy, 'src_budget')
    assert result['removed_files'] == 1
    assert catalog.list_records(policy)['count'] == 1


def test_scan_cancel_rolls_back_checkpoint(setup_catalog):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    f.write('rollout-b.jsonl', 'session_b')
    policy = f.configure(max_files=1)
    catalog = AgentCatalog(f.database, f.workspace)
    catalog.refresh(policy, 'src_budget')
    with sqlite3.connect(f.database) as connection:
        before = connection.execute('SELECT scan_cursor FROM catalog_refreshes').fetchone()[0]
    cancelled = threading.Event()
    cancelled.set()
    from tiancheng_mcp.jobs import JobCancelled
    with pytest.raises(JobCancelled):
        catalog.refresh(policy, 'src_budget', cancel_event=cancelled)
    with sqlite3.connect(f.database) as connection:
        assert connection.execute('SELECT scan_cursor FROM catalog_refreshes').fetchone()[0] == before
    assert catalog.refresh(policy, 'src_budget')['state'] == 'complete'


def test_cancel_after_last_read_does_not_commit_rows_or_progress(setup_catalog, monkeypatch):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    cancelled = threading.Event()
    actual = agent_catalog._read_metadata_bytes
    def read(*args):
        result = actual(*args)
        cancelled.set()
        return result
    catalog = AgentCatalog(f.database, f.workspace)
    monkeypatch.setattr(agent_catalog, '_read_metadata_bytes', read)
    from tiancheng_mcp.jobs import JobCancelled
    with pytest.raises(JobCancelled):
        catalog.refresh(policy, 'src_budget', cancel_event=cancelled)
    assert catalog.list_records(policy)['count'] == 0
    assert catalog.source_summaries(policy)[0]['last_refresh'] is None


def test_active_writing_retries_without_metadata_change(setup_catalog, monkeypatch):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    catalog = AgentCatalog(f.database, f.workspace)
    with monkeypatch.context() as patch:
        def changed(*_):
            raise agent_catalog.AgentCatalogError('file_changed_during_metadata_read')
        patch.setattr(agent_catalog, '_read_metadata_bytes', changed)
        first = catalog.refresh(policy, 'src_budget')
        assert first['scan_complete'] and first['state'] == 'partial' and first['retryable_files'] == 1
    assert catalog.refresh(policy, 'src_budget')['state'] == 'complete'
    assert catalog.list_records(policy)['count'] == 1


def test_limit_change_resets_progress_and_oversized_cache(setup_catalog):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    f.write('rollout-b.jsonl', 'session_b')
    policy = f.configure(max_files=1)
    catalog = AgentCatalog(f.database, f.workspace)
    assert catalog.refresh(policy, 'src_budget')['state'] == 'partial'
    policy = f.configure(max_files=10, max_scan_bytes=2048)
    result = catalog.refresh(policy, 'src_budget')
    assert result['state'] == 'complete' and result['scanned_files'] == 2


def test_schema_two_rows_survive_progress_migration(setup_catalog):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    catalog = AgentCatalog(f.database, f.workspace)
    catalog.refresh(policy, 'src_budget')
    with sqlite3.connect(f.database) as connection:
        connection.execute('ALTER TABLE catalog_files DROP COLUMN scan_generation')
        for name in ['scan_generation', 'scan_cursor', 'scan_fingerprint', 'cycle_incomplete', 'scan_complete', 'retryable_files']:
            connection.execute(f'ALTER TABLE catalog_refreshes DROP COLUMN {name}')
        connection.execute('PRAGMA user_version = 2')
    assert catalog.list_records(policy)['count'] == 1
    result = catalog.refresh(policy, 'src_budget')
    assert result['state'] == 'complete' and result['unchanged_files'] == 1


def test_group_collision_preflight_preserves_database_and_sidecars(tmp_path, monkeypatch):
    database = tmp_path / 'catalog.sqlite3'
    group = catalog_storage.database_group(database)
    for path in group:
        path.write_bytes(path.name.encode())
    monkeypatch.setattr(catalog_storage.uuid, 'uuid4', lambda: SimpleNamespace(hex='fixed'))
    class FixedClock:
        @staticmethod
        def now(_):
            return SimpleNamespace(strftime=lambda _: 'fixed-time')
    monkeypatch.setattr(catalog_storage, 'datetime', FixedClock)
    collision = Path(str(group[1]) + '.backup-fixed-time-fixed')
    collision.write_bytes(b'old-backup')
    with pytest.raises(CatalogStorageError, match='already exists'):
        move_database_group(database, 'backup')
    assert all(path.read_bytes() == path.name.encode() for path in group)
    assert collision.read_bytes() == b'old-backup'
    assert not Path(str(database) + '.backup-fixed-time-fixed').exists()


@pytest.mark.parametrize('fault', ['rename-oserror', 'rename-valueerror', 'refresh', 'install'])
def test_rebuild_failure_preserves_queryable_old_catalog(setup_catalog, monkeypatch, fault):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    catalog = AgentCatalog(f.database, f.workspace)
    catalog.refresh(policy, 'src_budget')
    reference = catalog.list_records(policy)['conversations'][0]['conversation_ref']
    actual_replace = Path.replace
    def replace(path, destination):
        if fault.startswith('rename-') and path == f.database:
            exception = OSError if fault == 'rename-oserror' else ValueError
            raise exception('synthetic rename failure')
        if fault == 'install' and '.rebuild-' in path.name and Path(destination) == f.database:
            raise OSError('synthetic install failure')
        return actual_replace(path, destination)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', replace)
        if fault == 'refresh':
            actual_refresh = AgentCatalog.refresh
            def refresh(self, *args, **kwargs):
                if '.rebuild-' in self.database_path.name:
                    raise RuntimeError('synthetic refresh failure')
                return actual_refresh(self, *args, **kwargs)
            patch.setattr(AgentCatalog, 'refresh', refresh)
        with pytest.raises((OSError, ValueError, RuntimeError), match='synthetic'):
            agent_admin.rebuild_catalog(f.config, f.database, f.workspace)
    assert catalog.inspect_record(policy, reference)['native_session_id'] == 'session_a'
    assert not list(f.database.parent.glob('*.rebuild-*'))


@pytest.mark.parametrize('exception', [OSError, ValueError])
@pytest.mark.parametrize('label', ['backup', 'corrupt'])
def test_group_mid_move_rolls_back_all_sidecars(tmp_path, monkeypatch, exception, label):
    database = tmp_path / 'catalog.sqlite3'
    group = catalog_storage.database_group(database)
    for path in group:
        path.write_bytes(path.name.encode())
    actual = Path.replace
    def replace(path, destination):
        if path == group[1]:
            raise exception('synthetic second move failure')
        return actual(path, destination)
    monkeypatch.setattr(Path, 'replace', replace)
    with pytest.raises(exception):
        move_database_group(database, label)
    assert all(path.read_bytes() == path.name.encode() for path in group)
    assert not list(tmp_path.glob(f'*.{label}-*'))


def test_failed_rollback_preserves_recovery_backup(tmp_path, monkeypatch):
    database = tmp_path / 'catalog.sqlite3'
    database.write_bytes(b'original-database')
    wal = Path(str(database) + '-wal')
    wal.write_bytes(b'original-wal')
    actual = Path.replace
    def replace(path, destination):
        if path == wal or '.backup-' in path.name:
            raise OSError('synthetic unavailable filesystem')
        return actual(path, destination)
    monkeypatch.setattr(Path, 'replace', replace)
    with pytest.raises(CatalogStorageError, match='rollback incomplete'):
        move_database_group(database, 'backup')
    backups = list(tmp_path.glob('catalog.sqlite3.backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == b'original-database'
    assert wal.read_bytes() == b'original-wal'


def test_rebuild_same_second_keeps_distinct_backups(setup_catalog):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    catalog = AgentCatalog(f.database, f.workspace)
    catalog.refresh(policy, 'src_budget')
    first = agent_admin.rebuild_catalog(f.config, f.database, f.workspace)
    second = agent_admin.rebuild_catalog(f.config, f.database, f.workspace)
    assert set(first['backup_files']).isdisjoint(second['backup_files'])
    assert all(Path(path).exists() for path in [*first['backup_files'], *second['backup_files']])
    assert catalog.list_records(policy)['count'] == 1


def test_rebuild_temp_cleanup_failure_reports_committed_result(setup_catalog, monkeypatch):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    AgentCatalog(f.database, f.workspace).refresh(policy, 'src_budget')
    actual = Path.unlink
    def unlink(path, *args, **kwargs):
        if '.rebuild-' in path.name and path.name.endswith('.lock'):
            raise PermissionError('synthetic cleanup failure')
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    result = agent_admin.rebuild_catalog(f.config, f.database, f.workspace)
    assert result['rebuilt'] and len(result['cleanup_pending']) == 1
    assert Path(result['cleanup_pending'][0]).exists()
    assert AgentCatalog(f.database, f.workspace).list_records(policy)['count'] == 1


def test_rebuild_refuses_active_external_sqlite_writer(setup_catalog):
    f = setup_catalog
    f.write('rollout-a.jsonl', 'session_a')
    policy = f.configure()
    catalog = AgentCatalog(f.database, f.workspace)
    catalog.refresh(policy, 'src_budget')
    with closing(sqlite3.connect(f.database)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(CatalogStorageError, match='busy|active'):
            agent_admin.rebuild_catalog(f.config, f.database, f.workspace)
        connection.rollback()
    assert catalog.list_records(policy)['count'] == 1
    assert not list(f.database.parent.glob('*.backup-*'))


def test_shared_maintenance_lock_blocks_other_instances_and_processes(setup_catalog):
    f = setup_catalog
    policy = f.configure()
    first = AgentCatalog(f.database, f.workspace)
    second = AgentCatalog(f.database, f.workspace)
    assert first._lock is second._lock
    entered = threading.Event()
    done = threading.Event()
    def refresh():
        entered.set()
        second.refresh(policy, 'src_budget')
        done.set()
    with catalog_lock(f.database):
        thread = threading.Thread(target=refresh)
        thread.start()
        assert entered.wait(3)
        assert not done.wait(0.05)
    thread.join(3)
    assert done.is_set()
    # Synthetic child performs actual OS lock acquisition; read the handshake
    # before checking that it cannot cross the parent's held lock.
    code = "from pathlib import Path;from tiancheng_mcp.catalog_storage import catalog_lock;import sys;print('ready',flush=True)\nwith catalog_lock(Path(sys.argv[1])): print('entered',flush=True)"
    with catalog_lock(f.database):
        process = subprocess.Popen([sys.executable, '-c', code, str(f.database)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        assert process.stdout.readline().strip() == 'ready'
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0 and stdout.strip() == 'entered', stderr
