"""Bounded static image decoding in a disposable, cancellable worker process.

Callers must resolve and authorize the path through their jail first. The worker
receives bytes, never a host path, and cannot select additional files or URLs.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .jobs import JobCancelled

ImageMode = Literal['preview', 'original', 'metadata']
_FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}
_SUFFIXES = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}
_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class ImageLimits:
    source_bytes: int = 32 * 1024 * 1024
    pixels: int = 24_000_000
    preview_bytes: int = 1024 * 1024
    original_bytes: int = 4 * 1024 * 1024
    json_bytes: int = 6 * 1024 * 1024
    edge: int = 1600
    seconds: float = 30

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise ValueError(f'Invalid image limit: {key}')
            if key != 'seconds' and not isinstance(value, int):
                raise ValueError(f'Image limit must be an integer: {key}')


def bounded_file_read(path: Path, maximum: int) -> bytes:
    """Read one regular file, refusing links and changes to its identity/size."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Media source must be a regular file without reparse points')
    if before.st_size > maximum:
        raise ValueError('Media source exceeds byte limit')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError('Media source changed while opening')
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum:
        raise ValueError('Media source exceeds byte limit')
    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or len(data) != opened.st_size:
        raise ValueError('Media source changed while reading')
    return data


def _decode(data: bytes, mode: ImageMode, suffix: str, limits: ImageLimits) -> dict:
    # Imported only inside the worker. Restrict plugins before Image.open.
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = limits.pixels
    if suffix not in _SUFFIXES:
        raise ValueError('Supported extensions are PNG, JPEG and WebP')
    with Image.open(io.BytesIO(data), formats=list(_FORMATS)) as source:
        if source.format != _SUFFIXES[suffix]:
            raise ValueError('Image format does not match its extension')
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('Animated images are not supported')
        width, height = source.size
        if width * height > limits.pixels:
            raise ValueError('Image exceeds pixel limit')
        source.load()  # metadata mode also validates complete decoding
        metadata = {'source_bytes': len(data), 'source_sha256': hashlib.sha256(data).hexdigest(),
                    'source_width': width, 'source_height': height, 'source_mime_type': _FORMATS[source.format],
                    'mode': mode}
        if mode == 'metadata':
            return {'metadata': metadata}
        if mode == 'original':
            if len(data) > limits.original_bytes:
                raise ValueError('Original image exceeds output limit; use preview')
            output, mime = data, _FORMATS[source.format]
            out_size = source.size
        else:
            frame = ImageOps.exif_transpose(source)
            frame.thumbnail((limits.edge, limits.edge), Image.Resampling.LANCZOS)
            # Keep transparency; omit EXIF/ICC and other source metadata.
            transparent = frame.mode in ('RGBA', 'LA') or 'transparency' in frame.info
            frame = frame.convert('RGBA' if transparent else 'RGB')
            for _ in range(12):
                buffer = io.BytesIO()
                frame.save(buffer, format='PNG' if transparent else 'JPEG', **({} if transparent else {'quality': 85}))
                output = buffer.getvalue()
                if len(output) <= limits.preview_bytes:
                    break
                if max(frame.size) <= 1:
                    raise ValueError('Preview cannot fit output limit')
                frame = frame.resize((max(1, frame.width * 3 // 4), max(1, frame.height * 3 // 4)), Image.Resampling.LANCZOS)
            else:
                raise ValueError('Preview cannot fit output limit')
            mime = 'image/png' if transparent else 'image/jpeg'
            out_size = frame.size
        metadata.update({'returned_bytes': len(output), 'returned_sha256': hashlib.sha256(output).hexdigest(),
                         'mime_type': mime, 'width': out_size[0], 'height': out_size[1],
                         'transformed': output != data})
        return {'metadata': metadata, 'data': base64.b64encode(output).decode('ascii')}


def read_image(path: Path, mode: ImageMode = 'preview', *, limits: ImageLimits = ImageLimits(),
               cancel: threading.Event | None = None) -> dict:
    if mode not in ('preview', 'original', 'metadata'):
        raise ValueError('Image mode must be preview, original or metadata')
    deadline = time.monotonic() + limits.seconds
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise JobCancelled('Image read cancelled')
        if time.monotonic() >= deadline:
            raise TimeoutError('Image processing deadline exceeded')
    while not _SLOTS.acquire(timeout=0.05):
        check()
    child = None
    transfer = None
    try:
        check()
        data = bounded_file_read(path, limits.source_bytes)
        check()
        child = subprocess.Popen([sys.executable, '-I', '-m', 'tiancheng_mcp.image_io', '--worker', mode,
                                  path.suffix.lower(), json.dumps(asdict(limits))],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={name: value for name, value in os.environ.items()
                                      if name.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}},
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        completed = threading.Event()
        output = b''
        communication_error = None

        def communicate() -> None:
            nonlocal output, communication_error
            try:
                # One owner sends all input and drains output. On POSIX,
                # retrying communicate(input=None) after a short timeout can
                # leave partially written stdin permanently unregistered.
                output, _ = child.communicate(input=data)
            except BaseException as exc:
                communication_error = exc
            finally:
                completed.set()

        transfer = threading.Thread(target=communicate, name='tiancheng-image-io')
        transfer.start()
        while not completed.wait(timeout=0.05):
            check()
        check()
        transfer.join()
        if communication_error is not None:
            raise communication_error
        if len(output) > limits.json_bytes:
            raise ValueError('Image response exceeds JSON byte limit')
        if child.returncode != 0:
            raise ValueError('Image worker failed to decode the source')
        result = json.loads(output)
        if 'error' in result:
            raise ValueError(result['error'])
        return result
    finally:
        try:
            if child is not None:
                if child.poll() is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass  # The worker exited between poll and kill.
                if transfer is not None and transfer.ident is not None:
                    # Killing the worker unblocks input/output. Never start a
                    # second communicate call concurrently with its owner.
                    transfer.join()
                child.wait()
                for stream in (child.stdin, child.stdout):
                    if stream is not None:
                        stream.close()
        finally:
            _SLOTS.release()


def _worker() -> None:
    limits = ImageLimits(**json.loads(sys.argv[4]))
    try:
        data = sys.stdin.buffer.read(limits.source_bytes + 1)
        if len(data) > limits.source_bytes:
            raise ValueError('Image source exceeds byte limit')
        result = _decode(data, sys.argv[2], sys.argv[3], limits)
    except Exception as exc:
        # File paths, codec traceback and source bytes never reach protocol logs.
        result = {'error': str(exc)[:200] if isinstance(exc, ValueError) else 'Invalid or unsupported image'}
    output = json.dumps(result, separators=(',', ':')).encode('utf-8')
    if len(output) > limits.json_bytes:
        output = b'{"error":"Image response exceeds JSON byte limit"}'
    sys.stdout.buffer.write(output)


if __name__ == '__main__' and len(sys.argv) == 5 and sys.argv[1] == '--worker':
    _worker()
