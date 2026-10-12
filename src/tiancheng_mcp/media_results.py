"""Native MCP media envelopes, kept out of JSON-only legacy job results."""
from __future__ import annotations

import json
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from mcp.types import CallToolResult, ImageContent, TextContent
from .image_io import ImageLimits, bounded_file_read


def image_result(payload: dict, *, limits: ImageLimits = ImageLimits()) -> CallToolResult:
    metadata = payload['metadata']
    content = ([ImageContent(data=payload['data'], mime_type=metadata['mime_type'])]
               if 'data' in payload else [TextContent(text=json.dumps(metadata, separators=(',', ':')))])
    result = CallToolResult(content=content, structured_content=metadata)
    if len(result.model_dump_json(by_alias=True, exclude_none=True).encode('utf-8')) > limits.json_bytes:
        raise ValueError('Image response exceeds JSON byte limit')
    return result


class MediaStore:
    """Session-only bounded pixels; every retrieval rechecks current read rights."""
    def __init__(self, *, ttl: float = 900, max_items: int = 64, max_bytes: int = 64 * 1024 * 1024):
        self.ttl, self.max_items, self.max_bytes = ttl, max_items, max_bytes
        self._entries: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._closed = False

    def add(self, payload: dict, authorize: Callable[[], Path]) -> dict:
        size = len(json.dumps(payload).encode('utf-8'))
        with self._lock:
            if self._closed:
                raise RuntimeError('Media result cache is closed')
            now = time.monotonic()
            self._entries = {key: item for key, item in self._entries.items() if item[0] > now}
            if len(self._entries) >= self.max_items or sum(item[1] for item in self._entries.values()) + size > self.max_bytes:
                raise ValueError('Media result cache is full; retry after results expire')
            key = secrets.token_urlsafe(24)
            self._entries[key] = (now + self.ttl, size, payload, authorize)
        return {'media_id': key}

    def response(self, media_id: str) -> CallToolResult:
        with self._lock:
            item = self._entries.get(media_id)
            if item is None or item[0] <= time.monotonic():
                self._entries.pop(media_id, None)
                raise ValueError('Unknown or expired media result')
        _, _, payload, authorize = item
        path = authorize()
        current = bounded_file_read(path, ImageLimits().source_bytes)
        if hashlib.sha256(current).hexdigest() != payload['metadata']['source_sha256']:
            raise ValueError('Media source changed; read the image again')
        authorize()  # Permissions may have expired during the bounded read.
        with self._lock:
            if item[0] <= time.monotonic() or self._entries.get(media_id) is not item:
                raise ValueError('Unknown or expired media result')
        return image_result(payload)

    def clear(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
