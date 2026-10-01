"""Live process ownership and atomic admission shared by process and Agent."""
from __future__ import annotations

import subprocess
import os
import select
import ctypes
import threading
import time
import uuid
from typing import Any


class ProcessSlots:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: set[object] = set()
        self.closed = False

    def reserve(self, maximum: int) -> object:
        with self._lock:
            if self.closed:
                raise RuntimeError("Managed process runtime is shutting down")
            if len(self._tokens) >= maximum:
                raise RuntimeError(f"At most {maximum} managed processes may run")
            token = object()
            self._tokens.add(token)
            return token

    def release(self, token: object) -> None:
        with self._lock:
            self._tokens.discard(token)

    def close(self) -> None:
        with self._lock:
            self.closed = True

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._tokens)


class _ManagedProcess:
    """One long-running allowlisted process with bounded in-memory output."""

    def __init__(
        self,
        process_id: str,
        command: str,
        cwd: str,
        process: subprocess.Popen[bytes],
        kill_job: Any,
        output_limit: int,
        max_runtime_seconds: int,
        owner: str = "process",
    ) -> None:
        self.process_id = process_id
        self.owner = owner
        self.session_id = f"sess_{uuid.uuid4().hex}"
        self.command = command
        self.cwd = cwd
        self.process = process
        self.pid = process.pid
        self.kill_job = kill_job
        self.output_limit = output_limit
        self.max_runtime_seconds = max_runtime_seconds
        self.started_epoch = time.time()
        self.ended_epoch: float | None = None
        self.exit_code: int | None = None
        self.timed_out = False
        self.stop_requested = False
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.stdout_total = 0
        self.stderr_total = 0
        self.lock = threading.Lock()
        self.reader_threads: list[threading.Thread] = []
        self.stdin_lock = threading.Lock()
        self.stdin_closed = process.stdin is None
        self.resource_lock = threading.RLock()
        self.resources_released = False
        self.finished = threading.Event()
        self.completion_callback = None
        self.reader_stop = threading.Event()
        self.output_drain_incomplete = False

    def append_output(self, stream: str, chunk: bytes) -> None:
        with self.lock:
            buffer = self.stdout if stream == "stdout" else self.stderr
            if stream == "stdout":
                self.stdout_total += len(chunk)
            else:
                self.stderr_total += len(chunk)
            buffer.extend(chunk)
            if len(buffer) > self.output_limit:
                del buffer[: len(buffer) - self.output_limit]

    def snapshot_output(
        self, stream: str, maximum: int, after_bytes: int = 0
    ) -> dict[str, Any]:
        if after_bytes < 0:
            raise ValueError("after_bytes must be non-negative")
        with self.lock:
            result: dict[str, Any] = {}
            for name, buffer, total in (
                ("stdout", self.stdout, self.stdout_total),
                ("stderr", self.stderr, self.stderr_total),
            ):
                if stream not in {name, "both"}:
                    continue
                base = max(0, total - len(buffer))
                requested = after_bytes
                gap = requested < base
                start = max(requested, base)
                relative = start - base
                chunk = bytes(buffer[relative : relative + maximum])
                result[name] = chunk.decode("utf-8", errors="replace")
                result[f"{name}_bytes_total"] = total
                result[f"{name}_offset_bytes"] = start
                result[f"{name}_next_offset_bytes"] = start + len(chunk)
                result[f"{name}_truncated"] = gap or (start + len(chunk) < total)
                result[f"{name}_cursor_gap"] = gap
            return result

    def protocol_stdout(self, after_bytes: int, maximum: int) -> tuple[bytes, int, bool]:
        """Internal raw stream for a strict, incremental JSON-RPC decoder."""
        with self.lock:
            base = max(0, self.stdout_total - len(self.stdout))
            start = max(base, after_bytes)
            chunk = bytes(self.stdout[start - base:start - base + maximum])
            return chunk, start + len(chunk), after_bytes < base

    def send_input(self, text: str, close_stdin: bool = False) -> int:
        if not isinstance(text, str):
            raise ValueError("input must be text")
        data = text.encode("utf-8")
        if len(data) > 256 * 1024:
            raise ValueError("input is limited to 256 KiB per call")
        with self.stdin_lock:
            if self.stdin_closed or self.process is None or self.process.stdin is None:
                raise RuntimeError("process stdin is closed")
            if self.process.poll() is not None:
                raise RuntimeError("process has already exited")
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                if close_stdin:
                    self.process.stdin.close()
                    self.stdin_closed = True
            except (BrokenPipeError, OSError) as exc:
                self.stdin_closed = True
                raise RuntimeError("process stdin is unavailable") from exc
        return len(data)


def _drain_managed_stream(
    stream: Any, record: _ManagedProcess, stream_name: str
) -> None:
    descriptor = stream.fileno()
    if os.name == "nt":
        import msvcrt
        handle = msvcrt.get_osfhandle(descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        while not record.reader_stop.is_set():
            if os.name == "nt":
                available = ctypes.c_ulong()
                if not kernel32.PeekNamedPipe(ctypes.c_void_p(handle), None, 0, None,
                        ctypes.byref(available), None):
                    code = ctypes.get_last_error()
                    if code in {109, 233}:
                        break
                    raise OSError(code, "Cannot inspect managed output pipe")
                if not available.value:
                    record.reader_stop.wait(0.02)
                    continue
                maximum = min(65536, available.value)
            else:
                if not select.select([descriptor], [], [], 0.05)[0]:
                    continue
                maximum = 65536
            chunk = os.read(descriptor, maximum)
            if not chunk:
                break
            record.append_output(stream_name, chunk)
    except OSError:
        record.output_drain_incomplete = True
    finally:
        try:
            stream.close()
        except OSError:
            record.output_drain_incomplete = True
