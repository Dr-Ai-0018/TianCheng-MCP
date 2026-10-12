from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image
from mcp import Client, StdioServerParameters
from mcp.types import ImageContent
from tiancheng_mcp.media_results import MediaStore
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp.security import WorkspaceSecurityError
from test_stdio import _isolated_server_config_args


def picture(path):
    Image.new('RGB', (32, 16), 'red').save(path)
    return path.read_bytes()


def test_workspace_media_rechecks_source_and_jail(service, workspace):
    path = workspace / 'sample.png'; original = picture(path)
    handle = service.read_image('sample.png', 'original')
    result = service.media.response(handle['media_id'])
    assert base64.b64decode(result.content[0].data) == original
    for bad in ('../sample.png', str(path)):
        with pytest.raises(WorkspaceSecurityError): service.read_image(bad)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'): service.media.response(handle['media_id'])


def test_image_audit_contains_only_safe_metadata(service, workspace):
    raw = picture(workspace / 'sample.png')
    service.audited('read_image', 'sample.png', lambda: service.read_image('sample.png'))
    lines = service.audit.path.read_text(encoding='utf-8')
    media = json.loads(lines.splitlines()[-1])['media']
    assert media['source_bytes'] == len(raw)
    assert len(media['source_sha256']) == 64
    assert base64.b64encode(raw).decode() not in lines


def test_external_grant_is_checked_on_delayed_retrieval(tmp_path):
    external = tmp_path / 'external'; external.mkdir(); picture(external / 'sample.png')
    service = TianChengService(tmp_path / 'workspace', tmp_path / 'audit', allow_external_grants=True)
    try:
        pending = service.request_external_access(str(external), 'read')
        grant = service.approve_external_access(pending['request_id'], pending['challenge'], '批准')
        handle = service.read_image('sample.png', external=True, grant_id=grant['grant_id'])
        assert isinstance(service.media.response(handle['media_id']).content[0], ImageContent)
        service.revoke_external_access(grant['grant_id'])
        with pytest.raises(PermissionError): service.media.response(handle['media_id'])
    finally: service.shutdown()


def test_static_external_policy_and_disabled_gate(tmp_path):
    external = tmp_path / 'external'; external.mkdir(); path = external / 'sample.png'; picture(path)
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'rules':[{'path':str(tmp_path/'workspace'),'mode':'full'},
                                         {'path':str(external),'mode':'read'}]}))
    service = TianChengService(tmp_path / 'workspace', None, access_policy_path=policy, allow_external_grants=True)
    try:
        handle = service.read_image(str(path), external=True)
        assert service.media.response(handle['media_id']).structured_content['source_width'] == 32
        service.access_policy = type(service.access_policy).default(service.jail.root)
        with pytest.raises(PermissionError): service.media.response(handle['media_id'])
        service.external_grants.enabled = False
        with pytest.raises(PermissionError, match='disabled'): service.read_image(str(path), external=True)
    finally: service.shutdown()


def test_media_cache_bounds_expiry_and_session_isolation(service, workspace, monkeypatch):
    picture(workspace / 'sample.png')
    service.media = MediaStore(ttl=900, max_items=1)
    handle = service.read_image('sample.png')
    with pytest.raises(ValueError, match='full'): service.read_image('sample.png')
    with pytest.raises(ValueError, match='Unknown'): MediaStore().response(handle['media_id'])
    from tiancheng_mcp import media_results
    real_clock = time.monotonic
    with monkeypatch.context() as patch:
        patch.setattr(media_results.time, 'monotonic', lambda: real_clock() + 901)
        with pytest.raises(ValueError, match='expired'): service.media.response(handle['media_id'])
    service.media = MediaStore(max_bytes=1)
    with pytest.raises(ValueError, match='full'): service.read_image('sample.png')
    service.media.clear()
    with pytest.raises(RuntimeError, match='closed'): service.read_image('sample.png')


def test_background_pixels_do_not_go_through_old_json_result(service, workspace, monkeypatch):
    from tiancheng_mcp import service as module
    picture(workspace / 'sample.png')
    original_decode = module.decode_image
    def slow(*args, **kwargs):
        time.sleep(1.2)
        return original_decode(*args, **kwargs)
    monkeypatch.setattr(module, 'decode_image', slow)
    service.interactive_timeout_seconds = 1
    pending = service.run_with_fallback('read_image', 'sample.png', lambda: service.read_image('sample.png'))
    assert 'job_id' in pending
    job_id = pending['job_id']
    for _ in range(100):
        if service.job_status(job_id)['state'] == 'succeeded': break
        time.sleep(0.05)
    legacy = service.job_result(job_id)
    assert 'media_id' in legacy['result']
    assert 'data' not in json.dumps(legacy)
    assert isinstance(service.media_result(job_id).content[0], ImageContent)


@pytest.mark.asyncio
async def test_real_safe_stdio_native_image(workspace: Path, tmp_path: Path):
    original = picture(workspace / 'sample.png')
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m','tiancheng_mcp','--workspace',str(workspace),'--audit-dir',str(tmp_path/'audit'),
              *_isolated_server_config_args(tmp_path)], cwd=str(Path(__file__).resolve().parents[1]))
    async with Client(parameters, mode='legacy', raise_exceptions=False) as client:
        tools = (await client.list_tools()).tools
        assert 'read_image' in {tool.name for tool in tools}
        assert 'external_read_image' not in {tool.name for tool in tools}
        result = await client.call_tool('read_image', {'path':'sample.png','mode':'original'})
        assert not result.is_error
        assert isinstance(result.content[0], ImageContent)
        assert base64.b64decode(result.content[0].data) == original
        denied = await client.call_tool('read_image', {'path':'../sample.png'})
        assert denied.is_error
        assert not any(isinstance(item, ImageContent) for item in denied.content)
