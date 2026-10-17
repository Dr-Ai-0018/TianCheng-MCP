"""Bounded per-producer JSONL persistence. Observation faults never undo work."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .execution_config import CaptureSettings
from .execution_events import KINDS, OUTCOMES, PHASES, label, redact_output, safe_resources, safe_result, safe_target, utc
from .execution_paths import FileLock, atomic_json, checked_path, private_directory, private_open, producer_live, validate_private, validate_root

_SEGMENT = re.compile(r'(summary|detail)-[0-9]{6}\.jsonl\Z')
_UUID = re.compile(r'[a-f0-9]{32}\Z')
_FIELDS = {'operation_id', 'parent_operation_id', 'invocation_id', 'origin_invocation_id', 'resources',
           'kind', 'phase', 'tool', 'provider', 'target', 'origin', 'evidence', 'invocation_outcome',
           'operation_outcome', 'result', 'capture', 'detail_refs'}


def encoded(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def read_json(path: Path, maximum: int = 128 * 1024) -> dict:
    validate_private(path)
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum: raise ValueError('Record metadata exceeds its budget')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('Invalid record metadata')
    return value


@dataclass(frozen=True)
class StoreLimits:
    # Internal test injection only; user configuration retains validated MiB
    # limits. Tiny values let real rotation/pressure tests stay inexpensive.
    segment_bytes: int = 5 * 1024 * 1024
    normal_items: int = 1024
    normal_bytes: int = 4 * 1024 * 1024
    critical_items: int = 256
    critical_bytes: int = 1024 * 1024
    summary_bytes: int | None = None
    detail_bytes: int | None = None
    metadata_bytes: int = 8 * 1024 * 1024
    sync_seconds: float = 1.0


class ExecutionStore:
    def __init__(self, root: Path, settings: CaptureSettings | None = None,
                 *, workspace: Path | None = None, limits: StoreLimits | None = None,
                 producer_origin: str = 'local_admin') -> None:
        if producer_origin not in ('mcp', 'local_admin', 'supervisor'):
            raise ValueError('Invalid producer origin')
        self.producer_origin = producer_origin
        self.settings = settings or CaptureSettings()
        self.root = Path(os.path.abspath(root)) if self.settings.mode == 'off' else validate_root(root, workspace)
        self.limits = limits or StoreLimits()
        self.producer_id = uuid.uuid4().hex
        self.directory = self.root / 'producers' / self.producer_id
        self.started = time.monotonic()
        self._condition = threading.Condition()
        self._normal, self._critical = deque(), deque()
        self._normal_bytes = self._critical_bytes = 0
        self._seq = self._written = self._synced = 0
        self._failures = self._dropped_summary = self._dropped_detail = 0
        self._last_error = None
        self._loss_pending = 0
        self._loss_first = self._loss_last = None
        self._stopping = self._closed = False
        self._available = False
        self._thread = None
        self._owner = None
        self._files: dict[str, tuple[Path, object]] = {}
        self._numbers = {'summary': 0, 'detail': 0}
        self._segments: list[dict] = []
        self._last_sync = None
        self._manifest = {'schema_version': 1, 'producer_id': self.producer_id,
                          'mode': self.settings.mode, 'started_at': utc(), 'closed': False}
        if self.settings.mode == 'off': return
        try:
            private_directory(self.root)
            private_directory(self.root / 'producers', self.root)
            setup_lock = FileLock(self.root / 'store.lock').acquire(timeout=2)
            try:
                producers = self._producer_dirs()
                if len(producers) >= 2048 or sum(producer_live(p) is not False for p in producers) >= 64:
                    raise RuntimeError('Producer capacity exceeded')
                private_directory(self.directory, self.root)
                self._owner = FileLock(self.directory / 'owner.lock').acquire()
                self._write_manifest()
            finally:
                setup_lock.release()
            self._available = True
            self._thread = threading.Thread(target=self._writer, name='execution-record-writer', daemon=True)
            self._thread.start()
            self.publish(self._producer_event('started'))
        except Exception as exc:
            self.note_failure(type(exc).__name__)
            if self._owner: self._owner.release(); self._owner = None

    def _producer_dirs(self) -> list[Path]:
        directories = []
        for p in (self.root / 'producers').iterdir():
            if len(directories) >= 2048: raise RuntimeError('Producer metadata capacity exceeded')
            if not _UUID.fullmatch(p.name): raise PermissionError('Unexpected record directory')
            validate_private(p, directory=True); directories.append(p)
        return directories

    def _producer_event(self, phase: str, result: dict | None = None) -> dict:
        return {'operation_id': None, 'parent_operation_id': None, 'invocation_id': None,
            'kind': 'integrity' if phase == 'loss' else 'producer', 'phase': phase,
            'origin': {'channel': self.producer_origin}, 'evidence': 'observed',
            'invocation_outcome': 'unknown', 'operation_outcome': 'unknown',
            'result': result or {}, 'capture': {'mode': self.settings.mode, 'payload_truncated': False}}

    def note_failure(self, error: str) -> None:
        with self._condition:
            self._failures = min(self._failures + 1, 2**63 - 1)
            self._last_error = label(error) or 'ObservationError'

    def _project(self, payload: dict, channel: str) -> dict:
        allowed = _FIELDS if channel == 'summary' else {'operation_id', 'parent_operation_id', 'invocation_id',
            'kind', 'phase', 'stream', 'text', 'source_bytes', 'capture'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError('Unexpected event payload')
        for key in ('operation_id', 'parent_operation_id', 'invocation_id', 'origin_invocation_id'):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or not _UUID.fullmatch(value)):
                raise ValueError('Invalid observation identity')
        if payload.get('kind') not in KINDS or payload.get('phase') not in PHASES:
            raise ValueError('Invalid observation phase')
        capture = payload.get('capture', {})
        if not isinstance(capture, dict) or capture.get('mode') not in ('off', 'summary', 'detail'):
            raise ValueError('Invalid capture policy')
        result = {key: payload.get(key) for key in ('operation_id', 'parent_operation_id', 'invocation_id', 'kind', 'phase')}
        result['capture'] = {'mode': capture['mode'], 'payload_truncated': bool(capture.get('payload_truncated')),
                             'capture_disabled': bool(capture.get('capture_disabled'))}
        if channel == 'detail':
            if self.settings.mode != 'detail' or payload.get('stream') not in ('stdout', 'stderr') or not isinstance(payload.get('text'), str):
                raise ValueError('Invalid detail event')
            if time.monotonic() - self.started >= self.settings.detail_window_minutes * 60:
                return {}
            text, clipped = redact_output(payload['text'], 12 * 1024)
            result.update(stream=payload['stream'], text=text,
                          source_bytes=payload.get('source_bytes') if type(payload.get('source_bytes')) is int else 0)
            result['capture']['payload_truncated'] |= clipped
        else:
            if payload.get('operation_outcome', 'unknown') not in OUTCOMES:
                raise ValueError('Invalid operation outcome')
            origin = payload.get('origin', {})
            if not isinstance(origin, dict): raise ValueError('Invalid origin')
            result.update(origin={'channel': origin.get('channel') if origin.get('channel') in ('mcp', 'local_admin', 'supervisor') else 'mcp'},
                evidence=payload.get('evidence') if payload.get('evidence') in ('observed','inferred','unavailable') else 'unavailable',
                invocation_outcome=payload.get('invocation_outcome') if payload.get('invocation_outcome') in ('accepted','returned','error','unknown') else 'unknown',
                operation_outcome=payload.get('operation_outcome', 'unknown'), result=safe_result(payload.get('result')),
                resources=safe_resources(payload.get('resources')), target=safe_target(payload.get('target')),
                tool=label(payload.get('tool')), provider=label(payload.get('provider')),
                origin_invocation_id=payload.get('origin_invocation_id'), detail_refs=[])
        return result

    def publish(self, payload: dict, *, channel: str = 'summary') -> str:
        if self.settings.mode == 'off': return 'disabled'
        try:
            if channel not in ('summary', 'detail'): raise ValueError('Invalid event channel')
            event = self._project(payload, channel)
            if not event: return 'disabled'
            critical = channel == 'summary' and event['phase'] not in ('progress',)
            with self._condition:
                if self._stopping or not self._available:
                    self._drop(channel); return 'dropped'
                self._seq += 1
                event.update(schema_version=1, producer_id=self.producer_id, event_id=uuid.uuid4().hex,
                    seq=self._seq, occurred_at=utc(), recorded_at='0000-00-00T00:00:00.000000+00:00',
                    elapsed_ms=int((time.monotonic() - self.started) * 1000), channel=channel)
                raw = encoded(event)
                if len(raw) > (4096 if channel == 'summary' else 16384):
                    self._drop(channel, self._seq); return 'dropped'
                q = self._critical if critical else self._normal
                max_items = self.limits.critical_items if critical else self.limits.normal_items
                maximum = self.limits.critical_bytes if critical else self.limits.normal_bytes
                used = self._critical_bytes if critical else self._normal_bytes
                if len(q) >= max_items or used + len(raw) > maximum:
                    self._drop(channel, self._seq); return 'dropped'
                q.append((self._seq, channel, event, len(raw)))
                if critical: self._critical_bytes += len(raw)
                else: self._normal_bytes += len(raw)
                self._condition.notify_all()
            return 'accepted'
        except Exception as exc:
            self.note_failure(type(exc).__name__)
            with self._condition: self._drop(channel)
            return 'dropped'

    def _drop(self, channel: str, seq: int | None = None) -> None:
        if channel == 'detail': self._dropped_detail += 1
        else: self._dropped_summary += 1
        self._loss_pending += 1
        if seq is not None:
            self._loss_first = seq if self._loss_first is None else min(seq, self._loss_first)
            self._loss_last = seq if self._loss_last is None else max(seq, self._loss_last)

    def health(self) -> dict:
        with self._condition:
            return {'mode': self.settings.mode, 'producer_id': self.producer_id,
                'storage_available': self._available, 'queue_depth': len(self._normal) + len(self._critical),
                'queue_bytes': self._normal_bytes + self._critical_bytes,
                'dropped_summary': self._dropped_summary, 'dropped_detail': self._dropped_detail,
                'write_failure_count': self._failures, 'last_error_type': self._last_error,
                'observed_seq': self._seq, 'written_seq': self._written, 'synced_seq': self._synced,
                'last_sync_time': self._last_sync, 'shutdown_incomplete': self._stopping and not self._closed}

    def _write_manifest(self) -> None:
        health = self.health()
        if self._manifest['closed']:
            # This snapshot is committed atomically only after the final sync.
            health.update(storage_available=False, shutdown_incomplete=False)
        report = {**self._manifest, 'segments': self._segments, 'health': health}
        raw = encoded(report)
        if len(raw) > 128 * 1024: raise RuntimeError('Producer manifest exceeds its budget')
        if (self.root / 'producers').exists():
            _, metadata = self._catalog()
            if metadata + len(raw) > self.limits.metadata_bytes:
                raise RuntimeError('Record metadata budget exhausted')
        atomic_json(self.directory / 'manifest.json', raw)

    def _catalog(self) -> tuple[list[dict], int]:
        segments = []; metadata = 0
        for producer in self._producer_dirs():
            live = producer_live(producer)
            files = list(producer.iterdir())
            if len(files) > 1024: raise RuntimeError('Segment metadata capacity exceeded')
            highest = {c: max((p.name for p in files if p.name.startswith(c+'-') and _SEGMENT.fullmatch(p.name)), default='') for c in ('summary', 'detail')}
            for p in files:
                validate_private(p)
                info = p.stat()
                match = _SEGMENT.fullmatch(p.name)
                if match:
                    reservation = p.with_name(p.name + '.reserve')
                    reserved = read_json(reservation).get('bytes', 0) if reservation.exists() else 0
                    if type(reserved) is not int or reserved < 0 or reserved > 16 * 1024 * 1024:
                        raise ValueError('Invalid segment reservation')
                    segments.append({'path': p, 'channel': match[1], 'size': info.st_size,
                        'charged': max(info.st_size, reserved) if live is not False else info.st_size,
                        'mtime': info.st_mtime, 'active': live is not False and p.name == highest[match[1]]})
                else: metadata += info.st_size
            if len(segments) > 4096: raise RuntimeError('Segment metadata capacity exceeded')
        for p in (self.root/'retention.json', self.root/'store.lock'):
            if p.exists(): validate_private(p); metadata += p.stat().st_size
        return segments, metadata

    def _collect(self, channel: str, incoming: int) -> None:
        segments, metadata = self._catalog()
        if metadata > self.limits.metadata_bytes: raise RuntimeError('Record metadata budget exhausted')
        maximum = (self.limits.summary_bytes if channel == 'summary' else self.limits.detail_bytes)
        if maximum is None:
            maximum = getattr(self.settings, channel+'_max_mib') * 1024 * 1024
        current = [s for s in segments if s['channel'] == channel]
        used = sum(s['charged'] for s in current)
        cutoff = time.time() - getattr(self.settings, channel+'_retention_days') * 86400
        removed = []
        for segment in sorted(current, key=lambda s: (s['mtime'], str(s['path']))):
            if segment['active']: continue
            if segment['mtime'] >= cutoff and used + incoming <= maximum: continue
            path = checked_path(segment['path'], self.root); validate_private(path)
            path.unlink()
            reservation = path.with_name(path.name + '.reserve')
            if reservation.exists():
                validate_private(reservation); reservation.unlink()
            used -= segment['charged']; removed.append((path.parent.name, path.name))
        if removed:
            old = read_json(self.root/'retention.json') if (self.root/'retention.json').exists() else {}
            report = {'schema_version': 1, 'generation': int(old.get('generation',0))+1,
                      'last_evicted_at': utc(), 'removed_segments_total': int(old.get('removed_segments_total',0))+len(removed),
                      'last_removed': removed[-32:]}
            atomic_json(self.root/'retention.json', encoded(report))
            self._segments[:] = [s for s in self._segments if (self.directory / s['name']).exists()]
        if used + incoming > maximum: raise RuntimeError('Record storage budget exhausted')

    def _write_event(self, channel: str, event: dict) -> None:
        event = {**event, 'recorded_at': utc()}
        raw = encoded(event)
        current = self._files.get(channel)
        if current is None or current[1].tell() + len(raw) > self.limits.segment_bytes:
            with FileLock(self.root/'store.lock'):
                current = self._files.get(channel)
                if current:
                    current[1].flush(); os.fsync(current[1].fileno()); current[1].close()
                    reservation = current[0].with_name(current[0].name + '.reserve')
                    validate_private(reservation); reservation.unlink()
                    self._files.pop(channel)
                self._collect(channel, max(self.limits.segment_bytes, len(raw)))
                self._numbers[channel] += 1
                if self._numbers[channel] > 999999: raise RuntimeError('Segment identifiers exhausted')
                path = self.directory / f'{channel}-{self._numbers[channel]:06d}.jsonl'
                handle = private_open(path)
                reservation = path.with_name(path.name + '.reserve')
                try:
                    atomic_json(reservation, encoded({'bytes': max(self.limits.segment_bytes, len(raw))}))
                except Exception:
                    handle.close(); path.unlink(missing_ok=True); raise
                self._files[channel] = (path, handle)
                self._segments.append({'name': path.name, 'channel': channel, 'first_seq': event['seq'], 'last_seq': event['seq']})
                current = (path, handle)
        # Capacity is reserved at segment creation under the global lock. Every
        # append consumes that credit, rather than rescanning all private ACLs
        # while holding a shared lock for each short operation.
        handle = current[1]; handle.seek(0, os.SEEK_END); offset = handle.tell()
        if offset + len(raw) > max(self.limits.segment_bytes, len(raw)):
            raise RuntimeError('Active segment exceeded its reservation')
        try:
            handle.write(raw); handle.flush()
        except Exception:
            handle.seek(offset); handle.truncate(); raise
        segment = next(s for s in self._segments if s['name'] == current[0].name)
        segment['last_seq'] = event['seq']
        with self._condition: self._written = event['seq']

    def _sync(self, *, metadata: bool = True) -> None:
        for _, handle in self._files.values(): handle.flush(); os.fsync(handle.fileno())
        with self._condition:
            self._synced = self._written; self._last_sync = utc()
        if metadata:
            with FileLock(self.root/'store.lock'):
                self._write_manifest()

    def _writer(self) -> None:
        synced_at = time.monotonic()
        try:
            while True:
                with self._condition:
                    if not self._normal and not self._critical:
                        if self._stopping: break
                        self._condition.wait(timeout=self.limits.sync_seconds)
                    candidates = [q for q in (self._normal,self._critical) if q]
                    item = None
                    if candidates:
                        q = min(candidates,key=lambda q:q[0][0]); item = q.popleft()
                        if q is self._critical: self._critical_bytes -= item[3]
                        else: self._normal_bytes -= item[3]
                try:
                    if item: self._write_event(item[1],item[2])
                    if time.monotonic()-synced_at >= self.limits.sync_seconds or (item and item[2]['phase'] in ('completed','failed','denied','closed')):
                        due = time.monotonic()-synced_at >= self.limits.sync_seconds
                        # During bounded shutdown only the final manifest needs
                        # publishing. Still sync every terminal event's bytes.
                        self._sync(metadata=due and not self._stopping)
                        if due: synced_at = time.monotonic()
                except Exception as exc:
                    self.note_failure(type(exc).__name__)
                    if item:
                        with self._condition: self._drop(item[1],item[0])
                    # Avoid a busy loop on a stalled/broken disk. The bounded
                    # admission queues keep producer calls independent.
                    time.sleep(min(0.05,self.limits.sync_seconds))
                with self._condition:
                    if self._loss_pending and not self._stopping:
                        pending=self._loss_pending; self._loss_pending=0
                        first,last=self._loss_first,self._loss_last
                        self._loss_first=self._loss_last=None
                    else: pending=0
                if pending:
                    outcome=self.publish(self._producer_event('loss',{'dropped_count':pending,
                        'loss_first_seq':first, 'loss_last_seq':last}))
                    if outcome != 'accepted':
                        with self._condition:
                            self._loss_pending += pending
                            if first is not None: self._loss_first = first
                            if last is not None: self._loss_last = last
            # The final manifest below contains the synced watermark and closed
            # state. Avoid a redundant full catalog scan and intermediate write,
            # especially while several producers share the Windows ACL boundary.
            self._sync(metadata=False)
            self._manifest['closed'] = True
            self._manifest['closed_at'] = utc()
            with FileLock(self.root/'store.lock'):
                for path,_ in self._files.values():
                    reservation = path.with_name(path.name + '.reserve')
                    if reservation.exists(): validate_private(reservation); reservation.unlink()
                self._write_manifest()
            with self._condition: self._closed = True
        except Exception as exc:
            self.note_failure(type(exc).__name__)
        finally:
            with self._condition:
                self._available = False
            for _,handle in self._files.values():
                try: handle.close()
                except OSError: pass
            if self._owner: self._owner.release(); self._owner=None

    def close(self, deadline_seconds: float = 2) -> str:
        if self.settings.mode=='off': return 'drained'
        with self._condition:
            already=self._stopping
        if not already: self.publish(self._producer_event('closed'))
        with self._condition:
            self._stopping=True; self._condition.notify_all()
        if self._thread: self._thread.join(timeout=max(0,min(deadline_seconds,2)))
        return 'drained' if self._closed else 'incomplete'
