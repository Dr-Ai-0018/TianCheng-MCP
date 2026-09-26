from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Callable, Iterable

from .proxy import ProxySettings
from .service import _WindowsKillJob


_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CREATE_NEW_PROCESS_GROUP = 0x00000200 if os.name == "nt" else 0
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DURATION_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>s|m|h)$")
_FAILURE_MARKERS = (
    "dispatcher received mcp upstream error",
    "command response deadline reached",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_duration(value: str) -> int:
    """Validate a tunnel-client duration and return seconds."""

    match = _DURATION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("duration must use a positive integer followed by s, m, or h")
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group("unit")]
    seconds = int(match.group("amount")) * multiplier
    if seconds > 7 * 24 * 3600:
        raise ValueError("duration must not exceed 168h")
    return seconds


def _bounded_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class SupervisorSettings:
    enabled: bool
    connection_max_ttl: str
    health_poll_seconds: int
    startup_grace_seconds: int
    upstream_error_threshold: int
    upstream_error_window_seconds: int
    restart_backoff_seconds: tuple[int, ...]
    restart_budget_max_attempts: int
    restart_budget_window_seconds: int
    stable_reset_seconds: int

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisorSettings":
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("supervisor config must be an object")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("supervisor.enabled must be a boolean")
        ttl = value.get("mcpConnectionMaxTtl", "24h")
        if not isinstance(ttl, str):
            raise ValueError("supervisor.mcpConnectionMaxTtl must be a duration string")
        parse_duration(ttl)
        raw_backoff = value.get("restartBackoffSeconds", [1, 2, 5, 10, 30, 60])
        if not isinstance(raw_backoff, list) or not raw_backoff:
            raise ValueError("supervisor.restartBackoffSeconds must be a non-empty array")
        backoff = tuple(
            _bounded_int(item, minimum=0, maximum=300, label="restart backoff")
            for item in raw_backoff
        )
        raw_budget = value.get("restartBudget", {})
        if not isinstance(raw_budget, dict):
            raise ValueError("supervisor.restartBudget must be an object")
        return cls(
            enabled=enabled,
            connection_max_ttl=ttl,
            health_poll_seconds=_bounded_int(
                value.get("healthPollSeconds", 5),
                minimum=1,
                maximum=60,
                label="health poll seconds",
            ),
            startup_grace_seconds=_bounded_int(
                value.get("startupGraceSeconds", 20),
                minimum=1,
                maximum=300,
                label="startup grace seconds",
            ),
            upstream_error_threshold=_bounded_int(
                value.get("upstreamErrorThreshold", 3),
                minimum=1,
                maximum=20,
                label="upstream error threshold",
            ),
            upstream_error_window_seconds=_bounded_int(
                value.get("upstreamErrorWindowSeconds", 30),
                minimum=1,
                maximum=600,
                label="upstream error window seconds",
            ),
            restart_backoff_seconds=backoff,
            restart_budget_max_attempts=_bounded_int(
                raw_budget.get("maxAttempts", 5),
                minimum=1,
                maximum=50,
                label="restart budget max attempts",
            ),
            restart_budget_window_seconds=_bounded_int(
                raw_budget.get("windowSeconds", 600),
                minimum=10,
                maximum=86400,
                label="restart budget window seconds",
            ),
            stable_reset_seconds=_bounded_int(
                value.get("stableResetSeconds", 300),
                minimum=10,
                maximum=86400,
                label="stable reset seconds",
            ),
        )


@dataclass(frozen=True)
class LauncherConfig:
    tunnel_client: Path
    profile_dir: Path | None
    health_base_url: str
    settings: SupervisorSettings


def load_launcher_config(defaults_path: Path, local_path: Path) -> LauncherConfig:
    defaults = json.loads(defaults_path.read_text(encoding="utf-8-sig"))
    if not isinstance(defaults, dict):
        raise ValueError("launcher defaults must be an object")
    overrides: object = {}
    if local_path.is_file():
        overrides = json.loads(local_path.read_text(encoding="utf-8-sig"))
    if not isinstance(overrides, dict):
        raise ValueError("launcher local config must be an object")
    merged = dict(defaults)
    merged.update(overrides)
    tunnel_value = merged.get("tunnelClient")
    if not isinstance(tunnel_value, str) or not tunnel_value.strip():
        raise ValueError("tunnelClient is required")
    tunnel_client = Path(tunnel_value).resolve()
    if not tunnel_client.is_file():
        raise ValueError(f"tunnel-client does not exist: {tunnel_client}")
    profile_value = merged.get("profileDir", "")
    if not isinstance(profile_value, str):
        raise ValueError("profileDir must be a string")
    profile_dir = Path(profile_value).resolve() if profile_value.strip() else None
    health = merged.get("healthBaseUrl", "http://127.0.0.1:8080")
    if not isinstance(health, str):
        raise ValueError("healthBaseUrl must be an http://127.0.0.1 URL")
    health_parts = urllib.parse.urlsplit(health)
    if (
        health_parts.scheme != "http"
        or health_parts.hostname != "127.0.0.1"
        or health_parts.port is None
        or health_parts.username is not None
        or health_parts.password is not None
        or health_parts.query
        or health_parts.fragment
    ):
        raise ValueError("healthBaseUrl must be an http://127.0.0.1 URL")
    return LauncherConfig(
        tunnel_client=tunnel_client,
        profile_dir=profile_dir,
        health_base_url=health.rstrip("/"),
        settings=SupervisorSettings.from_mapping(merged.get("supervisor")),
    )


class FailureWindow:
    def __init__(self, *, threshold: int, window_seconds: int) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._events: collections.deque[float] = collections.deque()

    def record(self, timestamp: float) -> bool:
        self._events.append(timestamp)
        cutoff = timestamp - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events) >= self.threshold

    def clear(self) -> None:
        self._events.clear()


class RestartBudget:
    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: collections.deque[float] = collections.deque()

    def allow(self, timestamp: float) -> bool:
        cutoff = timestamp - self.window_seconds
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
        if len(self._attempts) >= self.max_attempts:
            return False
        self._attempts.append(timestamp)
        return True

    def clear(self) -> None:
        self._attempts.clear()

    @property
    def count(self) -> int:
        return len(self._attempts)


class ProfileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    def __enter__(self) -> "ProfileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("supervisor is already running for this profile") from exc
        # Keep the locked sentinel byte intact.  Truncating the byte that
        # msvcrt.locking owns makes LK_UNLCK fail with EACCES on Windows.
        handle.seek(1)
        handle.truncate()
        handle.write(("\n" + str(os.getpid())).encode("ascii"))
        handle.flush()
        self._file = handle
        return self

    def __exit__(self, *_: object) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # Closing the handle releases an OS-owned lock even if a
                    # damaged legacy lock file cannot be explicitly unlocked.
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The OS lock is already released.  A stale path is reusable on
            # the next start, but graceful stop normally removes it.
            pass


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(data, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
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
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _classify_failure_line(line: str) -> tuple[str, bool] | None:
    """Return (reason, recover_immediately) for actionable tunnel failures."""

    lowered = line.casefold()
    if all(
        marker in lowered
        for marker in ("failure_source", "client_internal", "upstream_response_received")
    ) and ("false" in lowered or "502" in lowered):
        return ("client_internal_without_upstream", True)
    if any(marker in lowered for marker in _FAILURE_MARKERS):
        return ("repeated_mcp_upstream_failure", False)
    return None


class TunnelSupervisor:
    def __init__(
        self,
        *,
        config: LauncherConfig,
        profile: str,
        state_path: Path,
        stop_path: Path | None = None,
        recovery_log_path: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if _PROFILE_PATTERN.fullmatch(profile) is None:
            raise ValueError("invalid profile name")
        self.config = config
        self.profile = profile
        self.state_path = state_path
        self.stop_path = stop_path
        self.recovery_log_path = recovery_log_path
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.recover_event = threading.Event()
        self.failure_reason: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.kill_job: _WindowsKillJob | None = None
        self.generation = 0
        self.started_at: float | None = None
        self.failure_window = FailureWindow(
            threshold=config.settings.upstream_error_threshold,
            window_seconds=config.settings.upstream_error_window_seconds,
        )
        self.failure_lock = threading.Lock()
        self.restart_budget = RestartBudget(
            max_attempts=config.settings.restart_budget_max_attempts,
            window_seconds=config.settings.restart_budget_window_seconds,
        )
        self.last_healthy_at: str | None = None
        self.last_recovery_reason: str | None = None
        self.backoff_until: str | None = None

    def _write_state(self, state: str, **extra: object) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "profile": self.profile,
            "supervisor_pid": os.getpid(),
            "tunnel_pid": self.process.pid if self.process and self.process.poll() is None else None,
            "state": state,
            "generation": self.generation,
            "mcp_transport": "inferred" if state == "healthy" else state,
            "probe_mode": "process_and_failure_log",
            "last_healthy_at": self.last_healthy_at,
            "recovery_reason": self.last_recovery_reason,
            "last_recovery_reason": self.last_recovery_reason,
            "restart_count": self.restart_budget.count,
            "restart_count_window": self.restart_budget.count,
            "backoff_until": self.backoff_until,
            "updated_at": _utc_now(),
        }
        payload.update(extra)
        try:
            _atomic_write_json(self.state_path, payload)
        except OSError as exc:
            # Status persistence must never take the live tunnel down.  Keep
            # the warning bounded and do not include config, argv, or env.
            print(
                f"Tunnel supervisor warning: could not update state ({type(exc).__name__})",
                file=sys.stderr,
            )

    def _command(self) -> list[str]:
        command = [
            str(self.config.tunnel_client),
            "run",
            "--profile",
            self.profile,
            "--mcp.connection-max-ttl",
            self.config.settings.connection_max_ttl,
        ]
        if self.config.profile_dir is not None:
            command.extend(("--profile-dir", str(self.config.profile_dir)))
        return command

    def _tee(self, stream: IO[str], output: IO[str], generation: int) -> None:
        try:
            for line in iter(stream.readline, ""):
                # A redirected Windows console may not encode all UTF-8 log
                # characters. Keep draining and classifying even if display fails.
                try:
                    try:
                        output.write(line)
                    except UnicodeEncodeError:
                        encoding = getattr(output, "encoding", None) or "ascii"
                        output.write(line.encode(encoding, errors="backslashreplace").decode(encoding))
                    output.flush()
                except (OSError, ValueError):
                    # Closed/broken output is not a tunnel failure. Dropping the
                    # display copy must not disable recovery or block its pipe.
                    pass
                classified = _classify_failure_line(line)
                if classified is None or generation != self.generation:
                    continue
                reason, immediate = classified
                with self.failure_lock:
                    threshold_reached = immediate or self.failure_window.record(
                        self.monotonic()
                    )
                    if threshold_reached and generation == self.generation:
                        self.failure_reason = reason
                        self.recover_event.set()
        finally:
            stream.close()

    def _spawn(self) -> None:
        self.generation += 1
        self.failure_window.clear()
        self.recover_event.clear()
        self.failure_reason = None
        self._write_state("starting")
        self.process = subprocess.Popen(
            self._command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )
        self.kill_job = _WindowsKillJob(self.process)  # type: ignore[arg-type]
        if os.name == "nt" and not self.kill_job.active:
            _terminate_process_tree(self.process)
            self.process = None
            self.kill_job.close()
            self.kill_job = None
            raise OSError("Windows process-tree guard could not be established")
        self._write_state("starting")
        self.started_at = self.monotonic()
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(
            target=self._tee,
            args=(self.process.stdout, sys.stdout, self.generation),
            daemon=True,
            name="tunnel-stdout",
        ).start()

    def _spawn_with_recovery(self) -> bool:
        while not self._stop_requested():
            try:
                self._spawn()
                return True
            except OSError:
                self.last_recovery_reason = "tunnel_client_spawn_failed"
                if not self.restart_budget.allow(self.monotonic()):
                    self._write_state("failed", error="restart_budget_exhausted")
                    return False
                self._write_state("recovering", error="tunnel_client_spawn_failed")
                if self._backoff("tunnel_client_spawn_failed"):
                    return False
        return False
        threading.Thread(
            target=self._tee,
            args=(self.process.stderr, sys.stderr, self.generation),
            daemon=True,
            name="tunnel-stderr",
        ).start()

    def _ready(self) -> bool:
        try:
            # The admin listener is loopback-only. Never send this probe to a
            # configured proxy, even when the host has proxy env variables.
            with urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            ).open(
                f"{self.config.health_base_url}/readyz", timeout=2
            ) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _stop_child(self) -> None:
        process, self.process = self.process, None
        kill_job, self.kill_job = self.kill_job, None
        if process is not None:
            if kill_job is not None and kill_job.active:
                kill_job.terminate()
            _terminate_process_tree(process)
        if kill_job is not None:
            kill_job.close()

    def request_stop(self, *_: object) -> None:
        self.stop_event.set()

    def _stop_requested(self) -> bool:
        if self.stop_event.is_set():
            return True
        if self.stop_path is not None and self.stop_path.is_file():
            self.stop_event.set()
            return True
        return False

    def _wait_with_stop(self, seconds: float) -> bool:
        deadline = self.monotonic() + seconds
        while not self._stop_requested():
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(0.5, remaining))
        return True

    def _append_recovery_event(self, *, reason: str, backoff_until: str | None) -> None:
        if self.recovery_log_path is None:
            return
        payload = {
            "time": _utc_now(),
            "profile": self.profile,
            "generation": self.generation + 1,
            "failed_generation": self.generation,
            "recovery_reason": reason,
            "restart_count": self.restart_budget.count,
            "backoff_until": backoff_until,
        }
        try:
            self.recovery_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.recovery_log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            print(
                f"Tunnel supervisor warning: could not append recovery log ({type(exc).__name__})",
                file=sys.stderr,
            )

    def _backoff(self, reason: str) -> bool:
        attempt = max(0, self.restart_budget.count - 1)
        values = self.config.settings.restart_backoff_seconds
        seconds = values[min(attempt, len(values) - 1)]
        delay = seconds + (random.uniform(0, min(1.0, seconds * 0.1)) if seconds else 0)
        self.backoff_until = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        self._append_recovery_event(reason=reason, backoff_until=self.backoff_until)
        self._write_state("backoff", backoff_seconds=round(delay, 3))
        return self._wait_with_stop(delay)

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)
        healthy_written = False
        terminal_state = "stopped"
        try:
            if not self._spawn_with_recovery():
                if not self._stop_requested():
                    terminal_state = "failed"
                    return 2
                return 0
            while not self._stop_requested():
                assert self.process is not None
                exit_code = self.process.poll()
                elapsed = self.monotonic() - (self.started_at or self.monotonic())
                if exit_code is None and elapsed >= self.config.settings.startup_grace_seconds:
                    ready = self._ready()
                    if ready and not healthy_written:
                        self.last_healthy_at = _utc_now()
                        self._write_state("healthy", tunnel_ready=True)
                        healthy_written = True
                    elif not ready:
                        self._write_state("degraded", tunnel_ready=False)
                        healthy_written = False
                if (
                    exit_code is None
                    and elapsed >= self.config.settings.stable_reset_seconds
                    and self.restart_budget.count
                ):
                    self.restart_budget.clear()
                    self._write_state("healthy", tunnel_ready=self._ready())
                reason: str | None = None
                if exit_code is not None:
                    reason = f"tunnel_client_exit_{exit_code}"
                elif self.recover_event.is_set():
                    reason = self.failure_reason or "mcp_transport_failure"
                if reason is None:
                    self.stop_event.wait(self.config.settings.health_poll_seconds)
                    continue
                now = self.monotonic()
                self.last_recovery_reason = reason
                if not self.restart_budget.allow(now):
                    self._append_recovery_event(
                        reason="restart_budget_exhausted", backoff_until=None
                    )
                    self._write_state("failed", error="restart_budget_exhausted")
                    terminal_state = "failed"
                    return 2
                self._write_state("recovering")
                self._stop_child()
                if self._backoff(reason):
                    break
                healthy_written = False
                if not self._spawn_with_recovery():
                    if not self._stop_requested():
                        terminal_state = "failed"
                        return 2
                    break
            return 0
        finally:
            if terminal_state != "failed":
                self._write_state("stopping")
            self._stop_child()
            if terminal_state != "failed":
                self._write_state("stopped")
            if self.stop_path is not None:
                try:
                    self.stop_path.unlink()
                except FileNotFoundError:
                    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Supervise one TianCheng tunnel profile")
    parser.add_argument("--defaults", type=Path, required=True)
    parser.add_argument("--local-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = load_launcher_config(args.defaults.resolve(), args.local_config.resolve())
        ProxySettings.load(
            args.defaults.resolve(), args.local_config.resolve()
        ).apply_to_process()
        if _PROFILE_PATTERN.fullmatch(args.profile) is None:
            raise ValueError("invalid profile name")
        if args.check:
            print(
                json.dumps(
                    {
                        "valid": True,
                        "profile": args.profile,
                        "supervisor_enabled": config.settings.enabled,
                        "mcp_connection_max_ttl": config.settings.connection_max_ttl,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if not config.settings.enabled:
            raise ValueError("supervisor is disabled in launcher config")
        if args.state_dir is None:
            raise ValueError("--state-dir is required unless --check is used")
        state_dir = args.state_dir.resolve()
        state_path = state_dir / f"tunnel-supervisor-{args.profile}.json"
        lock_path = state_dir / f"tunnel-supervisor-{args.profile}.lock"
        stop_path = state_dir / f"tunnel-supervisor-{args.profile}.stop"
        try:
            stop_path.unlink()
        except FileNotFoundError:
            pass
        recovery_log_path = (
            args.defaults.resolve().parent.parent
            / "logs"
            / f"tunnel-supervisor-{args.profile}.jsonl"
        )
        with ProfileLock(lock_path):
            return TunnelSupervisor(
                config=config,
                profile=args.profile,
                state_path=state_path,
                stop_path=stop_path,
                recovery_log_path=recovery_log_path,
            ).run()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Tunnel supervisor error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
