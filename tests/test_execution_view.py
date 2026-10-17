from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tiancheng_mcp.execution_config import CaptureSettings
from tiancheng_mcp.execution_events import EventSink
from tiancheng_mcp.execution_paths import private_directory
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore
from tiancheng_mcp.execution_view import report_lines, terminal_text
from tiancheng_mcp.observability_admin import main

ROOT = Path(__file__).resolve().parents[1]


def history(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = ExecutionStore(tmp_path / 'records', CaptureSettings(mode='detail'))
    sink = EventSink(store)
    context = sink.begin_invocation('run_command')
    sink.emit(context, 'process', 'started')
    sink.detail(context, 'stdout', '中文🙂\x1b[31m safe\u202e\napi_key=synthetic-secret\n')
    sink.emit(context, 'process', 'completed', {'exit_code': 7}, operation_outcome='failed')
    sink.finish_detail(context)
    assert store.close() == 'drained'
    config = tmp_path / 'local.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'python': sys.executable,
        'envFile': str(tmp_path / 'absent.env'),
        'observability': {'mode': 'summary', 'storageRoot': str(store.root)}}), encoding='utf-8')
    return store.root, context.operation_id, config


def test_readable_view_preserves_failed_result_and_sanitizes_terminal(tmp_path):
    root, operation, _ = history(tmp_path)
    report = ExecutionQuery(root).show(operation, detail=True)
    text = '\n'.join(report_lines('show', report))
    assert '操作结果 failed' in text and 'exit_code' in text and '7' in text
    assert '中文🙂' in text and 'synthetic-secret' not in text
    assert '\x1b' not in text and '\u202e' not in text
    assert terminal_text('a\r\x00\u202eb') == 'a   b'
    report.update(record_complete=False, cursor_gap=True, scan_incomplete=True, ambiguous_matches=True)
    text = '\n'.join(report_lines('show', report))
    assert '记录完整 False' in text and '多重匹配 True' in text
    assert '缺口' in text and '扫描尚未完成' in text


def test_export_preview_matches_written_snapshot_and_does_not_create_files(tmp_path):
    root, operation, _ = history(tmp_path)
    query = ExecutionQuery(root)
    before = set(tmp_path.rglob('*'))
    preview = query.preview_export(operation, detail=True)
    assert set(tmp_path.rglob('*')) == before
    output = private_directory(tmp_path / 'exports') / 'evidence.json'
    receipt = query.export(operation, output, detail=True)
    assert {k:v for k,v in preview.items() if k != 'preview'} == receipt
    data = json.loads(output.read_bytes())
    assert data['product_version'] and data['operation'] == operation
    assert b'synthetic-secret' not in output.read_bytes()
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        query.export(operation, output)
    assert output.read_bytes() == original


@pytest.mark.parametrize('flag', ['dropped_detail', 'shutdown_incomplete'])
def test_view_and_export_cannot_hide_detail_loss_or_incomplete_shutdown(tmp_path, flag):
    root, operation, _ = history(tmp_path)
    manifest = next(root.glob('producers/*/manifest.json'))
    data = json.loads(manifest.read_bytes())
    data['health'][flag] = True if flag == 'shutdown_incomplete' else 1
    manifest.write_text(json.dumps(data), encoding='utf-8')
    report = ExecutionQuery(root).show(operation, detail=True)
    assert not report['record_complete']
    assert any(q['reason'] == 'capture_loss' for q in report['quality'])
    preview = ExecutionQuery(root).preview_export(operation, detail=True)
    assert any(q['reason'] == 'capture_loss' for q in preview['quality'])


def test_cli_json_text_and_menu_view_share_query_facts(tmp_path, capsys):
    _, operation, config = history(tmp_path)
    base = ['--project-root', str(ROOT), '--local', str(config)]
    main(base + ['status'])
    status = json.loads(capsys.readouterr().out)
    assert status['capture_config']['scope'] == 'next_start'
    assert status['capture_config']['active_mode'] == 'unknown'
    main(base + ['show', operation, '--detail', '--view'])
    report = json.loads(capsys.readouterr().out)
    assert report['operation_outcome'] == 'failed'
    assert '操作结果 failed' in '\n'.join(report['display_lines'])
    main(base + ['show', operation, '--text'])
    assert '操作结果 failed' in capsys.readouterr().out
    main(base + ['export', operation, '--preview'])
    assert json.loads(capsys.readouterr().out)['preview'] is True


def test_follow_interrupt_only_ends_reader_and_retains_business_writer(tmp_path, monkeypatch, capsys):
    from tiancheng_mcp import observability_admin
    root, operation, config = history(tmp_path)
    live = ExecutionStore(root)
    sink = EventSink(live)
    context = sink.begin_invocation('still_running')
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(observability_admin.time, 'sleep', interrupt)
    main(['--project-root', str(ROOT), '--local', str(config), 'follow', operation, '--text'])
    assert '操作 failed' in capsys.readouterr().out
    assert sink.emit(context, 'tool', 'completed', operation_outcome='succeeded') == 'accepted'
    assert live.close() == 'drained'
    assert ExecutionQuery(root).show(context.operation_id)['operation_outcome'] == 'succeeded'


@pytest.mark.skipif(os.name != 'nt', reason='Windows records menu')
def test_readable_cli_overrides_windows_ascii_pipe_encoding(tmp_path):
    root, operation, config = history(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'tiancheng_mcp.observability_admin',
        '--project-root', str(ROOT), '--local', str(config), '--storage-root', str(root),
        'show', operation, '--detail', '--text'], env=dict(os.environ, PYTHONIOENCODING='ascii'),
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert '中文🙂' in result.stdout and '操作结果 failed' in result.stdout
    assert '\x1b' not in result.stdout and '\u202e' not in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows records menu')
def test_records_menu_reads_history_and_export_cancel_is_read_only(tmp_path):
    _, operation, config = history(tmp_path)
    before = config.read_bytes()
    command = [shutil.which('pwsh'), '-NoLogo', '-NoProfile', '-File', str(ROOT/'tc.ps1'),
               '-Action', 'records', '-ConfigPath', str(config), '-NoPause']
    status = subprocess.run(command + ['-Json'], capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)['capture_config']['active_mode'] == 'unknown'
    result = subprocess.run(command, input=f'2\n4\n{operation}\n\n7\n{operation}\n\nCANCEL\n0\n',
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '操作结果 failed' in result.stdout and '尚未写文件' in result.stdout
    assert 'synthetic-secret' not in result.stdout
    assert config.read_bytes() == before
    assert not list(tmp_path.rglob('evidence.json'))
