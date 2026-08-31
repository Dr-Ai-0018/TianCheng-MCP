"""Small, daemon-thread job runtime used to keep MCP calls below bridge deadlines.

The runtime deliberately lives at the application layer instead of depending on
the evolving MCP Tasks specification.  A request may wait briefly for a result;
if it does not finish in that budget, the caller receives a stable job handle and
can use the status/result/cancel tools without holding the stdio request open.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable


JobOperation = Callable[[threading.Event], Any]


# The worker installs the current job's cancellation token in a context
# variable.  Service code (including scoped external services) can therefore
# cooperate with cancellation without changing every public tool signature.
_CURRENT_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "tiancheng_current_cancel_event", default=None
)
_CURRENT_JOB_ID: ContextVar[str | None] = ContextVar("tiancheng_current_job_id", default=None)


def current_cancel_event() -> threading.Event | None:
    return _CURRENT_CANCEL_EVENT.get()


def current_job_id() -> str | None:
    return _CURRENT_JOB_ID.get()


class JobCancelled(Exception):
    """Operation cooperatively stopped after a cancellation request."""


@dataclass
class JobRecord:
    job_id: str
    operation: str
    runner: JobOperation
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    state: str = "queued"
    cancel_requested: bool = False
    result: Any = None
    exception: Exception | None = None
    error: str | None = None
    error_type: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    def status(self) -> dict[str, Any]:
        now = time.time()
        started = self.started_at or self.created_at
        end = self.finished_at or now
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "state": self.state,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(max(0.0, end - started) * 1000, 1),
            "result_available": self.state == "succeeded" and self.result is not None,
            "error": self.error,
            "error_type": self.error_type,
            **{key: value for key, value in self.metadata.items() if key not in {"grant_id"}},
        }


class JobManager:
    """Daemon-worker job queue with bounded retention and short-wait fallback."""

    def __init__(
        self,
        *,
        workers: int = 4,
        max_queue: int = 64,
        max_records: int = 256,
        retention_seconds: int = 3600,
    ) -> None:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be between 1 and 16")
        self._queue: queue.Queue[JobRecord | None] = queue.Queue(maxsize=max_queue)
        self._records: dict[str, JobRecord] = {}
        self._idempotency: dict[str, tuple[str, str, JobRecord]] = {}
        self._lock = threading.RLock()
        self._max_records = max_records
        self._retention_seconds = retention_seconds
        self._stopping = threading.Event()
        self._workers: list[threading.Thread] = []
        for index in range(workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"tiancheng-job-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def submit(
        self,
        operation: str,
        runner: JobOperation,
        *,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> JobRecord:
        if self._stopping.is_set():
            raise RuntimeError("Job manager is shutting down")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("idempotency_key must be 1-128 characters")
            if idempotency_fingerprint is None:
                idempotency_fingerprint = operation
            if not isinstance(idempotency_fingerprint, str) or len(idempotency_fingerprint) > 128:
                raise ValueError("idempotency_fingerprint must be bounded text")
        record = JobRecord(
            job_id=f"job_{uuid.uuid4().hex}",
            operation=operation,
            runner=runner,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._purge_locked(time.time())
            self._reclaim_capacity_locked()
            if idempotency_key is not None:
                existing = self._idempotency.get(idempotency_key)
                if existing is not None:
                    existing_operation, existing_fingerprint, existing_record = existing
                    if (existing_operation, existing_fingerprint) != (
                        operation,
                        idempotency_fingerprint,
                    ):
                        raise ValueError("idempotency_key was already used for a different operation")
                    return existing_record
            if len(self._records) >= self._max_records:
                raise RuntimeError("Too many retained jobs; wait for old jobs to expire")
            self._records[record.job_id] = record
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (
                    operation,
                    idempotency_fingerprint or operation,
                    record,
                )
        try:
            self._queue.put_nowait(record)
        except queue.Full as exc:
            with self._lock:
                self._records.pop(record.job_id, None)
                if idempotency_key is not None:
                    self._idempotency.pop(idempotency_key, None)
            raise RuntimeError("Job queue is full; retry after current jobs finish") from exc
        return record

    def submit_and_wait(
        self,
        operation: str,
        runner: JobOperation,
        *,
        interactive_timeout: float,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> tuple[JobRecord, bool, Any]:
        record = self.submit(
            operation,
            runner,
            metadata=metadata,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
        )
        if record.done.wait(timeout=interactive_timeout):
            if record.exception is not None:
                raise record.exception
            return record, True, record.result
        return record, False, {
            "execution": "background",
            "job_id": record.job_id,
            "state": record.state,
            "operation": record.operation,
            "message": "Request exceeded the interactive budget; use job_status/job_result to continue.",
        }

    def get(self, job_id: str) -> JobRecord:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id is required")
        with self._lock:
            self._purge_locked(time.time())
            record = self._records.get(job_id)
        if record is None:
            raise FileNotFoundError("Unknown or expired job")
        return record

    def status(self, job_id: str) -> dict[str, Any]:
        return self.get(job_id).status()

    def list(self, *, include_finished: bool = True, limit: int = 50) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            self._purge_locked(time.time())
            records = list(self._records.values())
        records.sort(key=lambda item: item.created_at, reverse=True)
        if not include_finished:
            records = [item for item in records if item.state not in {"succeeded", "failed", "cancelled", "expired"}]
        return {"jobs": [item.status() for item in records[:limit]], "truncated": len(records) > limit}

    def result(self, job_id: str) -> Any:
        record = self.get(job_id)
        if not record.done.is_set():
            return {"job_id": job_id, "state": record.state, "ready": False}
        if record.error is not None:
            return {
                "job_id": job_id,
                "state": record.state,
                "ready": True,
                "error": record.error,
                "error_type": record.error_type,
            }
        return {"job_id": job_id, "state": record.state, "ready": True, "result": record.result}

    def cancel(self, job_id: str, reason: str = "") -> dict[str, Any]:
        record = self.get(job_id)
        if record.done.is_set():
            return {**record.status(), "already_finished": True}
        record.cancel_requested = True
        record.cancel_event.set()
        if record.state == "queued":
            record.state = "cancelled"
            record.finished_at = time.time()
            record.done.set()
        status = record.status()
        status["reason"] = (reason or "cancelled by caller")[:500]
        status["accepted"] = True
        return status

    def cancel_for_grant(self, grant_id: str, reason: str = "grant revoked") -> int:
        with self._lock:
            records = [item for item in self._records.values() if item.metadata.get("grant_id") == grant_id]
        count = 0
        for record in records:
            record.metadata["grant_revoked"] = True
            if record.done.is_set():
                # Do not expose a completed external result after its scope
                # has been revoked.  The bytes may remain in memory only until
                # this record is purged, but are never returned again.
                record.state = "expired"
                record.result = None
                record.error = reason
                record.error_type = "GrantRevoked"
                continue
            if not record.done.is_set():
                self.cancel(record.job_id, reason)
                count += 1
        return count

    def shutdown(self, *, wait_seconds: float = 2.0) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        with self._lock:
            records = list(self._records.values())
        for record in records:
            if not record.done.is_set():
                record.cancel_requested = True
                record.cancel_event.set()
                if record.state == "queued":
                    record.state = "expired"
                    record.error = "MCP server shutting down"
                    record.error_type = "Shutdown"
                    record.finished_at = time.time()
                    record.done.set()
        # Workers are daemon threads as a last-resort safety net, but normal
        # shutdown waits briefly so cooperative scanners and subprocess jobs
        # actually leave the process before stdio closes.
        deadline = time.monotonic() + max(0.0, wait_seconds)
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                record = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if record is None:
                self._queue.task_done()
                return
            if record.done.is_set():
                self._queue.task_done()
                continue
            record.state = "running"
            record.started_at = time.time()
            token = _CURRENT_CANCEL_EVENT.set(record.cancel_event)
            job_token = _CURRENT_JOB_ID.set(record.job_id)
            try:
                record.result = record.runner(record.cancel_event)
                if record.cancel_requested:
                    record.result = None
                    record.error = "Job cancelled"
                    record.error_type = "JobCancelled"
                    record.state = "cancelled"
                else:
                    record.state = "succeeded"
            except JobCancelled as exc:
                record.exception = exc
                record.error = str(exc) or "Job cancelled"
                record.error_type = type(exc).__name__
                record.state = "cancelled"
            except Exception as exc:  # noqa: BLE001 - preserve failure for job_status
                record.exception = exc
                record.error = str(exc) or type(exc).__name__
                record.error_type = type(exc).__name__
                record.state = "cancelled" if record.cancel_requested else "failed"
            finally:
                _CURRENT_CANCEL_EVENT.reset(token)
                _CURRENT_JOB_ID.reset(job_token)
                record.finished_at = time.time()
                record.done.set()
                self._queue.task_done()

    def _drop_locked(self, key: str) -> None:
        record = self._records.pop(key, None)
        if record is None:
            return
        stale_keys = [
            idem_key
            for idem_key, (_, _, idem_record) in self._idempotency.items()
            if idem_record is record
        ]
        for idem_key in stale_keys:
            self._idempotency.pop(idem_key, None)

    def _purge_locked(self, now: float) -> None:
        expired = [
            key
            for key, record in self._records.items()
            if record.done.is_set()
            and record.finished_at is not None
            and now - record.finished_at > self._retention_seconds
        ]
        for key in expired:
            self._drop_locked(key)

    def _reclaim_capacity_locked(self) -> None:
        """Evict the oldest finished records once the table is full.

        Retention alone is not enough: a caller that legitimately polls a
        long-running operation can fill the table long before the retention
        window elapses.  Refusing new work for a full hour in that situation
        would disable every job-routed tool, so reclaim finished records in
        completion order instead.  Unfinished records are never evicted.
        """

        overflow = len(self._records) - self._max_records + 1
        if overflow <= 0:
            return
        finished = sorted(
            (
                record
                for record in self._records.values()
                if record.done.is_set() and record.finished_at is not None
            ),
            key=lambda record: record.finished_at,
        )
        for record in finished[:overflow]:
            self._drop_locked(record.job_id)
