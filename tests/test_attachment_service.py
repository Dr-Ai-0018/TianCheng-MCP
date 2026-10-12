from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path
import pytest
from mcp import Client, StdioServerParameters
from tiancheng_mcp import service as service_module
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp.security import WorkspaceSecurityError
from test_stdio import _isolated_server_config_args

FILE = {'download_url': 'https://files.example.com/data?signature=synthetic-secret',
        'file_id': 'synthetic-file', 'file_name': '../../unsafe.exe', 'mime_type': 'application/x-executable'}
MEDIA = {'attachments': {'mode': 'direct', 'hosts': ['files.example.com']}}
PROXY_MEDIA = {'attachments': {'mode': 'clash_proxy', 'hosts': ['files.example.com'],
                               'proxy_url': 'http://127.0.0.1:7897'}}


def enabled(workspace, tmp_path):
    return TianChengService(workspace, tmp_path / 'audit', media_config=MEDIA)


def test_default_disabled_without_network(service, monkeypatch):
    monkeypatch.setattr(service_module, 'download_attachment', lambda *a, **k: pytest.fail('network'))
    with pytest.raises(PermissionError, match='disabled'): service.import_attachment(FILE)


def test_original_bytes_random_name_and_no_overwrite(workspace, tmp_path, monkeypatch):
    raw = b'PK\0synthetic binary\xff'
    calls = []
    monkeypatch.setattr(service_module, 'download_attachment', lambda *a, **k: calls.append(a) or raw)
    instance = enabled(workspace, tmp_path)
    try:
        result = instance.import_attachment(FILE)
        assert result['path'].startswith('upload-') and result['path'].endswith('.bin')
        assert (workspace / result['path']).read_bytes() == raw
        assert result['sha256'] == hashlib.sha256(raw).hexdigest()
        assert result['atomic_no_overwrite'] is True
        with pytest.raises(FileExistsError): instance.import_attachment(FILE, result['path'])
        assert len(calls) == 1
        for bad in ('../bad', str(tmp_path / 'bad'), 'file:stream'):
            with pytest.raises(WorkspaceSecurityError): instance.import_attachment(FILE, bad)
        assert len(calls) == 1 and not list(workspace.glob('.tiancheng-upload-*'))
    finally: instance.shutdown()


def test_concurrent_destination_and_disk_failure_cleanup(workspace, tmp_path, monkeypatch):
    instance = enabled(workspace, tmp_path)
    def raced(*args, **kwargs):
        (workspace / 'existing.bin').write_bytes(b'concurrent writer')
        return b'attachment'
    monkeypatch.setattr(service_module, 'download_attachment', raced)
    try:
        with pytest.raises(FileExistsError): instance.import_attachment(FILE, 'existing.bin')
        assert (workspace / 'existing.bin').read_bytes() == b'concurrent writer'
        monkeypatch.setattr(service_module, 'download_attachment', lambda *a, **k: b'attachment')
        def full(*args): raise OSError('Synthetic disk full')
        monkeypatch.setattr(service_module.os, 'fsync', full)
        with pytest.raises(OSError, match='disk full'): instance.import_attachment(FILE, 'new.bin')
        assert not (workspace / 'new.bin').exists()
        assert not list(workspace.glob('.tiancheng-upload-*'))
    finally: instance.shutdown()


@pytest.mark.asyncio
async def test_real_stdio_file_ref_and_default_gate(workspace, tmp_path):
    for settings in ({}, MEDIA, PROXY_MEDIA):
        parameters = StdioServerParameters(command=sys.executable,
            args=['-m', 'tiancheng_mcp', '--workspace', str(workspace),
                  '--media-config', json.dumps(settings), *_isolated_server_config_args(tmp_path)],
            cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(parameters, mode='legacy', raise_exceptions=False) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            if not settings:
                assert 'import_attachment' not in tools
                continue
            tool = tools['import_attachment']
            wire = tool.model_dump(by_alias=True, exclude_none=True)
            assert wire['_meta']['openai/fileParams'] == ['file']
            schema = wire['inputSchema']['$defs']['FileReference']
            assert set(schema['properties']) == {'download_url', 'file_id', 'mime_type', 'file_name'}
            assert set(schema['required']) == {'download_url', 'file_id'}
            # Reject before spawning any network worker; signed source must not
            # appear in protocol errors, audit, or job metadata/results.
            rejected = await client.call_tool('import_attachment', {'file': {
                **FILE, 'download_url': 'https://localhost/data?signature=synthetic-secret'},
                'idempotency_key': 'synthetic-test'})
            assert rejected.is_error
            assert 'synthetic-secret' not in rejected.model_dump_json()
            invalid = await client.call_tool('import_attachment', {'file': {
                'download_url': FILE['download_url']}, 'idempotency_key': 'missing-file-id'})
            assert invalid.is_error
            assert 'synthetic-secret' not in invalid.model_dump_json()


@pytest.mark.parametrize('settings,expected_proxy', [(MEDIA, None), (PROXY_MEDIA, 'http://127.0.0.1:7897')])
def test_import_uses_only_instance_selected_transport(workspace, tmp_path, monkeypatch, settings, expected_proxy):
    calls = []
    monkeypatch.setattr(service_module, 'download_attachment', lambda *a, **k: calls.append(k) or b'original')
    instance = TianChengService(workspace, tmp_path / 'audit', media_config=settings)
    try:
        assert instance.workspace_info()['capabilities']['attachment_imports']
        receipt = instance.import_attachment(FILE, 'selected.bin')
        assert calls[0]['proxy_url'] == expected_proxy
        assert (workspace / receipt['path']).read_bytes() == b'original'
        assert 'proxy_url' not in receipt and 'synthetic-secret' not in json.dumps(receipt)
    finally: instance.shutdown()


def test_retry_receipt_is_idempotent_and_audit_excludes_source(workspace, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service_module, 'download_attachment', lambda *a, **k: calls.append(True) or b'original')
    instance = enabled(workspace, tmp_path)
    try:
        def run():
            return instance.run_with_fallback('import_attachment', 'sample.bin',
                lambda: instance.import_attachment(FILE, 'sample.bin'),
                idempotency_key='retry-synthetic', idempotency_fingerprint='same-file-and-destination')
        first = run()
        assert run() == first
        assert len(calls) == 1
        assert 'synthetic-secret' not in instance.audit.path.read_text(encoding='utf-8')
        event = json.loads(instance.audit.path.read_text(encoding='utf-8').splitlines()[-1])
        assert event['media']['source_sha256'] == hashlib.sha256(b'original').hexdigest()
        assert 'synthetic-secret' not in json.dumps(instance.job_list())
        with pytest.raises(ValueError, match='different operation'):
            instance.run_with_fallback('import_attachment', 'other.bin', lambda: {},
                idempotency_key='retry-synthetic', idempotency_fingerprint='different-file')
    finally: instance.shutdown()


def test_cancellation_before_commit_leaves_no_file(workspace, tmp_path, monkeypatch):
    from tiancheng_mcp.jobs import JobCancelled
    instance = enabled(workspace, tmp_path)
    checks = []
    def cancel_at_commit():
        checks.append(True)
        if len(checks) >= 3: raise JobCancelled('Synthetic cancellation')
    monkeypatch.setattr(instance, '_check_cancelled', cancel_at_commit)
    try:
        with pytest.raises(JobCancelled): instance._commit_attachment('cancelled.bin', b'original')
        assert not (workspace / 'cancelled.bin').exists()
        assert not list(workspace.glob('.tiancheng-upload-*'))
    finally: instance.shutdown()
