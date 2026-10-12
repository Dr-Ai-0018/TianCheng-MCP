"""Synthetic stdio peer for media contract tests; never reads host files or URLs."""
from __future__ import annotations

import base64
import hashlib
import struct
import zlib
import secrets
from typing import Any

from mcp.server import MCPServer
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, ResourceLink
from typing_extensions import NotRequired, TypedDict

from tiancheng_mcp.jobs import JobManager


def sample_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack('!I', len(payload)) + kind + payload + struct.pack('!I', zlib.crc32(kind + payload))
    colors = secrets.SystemRandom().sample([(255, 0, 0), (0, 0, 255), (255, 255, 0), (0, 180, 0)], 4)
    rows = bytearray()
    for y in range(80):
        rows.append(0)
        for x in range(80):
            rows.extend(colors[(2 if y >= 40 else 0) + (1 if x >= 40 else 0)])
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 80, 80, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows))) + chunk(b'IEND', b''))


IMAGE = sample_png()
FILE = b'\x00synthetic-artifact\xff\n'
URI = 'tiancheng-artifact://synthetic-probe'


class FileReference(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


def image_result() -> CallToolResult:
    return CallToolResult(
        content=[ImageContent(data=base64.b64encode(IMAGE).decode('ascii'), mime_type='image/png')],
        structured_content={'bytes': len(IMAGE), 'sha256': hashlib.sha256(IMAGE).hexdigest()},
    )


def create_probe() -> MCPServer:
    server = MCPServer('synthetic-media-contract-probe')
    jobs = JobManager(workers=1)

    @server.tool()
    def probe_image() -> CallToolResult:
        return image_result()

    @server.resource(URI, mime_type='application/octet-stream')
    def probe_resource() -> bytes:
        return FILE

    @server.tool()
    def probe_link() -> CallToolResult:
        return CallToolResult(content=[ResourceLink(uri=URI, name='synthetic.bin',
                                                    mime_type='application/octet-stream', size=len(FILE))])

    @server.tool()
    def probe_embedded() -> CallToolResult:
        return CallToolResult(content=[EmbeddedResource(resource=BlobResourceContents(
            uri=URI, mime_type='application/octet-stream', blob=base64.b64encode(FILE).decode('ascii')))])

    @server.tool(meta={'openai/fileParams': ['file']})
    def probe_file_reference(file: FileReference) -> dict[str, Any]:
        # Validate the host's shape only. Do not fetch or echo the signed URL.
        return {'accepted': True, 'has_file_id': bool(file['file_id']), 'network_access': False}

    @server.tool()
    def probe_delayed_image() -> dict[str, Any]:
        def operation(cancel_event):
            if cancel_event.wait(0.05):
                raise RuntimeError('Cancelled')
            return {'media_kind': 'synthetic-image'}
        record = jobs.submit('probe_delayed_image', operation)
        return {'job_id': record.job_id}

    @server.tool()
    def probe_media_result(job_id: str) -> CallToolResult:
        payload = jobs.result(job_id)
        if not payload.get('ready'):
            from mcp.types import TextContent
            return CallToolResult(content=[TextContent(text='Pending')], structured_content={'ready': False})
        if payload.get('state') != 'succeeded':
            raise RuntimeError('Synthetic media job failed')
        return image_result()

    return server


if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path)
    options = parser.parse_args()
    if options.evidence_dir is not None:
        # Explicit operator-only synthetic evidence; never supplied by MCP arguments.
        options.evidence_dir.mkdir(parents=True, exist_ok=True)
        (options.evidence_dir / 'sample.png').write_bytes(IMAGE)
        (options.evidence_dir / 'sample.bin').write_bytes(FILE)
        (options.evidence_dir / 'manifest.json').write_text(json.dumps({
            'image_sha256': hashlib.sha256(IMAGE).hexdigest(),
            'file_sha256': hashlib.sha256(FILE).hexdigest(),
        }), encoding='utf-8')
    create_probe().run(transport='stdio')
