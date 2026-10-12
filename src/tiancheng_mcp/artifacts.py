"""Opaque, instance-local exports. Links never replace current read authority."""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from mcp.types import BlobResourceContents, CallToolResult, ReadResourceResult, ResourceLink
from .image_io import bounded_file_read

URI_PREFIX = 'tiancheng-artifact://file/'
MAX_EXPORT_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_JSON_BYTES = 6 * 1024 * 1024
MIME_TYPE = 'application/octet-stream'


@dataclass(frozen=True)
class Artifact:
    expires: float
    name: str
    size: int
    sha256: str
    authorize: Callable[[], Path]


class ArtifactStore:
    def __init__(self, *, ttl: float = 900, max_items: int = 64):
        self.ttl, self.max_items = ttl, max_items
        self._entries: dict[str, Artifact] = {}
        self._lock = threading.Lock()
        self._closed = False

    def export(self, authorize: Callable[[], Path]) -> dict:
        path = authorize()
        data = bounded_file_read(path, MAX_EXPORT_BYTES)
        if authorize() != path:
            raise ValueError('Export source changed')
        entry = Artifact(time.monotonic() + self.ttl, path.name, len(data), hashlib.sha256(data).hexdigest(), authorize)
        with self._lock:
            if self._closed:
                raise RuntimeError('Export registry is closed')
            now = time.monotonic()
            self._entries = {key: value for key, value in self._entries.items() if value.expires > now}
            if len(self._entries) >= self.max_items:
                raise ValueError('Export registry is full; retry after handles expire')
            key = secrets.token_urlsafe(24)
            self._entries[key] = entry
        return {'artifact_id': key, 'media': {'source_bytes': entry.size, 'source_sha256': entry.sha256}}

    def _entry(self, artifact_id: str) -> Artifact:
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', artifact_id):
            raise ValueError('Unknown or expired export')
        with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None or entry.expires <= time.monotonic():
                self._entries.pop(artifact_id, None)
                raise ValueError('Unknown or expired export')
            return entry

    def read(self, artifact_id: str) -> bytes:
        entry = self._entry(artifact_id)
        path = entry.authorize()
        data = bounded_file_read(path, MAX_EXPORT_BYTES)
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError('Export source changed; export the file again')
        if entry.authorize() != path or self._entry(artifact_id) is not entry:
            raise ValueError('Export source changed or handle expired')
        # Check the complete resource shape, not just raw bytes/base64 length.
        wire = ReadResourceResult(contents=[BlobResourceContents(uri=URI_PREFIX + artifact_id,
            mime_type=MIME_TYPE, blob=base64.b64encode(data).decode('ascii'))])
        if len(wire.model_dump_json(by_alias=True, exclude_none=True).encode('utf-8')) > MAX_RESOURCE_JSON_BYTES:
            raise ValueError('Export response exceeds JSON byte limit')
        return data

    def response(self, artifact_id: str) -> CallToolResult:
        self.read(artifact_id)  # Revalidate authorization even for a delayed link.
        entry = self._entry(artifact_id)
        return CallToolResult(content=[ResourceLink(uri=URI_PREFIX + artifact_id, name=entry.name,
            mime_type=MIME_TYPE, size=entry.size)], structured_content={
                'bytes': entry.size, 'sha256': entry.sha256, 'file_name': entry.name,
                'expires_in_seconds': max(0, int(entry.expires - time.monotonic())),
                'scope': 'service-instance', 'download_verified': False,
            })

    def clear(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
