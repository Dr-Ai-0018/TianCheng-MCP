"""F0: real SDK/stdio media wiring, independent of ChatGPT UI support."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, ImageContent


PROBE = Path(__file__).with_name('_media_probe.py')


def peer(mode: str = 'legacy') -> Client:
    return Client(StdioServerParameters(command=sys.executable, args=[str(PROBE)],
                                       cwd=str(PROBE.parent.parent), encoding='utf-8'),
                  mode=mode, raise_exceptions=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['legacy', 'auto'])
async def test_stdio_image_is_native_and_not_duplicated_in_metadata(mode: str) -> None:
    async with peer(mode) as client:
        result = await client.call_tool('probe_image', {})
        assert not result.is_error
        assert [item.type for item in result.content] == ['image']
        block = result.content[0]
        data = base64.b64decode(block.data, validate=True)
        assert data.startswith(b'\x89PNG\r\n\x1a\n')
        assert block.mime_type == 'image/png'
        assert result.structured_content == {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        wire = result.model_dump(mode='json', by_alias=True)
        assert wire['content'][0]['mimeType'] == 'image/png'
        assert 'data' not in wire['structuredContent']


@pytest.mark.asyncio
async def test_stdio_resource_link_and_embedded_return_identical_binary() -> None:
    async with peer() as client:
        assert client.protocol_version == '2025-11-25'
        assert client.server_capabilities.resources is not None
        linked = await client.call_tool('probe_link', {})
        assert not linked.is_error
        block = linked.content[0]
        assert block.type == 'resource_link'
        read = await client.read_resource(str(block.uri))
        data = base64.b64decode(read.contents[0].blob, validate=True)
        assert data == b'\x00synthetic-artifact\xff\n'
        embedded = await client.call_tool('probe_embedded', {})
        assert not embedded.is_error
        assert embedded.content[0].type == 'resource'
        assert base64.b64decode(embedded.content[0].resource.blob, validate=True) == data
        assert block.size == len(data)


@pytest.mark.asyncio
async def test_stdio_file_params_survive_schema_and_validate_required_fields() -> None:
    async with peer() as client:
        listed = await client.list_tools()
        tool = next(item for item in listed.tools if item.name == 'probe_file_reference')
        wire = tool.model_dump(mode='json', by_alias=True)
        assert wire['_meta']['openai/fileParams'] == ['file']
        schema = wire['inputSchema']
        reference = schema['$defs']['FileReference']
        assert set(reference['properties']) == {'download_url', 'file_id', 'mime_type', 'file_name'}
        assert set(reference['required']) == {'download_url', 'file_id'}
        assert all(item['type'] == 'string' for item in reference['properties'].values())
        assert 'file' in schema['required']
        marker = 'SIGNED_URL_MUST_NOT_BE_ECHOED'
        result = await client.call_tool('probe_file_reference', {'file': {
            'download_url': 'https://example.invalid/' + marker, 'file_id': 'synthetic-id'}})
        assert not result.is_error
        assert marker not in result.model_dump_json()
        assert result.structured_content['network_access'] is False
        refused = await client.call_tool('probe_file_reference', {'file': {'file_id': 'synthetic-id'}})
        assert refused.is_error


@pytest.mark.asyncio
async def test_delayed_job_result_reconstructs_native_image() -> None:
    async with peer() as client:
        started = await client.call_tool('probe_delayed_image', {})
        assert not started.is_error
        job_id = started.structured_content['job_id']
        for _ in range(30):
            result = await client.call_tool('probe_media_result', {'job_id': job_id})
            assert not result.is_error
            if result.content[0].type == 'image':
                assert result.structured_content['bytes'] > 0
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail('Synthetic job did not finish')


@pytest.mark.parametrize('raw_size', [1, 1024, 4 * 1024 * 1024])
def test_raw_base64_and_wire_budgets_are_distinct(raw_size: int) -> None:
    encoded = base64.b64encode(b'\0' * raw_size).decode('ascii')
    assert len(encoded) == 4 * ((raw_size + 2) // 3)
    result = CallToolResult(content=[ImageContent(data=encoded, mime_type='image/png')])
    # This is serialization arithmetic, not an image validity or Tunnel-limit test.
    wire_bytes = len(json.dumps(result.model_dump(mode='json', by_alias=True),
                               ensure_ascii=False).encode('utf-8'))
    assert wire_bytes > len(encoded)
    assert wire_bytes < 6 * 1024 * 1024
