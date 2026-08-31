"""Workspace, Git, search, and optional process services."""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .agent_catalog import AgentCatalog
from .agent_sources import _SENSITIVE_COMPONENTS, AgentSourcePolicy
from .agents import (
    MAX_AGENT_EVENTS,
    MAX_AGENT_RUNS_PER_SESSION,
    MAX_AGENT_SESSIONS,
    AgentProfileRegistry,
    AgentRunState,
    AgentSessionState,
    NormalizedEvent,
    new_run_id,
    new_session_id,
    redact_text,
)
from .audit import AuditLogger
from .grants import ExternalGrantManager
from .jobs import JobCancelled, JobManager, current_cancel_event, current_job_id
from .policy import AccessPolicy, AccessPolicyError, AccessRule, _canonical_target
from .security import WorkspaceJail, WorkspaceSecurityError, compile_glob


DEFAULT_READ_BYTES = 256 * 1024
MAX_READ_BYTES = 1024 * 1024
MAX_TEXT_SCAN_BYTES = 8 * 1024 * 1024
MAX_EDIT_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
DEFAULT_MANAGED_OUTPUT_BYTES = 512 * 1024
MAX_MANAGED_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_MANAGED_PROCESSES = 32
DEFAULT_GIT_OUTPUT_BYTES = 512 * 1024
MAX_LIST_DEPTH = 5
MAX_LIST_ENTRIES = 1000
MAX_GLOB_RESULTS = 1000
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_SCAN_BYTES = 64 * 1024 * 1024
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_HASH_FILE_BYTES = 256 * 1024 * 1024
MAX_GLOB_SCANNED_ENTRIES = 100_000
MAX_SEARCH_SCANNED_FILES = 10_000
DEFAULT_INTERACTIVE_TIMEOUT_SECONDS = 75
MAX_INTERACTIVE_TIMEOUT_SECONDS = 90
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_SENSITIVE_GIT_COMMANDS = frozenset(
    {"credential", "credential-cache", "credential-manager", "credential-store"}
)
_GIT_TOKEN_RE = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9-]+|sk-[A-Za-z0-9_-]+)\b"
)
_GIT_URL_CREDENTIAL_RE = re.compile(r"(?i)(\b(?:https?|ssh|git)://)[^\s/@:]+:[^\s/@]+@")
_SAFE_USER_ENVIRONMENT_NAMES = (
    "APPDATA",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "SSH_AUTH_SOCK",
    "USERPROFILE",
    "CODEX_HOME",
)
_PROTECTED_ENVIRONMENT_NAMES = frozenset(
    {
        "CONTROL_PLANE_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_SECRET_KEY",
    }
)
_EXEC_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_SEARCH_EXCLUDES = (
    "!**/.git/**",
    "!**/.tiancheng-trash/**",
    "!**/.tiancheng-tmp/**",
    "!**/node_modules/**",
    "!**/.venv/**",
)
_T = TypeVar("_T")

# These operations are strictly bounded and are needed to inspect/stop a busy
# job queue.  Running them inline keeps recovery tools responsive even when all
# background workers are occupied by large scans or commands.
_LIGHTWEIGHT_TOOLS = frozenset(
    {
        "workspace_info",
        "stat",
        "read_text",
        "read_text_chunk",
        "external_stat",
        "external_read_text",
        "external_read_text_chunk",
        "access_policy_explain",
        "access_policy_reload",
        "agent_catalog_providers",
        "agent_catalog_sources",
        "agent_catalog_list",
        "agent_catalog_inspect",
        # Observing a managed process or agent run is the documented polling
        # surface: start returns immediately and the caller loops on
        # output/events with an incremental cursor.  Routing those polls
        # through the job queue would allocate one record per poll and
        # exhaust the bounded record table.
        "process_status",
        "process_output",
        "list_processes",
        "agent_session_list",
        "agent_session_inspect",
        "agent_session_close",
        "agent_run_inspect",
        "agent_run_events",
        "agent_run_result",
        "agent_run_cancel",
    }
)


class _NullAudit:
    """Used by scoped external helpers; the outer MCP call is audited."""

    directory = Path()

    def record(self, **_: Any) -> None:
        return


def _redact_git_text(value: str) -> str:
    """Remove common credential forms before Git output reaches MCP clients."""

    redacted = _GIT_URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    redacted = _GIT_TOKEN_RE.sub("<redacted-token>", redacted)
    return redacted[:MAX_COMMAND_OUTPUT_BYTES]


if os.name == "nt":

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _WindowsKillJob:
    """Best-effort Windows Job Object that prevents surviving child processes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle, ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        )
        if not assigned:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return
        self._handle = int(handle)

    @property
    def active(self) -> bool:
        return self._handle is not None

    def terminate(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject(ctypes.c_void_p(self._handle), 1)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process tree without invoking a command shell."""

    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        if taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=_CREATE_NO_WINDOW,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    if process.poll() is None:
        process.kill()


def _iso_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


def _bounded_int(value: int, *, minimum: int, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _detect_encoding(sample: bytes) -> str:
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if b"\x00" in sample:
        raise ValueError("Binary file refused: NUL byte detected")
    return "utf-8"


def _decode_text(data: bytes, encoding: str, *, allow_incomplete_tail: bool) -> str:
    try:
        return data.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        if allow_incomplete_tail and exc.end == len(data):
            return data[: exc.start].decode(encoding, errors="strict")
        raise


def _truncate_utf8(text: str, maximum: int) -> tuple[str, bool, int]:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text, False, len(encoded)
    shortened = encoded[:maximum]
    while shortened:
        try:
            return shortened.decode("utf-8"), True, len(shortened)
        except UnicodeDecodeError as exc:
            shortened = shortened[: exc.start]
    return "", True, 0


def _drain_stream(stream: Any, maximum: int, result: dict[str, Any], key: str) -> None:
    kept = bytearray()
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if len(kept) < maximum:
            kept.extend(chunk[: maximum - len(kept)])
    result[key] = (bytes(kept), total)


def _run_process_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    maximum_output: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("Job cancelled before process start")
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    kill_job = _WindowsKillJob(process)
    assert process.stdout is not None
    assert process.stderr is not None
    drained: dict[str, Any] = {}
    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stdout, maximum_output, drained, "stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stderr, maximum_output, drained, "stderr"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    cancelled = False
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                if kill_job.active:
                    kill_job.terminate()
                else:
                    _terminate_process_tree(process)
                exit_code = process.wait(timeout=5)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                if kill_job.active:
                    kill_job.terminate()
                else:
                    _terminate_process_tree(process)
                exit_code = process.wait(timeout=5)
                break
            try:
                exit_code = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        # A stubborn process gets a final hard kill; this path is shared by
        # cancellation and timeout and must never leave a child behind.
        timed_out = timed_out and not cancelled
        if kill_job.active:
            kill_job.terminate()
        else:
            _terminate_process_tree(process)
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
    finally:
        # KILL_ON_JOB_CLOSE also refuses detached background children after the
        # main process exits normally. Exec commands may not leave daemons behind.
        kill_job.close()
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    stdout_bytes, stdout_total = drained.get("stdout", (b"", 0))
    stderr_bytes, stderr_total = drained.get("stderr", (b"", 0))
    return {
        "exit_code": exit_code,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "stdout_truncated": stdout_total > len(stdout_bytes),
        "stderr_truncated": stderr_total > len(stderr_bytes),
        "stdout_bytes_total": stdout_total,
        "stderr_bytes_total": stderr_total,
        "timeout": timed_out,
        "cancelled": cancelled,
    }


class _ManagedProcess:
    """One long-running allowlisted process with bounded in-memory output."""

    def __init__(
        self,
        process_id: str,
        command: str,
        cwd: str,
        process: subprocess.Popen[bytes],
        kill_job: _WindowsKillJob,
        output_limit: int,
        max_runtime_seconds: int,
    ) -> None:
        self.process_id = process_id
        self.session_id = f"sess_{uuid.uuid4().hex}"
        self.command = command
        self.cwd = cwd
        self.process = process
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

    def send_input(self, text: str, close_stdin: bool = False) -> int:
        if not isinstance(text, str):
            raise ValueError("input must be text")
        data = text.encode("utf-8")
        if len(data) > 256 * 1024:
            raise ValueError("input is limited to 256 KiB per call")
        with self.stdin_lock:
            if self.stdin_closed or self.process.stdin is None:
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
    # read() blocks until it has the full request or the pipe closes, so a
    # long-running process that emits a few hundred bytes at a time would
    # deliver nothing until it exited. read1() returns whatever one underlying
    # read yields, which is what makes incremental output actually incremental.
    read_available = getattr(stream, "read1", None) or stream.read
    while True:
        chunk = read_available(65536)
        if not chunk:
            break
        record.append_output(stream_name, chunk)


class TianChengService:
    def __init__(
        self,
        workspace: str | Path,
        audit_directory: str | Path | None,
        *,
        allow_exec: bool = False,
        passthrough_env: Sequence[str] = (),
        allow_external_grants: bool = False,
        totp_secret: str | None = None,
        interactive_timeout_seconds: int = DEFAULT_INTERACTIVE_TIMEOUT_SECONDS,
        enable_jobs: bool = True,
        access_policy_path: str | Path | None = None,
        access_policy: AccessPolicy | None = None,
        agent_source_policy: AgentSourcePolicy | None = None,
        agent_source_policy_path: str | Path | None = None,
        agent_catalog_path: str | Path | None = None,
        enable_agent_catalog: bool = True,
        allow_policy_hot_reload: bool = False,
    ) -> None:
        self.jail = WorkspaceJail(workspace, create=True)
        self.access_policy_path = Path(access_policy_path) if access_policy_path else (
            Path(__file__).resolve().parents[2] / "config" / "access-policy.json"
        )
        self._access_policy_is_default = access_policy_path is None
        policy_location = self.access_policy_path.resolve(strict=False)
        if policy_location == self.jail.root or self.jail.root in policy_location.parents:
            raise ValueError("Access policy must be stored outside the workspace")
        if access_policy is not None:
            self.access_policy = access_policy
        else:
            self.access_policy = self._load_access_policy()
        if audit_directory is None:
            self.audit = _NullAudit()
            self.audit_directory = Path(__file__).resolve().parents[2] / "logs"
        else:
            audit_path = Path(audit_directory).resolve()
            if self.jail.root == audit_path or self.jail.root in audit_path.parents:
                raise ValueError("Audit directory must be outside the workspace")
            self.audit = AuditLogger(audit_path)
            self.audit_directory = audit_path
        self.allow_exec = allow_exec
        self.allow_policy_hot_reload = bool(allow_policy_hot_reload)
        self._policy_changes: dict[str, dict[str, Any]] = {}
        self._policy_change_lock = threading.Lock()
        self.interactive_timeout_seconds = _bounded_int(
            interactive_timeout_seconds,
            minimum=1,
            maximum=MAX_INTERACTIVE_TIMEOUT_SECONDS,
            label="interactive_timeout_seconds",
        )
        self.external_grants = ExternalGrantManager(
            self.jail.root,
            enabled=allow_external_grants,
            totp_secret=totp_secret,
            access_policy=self.access_policy,
        )
        self.passthrough_env = self._validate_passthrough_env(passthrough_env)
        self.git_executable = shutil.which("git")
        self.rg_executable = shutil.which("rg")
        if self.rg_executable:
            resolved_rg = Path(self.rg_executable).resolve()
            try:
                resolved_rg.relative_to(self.jail.root)
            except ValueError:
                self.rg_executable = str(resolved_rg)
            else:
                raise WorkspaceSecurityError("Refusing ripgrep executable from inside workspace")
        self._exec_commands = self._discover_exec_commands() if allow_exec else {}
        self._agent_only_commands = (
            self._discover_agent_only_commands() if allow_exec else {}
        )
        self.agent_profiles = AgentProfileRegistry(
            {**self._exec_commands, **self._agent_only_commands}
        )
        project_root = Path(__file__).resolve().parents[2]
        self.agent_source_policy_path = (
            Path(agent_source_policy_path)
            if agent_source_policy_path is not None
            else project_root / "config" / "agent-sources.json"
        )
        self.agent_catalog: AgentCatalog | None = None
        if enable_agent_catalog:
            if agent_source_policy is None:
                source_policy_location = self.agent_source_policy_path.resolve(
                    strict=False
                )
                if (
                    source_policy_location == self.jail.root
                    or self.jail.root in source_policy_location.parents
                ):
                    raise ValueError(
                        "Agent source policy must be stored outside the workspace"
                    )
                self.agent_source_policy = AgentSourcePolicy.load(
                    self.agent_source_policy_path
                )
            else:
                self.agent_source_policy = agent_source_policy
            catalog_location = Path(agent_catalog_path) if agent_catalog_path else (
                project_root / "state" / "agent-catalog.sqlite3"
            )
            self.agent_catalog = AgentCatalog(catalog_location, self.jail.root)
        else:
            self.agent_source_policy = AgentSourcePolicy.empty()
        self._agent_sessions: dict[str, AgentSessionState] = {}
        self._agent_lock = threading.RLock()
        self._processes: dict[str, _ManagedProcess] = {}
        self._process_lock = threading.Lock()
        self.jobs: JobManager | None = JobManager() if enable_jobs else None
        self._grant_reaper_stop = threading.Event()
        self._grant_reaper: threading.Thread | None = None
        if self.external_grants.enabled and self.jobs is not None:
            self._grant_reaper = threading.Thread(
                target=self._grant_reaper_loop,
                name="tiancheng-grant-reaper",
                daemon=True,
            )
            self._grant_reaper.start()

    def _load_access_policy(self) -> AccessPolicy:
        try:
            return AccessPolicy.load(self.access_policy_path, self.jail.root)
        except AccessPolicyError as exc:
            # The default policy is machine-specific (it is rooted at the
            # configured workspace).  A caller may intentionally use a temporary or
            # alternate workspace for tests/development.  In that case a
            # policy whose root does not match must not expose its external
            # rules; fall back to the jail-only policy.  Explicit policy paths
            # still fail closed so configuration mistakes are visible.
            if self._access_policy_is_default and str(exc) == (
                "Policy must contain an enabled workspace root rule"
            ):
                return AccessPolicy.default(self.jail.root)
            raise

    def _grant_reaper_loop(self) -> None:
        while not self._grant_reaper_stop.wait(0.25):
            for grant_id in self.external_grants.expire():
                if self.jobs is not None:
                    self.jobs.cancel_for_grant(grant_id, "grant expired")

    @staticmethod
    def _check_cancelled() -> None:
        event = current_cancel_event()
        if event is not None and event.is_set():
            raise JobCancelled("Job cancelled")

    @staticmethod
    def _cancel_event() -> threading.Event | None:
        return current_cancel_event()

    @staticmethod
    def _validate_passthrough_env(names: Sequence[str]) -> tuple[str, ...]:
        if isinstance(names, (str, bytes)):
            raise ValueError("passthrough_env must be a sequence of variable names")
        normalized: list[str] = []
        for name in names:
            if not isinstance(name, str) or not _EXEC_ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError(f"Invalid passthrough environment variable name: {name!r}")
            upper = name.upper()
            if upper in _PROTECTED_ENVIRONMENT_NAMES:
                raise PermissionError(f"Environment variable {upper} is never passed to child processes")
            if upper in {
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
            }:
                raise PermissionError(f"Environment variable {upper} cannot override execution policy")
            if name not in normalized:
                normalized.append(name)
        if len(normalized) > 32:
            raise ValueError("At most 32 passthrough environment variables may be configured")
        return tuple(normalized)

    def audited(
        self,
        tool: str,
        relative_path: str | None,
        operation: Callable[[], _T],
    ) -> _T:
        started = time.perf_counter()
        job_id = current_job_id()
        try:
            result = operation()
        except Exception as exc:
            self.audit.record(
                tool=tool,
                relative_path=relative_path,
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                job_id=job_id,
                state="cancelled" if isinstance(exc, JobCancelled) else "failed",
            )
            raise
        output_truncated = result.get("output_truncated") if isinstance(result, dict) else None
        self.audit.record(
            tool=tool,
            relative_path=relative_path,
            success=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            job_id=job_id,
            state="succeeded",
            output_truncated=output_truncated if isinstance(output_truncated, bool) else None,
        )
        return result

    def run_with_fallback(
        self,
        tool: str,
        relative_path: str | None,
        operation: Callable[[], _T],
        *,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> _T | dict[str, Any]:
        """Run every tool behind a short wait; return a job handle if it overruns."""

        # Job control and grant lifecycle calls must remain immediately usable
        # even when all background workers are busy.
        if tool in {
            "job_status",
            "job_result",
            "job_cancel",
            "job_list",
            "external_grant_status",
            "revoke_external_access",
            "cancel_external_access_request",
        } or tool in _LIGHTWEIGHT_TOOLS:
            return self.audited(tool, relative_path, operation)

        if self.jobs is None:
            return self.audited(tool, relative_path, operation)
        record, completed, result = self.jobs.submit_and_wait(
            tool,
            lambda _cancel_event: self.audited(tool, relative_path, operation),
            interactive_timeout=self.interactive_timeout_seconds,
            metadata=metadata,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
        )
        if not completed:
            self.audit.record(
                tool=tool,
                relative_path=relative_path,
                success=True,
                duration_ms=self.interactive_timeout_seconds * 1000,
                job_id=record.job_id,
                state=record.state,
                reason="automatically continued as background job",
            )
        return result

    def job_status(self, job_id: str) -> dict[str, Any]:
        if self.jobs is None:
            raise RuntimeError("Job manager is disabled for this service instance")
        return self.jobs.status(job_id)

    def job_result(
        self,
        job_id: str,
        *,
        cursor: int = 0,
        max_items: int = 100,
        max_bytes: int = DEFAULT_COMMAND_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        cursor = _bounded_int(cursor, minimum=0, maximum=1_000_000, label="cursor")
        max_items = _bounded_int(max_items, minimum=1, maximum=500, label="max_items")
        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_COMMAND_OUTPUT_BYTES, label="max_bytes"
        )
        if self.jobs is None:
            raise RuntimeError("Job manager is disabled for this service instance")
        payload = self.jobs.result(job_id)
        if not payload.get("ready"):
            return payload
        value = payload.get("result")
        if isinstance(value, list):
            payload = {
                **payload,
                "result": value[cursor : cursor + max_items],
                "cursor": cursor,
                "next_cursor": cursor + max_items if cursor + max_items < len(value) else None,
            }
            value = payload["result"]
        elif isinstance(value, dict):
            list_key = next(
                (key for key, item in value.items() if isinstance(item, list)),
                None,
            )
            if list_key is not None:
                items = value[list_key]
                payload = {
                    **payload,
                    "result": {
                        **value,
                        list_key: items[cursor : cursor + max_items],
                    },
                    "cursor": cursor,
                    "next_cursor": cursor + max_items if cursor + max_items < len(items) else None,
                    "items_key": list_key,
                }
                value = payload["result"]
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= maximum:
            return payload
        return {
            "job_id": job_id,
            "state": payload.get("state"),
            "ready": True,
            "result_truncated": True,
            "result_bytes": len(encoded),
            "max_bytes": maximum,
            "message": "Job result exceeds max_bytes; use a narrower operation or add paging.",
        }

    def job_cancel(self, job_id: str, reason: str = "") -> dict[str, Any]:
        if self.jobs is None:
            raise RuntimeError("Job manager is disabled for this service instance")
        result = self.jobs.cancel(job_id, reason)
        self.audit.record(
            tool="job_cancel",
            relative_path="<job>",
            success=True,
            duration_ms=0,
            job_id=job_id,
            state=result.get("state"),
            reason=reason or "cancelled by caller",
        )
        return result

    def job_list(self, include_finished: bool = True, limit: int = 50) -> dict[str, Any]:
        if self.jobs is None:
            raise RuntimeError("Job manager is disabled for this service instance")
        return self.jobs.list(include_finished=include_finished, limit=limit)

    def workspace_info(self) -> dict[str, Any]:
        return {
            "workspace_root": str(self.jail.root),
            "server_version": __version__,
            "capabilities": {
                "file_operations": True,
                "recursive_listing": True,
                "glob": True,
                "text_search": True,
                "ripgrep_search": self.rg_executable is not None,
                "trash_delete": True,
                "local_git": self.git_executable is not None,
                "command_execution": self.allow_exec,
                "managed_processes": self.allow_exec,
                "local_agent_profiles": bool(self.agent_profiles.names()),
                "local_agent_catalog": self.agent_catalog is not None,
                "background_jobs": self.jobs is not None,
                "external_grants": self.external_grants.enabled,
                "access_policy_hot_reload": self.allow_policy_hot_reload,
            },
            "access_policy_reload_mode": (
                "hot" if self.allow_policy_hot_reload else "cold"
            ),
            "command_execution_enabled": self.allow_exec,
            "command_execution_policy": (
                "guarded-development" if self.allow_exec else "disabled"
            ),
            "explicit_env_passthrough_enabled": bool(self.passthrough_env),
            "available_exec_commands": (
                sorted(self._exec_commands) if self.allow_exec else []
            ),
            "available_agent_profiles": list(self.agent_profiles.names()),
            "available_agent_providers": list(self.agent_profiles.providers()),
            "agent_sources": self.agent_source_policy.summary(),
            "git_available": self.git_executable is not None,
            "ripgrep_available": self.rg_executable is not None,
            "interactive_timeout_seconds": self.interactive_timeout_seconds,
            "access_policy": self.access_policy.summary(),
        }

    def agent_catalog_providers(self) -> dict[str, Any]:
        self._require_agent_catalog()
        runtime = {
            item["provider"]: item for item in self.agent_profiles.providers()
        }
        configured = {
            source.provider
            for source in self.agent_source_policy.sources
            if source.enabled
        }
        providers: list[dict[str, Any]] = []
        for provider, display_name in (
            ("codex", "Codex"),
            ("claude-code", "Claude Code"),
        ):
            runtime_summary = runtime.get(provider)
            capabilities = (
                dict(runtime_summary["capabilities"])
                if runtime_summary
                else {
                    "create": False,
                    "attach": False,
                    "resume": False,
                    "discover": False,
                    "stream": False,
                    "cancel": False,
                    "steer": False,
                    "interaction": False,
                    "fork": False,
                }
            )
            capabilities["discover"] = True
            providers.append(
                {
                    "provider": provider,
                    "display_name": display_name,
                    "catalog_parser": True,
                    "source_configured": provider in configured,
                    "runtime_available": runtime_summary is not None,
                    "runtime_profiles": (
                        runtime_summary["profiles"] if runtime_summary else []
                    ),
                    "capabilities": capabilities,
                }
            )
        return {"providers": providers, "count": len(providers)}

    def agent_catalog_sources(self) -> dict[str, Any]:
        catalog = self._require_agent_catalog()
        sources = catalog.source_summaries(self.agent_source_policy)
        return {"sources": sources, "count": len(sources)}

    def agent_catalog_refresh(self, source_id: str) -> dict[str, Any]:
        catalog = self._require_agent_catalog()
        return catalog.refresh(
            self.agent_source_policy,
            source_id,
            cancel_event=current_cancel_event(),
        )

    def agent_catalog_list(
        self,
        provider: str = "",
        source_id: str = "",
        query: str = "",
        cursor: int = 0,
        limit: int = 50,
        max_bytes: int = 128 * 1024,
    ) -> dict[str, Any]:
        catalog = self._require_agent_catalog()
        page = catalog.list_records(
            self.agent_source_policy,
            provider=provider,
            source_id=source_id,
            query=query,
            cursor=cursor,
            limit=limit,
            max_bytes=max_bytes,
        )
        page["conversations"] = [
            self._decorate_agent_catalog_record(record)
            for record in page["conversations"]
        ]
        return page

    def agent_catalog_inspect(self, conversation_ref: str) -> dict[str, Any]:
        catalog = self._require_agent_catalog()
        return self._decorate_agent_catalog_record(
            catalog.inspect_record(self.agent_source_policy, conversation_ref)
        )

    def _catalog_cwd_scope(self, record: dict[str, Any]) -> tuple[str, str | None]:
        """Classify a recorded working directory against the current policy.

        A conversation can be resumed only where its original directory is
        still reachable: inside the workspace, or inside a whitelisted rule.
        The absolute path itself never reaches the caller.
        """

        if record.get("cwd") is not None:
            return "workspace", record["cwd"]
        raw = record.get("cwd_absolute")
        if not isinstance(raw, str) or not raw:
            return "unavailable", None
        try:
            decision = self.access_policy.explain(raw, "read")
        except (AccessPolicyError, WorkspaceSecurityError, ValueError):
            return "unavailable", None
        if not decision.allowed or decision.requires_approval:
            return "outside-policy", None
        if decision.rule_path is None or decision.rule_path == self.jail.root:
            return "unavailable", None
        return "access-policy", f"<policy>/{Path(raw).name}"

    def _decorate_agent_catalog_record(
        self, record: dict[str, Any]
    ) -> dict[str, Any]:
        resumable = False
        attachable = False
        for profile_name in self.agent_profiles.names():
            profile = self.agent_profiles.get(profile_name)
            if profile.provider != record["provider"]:
                continue
            adapter = self.agent_profiles.adapter_for_profile(profile)
            resumable = resumable or adapter.capabilities.resume
            attachable = attachable or adapter.capabilities.attach
        scope, display = self._catalog_cwd_scope(record)
        payload = {key: value for key, value in record.items() if key != "cwd_absolute"}
        return {
            **payload,
            "cwd": display,
            "cwd_scope": scope,
            "resumable": resumable,
            "attachable": attachable and scope in {"workspace", "access-policy"},
        }

    def _require_agent_catalog(self) -> AgentCatalog:
        if self.agent_catalog is None:
            raise RuntimeError("Agent catalog is disabled for this scoped service")
        return self.agent_catalog

    def access_policy_explain(self, path: str, operation: str = "read") -> dict[str, Any]:
        """Explain static policy matching without reading the target contents."""

        return self.access_policy.explain(path, operation).as_dict()

    def reload_access_policy(self) -> dict[str, Any]:
        """Atomically replace the in-memory policy after validating the full file."""

        if self.access_policy_path.resolve(strict=False) == self.jail.root or self.jail.root in self.access_policy_path.resolve(strict=False).parents:
            raise ValueError("Access policy must be stored outside the workspace")
        loaded = self._load_access_policy()
        self.access_policy = loaded
        self.external_grants.access_policy = loaded
        return {"reloaded": True, **loaded.summary()}

    def _require_policy_hot_reload(self) -> None:
        if not self.allow_policy_hot_reload:
            raise PermissionError(
                "Access-policy hot reload is disabled; restart with "
                "--allow-policy-hot-reload or edit the policy locally"
            )

    def _validate_hot_reload_target(self, raw: str) -> Path:
        """Reject directories that must never be granted by a chat approval.

        The approval only means "the user agreed to expose this folder".  It
        must not be usable to hand over the server's own code, its policy and
        audit files, a whole drive, or a directory whose name says it holds
        credentials -- those turn one approval into permanent self-escalation.
        """

        candidate = _canonical_target(raw)
        if candidate.parent == candidate:
            raise PermissionError("A filesystem root cannot be whitelisted")
        if not candidate.is_dir():
            raise PermissionError("Only an existing directory can be whitelisted")
        project_root = Path(__file__).resolve().parents[2]
        protected = [project_root, self.audit_directory, self.access_policy_path.parent]
        for reserved in protected:
            reserved = Path(reserved).resolve(strict=False)
            if candidate == reserved or reserved in candidate.parents or candidate in reserved.parents:
                raise PermissionError(
                    "This directory holds the server's own code, policy, or logs "
                    "and cannot be whitelisted from chat"
                )
        if candidate in self.jail.root.parents:
            raise PermissionError(
                "A parent of the workspace cannot be whitelisted; it would shadow "
                "the workspace rule"
            )
        for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(name)
            if not value:
                continue
            system_path = Path(value).resolve(strict=False)
            if candidate == system_path or system_path in candidate.parents:
                raise PermissionError("System directories cannot be whitelisted")
        for part in candidate.parts:
            if part.casefold() in _SENSITIVE_COMPONENTS:
                raise PermissionError(
                    f"Refusing a path that contains a sensitive component: {part!r}"
                )
        decision = self.access_policy.explain(candidate, "list")
        if decision.rule_path is not None and decision.mode == "deny":
            raise PermissionError(
                "An explicit deny rule covers this path and cannot be overridden"
            )
        return candidate

    def policy_change_request(
        self,
        paths: list[str],
        mode: str = "read",
        allow_exec: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        """Stage a whitelist change for the user to approve in conversation."""

        self._require_policy_hot_reload()
        if mode not in {"browse", "read", "write", "full"}:
            raise ValueError("mode must be browse, read, write, or full")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
            raise ValueError("paths must be a list of 1-16 directories")
        if allow_exec and mode != "full":
            raise ValueError("allow_exec requires mode=full")
        targets = [self._validate_hot_reload_target(value) for value in paths]
        now = time.time()
        with self._policy_change_lock:
            self._policy_changes = {
                key: item
                for key, item in self._policy_changes.items()
                if item["expires_at"] > now
            }
            if len(self._policy_changes) >= 3:
                raise RuntimeError("Too many pending access-policy changes")
            request_id = uuid.uuid4().hex
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            challenge = (
                "".join(secrets.choice(alphabet) for _ in range(4))
                + "-"
                + "".join(secrets.choice(alphabet) for _ in range(4))
            )
            self._policy_changes[request_id] = {
                "request_id": request_id,
                "challenge": challenge,
                "paths": [str(target) for target in targets],
                "mode": mode,
                "allow_exec": bool(allow_exec),
                "note": str(note)[:500],
                "expires_at": now + 300,
                "attempts": 0,
            }
        return {
            "request_id": request_id,
            "challenge": challenge,
            "paths": [str(target) for target in targets],
            "mode": mode,
            "allow_exec": bool(allow_exec),
            "expires_at": _iso_timestamp(now + 300),
            "status": "pending",
            "effect": (
                "On approval these directories are written into the access policy "
                "and take effect immediately, with no restart."
            ),
            "instructions": (
                "Show the user the exact paths and mode above and wait for an "
                "explicit reply. Only then submit request_id, challenge, and "
                "confirmation='批准'."
            ),
        }

    def policy_change_approve(
        self, request_id: str, challenge: str, confirmation: str = ""
    ) -> dict[str, Any]:
        self._require_policy_hot_reload()
        if confirmation != "批准":
            raise PermissionError(
                "Explicit user confirmation is required: confirmation must be '批准'"
            )
        now = time.time()
        with self._policy_change_lock:
            pending = self._policy_changes.get(request_id)
            if pending is None or pending["expires_at"] <= now:
                self._policy_changes.pop(request_id, None)
                raise PermissionError("Unknown or expired access-policy change request")
            pending["attempts"] += 1
            if pending["attempts"] > 5:
                self._policy_changes.pop(request_id, None)
                raise PermissionError("Too many approval attempts; request cancelled")
            if not hmac.compare_digest(str(challenge), str(pending["challenge"])):
                raise PermissionError("Invalid or mismatched approval challenge")
            self._policy_changes.pop(request_id, None)
        # Re-validate at approval time: the policy may have changed since the
        # request was staged.
        targets = [self._validate_hot_reload_target(value) for value in pending["paths"]]
        added = [
            AccessRule(
                target,
                pending["mode"],
                False,
                True,
                pending["allow_exec"],
                pending["note"],
            )
            for target in targets
        ]
        updated = self.access_policy.with_rules(added)
        saved = updated.save_atomic(self.access_policy_path)
        reloaded = self.reload_access_policy()
        return {
            "approved": True,
            "paths": [str(target) for target in targets],
            "mode": pending["mode"],
            "allow_exec": pending["allow_exec"],
            "effective_immediately": True,
            "backup": saved["backup"],
            **reloaded,
        }

    def policy_change_cancel(self, request_id: str) -> dict[str, Any]:
        self._require_policy_hot_reload()
        with self._policy_change_lock:
            removed = self._policy_changes.pop(request_id, None)
        return {"request_id": request_id, "cancelled": removed is not None}

    def policy_change_status(self) -> dict[str, Any]:
        self._require_policy_hot_reload()
        now = time.time()
        with self._policy_change_lock:
            pending = [
                {
                    "request_id": item["request_id"],
                    "paths": item["paths"],
                    "mode": item["mode"],
                    "allow_exec": item["allow_exec"],
                    "expires_at": _iso_timestamp(item["expires_at"]),
                }
                for item in self._policy_changes.values()
                if item["expires_at"] > now
            ]
        return {"pending": pending, "count": len(pending)}

    def request_external_access(
        self, path: str, mode: str = "read", ttl_seconds: int = 600, reason: str = ""
    ) -> dict[str, object]:
        return self.external_grants.request(path, mode, ttl_seconds, reason)

    def approve_external_access(self, request_id: str, challenge: str, confirmation: str = "") -> dict[str, object]:
        return self.external_grants.approve(request_id, challenge, confirmation)

    def external_grant_status(self) -> dict[str, object]:
        return self.external_grants.status()

    def revoke_external_access(self, grant_id: str) -> dict[str, object]:
        result = self.external_grants.revoke(grant_id)
        if self.jobs is not None:
            result = {**result, "cancelled_jobs": self.jobs.cancel_for_grant(grant_id)}
        return result

    def cancel_external_access_request(self, request_id: str) -> dict[str, object]:
        return self.external_grants.cancel_request(request_id)

    def _external_service(self, grant_id: str, required_mode: str) -> TianChengService:
        _, grant = self.external_grants.resolve(
            grant_id, ".", required_mode=required_mode, must_exist=True, expect="directory"
        )
        return TianChengService(
            grant.root,
            None,
            allow_exec=grant.mode == "exec",
            passthrough_env=self.passthrough_env,
            enable_jobs=False,
            enable_agent_catalog=False,
        )

    def external_list_dir(self, grant_id: str, path: str = ".", depth: int = 1) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="read", must_exist=True, expect="directory")
        return self._external_service(grant_id, "read").list_dir(path, depth)

    def external_stat(self, grant_id: str, path: str) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="read", must_exist=True)
        return self._external_service(grant_id, "read").stat(path)

    def external_read_text(self, grant_id: str, path: str, start_line: int | None = None, end_line: int | None = None, max_bytes: int = DEFAULT_READ_BYTES) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="read", must_exist=True, expect="file")
        return self._external_service(grant_id, "read").read_text(path, start_line, end_line, max_bytes)

    def external_read_text_chunk(self, grant_id: str, path: str, offset_bytes: int = 0, max_bytes: int = DEFAULT_READ_BYTES) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="read", must_exist=True, expect="file")
        return self._external_service(grant_id, "read").read_text_chunk(path, offset_bytes, max_bytes)

    def external_write_text(self, grant_id: str, path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="write", must_exist=False, allow_root=False)
        return self._external_service(grant_id, "write").write_text(path, content, create_parents, expected_sha256)

    def external_append_text(self, grant_id: str, path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="write", must_exist=False, allow_root=False)
        return self._external_service(grant_id, "write").append_text(path, content, create_parents, expected_sha256)

    def external_mkdir(self, grant_id: str, path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="write", must_exist=False, allow_root=False)
        return self._external_service(grant_id, "write").mkdir(path, parents, exist_ok)

    def external_move(self, grant_id: str, source: str, destination: str) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, source, required_mode="write", must_exist=True, allow_root=False)
        self.external_grants.resolve(grant_id, destination, required_mode="write", must_exist=False, allow_root=False)
        return self._external_service(grant_id, "write").move(source, destination)

    def external_copy(self, grant_id: str, source: str, destination: str) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, source, required_mode="write", must_exist=True, allow_root=False)
        self.external_grants.resolve(grant_id, destination, required_mode="write", must_exist=False, allow_root=False)
        return self._external_service(grant_id, "write").copy(source, destination)

    def external_delete(self, grant_id: str, path: str) -> dict[str, Any]:
        self.external_grants.resolve(grant_id, path, required_mode="delete", must_exist=True, allow_root=False)
        return self._external_service(grant_id, "delete").delete(path)

    def external_glob(
        self, grant_id: str, pattern: str, max_results: int = 200, base_path: str = "."
    ) -> dict[str, Any]:
        self.external_grants.resolve(
            grant_id, base_path, required_mode="read", must_exist=True, expect="directory"
        )
        return self._external_service(grant_id, "read").glob(pattern, max_results, base_path)

    def external_search_text(
        self,
        grant_id: str,
        query: str,
        glob_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = 100,
        max_scan_bytes: int = 32 * 1024 * 1024,
        include_hidden: bool = True,
        timeout_seconds: int = 30,
        base_path: str = ".",
    ) -> dict[str, Any]:
        self.external_grants.resolve(
            grant_id, base_path, required_mode="read", must_exist=True, expect="directory"
        )
        return self._external_service(grant_id, "read").search_text(
            query,
            glob_pattern,
            case_sensitive,
            max_results,
            max_scan_bytes,
            include_hidden,
            False,
            True,
            timeout_seconds,
            base_path,
        )

    def external_run_command(self, grant_id: str, command: str, args: list[str] | None = None, cwd: str = ".", timeout_seconds: int = 60, max_output_bytes: int = DEFAULT_COMMAND_OUTPUT_BYTES) -> dict[str, Any]:
        scoped = self._external_service(grant_id, "exec")
        self.external_grants.resolve(grant_id, cwd, required_mode="exec", must_exist=True, expect="directory")
        return scoped.run_command(command, args, cwd, timeout_seconds, max_output_bytes)

    def _policy_scoped_service(self, path: str, operation: str) -> tuple["TianChengService", str]:
        """Build a short-lived service rooted at a static, non-approval rule."""

        decision = self.access_policy.authorize(path, operation)
        if decision.requires_approval:
            raise PermissionError(
                "This static rule requires approval; use request_external_access first"
            )
        if decision.rule_path is None or decision.rule_path == self.jail.root:
            raise PermissionError("Static policy does not grant external access for this path")
        root = decision.rule_path
        target = decision.path
        try:
            relative = target.relative_to(root).as_posix() or "."
        except ValueError as exc:
            raise WorkspaceSecurityError("Static policy path escaped its rule root") from exc
        scoped = TianChengService(
            root,
            None,
            allow_exec=operation == "exec" and decision.allow_exec,
            passthrough_env=self.passthrough_env,
            enable_jobs=False,
            access_policy=AccessPolicy.default(root),
        )
        return scoped, relative

    def _policy_scoped_pair(
        self, source: str, destination: str, operation: str
    ) -> tuple["TianChengService", str, str]:
        source_decision = self.access_policy.authorize(source, operation)
        destination_decision = self.access_policy.authorize(destination, operation)
        if source_decision.requires_approval or destination_decision.requires_approval:
            raise PermissionError(
                "This static rule requires approval; use request_external_access first"
            )
        if source_decision.rule_path is None or source_decision.rule_path != destination_decision.rule_path:
            source_rule = (
                "no matching rule"
                if source_decision.rule_path is None
                else f"{source_decision.mode} rule"
            )
            destination_rule = (
                "no matching rule"
                if destination_decision.rule_path is None
                else f"{destination_decision.mode} rule"
            )
            raise PermissionError(
                "Source and destination must share one compatible static policy rule "
                f"(source: {source_rule}; destination: {destination_rule})"
            )
        root = source_decision.rule_path
        if root == self.jail.root:
            raise PermissionError("Static policy does not grant external access for this path")
        try:
            source_relative = source_decision.path.relative_to(root).as_posix() or "."
            destination_relative = destination_decision.path.relative_to(root).as_posix() or "."
        except ValueError as exc:
            raise WorkspaceSecurityError("Static policy path escaped its rule root") from exc
        scoped = TianChengService(
            root,
            None,
            passthrough_env=self.passthrough_env,
            enable_jobs=False,
            access_policy=AccessPolicy.default(root),
        )
        return scoped, source_relative, destination_relative

    def policy_external_list_dir(self, path: str = ".", depth: int = 1) -> dict[str, Any]:
        # A browse rule exists only to reveal what sits directly under a
        # directory, so it never recurses regardless of the requested depth.
        decision = self.access_policy.explain(path, "list")
        browse_only = decision.mode == "browse"
        if browse_only:
            depth = 1
        scoped, relative = self._policy_scoped_service(path, "list")
        try:
            listing = scoped.list_dir(relative, depth)
        finally:
            scoped.shutdown()
        return {**listing, "policy_mode": decision.mode, "browse_only": browse_only}

    def policy_external_stat(self, path: str) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "read")
        try:
            return scoped.stat(relative)
        finally:
            scoped.shutdown()

    def policy_external_read_text(self, path: str, start_line: int | None = None, end_line: int | None = None, max_bytes: int = DEFAULT_READ_BYTES) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "read")
        try:
            return scoped.read_text(relative, start_line, end_line, max_bytes)
        finally:
            scoped.shutdown()

    def policy_external_read_text_chunk(self, path: str, offset_bytes: int = 0, max_bytes: int = DEFAULT_READ_BYTES) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "read")
        try:
            return scoped.read_text_chunk(relative, offset_bytes, max_bytes)
        finally:
            scoped.shutdown()

    def policy_external_write_text(self, path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "write")
        try:
            return scoped.write_text(relative, content, create_parents, expected_sha256)
        finally:
            scoped.shutdown()

    def policy_external_append_text(self, path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "write")
        try:
            return scoped.append_text(relative, content, create_parents, expected_sha256)
        finally:
            scoped.shutdown()

    def policy_external_mkdir(self, path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "write")
        try:
            return scoped.mkdir(relative, parents, exist_ok)
        finally:
            scoped.shutdown()

    def policy_external_move(self, source: str, destination: str) -> dict[str, Any]:
        scoped, source_relative, destination_relative = self._policy_scoped_pair(source, destination, "write")
        try:
            return scoped.move(source_relative, destination_relative)
        finally:
            scoped.shutdown()

    def policy_external_copy(self, source: str, destination: str) -> dict[str, Any]:
        scoped, source_relative, destination_relative = self._policy_scoped_pair(source, destination, "write")
        try:
            return scoped.copy(source_relative, destination_relative)
        finally:
            scoped.shutdown()

    def policy_external_delete(self, path: str) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(path, "delete")
        try:
            return scoped.delete(relative)
        finally:
            scoped.shutdown()

    def policy_external_glob(self, pattern: str, max_results: int = 200, base_path: str = ".") -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(base_path, "read")
        try:
            return scoped.glob(pattern, max_results, relative)
        finally:
            scoped.shutdown()

    def policy_external_search_text(self, query: str, glob_pattern: str = "**/*", case_sensitive: bool = False, max_results: int = 100, max_scan_bytes: int = 32 * 1024 * 1024, include_hidden: bool = True, timeout_seconds: int = 30, base_path: str = ".") -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(base_path, "read")
        try:
            return scoped.search_text(query, glob_pattern, case_sensitive, max_results, max_scan_bytes, include_hidden, False, True, timeout_seconds, relative)
        finally:
            scoped.shutdown()

    def policy_external_run_command(self, command: str, args: list[str] | None = None, cwd: str = ".", timeout_seconds: int = 60, max_output_bytes: int = DEFAULT_COMMAND_OUTPUT_BYTES) -> dict[str, Any]:
        scoped, relative = self._policy_scoped_service(cwd, "exec")
        try:
            return scoped.run_command(command, args, relative, timeout_seconds, max_output_bytes)
        finally:
            scoped.shutdown()

    def _metadata(self, path: Path) -> dict[str, Any]:
        stat_result = path.stat()
        if path.is_file():
            kind = "file"
            size: int | None = stat_result.st_size
        elif path.is_dir():
            kind = "directory"
            size = None
        else:
            kind = "other"
            size = None
        return {
            "path": self.jail.relative(path),
            "name": path.name or self.jail.root.name,
            "type": kind,
            "size": size,
            "modified_at": _iso_timestamp(stat_result.st_mtime),
            "created_at": _iso_timestamp(stat_result.st_ctime),
        }

    def list_dir(self, path: str = ".", depth: int = 1) -> dict[str, Any]:
        depth = _bounded_int(depth, minimum=1, maximum=MAX_LIST_DEPTH, label="depth")
        base = self.jail.resolve(path, must_exist=True, expect="directory")
        entries: list[dict[str, Any]] = []
        queue: list[tuple[Path, int]] = [(base, 1)]
        truncated = False
        while queue:
            self._check_cancelled()
            current, current_depth = queue.pop(0)
            try:
                # Do not materialize/sort an entire directory before applying
                # the result cap.  A pathological directory must remain
                # interruptible and bounded even when it contains millions of
                # entries.
                children: list[Path] = []
                iterator = current.iterdir()
                for child in iterator:
                    self._check_cancelled()
                    children.append(child)
                    if len(entries) + len(children) >= MAX_LIST_ENTRIES:
                        truncated = True
                        break
            except OSError as exc:
                raise OSError(f"Cannot list workspace directory: {self.jail.relative(current)}") from exc
            for child in children:
                self._check_cancelled()
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    queue.clear()
                    break
                try:
                    checked = self.jail.resolve(
                        self.jail.relative(child), must_exist=True, allow_root=False
                    )
                    metadata = self._metadata(checked)
                    entries.append(metadata)
                    if checked.is_dir() and current_depth < depth:
                        queue.append((checked, current_depth + 1))
                except WorkspaceSecurityError:
                    stat_result = child.lstat()
                    entries.append(
                        {
                            "path": self.jail.relative(child),
                            "name": child.name,
                            "type": "reparse_point",
                            "size": None,
                            "modified_at": _iso_timestamp(stat_result.st_mtime),
                        }
                    )
            if truncated:
                queue.clear()
        return {
            "path": self.jail.relative(base),
            "depth": depth,
            "entries": entries,
            "truncated": truncated,
            "max_entries": MAX_LIST_ENTRIES,
        }

    def stat(self, path: str) -> dict[str, Any]:
        checked = self.jail.resolve(path, must_exist=True)
        return self._metadata(checked)

    def hash_file(self, path: str, max_bytes: int = MAX_HASH_FILE_BYTES) -> dict[str, Any]:
        """Return a bounded SHA-256 digest for a workspace file."""

        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_HASH_FILE_BYTES, label="max_bytes"
        )
        checked = self.jail.resolve(path, must_exist=True, expect="file")
        size = checked.stat().st_size
        if size > maximum:
            raise ValueError(
                f"Hashing is limited to {maximum} bytes; file is {size} bytes"
            )
        digest = hashlib.sha256()
        with checked.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return {
            "path": self.jail.relative(checked),
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "max_bytes": maximum,
        }

    def read_text(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_READ_BYTES, label="max_bytes"
        )
        if start_line is not None:
            start_line = _bounded_int(
                start_line, minimum=1, maximum=10_000_000, label="start_line"
            )
        if end_line is not None:
            end_line = _bounded_int(
                end_line, minimum=1, maximum=10_000_000, label="end_line"
            )
        if end_line is not None and start_line is None:
            start_line = 1
        if start_line is not None and end_line is not None and end_line < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")

        checked = self.jail.resolve(path, must_exist=True, expect="file")
        file_size = checked.stat().st_size
        scan_limit = min(MAX_TEXT_SCAN_BYTES, max(maximum + 4, maximum))
        if start_line is not None or end_line is not None:
            scan_limit = MAX_TEXT_SCAN_BYTES
        with checked.open("rb") as stream:
            data = stream.read(scan_limit + 1)
        scan_truncated = len(data) > scan_limit
        if scan_truncated:
            data = data[:scan_limit]
        encoding = _detect_encoding(data)
        try:
            decoded = _decode_text(data, encoding, allow_incomplete_tail=scan_truncated)
        except UnicodeDecodeError as exc:
            raise ValueError("File is not valid UTF text and was refused") from exc

        selected = decoded
        actual_start = 1
        actual_end: int | None = None
        selection_truncated = False
        if start_line is not None or end_line is not None:
            lines = decoded.splitlines(keepends=True)
            actual_start = start_line or 1
            stop = end_line if end_line is not None else len(lines)
            selected = "".join(lines[actual_start - 1 : stop])
            actual_end = min(stop, len(lines)) if lines else 0
            selection_truncated = actual_start > 1 or stop < len(lines)

        content, output_truncated, returned_bytes = _truncate_utf8(selected, maximum)
        return {
            "path": self.jail.relative(checked),
            "content": content,
            "encoding": encoding,
            "file_size_bytes": file_size,
            "returned_bytes": returned_bytes,
            "start_line": actual_start,
            "end_line": actual_end,
            "truncated": scan_truncated or selection_truncated or output_truncated,
            "scan_truncated": scan_truncated,
            "output_truncated": output_truncated,
            "max_bytes": maximum,
        }

    def read_text_chunk(
        self,
        path: str,
        offset_bytes: int = 0,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        """Read a bounded source-byte chunk and return a stable continuation cursor."""

        offset = _bounded_int(
            offset_bytes, minimum=0, maximum=2**63 - 1, label="offset_bytes"
        )
        maximum = _bounded_int(
            max_bytes, minimum=4, maximum=MAX_READ_BYTES, label="max_bytes"
        )
        checked = self.jail.resolve(path, must_exist=True, expect="file")
        file_size = checked.stat().st_size
        if offset > file_size:
            raise ValueError("offset_bytes is beyond the end of the file")
        with checked.open("rb") as stream:
            sample = stream.read(8192)
            encoding = _detect_encoding(sample)
            if encoding == "utf-16" and offset % 2:
                raise ValueError("UTF-16 offset_bytes must be aligned to a 2-byte boundary")
            stream.seek(offset)
            data = stream.read(maximum)
        at_eof = offset + len(data) >= file_size
        if encoding != "utf-16" and b"\x00" in data:
            raise ValueError("Binary file refused: NUL byte detected")

        decode_encoding = encoding
        bom_bytes = 0
        if encoding == "utf-8-sig" and offset > 0:
            decode_encoding = "utf-8"
        elif encoding == "utf-16":
            if sample.startswith(codecs.BOM_UTF16_BE):
                decode_encoding = "utf-16" if offset == 0 else "utf-16-be"
            else:
                decode_encoding = "utf-16" if offset == 0 else "utf-16-le"
        try:
            content = _decode_text(
                data,
                decode_encoding,
                allow_incomplete_tail=not at_eof,
            )
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Chunk is not valid UTF text; use the previous next_offset_bytes cursor"
            ) from exc

        if offset == 0 and encoding == "utf-8-sig":
            bom_bytes = len(codecs.BOM_UTF8)
            consumed = bom_bytes + len(content.encode("utf-8"))
        elif offset == 0 and encoding == "utf-16":
            bom_bytes = 2
            endian = "utf-16-be" if sample.startswith(codecs.BOM_UTF16_BE) else "utf-16-le"
            consumed = bom_bytes + len(content.encode(endian))
        else:
            consumed = len(content.encode(decode_encoding))
        next_offset = offset + consumed
        if data and next_offset == offset:
            raise ValueError("max_bytes is too small to decode the next text character")
        return {
            "path": self.jail.relative(checked),
            "content": content,
            "encoding": encoding,
            "file_size_bytes": file_size,
            "offset_bytes": offset,
            "returned_source_bytes": consumed,
            "next_offset_bytes": next_offset,
            "eof": next_offset >= file_size,
            "max_bytes": maximum,
        }

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _validate_expected_sha256(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("expected_sha256 must be a 64-character hexadecimal SHA-256")
        return value.casefold()

    def _atomic_replace_bytes(self, target: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".tiancheng-write-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.jail.resolve(self.jail.relative(target), must_exist=False, allow_root=False)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def write_text(
        self,
        path: str,
        content: str,
        create_parents: bool = True,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, str):
            raise TypeError("content must be text")
        target = self.jail.resolve(path, must_exist=False, allow_root=False)
        if target.exists() and not target.is_file():
            raise IsADirectoryError(f"Cannot overwrite a directory: {path}")
        existed = target.exists()
        expected_hash = self._validate_expected_sha256(expected_sha256)
        previous_hash: str | None = None
        if existed:
            previous_hash = self._sha256_bytes(target.read_bytes())
        if expected_hash is not None and previous_hash != expected_hash:
            raise RuntimeError("File changed since it was read; expected_sha256 does not match")
        parent = target.parent
        if not parent.exists():
            if not create_parents:
                raise FileNotFoundError("Parent directory does not exist")
            self.jail.resolve(self.jail.relative(parent), must_exist=False)
            parent.mkdir(parents=True, exist_ok=False)
        self.jail.resolve(self.jail.relative(parent), must_exist=True, expect="directory")
        encoded = content.encode("utf-8")
        self._atomic_replace_bytes(target, encoded)
        return {
            "path": self.jail.relative(target),
            "bytes_written": len(encoded),
            "created": not existed,
            "atomic_replace": True,
            "previous_sha256": previous_hash,
            "sha256": self._sha256_bytes(encoded),
        }

    def edit_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Atomically perform an exact, count-checked text replacement."""

        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be non-empty text")
        if not isinstance(new_text, str):
            raise TypeError("new_text must be text")
        if old_text == new_text:
            raise ValueError("old_text and new_text must differ")
        expected_count = _bounded_int(
            expected_replacements,
            minimum=1,
            maximum=10_000,
            label="expected_replacements",
        )
        checked = self.jail.resolve(path, must_exist=True, expect="file", allow_root=False)
        size = checked.stat().st_size
        if size > MAX_EDIT_FILE_BYTES:
            raise ValueError(f"Exact text editing is limited to {MAX_EDIT_FILE_BYTES} bytes")
        data = checked.read_bytes()
        before_hash = self._sha256_bytes(data)
        expected_hash = self._validate_expected_sha256(expected_sha256)
        if expected_hash is not None and before_hash != expected_hash:
            raise RuntimeError("File changed since it was read; expected_sha256 does not match")
        encoding = _detect_encoding(data)
        try:
            text = _decode_text(data, encoding, allow_incomplete_tail=False)
        except UnicodeDecodeError as exc:
            raise ValueError("File is not valid UTF text and was refused") from exc
        actual_count = text.count(old_text)
        if actual_count != expected_count:
            raise RuntimeError(
                f"Exact replacement refused: expected {expected_count} matches, found {actual_count}"
            )
        updated = text.replace(old_text, new_text)
        if encoding == "utf-16" and data.startswith(codecs.BOM_UTF16_BE):
            encoded = codecs.BOM_UTF16_BE + updated.encode("utf-16-be")
        else:
            encoded = updated.encode(encoding)
        self._atomic_replace_bytes(checked, encoded)
        return {
            "path": self.jail.relative(checked),
            "replacements": actual_count,
            "bytes_written": len(encoded),
            "encoding": encoding,
            "previous_sha256": before_hash,
            "sha256": self._sha256_bytes(encoded),
            "atomic_replace": True,
        }

    def append_text(
        self,
        path: str,
        content: str,
        create_parents: bool = True,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, str):
            raise TypeError("content must be text")
        target = self.jail.resolve(path, must_exist=False, allow_root=False)
        if target.exists() and not target.is_file():
            raise IsADirectoryError(f"Cannot append to a directory: {path}")
        expected_hash = self._validate_expected_sha256(expected_sha256)
        previous_hash: str | None = None
        if target.exists():
            previous_hash = self._sha256_bytes(target.read_bytes())
        if expected_hash is not None and previous_hash != expected_hash:
            raise RuntimeError("File changed since it was read; expected_sha256 does not match")
        if not target.parent.exists():
            if not create_parents:
                raise FileNotFoundError("Parent directory does not exist")
            target.parent.mkdir(parents=True, exist_ok=False)
        self.jail.resolve(self.jail.relative(target.parent), must_exist=True, expect="directory")
        encoded = content.encode("utf-8")
        with target.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        current_hash = self._sha256_bytes(target.read_bytes())
        return {
            "path": self.jail.relative(target),
            "bytes_appended": len(encoded),
            "size": target.stat().st_size,
            "previous_sha256": previous_hash,
            "sha256": current_hash,
        }

    def mkdir(self, path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
        target = self.jail.resolve(path, must_exist=False, allow_root=False)
        existed = target.exists()
        if existed and not target.is_dir():
            raise FileExistsError(f"A file already exists at: {path}")
        target.mkdir(parents=parents, exist_ok=exist_ok)
        checked = self.jail.resolve(path, must_exist=True, expect="directory", allow_root=False)
        return {"path": self.jail.relative(checked), "created": not existed}

    def move(self, source: str, destination: str) -> dict[str, Any]:
        source_path = self.jail.resolve(source, must_exist=True, allow_root=False)
        destination_path = self.jail.resolve(destination, must_exist=False, allow_root=False)
        if destination_path.exists():
            raise FileExistsError("Destination already exists; move never overwrites")
        if source_path.is_dir() and source_path in destination_path.parents:
            raise ValueError("A directory cannot be moved inside itself")
        self.jail.reject_reparse_tree(source_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        self.jail.resolve(
            self.jail.relative(destination_path.parent), must_exist=True, expect="directory"
        )
        moved = Path(shutil.move(str(source_path), str(destination_path)))
        return {
            "source": source,
            "destination": self.jail.relative(moved.resolve(strict=True)),
        }

    def copy(self, source: str, destination: str) -> dict[str, Any]:
        source_path = self.jail.resolve(source, must_exist=True, allow_root=False)
        destination_path = self.jail.resolve(destination, must_exist=False, allow_root=False)
        if destination_path.exists():
            raise FileExistsError("Destination already exists; copy never overwrites")
        if source_path.is_dir() and source_path in destination_path.parents:
            raise ValueError("A directory cannot be copied inside itself")
        self.jail.reject_reparse_tree(source_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        self.jail.resolve(
            self.jail.relative(destination_path.parent), must_exist=True, expect="directory"
        )
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path, symlinks=True)
        else:
            shutil.copy2(source_path, destination_path, follow_symlinks=False)
        return {
            "source": self.jail.relative(source_path),
            "destination": self.jail.relative(destination_path),
            "type": "directory" if destination_path.is_dir() else "file",
        }

    def delete(self, path: str) -> dict[str, Any]:
        target = self.jail.resolve(path, must_exist=True, allow_root=False)
        trash = self.jail.resolve(".tiancheng-trash", must_exist=False, allow_root=False)
        trash.mkdir(parents=True, exist_ok=True)
        if target == trash:
            raise WorkspaceSecurityError("The trash root cannot be deleted through this tool")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = trash / f"{timestamp}-{uuid.uuid4().hex[:8]}-{target.name}"
        destination = self.jail.resolve(
            self.jail.relative(destination), must_exist=False, allow_root=False
        )
        os.replace(target, destination)
        metadata_directory = self._trash_metadata_directory(trash, create=True)
        metadata_path = metadata_directory / f"{destination.name}.json"
        metadata = {
            "original_path": self.jail.relative(target),
            "trash_path": self.jail.relative(destination),
            "deleted_at": datetime.now(UTC).isoformat(),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return {
            "original_path": metadata["original_path"],
            "trash_path": self.jail.relative(destination),
            "permanently_deleted": False,
        }

    def _trash_root(self) -> Path:
        trash = self.jail.resolve(".tiancheng-trash", must_exist=False, allow_root=False)
        trash.mkdir(parents=True, exist_ok=True)
        return self.jail.resolve(
            ".tiancheng-trash", must_exist=True, expect="directory", allow_root=False
        )

    def _trash_metadata_directory(self, trash: Path, *, create: bool) -> Path:
        label = f"{self.jail.relative(trash)}/.metadata"
        metadata = self.jail.resolve(label, must_exist=False, allow_root=False)
        if not metadata.exists():
            if not create:
                return metadata
            metadata.mkdir()
        return self.jail.resolve(
            label, must_exist=True, expect="directory", allow_root=False
        )

    def _trash_item(self, trash_path: str) -> tuple[Path, Path]:
        trash = self._trash_root()
        item = self.jail.resolve(trash_path, must_exist=True, allow_root=False)
        if item.parent != trash or item.name == ".metadata":
            raise WorkspaceSecurityError("trash_path must name one direct trash item")
        return trash, item

    def _read_trash_metadata(self, trash: Path, item_name: str) -> dict[str, Any] | None:
        metadata_directory = self._trash_metadata_directory(trash, create=False)
        metadata_path = metadata_directory / f"{item_name}.json"
        if not metadata_path.is_file():
            return None
        metadata_path = self.jail.resolve(
            self.jail.relative(metadata_path), must_exist=True, expect="file", allow_root=False
        )
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def trash_list(self, max_results: int = 200) -> dict[str, Any]:
        maximum = _bounded_int(
            max_results, minimum=1, maximum=MAX_GLOB_RESULTS, label="max_results"
        )
        trash = self._trash_root()
        items: list[dict[str, Any]] = []
        candidates = sorted(
            (entry for entry in trash.iterdir() if entry.name != ".metadata"),
            key=lambda entry: entry.name,
            reverse=True,
        )
        truncated = len(candidates) > maximum
        for entry in candidates[:maximum]:
            try:
                checked = self.jail.resolve(
                    self.jail.relative(entry), must_exist=True, allow_root=False
                )
                metadata = self._read_trash_metadata(trash, checked.name) or {}
                items.append(
                    {
                        **self._metadata(checked),
                        "original_path": metadata.get("original_path"),
                        "deleted_at": metadata.get("deleted_at"),
                    }
                )
            except WorkspaceSecurityError:
                items.append(
                    {
                        "path": self.jail.relative(entry),
                        "name": entry.name,
                        "type": "reparse_point",
                        "original_path": None,
                        "deleted_at": None,
                    }
                )
        return {
            "items": items,
            "count": len(items),
            "truncated": truncated,
            "max_results": maximum,
        }

    def trash_restore(
        self, trash_path: str, destination: str | None = None
    ) -> dict[str, Any]:
        trash, item = self._trash_item(trash_path)
        metadata = self._read_trash_metadata(trash, item.name) or {}
        restore_label = destination or metadata.get("original_path")
        if not isinstance(restore_label, str) or not restore_label:
            raise ValueError("destination is required when trash metadata is unavailable")
        target = self.jail.resolve(restore_label, must_exist=False, allow_root=False)
        if target.exists():
            raise FileExistsError("Restore destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.jail.resolve(
            self.jail.relative(target.parent), must_exist=True, expect="directory"
        )
        self.jail.reject_reparse_tree(item)
        os.replace(item, target)
        metadata_path = trash / ".metadata" / f"{item.name}.json"
        metadata_path.unlink(missing_ok=True)
        return {
            "trash_path": trash_path,
            "restored_path": self.jail.relative(target),
            "restored": True,
        }

    def trash_purge(self, trash_path: str | None = None) -> dict[str, Any]:
        trash = self._trash_root()
        if trash_path is None:
            items = [entry for entry in trash.iterdir() if entry.name != ".metadata"]
        else:
            _trash, item = self._trash_item(trash_path)
            items = [item]
        purged: list[str] = []
        for item in items:
            checked = self.jail.resolve(
                self.jail.relative(item), must_exist=True, allow_root=False
            )
            self.jail.reject_reparse_tree(checked)
            label = self.jail.relative(checked)
            if checked.is_dir():
                shutil.rmtree(checked)
            else:
                checked.unlink()
            (trash / ".metadata" / f"{item.name}.json").unlink(missing_ok=True)
            purged.append(label)
        metadata_directory = trash / ".metadata"
        if metadata_directory.is_dir() and not any(metadata_directory.iterdir()):
            metadata_directory.rmdir()
        return {"purged": purged, "count": len(purged), "permanently_deleted": True}

    def glob(self, pattern: str, max_results: int = 200, base_path: str = ".") -> dict[str, Any]:
        maximum = _bounded_int(
            max_results, minimum=1, maximum=MAX_GLOB_RESULTS, label="max_results"
        )
        matcher = compile_glob(pattern)
        base = self.jail.resolve(base_path, must_exist=True, expect="directory")
        results: list[dict[str, Any]] = []
        truncated = False
        scanned_entries = 0
        for current, directories, files in os.walk(base, followlinks=False):
            self._check_cancelled()
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                self._check_cancelled()
                child = current_path / name
                try:
                    self.jail.resolve(self.jail.relative(child), must_exist=True)
                    safe_directories.append(name)
                except WorkspaceSecurityError:
                    continue
            directories[:] = safe_directories
            for name in [*safe_directories, *files]:
                self._check_cancelled()
                scanned_entries += 1
                if scanned_entries > MAX_GLOB_SCANNED_ENTRIES:
                    truncated = True
                    break
                child = current_path / name
                try:
                    checked = self.jail.resolve(self.jail.relative(child), must_exist=True)
                except WorkspaceSecurityError:
                    continue
                relative = self.jail.relative(checked)
                match_relative = checked.relative_to(base).as_posix()
                if matcher.fullmatch(match_relative):
                    if len(results) >= maximum:
                        truncated = True
                        break
                    results.append(
                        {
                            "path": relative,
                            "type": "directory" if checked.is_dir() else "file",
                        }
                    )
            if truncated:
                break
        return {
            "pattern": pattern,
            "base_path": self.jail.relative(base),
            "results": results,
            "truncated": truncated,
            "max_results": maximum,
            "scanned_entries": scanned_entries,
            "max_scanned_entries": MAX_GLOB_SCANNED_ENTRIES,
        }

    def search_text(
        self,
        query: str,
        glob_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = 100,
        max_scan_bytes: int = 32 * 1024 * 1024,
        include_hidden: bool = True,
        respect_gitignore: bool = True,
        include_internal: bool = False,
        timeout_seconds: int = 30,
        base_path: str = ".",
    ) -> dict[str, Any]:
        self._check_cancelled()
        if not isinstance(query, str) or not query or "\x00" in query:
            raise ValueError("query must be non-empty text without NUL bytes")
        if "\r" in query or "\n" in query:
            raise ValueError("query must be a single-line literal")
        maximum = _bounded_int(
            max_results, minimum=1, maximum=MAX_SEARCH_RESULTS, label="max_results"
        )
        scan_limit = _bounded_int(
            max_scan_bytes,
            minimum=1,
            maximum=MAX_SEARCH_SCAN_BYTES,
            label="max_scan_bytes",
        )
        timeout = _bounded_int(
            timeout_seconds, minimum=1, maximum=120, label="timeout_seconds"
        )
        compile_glob(glob_pattern)
        base = self.jail.resolve(base_path, must_exist=True, expect="directory")
        if self.rg_executable:
            return self._search_text_rg(
                query,
                glob_pattern,
                case_sensitive,
                maximum,
                scan_limit,
                include_hidden,
                respect_gitignore,
                include_internal,
                timeout,
                base,
                self._cancel_event(),
            )
        result = self._search_text_python(
            query, glob_pattern, case_sensitive, maximum, scan_limit, base,
            self._cancel_event(),
        )
        result.update(
            {
                "engine": "python-fallback",
                "include_hidden": include_hidden,
                "respect_gitignore": False,
                "include_internal": include_internal,
                "base_path": self.jail.relative(base),
            }
        )
        return result

    def _search_text_rg(
        self,
        query: str,
        glob_pattern: str,
        case_sensitive: bool,
        maximum: int,
        scan_limit: int,
        include_hidden: bool,
        respect_gitignore: bool,
        include_internal: bool,
        timeout_seconds: int,
        base: Path,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        assert self.rg_executable is not None
        per_file_limit = min(MAX_SEARCH_FILE_BYTES, scan_limit)
        matcher = compile_glob(glob_pattern)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {
            "SystemRoot": system_root,
            "WINDIR": os.environ.get("WINDIR", system_root),
            "PATH": os.pathsep.join(
                (str(Path(self.rg_executable).parent), str(Path(system_root) / "System32"))
            ),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "NO_COLOR": "1",
        }

        # Enumerate only requested files first. This makes max_scan_bytes an
        # aggregate budget and prevents unrelated files from consuming rg's
        # bounded output before the requested glob is reached.
        file_arguments = [
            self.rg_executable,
            "--files",
            "--color",
            "never",
            "--sort",
            "path",
        ]
        normalized_glob = glob_pattern.replace("\\", "/")
        # ripgrep's --glob intentionally overrides ignore rules. Preserve
        # .gitignore semantics by applying the user glob in Python whenever
        # ignore handling is enabled; with respect_gitignore=false it is safe
        # and faster to let ripgrep prune the candidate list directly.
        if not respect_gitignore:
            file_arguments.extend(("--glob", normalized_glob))
        if include_hidden:
            file_arguments.append("--hidden")
        file_arguments.append("--no-require-git" if respect_gitignore else "--no-ignore")
        if not include_internal:
            for exclusion in _DEFAULT_SEARCH_EXCLUDES:
                file_arguments.extend(("--glob", exclusion))
        file_arguments.append(self.jail.relative(base))
        started = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("Job cancelled")
        file_list = _run_process_bounded(
            file_arguments,
            cwd=self.jail.root,
            env=environment,
            timeout_seconds=timeout_seconds,
            maximum_output=max(MAX_COMMAND_OUTPUT_BYTES, min(scan_limit, 8 * 1024 * 1024)),
            cancel_event=cancel_event,
        )
        if file_list["cancelled"]:
            raise JobCancelled("Job cancelled during ripgrep file enumeration")
        if file_list["timeout"]:
            raise TimeoutError("ripgrep file enumeration timed out")
        if file_list["exit_code"] not in {0, 1}:
            raise RuntimeError(f"ripgrep file enumeration failed: {file_list['stderr'].strip()}")

        candidates: list[str] = []
        scanned_bytes = 0
        scanned_files = 0
        scan_truncated = file_list["stdout_truncated"]
        for raw_path in file_list["stdout"].splitlines():
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Job cancelled during ripgrep candidate enumeration")
            raw_path = raw_path.strip()
            if not raw_path:
                continue
            relative_label = raw_path.replace("\\", "/")
            if relative_label.startswith("./"):
                relative_label = relative_label[2:]
            try:
                checked = self.jail.resolve(relative_label, must_exist=True, expect="file")
            except (OSError, WorkspaceSecurityError):
                continue
            checked_label = self.jail.relative(checked)
            match_relative = checked.relative_to(base).as_posix()
            if not matcher.fullmatch(match_relative):
                continue
            if scanned_files >= MAX_SEARCH_SCANNED_FILES:
                scan_truncated = True
                break
            size = checked.stat().st_size
            if size > per_file_limit:
                continue
            if scanned_bytes + size > scan_limit:
                scan_truncated = True
                break
            candidates.append(checked_label)
            scanned_files += 1
            scanned_bytes += size

        matches: list[dict[str, Any]] = []
        invalid_records = 0
        output_truncated = file_list["stdout_truncated"] or file_list["stderr_truncated"]
        batch_size = 128
        for offset in range(0, len(candidates), batch_size):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Job cancelled during ripgrep search")
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("ripgrep search timed out")
            arguments = [
                self.rg_executable,
                "--json",
                "--fixed-strings",
                "--line-number",
                "--sort",
                "path",
                "--color",
                "never",
            ]
            if not case_sensitive:
                arguments.append("--ignore-case")
            arguments.extend(("--", query, *candidates[offset : offset + batch_size]))
            result = _run_process_bounded(
                arguments,
                cwd=self.jail.root,
                env=environment,
                timeout_seconds=remaining,
                maximum_output=MAX_COMMAND_OUTPUT_BYTES,
                cancel_event=cancel_event,
            )
            if result["cancelled"]:
                raise JobCancelled("Job cancelled during ripgrep search")
            if result["timeout"]:
                raise TimeoutError("ripgrep search timed out")
            if result["exit_code"] not in {0, 1}:
                raise RuntimeError(f"ripgrep search failed: {result['stderr'].strip()}")
            output_truncated = output_truncated or result["stdout_truncated"] or result["stderr_truncated"]
            for raw_line in result["stdout"].splitlines():
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    invalid_records += 1
                    continue
                if record.get("type") != "match":
                    continue
                data = record.get("data", {})
                raw_path = data.get("path", {}).get("text")
                raw_context = data.get("lines", {}).get("text")
                line_number = data.get("line_number")
                if not isinstance(raw_path, str) or not isinstance(raw_context, str):
                    continue
                relative_label = raw_path.replace("\\", "/")
                if relative_label.startswith("./"):
                    relative_label = relative_label[2:]
                try:
                    checked = self.jail.resolve(relative_label, must_exist=True, expect="file")
                except (OSError, WorkspaceSecurityError):
                    continue
                checked_label = self.jail.relative(checked)
                match_relative = checked.relative_to(base).as_posix()
                if not matcher.fullmatch(match_relative):
                    continue
                context = raw_context.rstrip("\r\n").strip()
                if len(context) > 300:
                    context = context[:297] + "..."
                matches.append(
                    {
                        "path": checked_label,
                        "line": int(line_number),
                        "context": context,
                    }
                )
                if len(matches) >= maximum:
                    break
            if len(matches) >= maximum:
                break
        truncated = (
            len(matches) >= maximum
            or scan_truncated
            or output_truncated
            or invalid_records > 0
        )
        return {
            "query": query,
            "glob_pattern": glob_pattern,
            "base_path": self.jail.relative(base),
            "case_sensitive": case_sensitive,
            "results": matches,
            "truncated": truncated,
            "engine": "ripgrep",
            "include_hidden": include_hidden,
            "respect_gitignore": respect_gitignore,
            "include_internal": include_internal,
            "max_results": maximum,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "max_scan_bytes": scan_limit,
            "max_file_bytes": per_file_limit,
            "timeout_seconds": timeout_seconds,
            "output_truncated": output_truncated,
        }

    def _search_text_python(
        self,
        query: str,
        glob_pattern: str,
        case_sensitive: bool,
        maximum: int,
        scan_limit: int,
        base: Path,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be non-empty text")
        matcher = compile_glob(glob_pattern)
        needle = query if case_sensitive else query.casefold()
        results: list[dict[str, Any]] = []
        scanned_bytes = 0
        scanned_files = 0
        examined_files = 0
        skipped_binary = 0
        truncated = False
        for current, directories, files in os.walk(base, followlinks=False):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Job cancelled during text search")
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelled("Job cancelled during text search")
                if self._safe_walk_child(current_path / name):
                    safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelled("Job cancelled during text search")
                examined_files += 1
                if examined_files > MAX_SEARCH_SCANNED_FILES:
                    truncated = True
                    break
                child = current_path / name
                try:
                    checked = self.jail.resolve(self.jail.relative(child), must_exist=True)
                except WorkspaceSecurityError:
                    continue
                relative = self.jail.relative(checked)
                match_relative = checked.relative_to(base).as_posix()
                if not matcher.fullmatch(match_relative):
                    continue
                size = checked.stat().st_size
                if size > MAX_SEARCH_FILE_BYTES or scanned_bytes + size > scan_limit:
                    if scanned_bytes + size > scan_limit:
                        truncated = True
                        break
                    continue
                data = checked.read_bytes()
                scanned_bytes += len(data)
                scanned_files += 1
                try:
                    encoding = _detect_encoding(data[:8192])
                    text = _decode_text(data, encoding, allow_incomplete_tail=False)
                except (UnicodeDecodeError, ValueError):
                    skipped_binary += 1
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle in haystack:
                        context = line.strip()
                        if len(context) > 300:
                            context = context[:297] + "..."
                        results.append(
                            {"path": relative, "line": line_number, "context": context}
                        )
                        if len(results) >= maximum:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        return {
            "query": query,
            "glob_pattern": glob_pattern,
            "base_path": self.jail.relative(base),
            "case_sensitive": case_sensitive,
            "results": results,
            "truncated": truncated,
            "scanned_files": scanned_files,
            "examined_files": examined_files,
            "scanned_bytes": scanned_bytes,
            "skipped_binary_files": skipped_binary,
            "max_results": maximum,
            "max_scan_bytes": scan_limit,
            "max_file_bytes": min(MAX_SEARCH_FILE_BYTES, scan_limit),
            "max_scanned_files": MAX_SEARCH_SCANNED_FILES,
        }

    def _safe_walk_child(self, path: Path) -> bool:
        try:
            self.jail.resolve(self.jail.relative(path), must_exist=True, expect="directory")
            return True
        except (OSError, WorkspaceSecurityError):
            return False

    def _git_environment(self) -> dict[str, str]:
        if not self.git_executable:
            raise RuntimeError("Git is not available")
        git_command_directory = Path(self.git_executable).resolve().parent
        git_root = git_command_directory.parent
        executable_directories = [
            git_command_directory,
            git_root / "mingw64/bin",
            git_root / "usr/bin",
        ]
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {
            "SystemRoot": system_root,
            "WINDIR": os.environ.get("WINDIR", system_root),
            "ComSpec": os.environ.get("ComSpec", str(Path(system_root) / "System32/cmd.exe")),
            "PATH": os.pathsep.join(
                str(path) for path in [*executable_directories, Path(system_root) / "System32"]
                if path.is_dir()
            ),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C.UTF-8",
        }
        for name in _SAFE_USER_ENVIRONMENT_NAMES:
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    def _find_repo(self, repo: str) -> tuple[Path, Path]:
        start = self.jail.resolve(repo, must_exist=True, expect="directory")
        current = start
        while True:
            marker = current / ".git"
            if os.path.lexists(marker):
                if not marker.is_dir():
                    raise WorkspaceSecurityError(
                        "Only standalone repositories with a real .git directory are supported"
                    )
                checked_marker = self.jail.resolve(
                    self.jail.relative(marker), must_exist=True, expect="directory"
                )
                self._validate_git_directory(checked_marker)
                return current, checked_marker
            if current == self.jail.root:
                break
            current = current.parent
        raise ValueError("No local Git repository found at or above the requested path")

    def _validate_git_directory(self, git_directory: Path) -> None:
        self.jail.reject_reparse_tree(git_directory)
        alternates = git_directory / "objects/info/alternates"
        if alternates.exists():
            raise WorkspaceSecurityError("Git object alternates are not allowed")
        config = git_directory / "config"
        if not config.exists():
            return
        raw = config.read_text(encoding="utf-8", errors="strict")
        lowered = raw.casefold()
        if re.search(
            r"(?im)^\s*\[\s*(?:include(?:if)?|filter|credential|url\b|alias|gpg|diff\b)",
            lowered,
        ):
            raise WorkspaceSecurityError("Repository config contains an unsafe Git section")
        if re.search(
            r"(?im)^\s*(?:worktree|worktreeconfig|hookspath|excludesfile|attributesfile|"
            r"sshcommand|fsmonitor|textconv|external|signingkey|template)\s*=",
            lowered,
        ) or re.search(r"(?im)^\s*gpgsign\s*=\s*true\s*$", lowered):
            raise WorkspaceSecurityError("Repository config contains an unsafe Git setting")

    def _git(
        self,
        repo_path: Path,
        arguments: Sequence[str],
        *,
        max_output: int = DEFAULT_GIT_OUTPUT_BYTES,
        timeout_seconds: int = 30,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.git_executable:
            raise RuntimeError("Git is not available")
        environment = self._git_environment()
        if extra_env:
            environment.update(extra_env)
        result = _run_process_bounded(
            [self.git_executable, "-C", str(repo_path), *arguments],
            cwd=repo_path,
            env=environment,
            timeout_seconds=timeout_seconds,
            maximum_output=max_output,
            cancel_event=self._cancel_event(),
        )
        if result["cancelled"]:
            raise JobCancelled("Git command cancelled")
        if result["timeout"]:
            raise TimeoutError("Git command timed out")
        return result

    @staticmethod
    def _git_error(result: dict[str, Any]) -> str:
        return _redact_git_text((result.get("stderr", "") or "").strip()) or "unknown Git error"

    def git_init(self, path: str = ".") -> dict[str, Any]:
        target = self.jail.resolve(path, must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        self.jail.resolve(path, must_exist=True, expect="directory")
        if os.path.lexists(target / ".git"):
            raise FileExistsError("A Git repository already exists at this path")
        result = self._git(target, ["init", "-b", "main", "."])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git init failed: {self._git_error(result)}")
        return {"repository": self.jail.relative(target), "initialized": True}

    def git_status(self, repo: str = ".") -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        result = self._git(repo_path, ["status", "--short", "--branch", "--untracked-files=all"])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git status failed: {self._git_error(result)}")
        return {
            "repository": self.jail.relative(repo_path),
            "status": result["stdout"],
            "truncated": result["stdout_truncated"],
        }

    def _repo_relative_path(self, repo_path: Path, value: str) -> str:
        repo_label = self.jail.relative(repo_path)
        combined = value if repo_label == "." else f"{repo_label}/{value}"
        checked = self.jail.resolve(combined, must_exist=False)
        try:
            relative = checked.relative_to(repo_path)
        except ValueError as exc:
            raise WorkspaceSecurityError("Git file path must be inside the repository") from exc
        return relative.as_posix() or "."

    def git_diff(
        self,
        repo: str = ".",
        staged: bool = False,
        path: str | None = None,
        max_bytes: int = DEFAULT_GIT_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_COMMAND_OUTPUT_BYTES, label="max_bytes"
        )
        repo_path, _ = self._find_repo(repo)
        arguments = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            arguments.append("--cached")
        if path is not None:
            arguments.extend(("--", self._repo_relative_path(repo_path, path)))
        result = self._git(repo_path, arguments, max_output=maximum)
        if result["exit_code"] != 0:
            raise RuntimeError(f"git diff failed: {self._git_error(result)}")
        return {
            "repository": self.jail.relative(repo_path),
            "staged": staged,
            "diff": result["stdout"],
            "truncated": result["stdout_truncated"],
            "max_bytes": maximum,
        }

    def git_log(self, repo: str = ".", limit: int = 20) -> dict[str, Any]:
        count = _bounded_int(limit, minimum=1, maximum=100, label="limit")
        repo_path, _ = self._find_repo(repo)
        result = self._git(
            repo_path,
            ["log", f"-{count}", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s"],
        )
        if result["exit_code"] not in {0, 128}:
            raise RuntimeError(f"git log failed: {self._git_error(result)}")
        commits = []
        for line in result["stdout"].splitlines():
            fields = line.split("\x1f", 4)
            if len(fields) == 5:
                commits.append(
                    {
                        "hash": fields[0],
                        "short_hash": fields[1],
                        "author": fields[2],
                        "authored_at": fields[3],
                        "subject": fields[4],
                    }
                )
        return {"repository": self.jail.relative(repo_path), "commits": commits}

    def git_add(self, paths: list[str], repo: str = ".") -> dict[str, Any]:
        if not paths or len(paths) > 200:
            raise ValueError("paths must contain between 1 and 200 entries")
        repo_path, _ = self._find_repo(repo)
        relative_paths = [self._repo_relative_path(repo_path, value) for value in paths]
        result = self._git(repo_path, ["add", "--", *relative_paths])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git add failed: {self._git_error(result)}")
        return {"repository": self.jail.relative(repo_path), "added": relative_paths}

    def git_commit(
        self,
        message: str,
        repo: str = ".",
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> dict[str, Any]:
        for label, value, maximum in (("message", message, 10_000),):
            if not isinstance(value, str) or not value or "\x00" in value or len(value) > maximum:
                raise ValueError(f"{label} is invalid")
        if (author_name is None) != (author_email is None):
            raise ValueError("author_name and author_email must be provided together")
        if author_name is not None and author_email is not None:
            for label, value, maximum in (
                ("author_name", author_name, 200),
                ("author_email", author_email, 320),
            ):
                if not value or "\x00" in value or len(value) > maximum:
                    raise ValueError(f"{label} is invalid")
                if "\n" in value or "\r" in value:
                    raise ValueError("Git author identity cannot contain line breaks")
        repo_path, _ = self._find_repo(repo)
        identity_source = "explicit"
        identity: dict[str, str] = {}
        if author_name is None:
            configured_name = self._git(repo_path, ["config", "--get", "user.name"], max_output=4096)
            configured_email = self._git(repo_path, ["config", "--get", "user.email"], max_output=4096)
            if configured_name["exit_code"] == 0 and configured_email["exit_code"] == 0:
                identity_source = "git-config"
            else:
                author_name = "TianCheng MCP"
                author_email = "tiancheng@local.invalid"
                identity_source = "fallback"
        if author_name is not None and author_email is not None:
            identity = {
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": author_name,
                "GIT_COMMITTER_EMAIL": author_email,
            }
        result = self._git(
            repo_path,
            ["-c", "core.hooksPath=NUL", "-c", "commit.gpgSign=false", "commit", "-m", message],
            extra_env=identity,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"git commit failed: {self._git_error(result)}")
        verify = self._git(repo_path, ["rev-parse", "HEAD"], max_output=4096)
        if verify["exit_code"] != 0:
            raise RuntimeError("Commit succeeded but HEAD could not be verified")
        return {
            "repository": self.jail.relative(repo_path),
            "commit": verify["stdout"].strip(),
            "summary": result["stdout"].strip(),
            "identity_source": identity_source,
        }

    @staticmethod
    def _validate_remote_name(name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("Remote name must use letters, digits, dot, underscore, or dash")
        return name

    def _prepare_remote_url(self, value: str, *, base: Path) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 4096
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("Remote URL must be bounded single-line text")
        raw = value.strip()
        windows = PureWindowsPath(raw.replace("/", "\\"))
        if windows.drive or windows.root:
            raise WorkspaceSecurityError(
                "Local Git remotes must use a workspace-relative path"
            )
        parsed = urlsplit(raw)
        if parsed.scheme:
            scheme = parsed.scheme.casefold()
            if scheme not in {"https", "ssh", "git"}:
                raise ValueError("Remote URL scheme must be https, ssh, or git")
            if not parsed.hostname:
                raise ValueError("Remote URL must include a host")
            if parsed.password or (scheme == "https" and parsed.username):
                raise ValueError("Remote URLs cannot embed credentials")
            return raw
        scp = re.fullmatch(
            r"(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)",
            raw,
        )
        if scp:
            return raw
        base_label = self.jail.relative(base)
        combined = raw if base_label == "." else f"{base_label}/{raw}"
        local = self.jail.resolve(combined, must_exist=True)
        return str(local)

    @staticmethod
    def _display_remote_url(value: str) -> str:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return value
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))

    @staticmethod
    def _git_command_result(repo: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "repository": repo,
            "exit_code": result["exit_code"],
            "output": _redact_git_text(
                result["stdout"].strip() or result["stderr"].strip()
            ),
            "stdout_truncated": result["stdout_truncated"],
            "stderr_truncated": result["stderr_truncated"],
        }

    def git_remote_list(self, repo: str = ".") -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        names_result = self._git(repo_path, ["remote"])
        if names_result["exit_code"] != 0:
            raise RuntimeError(f"git remote failed: {self._git_error(names_result)}")
        remotes = []
        for name in names_result["stdout"].splitlines():
            name = name.strip()
            if not name:
                continue
            fetch = self._git(repo_path, ["remote", "get-url", "--all", name])
            push = self._git(repo_path, ["remote", "get-url", "--push", "--all", name])
            remotes.append(
                {
                    "name": name,
                    "fetch_urls": [
                        self._display_remote_url(url)
                        for url in fetch["stdout"].splitlines()
                        if url.strip()
                    ],
                    "push_urls": [
                        self._display_remote_url(url)
                        for url in push["stdout"].splitlines()
                        if url.strip()
                    ],
                }
            )
        return {"repository": self.jail.relative(repo_path), "remotes": remotes}

    def git_remote_add(self, name: str, url: str, repo: str = ".") -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        remote = self._validate_remote_name(name)
        checked_url = self._prepare_remote_url(url, base=repo_path)
        result = self._git(repo_path, ["remote", "add", remote, checked_url])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git remote add failed: {self._git_error(result)}")
        return {
            "repository": self.jail.relative(repo_path),
            "remote": remote,
            "url": self._display_remote_url(checked_url),
        }

    def git_remote_set_url(self, name: str, url: str, repo: str = ".") -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        remote = self._validate_remote_name(name)
        checked_url = self._prepare_remote_url(url, base=repo_path)
        result = self._git(repo_path, ["remote", "set-url", remote, checked_url])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git remote set-url failed: {self._git_error(result)}")
        return {
            "repository": self.jail.relative(repo_path),
            "remote": remote,
            "url": self._display_remote_url(checked_url),
        }

    def git_remote_remove(self, name: str, repo: str = ".") -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        remote = self._validate_remote_name(name)
        result = self._git(repo_path, ["remote", "remove", remote])
        if result["exit_code"] != 0:
            raise RuntimeError(f"git remote remove failed: {self._git_error(result)}")
        return {"repository": self.jail.relative(repo_path), "removed": remote}

    def git_clone(self, url: str, destination: str) -> dict[str, Any]:
        target = self.jail.resolve(destination, must_exist=False, allow_root=False)
        if target.exists():
            raise FileExistsError("Clone destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = self.jail.resolve(
            self.jail.relative(target.parent), must_exist=True, expect="directory"
        )
        checked_url = self._prepare_remote_url(url, base=self.jail.root)
        result = self._git(
            parent,
            ["clone", "--no-recurse-submodules", "--", checked_url, target.name],
            timeout_seconds=300,
            max_output=MAX_COMMAND_OUTPUT_BYTES,
        )
        if result["exit_code"] != 0:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {self._git_error(result)}")
        self._find_repo(self.jail.relative(target))
        return {
            "repository": self.jail.relative(target),
            "url": self._display_remote_url(checked_url),
            "output": (result["stdout"].strip() or result["stderr"].strip()),
        }

    def git_fetch(
        self,
        repo: str = ".",
        remote: str = "origin",
        prune: bool = True,
        tags: bool = False,
    ) -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        remote_name = self._validate_remote_name(remote)
        arguments = ["fetch"]
        if prune:
            arguments.append("--prune")
        if tags:
            arguments.append("--tags")
        arguments.extend(("--", remote_name))
        result = self._git(
            repo_path,
            arguments,
            timeout_seconds=300,
            max_output=MAX_COMMAND_OUTPUT_BYTES,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"git fetch failed: {self._git_error(result)}")
        return self._git_command_result(self.jail.relative(repo_path), result)

    def git_pull(
        self,
        repo: str = ".",
        remote: str = "origin",
        branch: str | None = None,
        strategy: str = "ff-only",
    ) -> dict[str, Any]:
        if strategy not in {"ff-only", "rebase", "merge"}:
            raise ValueError("strategy must be ff-only, rebase, or merge")
        repo_path, _ = self._find_repo(repo)
        remote_name = self._validate_remote_name(remote)
        arguments = [
            "-c",
            "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
            "pull",
            {"ff-only": "--ff-only", "rebase": "--rebase", "merge": "--no-rebase"}[strategy],
            "--",
            remote_name,
        ]
        if branch:
            arguments.append(branch)
        result = self._git(
            repo_path,
            arguments,
            timeout_seconds=300,
            max_output=MAX_COMMAND_OUTPUT_BYTES,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"git pull failed: {self._git_error(result)}")
        response = self._git_command_result(self.jail.relative(repo_path), result)
        response.update({"remote": remote_name, "branch": branch, "strategy": strategy})
        return response

    def git_push(
        self,
        repo: str = ".",
        remote: str = "origin",
        branch: str | None = None,
        set_upstream: bool = False,
        force_with_lease: bool = False,
        tags: bool = False,
    ) -> dict[str, Any]:
        repo_path, _ = self._find_repo(repo)
        remote_name = self._validate_remote_name(remote)
        arguments = ["push"]
        if set_upstream:
            arguments.append("--set-upstream")
        if force_with_lease:
            arguments.append("--force-with-lease")
        if tags:
            arguments.append("--tags")
        arguments.extend(("--", remote_name))
        if branch:
            arguments.append(branch)
        result = self._git(
            repo_path,
            arguments,
            timeout_seconds=300,
            max_output=MAX_COMMAND_OUTPUT_BYTES,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"git push failed: {self._git_error(result)}")
        response = self._git_command_result(self.jail.relative(repo_path), result)
        response.update(
            {
                "remote": remote_name,
                "branch": branch,
                "set_upstream": set_upstream,
                "force_with_lease": force_with_lease,
                "tags": tags,
            }
        )
        return response

    def _discover_exec_commands(self) -> dict[str, list[str]]:
        discovered: dict[str, list[str]] = {
            "python": [sys.executable],
            "pytest": [sys.executable, "-m", "pytest"],
        }
        for name in ("py", "uv", "git", "gh", "node", "rg"):
            executable = shutil.which(name)
            if executable:
                discovered[name] = [str(Path(executable).resolve())]
        node = discovered.get("node")
        if node:
            for name, script_name in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
                command_file = shutil.which(name)
                if not command_file:
                    continue
                candidate = Path(command_file).resolve().parent / "node_modules/npm/bin" / script_name
                if candidate.is_file():
                    discovered[name] = [*node, str(candidate)]
        codex_executable = shutil.which("codex.exe") or shutil.which("codex")
        if codex_executable:
            codex_path = Path(codex_executable).resolve()
            if codex_path.suffix.casefold() in {".cmd", ".ps1"}:
                codex_script = codex_path.parent / "node_modules/@openai/codex/bin/codex.js"
                node = discovered.get("node")
                if node and codex_script.is_file():
                    discovered["codex"] = [*node, str(codex_script)]
            else:
                discovered["codex"] = [str(codex_path)]
        for command in discovered.values():
            executable = Path(command[0]).resolve()
            try:
                executable.relative_to(self.jail.root)
            except ValueError:
                continue
            raise WorkspaceSecurityError("Refusing an allowlisted executable from inside the workspace")
        return discovered

    def _discover_agent_only_commands(self) -> dict[str, list[str]]:
        discovered: dict[str, list[str]] = {}
        claude_executable = shutil.which("claude.exe") or shutil.which("claude")
        if claude_executable:
            claude_path = Path(claude_executable).resolve()
            if claude_path.suffix.casefold() not in {".cmd", ".ps1"}:
                discovered["claude"] = [str(claude_path)]
        for command in discovered.values():
            executable = Path(command[0]).resolve()
            try:
                executable.relative_to(self.jail.root)
            except ValueError:
                continue
            raise WorkspaceSecurityError(
                "Refusing an agent executable from inside the workspace"
            )
        return discovered

    def _execution_environment(
        self, *, include_passthrough_env: bool = True
    ) -> dict[str, str]:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        executable_directories = {
            str(Path(command[0]).resolve().parent) for command in self._exec_commands.values()
        }
        temporary = self.jail.root / ".tiancheng-tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        environment = {
            "SystemRoot": system_root,
            "WINDIR": os.environ.get("WINDIR", system_root),
            "ComSpec": os.environ.get("ComSpec", str(Path(system_root) / "System32/cmd.exe")),
            "PATH": os.pathsep.join(sorted(executable_directories) + [str(Path(system_root) / "System32")]),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "NPM_CONFIG_CACHE": str(temporary / "npm-cache"),
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "PIP_CACHE_DIR": str(temporary / "pip-cache"),
            "PYTHONPYCACHEPREFIX": str(temporary / "pycache"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "UV_CACHE_DIR": str(temporary / "uv-cache"),
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
        for name in _SAFE_USER_ENVIRONMENT_NAMES:
            value = os.environ.get(name)
            if value:
                environment[name] = value
        if include_passthrough_env:
            for name in self.passthrough_env:
                value = os.environ.get(name)
                if value is not None:
                    environment[name] = value
        return environment

    def _prepare_exec_command(self, key: str, arguments: list[str]) -> list[str]:
        prefix = self._exec_commands[key]
        lowered = [value.casefold() for value in arguments]
        if key == "git" and any(value in _SENSITIVE_GIT_COMMANDS for value in lowered):
            raise PermissionError(
                "Credential plumbing commands are blocked because they can print keyring secrets"
            )
        if key == "gh" and lowered[:2] == ["auth", "token"]:
            raise PermissionError("gh auth token is blocked because it prints a secret")
        return [*prefix, *arguments]

    def _validated_exec_request(
        self, command: str, args: list[str] | None, cwd: str
    ) -> tuple[str, list[str], Path, list[str]]:
        if not self.allow_exec:
            raise PermissionError("Command execution is disabled; restart with --allow-exec")
        if not isinstance(command, str):
            raise TypeError("command must be text")
        key = command.casefold()
        if key.endswith(".exe"):
            key = key[:-4]
        if key not in self._exec_commands or any(token in command for token in ("/", "\\", ":")):
            raise PermissionError(
                "Command is not allowlisted; use one of: " + ", ".join(sorted(self._exec_commands))
            )
        arguments = args or []
        if not isinstance(arguments, list) or len(arguments) > 256:
            raise ValueError("args must be a list with at most 256 entries")
        if any(
            not isinstance(value, str) or "\x00" in value or len(value) > 8192
            for value in arguments
        ):
            raise ValueError("Each command argument must be bounded text without NUL bytes")
        working_directory = self.jail.resolve(cwd, must_exist=True, expect="directory")
        return key, arguments, working_directory, self._prepare_exec_command(key, arguments)

    def run_command(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str = ".",
        timeout_seconds: int = 60,
        max_output_bytes: int = DEFAULT_COMMAND_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        key, _arguments, working_directory, prepared_command = self._validated_exec_request(
            command, args, cwd
        )
        timeout = _bounded_int(
            timeout_seconds, minimum=1, maximum=300, label="timeout_seconds"
        )
        maximum = _bounded_int(
            max_output_bytes,
            minimum=1,
            maximum=MAX_COMMAND_OUTPUT_BYTES,
            label="max_output_bytes",
        )
        result = _run_process_bounded(
            prepared_command,
            cwd=working_directory,
            env=self._execution_environment(),
            timeout_seconds=timeout,
            maximum_output=maximum,
            cancel_event=self._cancel_event(),
        )
        if result["cancelled"]:
            raise JobCancelled("Command cancelled")
        return {
            "command": key,
            "cwd": self.jail.relative(working_directory),
            "policy": "guarded-development",
            **result,
        }

    def _managed_process_status(self, record: _ManagedProcess) -> dict[str, Any]:
        running = record.ended_epoch is None
        if running:
            state = "running"
        elif record.timed_out:
            state = "timed_out"
        elif record.stop_requested:
            state = "stopped"
        else:
            state = "exited"
        ended = record.ended_epoch
        return {
            "process_id": record.process_id,
            "session_id": record.session_id,
            "pid": record.process.pid,
            "command": record.command,
            "cwd": record.cwd,
            "state": state,
            "running": running,
            "exit_code": record.exit_code if not running else None,
            "timed_out": record.timed_out,
            "stdin_closed": record.stdin_closed,
            "started_at": _iso_timestamp(record.started_epoch),
            "ended_at": _iso_timestamp(ended) if ended is not None else None,
            "runtime_seconds": round((ended or time.time()) - record.started_epoch, 3),
            "max_runtime_seconds": record.max_runtime_seconds,
        }

    def _watch_managed_process(self, record: _ManagedProcess) -> None:
        try:
            exit_code = record.process.wait(timeout=record.max_runtime_seconds)
        except subprocess.TimeoutExpired:
            record.timed_out = True
            if record.kill_job.active:
                record.kill_job.terminate()
            else:
                _terminate_process_tree(record.process)
            try:
                exit_code = record.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                record.process.kill()
                exit_code = record.process.wait(timeout=5)
        finally:
            with record.stdin_lock:
                if record.process.stdin is not None and not record.stdin_closed:
                    try:
                        record.process.stdin.close()
                    except OSError:
                        pass
                    record.stdin_closed = True
            record.kill_job.close()
        for reader in record.reader_threads:
            reader.join(timeout=5)
        record.exit_code = exit_code
        record.ended_epoch = time.time()

    def start_process(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str = ".",
        max_runtime_seconds: int = 3600,
        output_limit_bytes: int = DEFAULT_MANAGED_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        key, _arguments, working_directory, prepared = self._validated_exec_request(
            command, args, cwd
        )
        return self._start_managed_process_prepared(
            key,
            prepared,
            working_directory,
            max_runtime_seconds=max_runtime_seconds,
            output_limit_bytes=output_limit_bytes,
            include_passthrough_env=True,
        )

    def _start_managed_process_prepared(
        self,
        command_key: str,
        prepared: list[str],
        working_directory: Path,
        *,
        max_runtime_seconds: int,
        output_limit_bytes: int,
        include_passthrough_env: bool = True,
        stdin_enabled: bool = True,
        policy_root: Path | None = None,
    ) -> dict[str, Any]:
        if not self.allow_exec:
            raise PermissionError("Command execution is disabled; restart with --allow-exec")
        registered_prefix = self._agent_only_commands.get(
            command_key
        ) or self._exec_commands.get(command_key)
        if not registered_prefix:
            raise PermissionError("Prepared command is not registered by this server")
        if (
            not isinstance(prepared, list)
            or len(prepared) < len(registered_prefix)
            or prepared[: len(registered_prefix)] != registered_prefix
            or len(prepared) > len(registered_prefix) + 256
            or any(
                not isinstance(value, str)
                or "\x00" in value
                or len(value) > 32_768
                for value in prepared
            )
        ):
            raise PermissionError("Prepared command does not match its registered executable")
        if not isinstance(working_directory, Path):
            raise TypeError("working_directory must be a Path")
        # Defence in depth: re-resolve the directory through a jail before
        # spawning, so a caller cannot hand over a path that passed an earlier
        # check but no longer resolves safely.  A whitelisted agent cwd is
        # re-checked against its own authorized root instead of the workspace;
        # the caller must already have authorized that root for this run.
        containing_jail = (
            self.jail if policy_root is None else WorkspaceJail(policy_root, create=False)
        )
        working_directory = containing_jail.resolve(
            containing_jail.relative(working_directory),
            must_exist=True,
            expect="directory",
        )
        runtime = _bounded_int(
            max_runtime_seconds,
            minimum=1,
            maximum=86_400,
            label="max_runtime_seconds",
        )
        output_limit = _bounded_int(
            output_limit_bytes,
            minimum=4096,
            maximum=MAX_MANAGED_OUTPUT_BYTES,
            label="output_limit_bytes",
        )
        with self._process_lock:
            active = sum(record.process.poll() is None for record in self._processes.values())
            if active >= MAX_MANAGED_PROCESSES:
                raise RuntimeError(f"At most {MAX_MANAGED_PROCESSES} managed processes may run")
        process = subprocess.Popen(
            prepared,
            cwd=str(working_directory),
            env=self._execution_environment(
                include_passthrough_env=include_passthrough_env
            ),
            # Agent adapters pass the complete prompt as an argument.  Giving
            # Codex/Claude an open pipe here makes them wait forever for
            # additional stdin instead of starting the request.  General
            # command sessions keep the pipe so process_input still works.
            stdin=subprocess.PIPE if stdin_enabled else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=_CREATE_NO_WINDOW,
        )
        process_id = uuid.uuid4().hex
        record = _ManagedProcess(
            process_id,
            command_key,
            self._managed_cwd_label(working_directory),
            process,
            _WindowsKillJob(process),
            output_limit,
            runtime,
        )
        with self._process_lock:
            self._processes[process_id] = record
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=_drain_managed_stream,
            args=(process.stdout, record, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_managed_stream,
            args=(process.stderr, record, "stderr"),
            daemon=True,
        )
        record.reader_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()
        threading.Thread(
            target=self._watch_managed_process,
            args=(record,),
            daemon=True,
        ).start()
        return self._managed_process_status(record)

    def _get_managed_process(self, process_id: str) -> _ManagedProcess:
        if not isinstance(process_id, str) or not re.fullmatch(r"[0-9a-f]{32}", process_id):
            raise ValueError("process_id is invalid")
        with self._process_lock:
            record = self._processes.get(process_id)
        if record is None:
            raise FileNotFoundError("Managed process was not found in this MCP session")
        return record

    def process_status(self, process_id: str) -> dict[str, Any]:
        return self._managed_process_status(self._get_managed_process(process_id))

    def list_processes(self, include_exited: bool = True) -> dict[str, Any]:
        with self._process_lock:
            records = list(self._processes.values())
        statuses = [self._managed_process_status(record) for record in records]
        if not include_exited:
            statuses = [status for status in statuses if status["running"]]
        statuses.sort(key=lambda item: item["started_at"], reverse=True)
        return {
            "processes": statuses,
            "count": len(statuses),
            "max_active_processes": MAX_MANAGED_PROCESSES,
        }

    def process_output(
        self,
        process_id: str,
        stream: str = "both",
        max_bytes: int = DEFAULT_COMMAND_OUTPUT_BYTES,
        after_bytes: int = 0,
    ) -> dict[str, Any]:
        if stream not in {"stdout", "stderr", "both"}:
            raise ValueError("stream must be stdout, stderr, or both")
        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_COMMAND_OUTPUT_BYTES, label="max_bytes"
        )
        after = _bounded_int(after_bytes, minimum=0, maximum=MAX_MANAGED_OUTPUT_BYTES * 1024, label="after_bytes")
        record = self._get_managed_process(process_id)
        return {
            **self._managed_process_status(record),
            **record.snapshot_output(stream, maximum, after),
            "stream": stream,
            "max_bytes": maximum,
            "after_bytes": after,
        }

    def process_input(
        self, process_id: str, input_text: str, close_stdin: bool = False
    ) -> dict[str, Any]:
        record = self._get_managed_process(process_id)
        bytes_sent = record.send_input(input_text, close_stdin)
        return {
            **self._managed_process_status(record),
            "bytes_sent": bytes_sent,
            "stdin_closed": record.stdin_closed,
        }

    def _managed_cwd_label(self, working_directory: Path) -> str:
        """Bounded display label for a managed process working directory.

        A whitelisted agent cwd legitimately sits outside the workspace, so
        this must not raise.  It also must not leak an absolute path into the
        audit log, which records only bounded relative labels.
        """

        try:
            return self.jail.relative(working_directory)
        except WorkspaceSecurityError:
            return f"<policy>/{working_directory.name}"

    @staticmethod
    def _is_absolute_request(value: str) -> bool:
        windows = PureWindowsPath(str(value).replace("/", "\\"))
        return bool(windows.drive or windows.root or windows.is_absolute())

    def _agent_cwd_operation(self, sandbox: str) -> str:
        """Agents need the same file capability their sandbox will use.

        A browse rule therefore never hosts an agent: it grants listing only.
        Running the agent itself is not treated as `exec`, because the command
        template is server-owned and confined to this directory rather than an
        arbitrary caller-supplied command line.
        """

        return "read" if sandbox == "read-only" else "write"

    def _authorize_agent_cwd(self, cwd: str, sandbox: str) -> tuple[str, str | None]:
        """Return the stored cwd and, for whitelisted paths, its rule root."""

        raw = "." if cwd is None or str(cwd) == "" else str(cwd)
        if not self._is_absolute_request(raw):
            resolved = self.jail.resolve(raw, must_exist=True, expect="directory")
            return self.jail.relative(resolved), None
        operation = self._agent_cwd_operation(sandbox)
        decision = self.access_policy.authorize(raw, operation)
        if decision.requires_approval:
            raise PermissionError(
                "This directory requires approval; request external access first"
            )
        root = decision.rule_path
        if root is None:
            raise PermissionError("No enabled access-policy rule covers this directory")
        relative = decision.path.relative_to(root).as_posix() or "."
        if root == self.jail.root:
            resolved = self.jail.resolve(relative, must_exist=True, expect="directory")
            return self.jail.relative(resolved), None
        scoped = WorkspaceJail(root, create=False)
        resolved = scoped.resolve(relative, must_exist=True, expect="directory")
        return scoped.relative(resolved), str(root)

    def _agent_working_directory(self, session: AgentSessionState) -> Path:
        """Resolve a session cwd, re-authorizing whitelisted roots every time."""

        if session.policy_root is None:
            return self.jail.resolve(session.cwd, must_exist=True, expect="directory")
        root = Path(session.policy_root)
        absolute = root if session.cwd == "." else root / session.cwd
        operation = self._agent_cwd_operation(session.sandbox)
        decision = self.access_policy.authorize(absolute, operation)
        if decision.requires_approval or decision.rule_path != root:
            raise PermissionError(
                "Agent working directory is no longer covered by its original "
                "access-policy rule"
            )
        scoped = WorkspaceJail(root, create=False)
        return scoped.resolve(session.cwd, must_exist=True, expect="directory")

    def agent_session_create(
        self, profile: str = "codex-default", cwd: str = ".", sandbox: str = "read-only"
    ) -> dict[str, Any]:
        selected = self.agent_profiles.get(profile)
        self.agent_profiles.require_capability(selected, "create")
        selected.validate_sandbox(sandbox)
        stored_cwd, policy_root = self._authorize_agent_cwd(cwd, sandbox)
        session = AgentSessionState(
            session_id=new_session_id(),
            profile=selected.name,
            cwd=stored_cwd,
            sandbox=sandbox,
            provider=selected.provider,
            policy_root=policy_root,
        )
        with self._agent_lock:
            if len(self._agent_sessions) >= MAX_AGENT_SESSIONS:
                raise RuntimeError(
                    f"At most {MAX_AGENT_SESSIONS} agent sessions may exist per MCP process"
                )
            self._agent_sessions[session.session_id] = session
        return self._agent_session_payload(session)

    def agent_session_attach(
        self,
        conversation_ref: str,
        profile: str = "codex-default",
        sandbox: str = "read-only",
    ) -> dict[str, Any]:
        catalog = self._require_agent_catalog()
        selected = self.agent_profiles.get(profile)
        self.agent_profiles.require_capability(selected, "attach")
        selected.validate_sandbox(sandbox)
        record = catalog.authorize_attachment(
            self.agent_source_policy, conversation_ref
        )
        if record["provider"] != selected.provider:
            raise ValueError(
                "Agent conversation provider does not match the selected profile"
            )
        # Resume where the conversation actually ran: inside the workspace, or
        # inside a directory the access policy now covers.  Anything else stays
        # visible as metadata but cannot be attached.
        raw_cwd = record.get("cwd_absolute")
        if isinstance(raw_cwd, str) and raw_cwd:
            request_cwd = raw_cwd
        elif isinstance(record.get("cwd"), str):
            request_cwd = str(record["cwd"])
        else:
            raise PermissionError(
                "Agent conversation working directory is unavailable"
            )
        try:
            stored_cwd, policy_root = self._authorize_agent_cwd(request_cwd, sandbox)
        except (PermissionError, WorkspaceSecurityError, FileNotFoundError) as exc:
            raise PermissionError(
                "Agent conversation working directory is not covered by the "
                "workspace or any enabled access-policy rule"
            ) from exc
        session = AgentSessionState(
            session_id=new_session_id(),
            profile=selected.name,
            cwd=stored_cwd,
            policy_root=policy_root,
            sandbox=sandbox,
            provider=selected.provider,
            native_session_id=str(record["native_session_id"]),
            conversation_ref=str(record["conversation_ref"]),
            source_id=str(record["source_id"]),
        )
        with self._agent_lock:
            if len(self._agent_sessions) >= MAX_AGENT_SESSIONS:
                raise RuntimeError(
                    f"At most {MAX_AGENT_SESSIONS} agent sessions may exist per MCP process"
                )
            self._agent_sessions[session.session_id] = session
        return self._agent_session_payload(session)

    @staticmethod
    def _agent_session_payload(session: AgentSessionState) -> dict[str, Any]:
        active = sum(
            1
            for run in session.runs.values()
            if run.state in {"queued", "running"}
        )
        return {
            "session_id": session.session_id,
            "provider": session.provider,
            "profile": session.profile,
            "cwd": session.cwd,
            # None means the workspace; otherwise the whitelisted rule root the
            # cwd is relative to, so the caller can tell where work lands.
            "cwd_policy_root": session.policy_root,
            "cwd_scope": "workspace" if session.policy_root is None else "access-policy",
            "sandbox": session.sandbox,
            "native_session_id": session.native_session_id,
            "thread_id": session.thread_id,
            "origin": "catalog" if session.conversation_ref else "new",
            "conversation_ref": session.conversation_ref,
            "source_id": session.source_id,
            "closed": session.closed,
            "run_count": len(session.runs),
            "active_run_count": active,
            "created_at": _iso_timestamp(session.created_epoch),
        }

    def _get_agent_session(self, session_id: str) -> AgentSessionState:
        if not isinstance(session_id, str) or not re.fullmatch(r"sess_[0-9a-f]{32}", session_id):
            raise ValueError("session_id is invalid")
        with self._agent_lock:
            session = self._agent_sessions.get(session_id)
        if session is None:
            raise FileNotFoundError("Agent session was not found in this MCP session")
        return session

    def agent_session_list(self) -> dict[str, Any]:
        with self._agent_lock:
            sessions = list(self._agent_sessions.values())
        sessions.sort(key=lambda item: item.created_epoch, reverse=True)
        return {"sessions": [self._agent_session_payload(item) for item in sessions], "count": len(sessions)}

    def agent_session_inspect(self, session_id: str) -> dict[str, Any]:
        return self._agent_session_payload(self._get_agent_session(session_id))

    def agent_session_close(self, session_id: str) -> dict[str, Any]:
        session = self._get_agent_session(session_id)
        with session.lock:
            with self._agent_lock:
                session.closed = True
                runs = list(session.runs.values())
        for run in runs:
            self._refresh_agent_run(session, run)
            if run.state in {"queued", "running"}:
                run.cancelled = True
                run.terminal_override = "cancelled"
                try:
                    self.stop_process(run.process_id, force=True)
                except (FileNotFoundError, RuntimeError):
                    pass
                self._refresh_agent_run(session, run)
        return self._agent_session_payload(session)

    def agent_run_start(self, session_id: str, prompt: str) -> dict[str, Any]:
        session = self._get_agent_session(session_id)
        with session.lock:
            return self._agent_run_start_locked(session, prompt)

    def _agent_run_start_locked(
        self, session: AgentSessionState, prompt: str
    ) -> dict[str, Any]:
        if session.closed:
            raise PermissionError("Agent session is closed")
        profile = self.agent_profiles.get(session.profile)
        adapter = self.agent_profiles.adapter_for_profile(profile)
        if profile.provider != session.provider or adapter.provider != session.provider:
            raise RuntimeError("Agent session provider binding does not match its profile")
        with self._agent_lock:
            existing_runs = list(session.runs.values())
        for existing in existing_runs:
            if existing.state in {"queued", "running"}:
                self._refresh_agent_run(session, existing)
        with self._agent_lock:
            active = [run for run in session.runs.values() if run.state in {"queued", "running"}]
            if active:
                raise RuntimeError("Only one active run is allowed per agent session")
            if len(session.runs) >= MAX_AGENT_RUNS_PER_SESSION:
                raise RuntimeError(
                    f"At most {MAX_AGENT_RUNS_PER_SESSION} runs may exist per agent session"
                )
        if session.conversation_ref is not None:
            self._reauthorize_attached_session(session)
        working_directory = self._agent_working_directory(session)
        prefix = self._agent_only_commands.get(profile.command) or self._exec_commands.get(
            profile.command
        )
        if not prefix:
            raise RuntimeError(f"{adapter.display_name} executable is not available")
        command = self.agent_profiles.build_command(
            profile,
            prefix,
            prompt=prompt,
            cwd=str(working_directory),
            sandbox=session.sandbox,
            native_session_id=session.native_session_id,
        )
        if command[: len(prefix)] != prefix or len(command) <= len(prefix):
            raise RuntimeError("Agent adapter returned an invalid executable prefix")
        parser = adapter.new_parser()
        started = self._start_managed_process_prepared(
            profile.command,
            command,
            working_directory,
            max_runtime_seconds=profile.max_runtime_seconds,
            output_limit_bytes=profile.max_output_bytes,
            include_passthrough_env=profile.pass_configured_environment,
            stdin_enabled=False,
            policy_root=(
                None if session.policy_root is None else Path(session.policy_root)
            ),
        )
        run = AgentRunState(
            new_run_id(),
            session.session_id,
            started["process_id"],
            parser=parser,
        )
        with self._agent_lock:
            session.runs[run.run_id] = run
        initial_state = "running" if started["running"] else (
            "succeeded" if started.get("exit_code") == 0 else "failed"
        )
        run.state = initial_state
        return {
            "execution": "background",
            "session_id": session.session_id,
            "provider": session.provider,
            "run_id": run.run_id,
            "job_id": None,
            "process_id": run.process_id,
            "state": initial_state,
            "native_session_id": session.native_session_id,
            "thread_id": session.thread_id,
            "next_seq": 0,
        }

    def _reauthorize_attached_session(self, session: AgentSessionState) -> None:
        if session.conversation_ref is None:
            return
        catalog = self._require_agent_catalog()
        record = catalog.authorize_attachment(
            self.agent_source_policy, session.conversation_ref
        )
        if (
            record["provider"] != session.provider
            or record["native_session_id"] != session.native_session_id
            or record["source_id"] != session.source_id
        ):
            raise PermissionError("Attached agent session binding is stale or changed")
        # Re-derive the working directory the same way attach did, so a policy
        # change that moved or revoked the rule is caught here rather than
        # silently resuming somewhere else.
        raw_cwd = record.get("cwd_absolute") or record.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd:
            raise PermissionError("Attached agent session binding is stale or changed")
        try:
            stored_cwd, policy_root = self._authorize_agent_cwd(
                raw_cwd, session.sandbox
            )
        except (PermissionError, WorkspaceSecurityError, FileNotFoundError) as exc:
            raise PermissionError(
                "Attached agent working directory is no longer authorized"
            ) from exc
        if stored_cwd != session.cwd or policy_root != session.policy_root:
            raise PermissionError("Attached agent session binding is stale or changed")

    def _get_agent_run(self, session_id: str, run_id: str) -> tuple[AgentSessionState, AgentRunState]:
        session = self._get_agent_session(session_id)
        if not isinstance(run_id, str) or not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise ValueError("run_id is invalid")
        run = session.runs.get(run_id)
        if run is None:
            raise FileNotFoundError("Agent run was not found in this session")
        return session, run

    @staticmethod
    def _append_agent_event(run: AgentRunState, event: NormalizedEvent | None) -> None:
        if event is None:
            return
        with run.lock:
            run.events.append(event)
            overflow = len(run.events) - MAX_AGENT_EVENTS
            if overflow > 0:
                del run.events[:overflow]

    def _update_agent_native_binding(
        self, session: AgentSessionState, run: AgentRunState
    ) -> None:
        candidate = run.parser.native_session_id
        if not candidate:
            return
        if (
            session.conversation_ref is not None
            and session.native_session_id != candidate
        ):
            run.terminal_override = "failed"
            run.error_summary = (
                "Attached agent returned a different native session id"
            )
            try:
                self.stop_process(run.process_id, force=True)
            except (FileNotFoundError, RuntimeError):
                pass
            return
        session.native_session_id = candidate

    def _refresh_agent_run(self, session: AgentSessionState, run: AgentRunState) -> dict[str, Any]:
        with run.lock:
            profile = self.agent_profiles.get(session.profile)
            adapter = self.agent_profiles.adapter_for_profile(profile)
            if profile.provider != session.provider or adapter.provider != session.provider:
                raise RuntimeError("Agent session provider binding does not match its profile")
            display_name = adapter.display_name
            output = self.process_output(
                run.process_id,
                stream="stdout",
                max_bytes=MAX_COMMAND_OUTPUT_BYTES,
                after_bytes=run.stdout_offset,
            )
            run.stdout_offset = output["stdout_next_offset_bytes"]
            if output.get("stdout_cursor_gap"):
                self._append_agent_event(
                    run,
                    run.parser.synthetic_event(
                        "status",
                        f"{display_name} stdout exceeded the retained process buffer",
                        {"reason": "stdout_cursor_gap"},
                    ),
                )
            run.pending_text += output.get("stdout", "")
            lines = run.pending_text.splitlines(keepends=False)
            if run.pending_text and not run.pending_text.endswith(("\n", "\r")):
                run.pending_text = lines.pop() if lines else run.pending_text
            else:
                run.pending_text = ""
            for line in lines:
                self._append_agent_event(run, run.parser.feed_line(line))
            self._update_agent_native_binding(session, run)

            status = self.process_status(run.process_id)
            if run.terminal_override is not None:
                run.state = run.terminal_override
            elif status["state"] == "timed_out":
                run.state = "timed_out"
            elif status["state"] == "stopped":
                run.state = "cancelled"
            elif status["running"]:
                run.state = "running"
            elif status.get("exit_code") == 0:
                run.state = "succeeded"
            else:
                run.state = "failed"

            terminal = run.state not in {"queued", "running"}
            if terminal and run.pending_text:
                self._append_agent_event(run, run.parser.feed_line(run.pending_text))
                run.pending_text = ""
                self._update_agent_native_binding(session, run)
            if terminal and run.ended_epoch is None:
                run.ended_epoch = time.time()
            if terminal and not run.terminal_event_emitted:
                stderr = self.process_output(
                    run.process_id,
                    stream="stderr",
                    max_bytes=MAX_COMMAND_OUTPUT_BYTES,
                    after_bytes=run.stderr_offset,
                )
                run.stderr_offset = stderr["stderr_next_offset_bytes"]
                stderr_text = stderr.get("stderr", "").strip()
                if run.state in {"failed", "timed_out"}:
                    fallback = run.error_summary or (
                        f"{display_name} run timed out"
                        if run.state == "timed_out"
                        else f"{display_name} exited with code {status.get('exit_code')}"
                    )
                    error_event = run.parser.synthetic_event(
                        "error",
                        stderr_text or fallback,
                        {
                            "state": run.state,
                            "exit_code": status.get("exit_code"),
                            "stderr": stderr_text or fallback,
                        },
                    )
                    run.error_summary = error_event.summary
                    self._append_agent_event(run, error_event)
                elif run.state == "succeeded":
                    self._append_agent_event(
                        run,
                        run.parser.synthetic_event(
                            "completed", f"{display_name} run completed"
                        ),
                    )
                else:
                    self._append_agent_event(
                        run,
                        run.parser.synthetic_event(
                            "status",
                            f"{display_name} run {run.state}",
                            {"state": run.state},
                        ),
                    )
                run.terminal_event_emitted = True
            return status

    def _agent_run_payload(
        self,
        session: AgentSessionState,
        run: AgentRunState,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        with run.lock:
            available_from = run.events[0].seq if run.events else run.parser.next_seq
            return {
                "session_id": session.session_id,
                "provider": session.provider,
                "run_id": run.run_id,
                "process_id": run.process_id,
                "state": run.state,
                "native_session_id": session.native_session_id,
                "thread_id": session.thread_id,
                "event_count": len(run.events),
                "available_from_seq": available_from,
                "next_seq": run.parser.next_seq,
                "result_ready": run.state not in {"queued", "running"},
                "has_result": run.parser.final_message is not None,
                "error": run.error_summary,
                "created_at": _iso_timestamp(run.created_epoch),
                "ended_at": _iso_timestamp(run.ended_epoch) if run.ended_epoch else None,
                "runtime_seconds": status["runtime_seconds"],
                "exit_code": status.get("exit_code"),
            }

    def agent_run_inspect(self, session_id: str, run_id: str) -> dict[str, Any]:
        session, run = self._get_agent_run(session_id, run_id)
        status = self._refresh_agent_run(session, run)
        with run.lock:
            return self._agent_run_payload(session, run, status)

    def agent_run_events(
        self,
        session_id: str,
        run_id: str,
        after_seq: int = 0,
        limit: int = 100,
        max_bytes: int = 64 * 1024,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        after = _bounded_int(after_seq, minimum=0, maximum=1_000_000, label="after_seq")
        page_limit = _bounded_int(limit, minimum=1, maximum=500, label="limit")
        byte_limit = _bounded_int(max_bytes, minimum=1, maximum=MAX_COMMAND_OUTPUT_BYTES, label="max_bytes")
        wait = _bounded_int(wait_ms, minimum=0, maximum=10_000, label="wait_ms")
        session, run = self._get_agent_run(session_id, run_id)
        deadline = time.monotonic() + (wait / 1000)
        while True:
            status = self._refresh_agent_run(session, run)
            with run.lock:
                available_from = run.events[0].seq if run.events else run.parser.next_seq
                effective_after = max(after, available_from)
                candidates = [event for event in run.events if event.seq >= effective_after]
                terminal = run.state not in {"queued", "running"}
            if candidates or terminal or time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

        selected: list[dict[str, Any]] = []
        used = 0
        required_bytes_for_next_event: int | None = None
        with run.lock:
            available_from = run.events[0].seq if run.events else run.parser.next_seq
            effective_after = max(after, available_from)
            for event in run.events:
                if event.seq < effective_after:
                    continue
                payload = event.as_dict()
                size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                if len(selected) >= page_limit or used + size > byte_limit:
                    required_bytes_for_next_event = size
                    break
                selected.append(payload)
                used += size
            next_seq = selected[-1]["seq"] + 1 if selected else effective_after
            has_more = any(event.seq >= next_seq for event in run.events)
            return {
                "session_id": session.session_id,
                "provider": session.provider,
                "run_id": run.run_id,
                "state": run.state,
                "native_session_id": session.native_session_id,
                "thread_id": session.thread_id,
                "events": selected,
                "available_from_seq": available_from,
                "next_seq": next_seq,
                "has_more": has_more,
                "required_bytes_for_next_event": required_bytes_for_next_event,
                "cursor_gap": after < available_from,
                "truncated": bool(status.get("stdout_truncated")) or after < available_from,
            }

    def agent_run_result(
        self, session_id: str, run_id: str, max_bytes: int = 64 * 1024
    ) -> dict[str, Any]:
        maximum = _bounded_int(
            max_bytes, minimum=1, maximum=MAX_COMMAND_OUTPUT_BYTES, label="max_bytes"
        )
        session, run = self._get_agent_run(session_id, run_id)
        status = self._refresh_agent_run(session, run)
        with run.lock:
            message = run.parser.final_message
            result = None
            truncated = False
            if message is not None:
                result, truncated, _ = _truncate_utf8(message, maximum)
            return {
                **self._agent_run_payload(session, run, status),
                "result": result,
                "truncated": truncated,
                "max_bytes": maximum,
            }

    def agent_run_cancel(self, session_id: str, run_id: str, reason: str = "") -> dict[str, Any]:
        session, run = self._get_agent_run(session_id, run_id)
        status = self._refresh_agent_run(session, run)
        if run.state not in {"queued", "running"}:
            safe_reason, _ = redact_text(reason or "run already finished", 1024)
            return {
                **self._agent_run_payload(session, run, status),
                "already_finished": True,
                "reason": safe_reason,
            }
        run.cancelled = True
        run.terminal_override = "cancelled"
        self.stop_process(run.process_id, force=True)
        status = self._refresh_agent_run(session, run)
        safe_reason, _ = redact_text(reason or "cancelled by caller", 1024)
        return {
            **self._agent_run_payload(session, run, status),
            "already_finished": False,
            "reason": safe_reason,
        }

    def stop_process(self, process_id: str, force: bool = False) -> dict[str, Any]:
        record = self._get_managed_process(process_id)
        if record.process.poll() is not None:
            return {**self._managed_process_status(record), "already_exited": True}
        record.stop_requested = True
        if force and record.kill_job.active:
            record.kill_job.terminate()
        else:
            try:
                record.process.terminate()
                record.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                if record.kill_job.active:
                    record.kill_job.terminate()
                else:
                    _terminate_process_tree(record.process)
        try:
            record.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(record.process)
        deadline = time.monotonic() + 2
        while record.ended_epoch is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return {**self._managed_process_status(record), "already_exited": False}

    def stop_all_processes(self) -> None:
        with self._process_lock:
            ids = [
                process_id
                for process_id, record in self._processes.items()
                if record.process.poll() is None
            ]
        for process_id in ids:
            try:
                self.stop_process(process_id, force=True)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop managed work without waiting on uncooperative background threads."""

        self._grant_reaper_stop.set()
        if self._grant_reaper is not None and self._grant_reaper.is_alive():
            self._grant_reaper.join(timeout=1)
        with self._agent_lock:
            sessions = list(self._agent_sessions.values())
            runs = [(session, run) for session in sessions for run in session.runs.values()]
        for session, run in runs:
            try:
                self._refresh_agent_run(session, run)
            except Exception:
                pass
        with self._agent_lock:
            active_runs: list[tuple[AgentSessionState, AgentRunState]] = []
            for session in sessions:
                session.closed = True
                for run in session.runs.values():
                    if run.state in {"queued", "running"}:
                        run.terminal_override = "aborted_on_shutdown"
                        active_runs.append((session, run))
        for session, run in active_runs:
            try:
                self.stop_process(run.process_id, force=True)
                self._refresh_agent_run(session, run)
            except Exception:
                run.state = "aborted_on_shutdown"
                run.ended_epoch = run.ended_epoch or time.time()
        if self.jobs is not None:
            self.jobs.shutdown(wait_seconds=2.0)
        self.stop_all_processes()
