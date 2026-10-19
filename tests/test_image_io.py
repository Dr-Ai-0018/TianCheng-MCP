from __future__ import annotations

import base64
import io
import os
import threading
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from PIL import Image
from mcp.types import ImageContent
from tiancheng_mcp.image_io import ImageLimits, bounded_file_read, read_image
from tiancheng_mcp.jobs import JobCancelled
from tiancheng_mcp.media_results import image_result


def sample(path: Path, fmt='PNG', size=(80, 40), mode='RGB', **kwargs) -> bytes:
    Image.new(mode, size, 'red').save(path, format=fmt, **kwargs)
    return path.read_bytes()


@pytest.mark.parametrize('suffix,fmt', [('.png', 'PNG'), ('.jpg', 'JPEG'), ('.webp', 'WEBP')])
def test_original_and_native_envelope(tmp_path, suffix, fmt):
    path = tmp_path / ('sample' + suffix)
    original = sample(path, fmt)
    payload = read_image(path, 'original')
    result = image_result(payload)
    assert isinstance(result.content[0], ImageContent)
    assert base64.b64decode(result.content[0].data) == original
    assert 'data' not in result.structured_content
    assert result.structured_content['transformed'] is False


def test_preview_orientation_transparency_and_no_upscale(tmp_path):
    path = tmp_path / 'alpha.png'
    sample(path, mode='RGBA')
    payload = read_image(path)
    with Image.open(io.BytesIO(base64.b64decode(payload['data']))) as result:
        assert result.size == (80, 40)
        assert result.mode == 'RGBA'
    path = tmp_path / 'rotated.jpg'
    exif = Image.Exif(); exif[274] = 6
    sample(path, 'JPEG', size=(40, 20), exif=exif)
    payload = read_image(path)
    assert (payload['metadata']['width'], payload['metadata']['height']) == (20, 40)
    with Image.open(io.BytesIO(base64.b64decode(payload['data']))) as result:
        assert not result.getexif()


def test_metadata_decodes_without_pixels_and_preview_is_bounded(tmp_path):
    path = tmp_path / 'sample.png'; sample(path, size=(2400, 1800))
    metadata = read_image(path, 'metadata')
    assert 'data' not in metadata
    payload = read_image(path, limits=replace(ImageLimits(), edge=200, preview_bytes=1000))
    assert payload['metadata']['returned_bytes'] <= 1000
    assert payload['metadata']['width'] <= 200


def test_limits_and_bad_images(tmp_path):
    path = tmp_path / 'sample.png'; original = sample(path)
    with pytest.raises(ValueError, match='byte limit'):
        read_image(path, limits=replace(ImageLimits(), source_bytes=len(original)-1))
    with pytest.raises(ValueError, match='pixel limit'):
        read_image(path, limits=replace(ImageLimits(), pixels=3000))
    with pytest.raises(ValueError, match='output limit'):
        read_image(path, 'original', limits=replace(ImageLimits(), original_bytes=1))
    with pytest.raises(ValueError, match='JSON byte'):
        image_result(read_image(path), limits=replace(ImageLimits(), json_bytes=10))
    path.write_bytes(original[:30])
    with pytest.raises(ValueError): read_image(path)
    path = tmp_path / 'fake.jpg'; path.write_bytes(original)
    with pytest.raises(ValueError, match='extension'): read_image(path)
    path = tmp_path / 'animated.png'
    Image.new('RGB', (8, 8), 'red').save(path, save_all=True, append_images=[Image.new('RGB',(8,8),'blue')])
    with pytest.raises(ValueError, match='Animated'): read_image(path)


def test_cancel_timeout_and_slot_recovery(tmp_path):
    path = tmp_path / 'sample.png'; sample(path)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(JobCancelled): read_image(path, cancel=cancel)
    with pytest.raises(TimeoutError): read_image(path, limits=replace(ImageLimits(), seconds=0.001))
    # A cancelled/terminated worker must free its slot and pipes.
    assert read_image(path, 'metadata')['metadata']['source_width'] == 80


def test_non_regular_source_and_mode(tmp_path):
    with pytest.raises(ValueError, match='regular file'): bounded_file_read(tmp_path, 100)
    with pytest.raises(ValueError, match='mode'): read_image(tmp_path, 'wrong')
    with pytest.raises(ValueError): ImageLimits(pixels=True)


@pytest.mark.parametrize('cancel_worker', [False, True])
def test_running_worker_is_terminated_and_reaped(tmp_path, monkeypatch, cancel_worker):
    from tiancheng_mcp import image_io
    path = tmp_path / 'sample.png'; sample(path)
    actual_popen = subprocess.Popen
    children = []
    monkeypatch.setenv('CONTROL_PLANE_API_KEY', 'secret-test-sentinel')
    monkeypatch.setenv('EXAMPLE_AGENT_KEY', 'agent-secret-sentinel')
    def stuck_worker(args, **kwargs):
        assert 'CONTROL_PLANE_API_KEY' not in kwargs['env']
        assert 'EXAMPLE_AGENT_KEY' not in kwargs['env']
        child = actual_popen([sys.executable, '-I', '-c',
                             'import sys,time; sys.stdin.buffer.read(); time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    cancel = threading.Event()
    monkeypatch.setattr(image_io.subprocess, 'Popen', stuck_worker)
    timer = threading.Timer(0.4, cancel.set) if cancel_worker else None
    if timer: timer.start()
    try:
        with pytest.raises(JobCancelled if cancel_worker else TimeoutError):
            read_image(path, cancel=cancel, limits=replace(ImageLimits(), seconds=2 if cancel_worker else 0.4))
    finally:
        if timer: timer.cancel(); timer.join()
    assert len(children) == 1
    assert children[0].poll() is not None


def test_large_input_survives_slow_worker_startup(tmp_path, monkeypatch):
    from tiancheng_mcp import image_io
    path = tmp_path / 'large.png'
    Image.frombytes('RGB', (512, 256), os.urandom(512 * 256 * 3)).save(path)
    assert path.stat().st_size > 65536
    actual_popen = subprocess.Popen
    children = []

    def slow_worker(args, **kwargs):
        code = ('import sys,time; time.sleep(.2); '
                'from tiancheng_mcp.image_io import _worker; '
                'sys.argv=["image_io","--worker",*sys.argv[1:]]; _worker()')
        child = actual_popen([args[0], '-I', '-c', code, *args[5:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(image_io.subprocess, 'Popen', slow_worker)
    payload = read_image(path, 'original', limits=replace(ImageLimits(), seconds=3))
    assert base64.b64decode(payload['data']) == path.read_bytes()
    assert len(children) == 1 and children[0].poll() == 0
    assert children[0].stdin.closed and children[0].stdout.closed


@pytest.mark.parametrize('cancel_worker', [False, True])
def test_blocked_input_is_cancelled_and_all_resources_recovered(tmp_path, monkeypatch, cancel_worker):
    from tiancheng_mcp import image_io
    path = tmp_path / 'large.png'
    Image.frombytes('RGB', (512, 256), os.urandom(512 * 256 * 3)).save(path)
    actual_popen = subprocess.Popen
    children = []
    io_threads = []

    def blocked_worker(args, **kwargs):
        child = actual_popen([args[0], '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        actual_communicate = child.communicate

        def communicate(*args, **kwargs):
            io_threads.append(threading.current_thread())
            return actual_communicate(*args, **kwargs)

        child.communicate = communicate
        children.append(child)
        return child

    cancel = threading.Event()
    timer = threading.Timer(.4, cancel.set) if cancel_worker else None
    with monkeypatch.context() as patch:
        patch.setattr(image_io.subprocess, 'Popen', blocked_worker)
        if timer:
            timer.start()
        try:
            with pytest.raises(JobCancelled if cancel_worker else TimeoutError):
                read_image(path, cancel=cancel,
                           limits=replace(ImageLimits(), seconds=3 if cancel_worker else .4))
        finally:
            if timer:
                timer.cancel()
                timer.join()
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed
    assert len(io_threads) == 1
    assert io_threads[0] is not threading.current_thread() and not io_threads[0].is_alive()
    assert image_io._SLOTS.acquire(blocking=False)
    try:
        assert image_io._SLOTS.acquire(blocking=False)
        image_io._SLOTS.release()
    finally:
        image_io._SLOTS.release()
    assert read_image(path, 'metadata')['metadata']['source_width'] == 512


@pytest.mark.parametrize('failure', ['thread_start', 'communication', 'worker_exit'])
def test_communication_failures_reap_worker_and_restore_slots(tmp_path, monkeypatch, failure):
    from tiancheng_mcp import image_io
    path = tmp_path / 'sample.png'
    sample(path)
    actual_popen = subprocess.Popen
    children = []

    def fail(*args, **kwargs):
        raise OSError('synthetic communication failure')

    def worker(args, **kwargs):
        code = 'raise SystemExit(3)' if failure == 'worker_exit' else 'import time; time.sleep(60)'
        child = actual_popen([args[0], '-I', '-c', code], **kwargs)
        children.append(child)
        if failure == 'communication':
            child.communicate = fail
        return child

    with monkeypatch.context() as patch:
        patch.setattr(image_io.subprocess, 'Popen', worker)
        if failure == 'thread_start':
            patch.setattr(threading.Thread, 'start', fail)
        expected = ValueError if failure == 'worker_exit' else OSError
        with pytest.raises(expected, match='worker failed' if failure == 'worker_exit' else 'synthetic'):
            read_image(path)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed
    assert read_image(path, 'metadata')['metadata']['source_width'] == 80
