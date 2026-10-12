from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from tiancheng_mcp.artifacts import ArtifactStore, MAX_EXPORT_BYTES, URI_PREFIX
from tiancheng_mcp.server import create_server
from tiancheng_mcp.service import TianChengService
from test_stdio import _isolated_server_config_args


@pytest.mark.parametrize('name', ['sample.pdf', 'sample.zip', 'sample.xlsx', 'sample.png', 'sample.bin'])
def test_exports_are_original_binary_without_interpretation(tmp_path, name):
    path = tmp_path / name; data = b'\x00synthetic-only\xff\n'; path.write_bytes(data)
    store = ArtifactStore()
    key = store.export(lambda: path)['artifact_id']
    link = store.response(key)
    assert link.content[0].type == 'resource_link'
    assert str(link.content[0].uri) == URI_PREFIX + key
    assert str(tmp_path) not in str(link.content[0].uri)
    assert link.structured_content['sha256'] == hashlib.sha256(data).hexdigest()
    assert link.structured_content['download_verified'] is False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: store.read(key), range(8))) == [data] * 8


def test_export_hash_bounds_revocation_expiry_and_instance(tmp_path, monkeypatch):
    from tiancheng_mcp import artifacts
    path = tmp_path / 'sample.bin'; path.write_bytes(b'original')
    allowed = True
    def authorize():
        if not allowed: raise PermissionError('Revoked')
        return path
    store = ArtifactStore(max_items=1)
    key = store.export(authorize)['artifact_id']
    with pytest.raises(ValueError, match='full'): store.export(authorize)
    for fake in (key, '../sample.bin', 'a'*32):
        with pytest.raises(ValueError, match='Unknown'): ArtifactStore().read(fake)
    allowed = False
    with pytest.raises(PermissionError): store.read(key)
    allowed = True
    path.write_bytes(b'changed!')
    with pytest.raises(ValueError, match='changed'): store.read(key)
    path.write_bytes(b'original')
    real_clock = artifacts.time.monotonic
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.time, 'monotonic', lambda: real_clock()+901)
        with pytest.raises(ValueError, match='expired'): store.read(key)
    path.write_bytes(b'\0' * (MAX_EXPORT_BYTES + 1))
    with pytest.raises(ValueError, match='byte limit'): store.export(authorize)
    store.clear()
    path.write_bytes(b'original')
    with pytest.raises(RuntimeError, match='closed'): store.export(authorize)


def test_external_export_rechecks_grant(tmp_path):
    external = tmp_path / 'external'; external.mkdir()
    (external / 'sample.bin').write_bytes(b'authorized original')
    service = TianChengService(tmp_path / 'workspace', None, allow_external_grants=True,
                              media_config={'exports': True})
    try:
        pending = service.request_external_access(str(external), 'read')
        grant = service.approve_external_access(pending['request_id'], pending['challenge'], '批准')
        key = service.export_file('sample.bin', external=True, grant_id=grant['grant_id'])['artifact_id']
        assert service.artifacts.read(key) == b'authorized original'
        service.revoke_external_access(grant['grant_id'])
        with pytest.raises(PermissionError): service.artifacts.read(key)
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_real_stdio_export_resource_contract(workspace, tmp_path):
    raw = bytes(range(256)) * 32
    (workspace / 'sample.bin').write_bytes(raw)
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'tiancheng_mcp', '--workspace', str(workspace),
              '--media-config', '{"exports":true,"images":false}',
              *_isolated_server_config_args(tmp_path)], cwd=str(Path(__file__).resolve().parents[1]))
    async with Client(parameters, mode='legacy', raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert 'export_file' in names and 'read_image' not in names
        assert 'external_export_file' not in names
        assert not (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        assert any(str(t.uri_template) == URI_PREFIX + '{artifact_id}' for t in templates)
        result = await client.call_tool('export_file', {'path': 'sample.bin'})
        assert not result.is_error
        assert result.content[0].type == 'resource_link'
        uri = str(result.content[0].uri)
        resource = await client.read_resource(uri)
        assert base64.b64decode(resource.contents[0].blob) == raw
        assert result.structured_content['sha256'] == hashlib.sha256(raw).hexdigest()
        (workspace / 'sample.bin').write_bytes(b'changed')
        with pytest.raises(MCPError, match='source changed'):
            await client.read_resource(uri)
        with pytest.raises(MCPError, match='Unknown'):
            await client.read_resource(URI_PREFIX + 'a' * 32)
        denied = await client.call_tool('export_file', {'path': '../sample.bin'})
        assert denied.is_error
