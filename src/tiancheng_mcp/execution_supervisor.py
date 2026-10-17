"""Independent Supervisor observations; never copy Tunnel logs or request IDs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import uuid

from .execution_config import CaptureSettings
from .execution_events import EventSink, OperationContext
from .execution_lifecycle import isolated
from .execution_store import ExecutionStore


class SupervisorObserver:
    def __init__(self, store: ExecutionStore, profile: str):
        self.store = store
        self.sink = EventSink(store)
        self.context = OperationContext(store.producer_id, uuid.uuid4().hex, uuid.uuid4().hex,
            tool='tunnel_supervisor', origin='supervisor',
            resources={'supervisor_profile': profile}, capture_mode=store.settings.mode)
        self._lock = threading.RLock()
        self._closed = False
        self._last_state = None
        self._last_ready = None
        self._child = None
        self._child_closed = True
        self._child_stop = False

    @isolated
    def started(self, settings):
        self.sink.emit(self.context, 'supervisor', 'started', {
            'scope': 'supervisor_lifecycle',
            'restart_budget_max_attempts': settings.restart_budget_max_attempts,
            'restart_budget_window_seconds': settings.restart_budget_window_seconds,
        })

    @isolated
    def state(self, state: str, generation: int, restart_count: int, reason: str | None,
              error: str | None, process_running: bool, tunnel_ready: bool | None):
        with self._lock:
            snapshot = (state, generation, restart_count, reason, error, process_running, tunnel_ready)
            if self._closed or snapshot == self._last_state:
                return
            self._last_state = snapshot
            self.sink.emit(self.context, 'supervisor', 'progress', {
                'scope': 'supervisor_state', 'state': state, 'generation': generation,
                'restart_count': restart_count, 'reason_code': error or reason,
                'process_running': process_running, 'tunnel_ready': tunnel_ready,
                'health_source': 'process_and_failure_log',
                'mcp_transport': 'inferred' if state == 'healthy' else 'unavailable',
            }, evidence='inferred' if state == 'healthy' else 'observed')

    @isolated
    def ready(self, generation: int, ready: bool):
        # A successful loopback readyz probe is observed; MCP stdio is unverified.
        with self._lock:
            if self._closed or self._last_ready == (generation, ready):
                return
            self._last_ready = (generation, ready)
            self.sink.emit(self.context, 'supervisor', 'progress', {
                'scope': 'tunnel_readiness', 'generation': generation, 'tunnel_ready': ready,
                'health_source': 'readyz_http', 'mcp_transport': 'unavailable',
            })

    @isolated
    def recovery(self, generation: int, reason: str, count: int, until: str | None):
        with self._lock:
            if self._closed:
                return
            self.sink.emit(self.context, 'supervisor', 'recovery', {
                'scope': 'supervisor_recovery', 'generation': generation,
                'next_generation': generation + 1, 'reason_code': reason,
                'restart_count': count, 'backoff_until': until,
            })

    @isolated
    def child_started(self, generation: int, pid: int):
        with self._lock:
            if self._closed:
                return
            self._child = self.sink.child(self.context)
            self._child_closed = False
            self._child_stop = False
            self.sink.emit(self._child, 'process', 'started', {
                'scope': 'tunnel_process', 'generation': generation, 'pid': pid,
            })

    @isolated
    def child_stop_requested(self, generation: int):
        with self._lock:
            if self._closed or self._child is None or self._child_closed or self._child_stop:
                return
            self._child_stop = True
            self.sink.emit(self._child, 'process', 'cancel_requested', {
                'scope': 'tunnel_process', 'generation': generation, 'cancel_requested': True,
            })

    @isolated
    def child_finished(self, generation: int, code: int | None):
        with self._lock:
            if self._closed or self._child is None or self._child_closed:
                return
            self._child_closed = True
            self.sink.emit(self._child, 'process', 'completed', {
                'scope': 'tunnel_process', 'generation': generation, 'exit_code': code,
                'termination_observed': type(code) is int,
            }, operation_outcome='unknown' if type(code) is not int else
                'cancelled' if self._child_stop else 'succeeded' if code == 0 else 'failed')

    @isolated
    def finished(self, state: str):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.sink.emit(self.context, 'supervisor', 'completed', {
                'scope': 'supervisor_lifecycle', 'state': state,
            }, operation_outcome='failed' if state == 'failed' else
                'cancelled' if state == 'stopped' else 'unknown')

    def health(self) -> dict:
        return self.store.health()

    def close(self):
        result = self.store.close()
        if result != 'drained':
            import sys
            print('Supervisor observation shutdown incomplete', file=sys.stderr)
        return result


def open_supervisor_observer(defaults: Path, local: Path, profile: str) -> SupervisorObserver | None:
    """Read only capture configuration; unrelated runtime options cannot block Tunnel."""
    store = None
    try:
        capture = {}
        workspace = None
        for path in (defaults, local):
            if path.is_file():
                document = json.loads(path.read_text(encoding='utf-8-sig'))
                fragment = document.get('observability', {})
                if not isinstance(fragment, dict):
                    raise ValueError('Invalid observability configuration')
                capture.update(fragment)
                workspace = document.get('workspace', workspace)
        settings = CaptureSettings.parse(capture)
        if settings.mode == 'off':
            return None
        project = Path(__file__).resolve().parents[2]
        workspace = os.environ.get('TIANCHENG_WORKSPACE') or workspace
        workspace_path = project / workspace if workspace else None
        store = ExecutionStore(settings.root(project), settings, workspace=workspace_path,
                               producer_origin='supervisor')
        return SupervisorObserver(store, profile)
    except Exception as exc:
        if store is not None:
            store.close()
        # No exception body: configuration can contain credentials and private paths.
        import sys
        print(f'Supervisor observation unavailable ({type(exc).__name__})', file=sys.stderr)
        return None
