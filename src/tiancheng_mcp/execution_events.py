"""Structured observation projections, distinct from existing MCP/Agent events."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import time
from types import MappingProxyType
from typing import Mapping, TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
import uuid
from contextvars import ContextVar

from .execution_config import CaptureSettings
if TYPE_CHECKING:
    from .execution_store import ExecutionStore

KINDS = {'tool', 'job', 'process', 'agent', 'approval', 'producer', 'supervisor', 'integrity'}
PHASES = {'received', 'validated', 'denied', 'queued', 'started', 'progress', 'handoff',
          'completed', 'failed', 'cancel_requested', 'cancelled', 'reused', 'expired',
          'decision_submitted', 'response_delivered', 'response_failed', 'withdrawn',
          'closed', 'loss', 'recovery', 'evicted'}
OUTCOMES = {'pending', 'succeeded', 'failed', 'denied', 'timed_out', 'cancelled', 'unknown'}
RESOURCES = {'job_id', 'process_id', 'agent_session_id', 'run_id', 'approval_id',
             'native_item_id', 'process_owner', 'supervisor_profile'}
_LABEL = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_INTS = {'exit_code', 'duration_ms', 'bytes', 'bytes_written', 'source_bytes', 'returned_bytes',
         'stdout_bytes_total', 'stderr_bytes_total', 'count', 'dropped_count', 'source_seq',
         'loss_first_seq', 'loss_last_seq', 'commands_observed', 'command_failures',
         'commands_unknown', 'provider_errors', 'generation', 'next_generation',
         'restart_count', 'restart_budget_max_attempts', 'restart_budget_window_seconds',
         'pid', 'backoff_ms'}
_BOOLS = {'timeout', 'cancel_requested', 'termination_observed', 'resources_released',
          'atomic_replace', 'atomic_no_overwrite', 'output_drain_incomplete', 'output_truncated',
          'tunnel_ready', 'process_running'}
_CODES = {'reason_code', 'state', 'command_outcome', 'program', 'scope',
          'decision', 'task_outcome', 'process_outcome', 'commands_status',
          'health_source', 'mcp_transport'}
_HASHES = {'sha256', 'before_sha256', 'after_sha256', 'source_sha256', 'returned_sha256'}
_SECRETS = (
    (re.compile(r'(?i)\b(?:sk-[\w-]{6,}|gh[pousr]_[\w]{6,}|github_pat_[\w]{6,})'), '[redacted]'),
    (re.compile(r'(?i)\b(?:bearer\s+)[^\s"\']+'), 'Bearer [redacted]'),
    (re.compile(r'(?i)((?:api[_-]?key|password|secret|token|signature|authorization)\s*[=:]\s*)[^\s,;"\']+'), r'\1[redacted]'),
    (re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)'), '[private key redacted]'),
)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def clip(value: str, maximum: int) -> tuple[str, bool]:
    raw = value[:maximum + 1].encode('utf-8')
    return raw[:maximum].decode('utf-8', errors='ignore'), len(value) > maximum or len(raw) > maximum


def redact_output(value: str, maximum: int) -> tuple[str, bool]:
    # Bound the input BEFORE regex work, including an unterminated private key.
    text, truncated = clip(value, maximum)
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    def url(match):
        try:
            p = urlsplit(match.group())
            if p.username or p.password or p.query or p.fragment:
                return urlunsplit((p.scheme, p.hostname or '', p.path, '', '')) + '[url credentials/query hidden]'
            return match.group()
        except ValueError:
            return '[url hidden]'
    text = re.sub(r'https?://[^\s"<>]+', url, text)
    return clip(text, maximum)[0], truncated


def label(value: object) -> str | None:
    if isinstance(value, str) and _LABEL.fullmatch(value):
        safe, _ = redact_output(value, 128)
        if safe == value:
            return value
    return None


def safe_result(value: Mapping | None) -> dict:
    result = {}
    if not isinstance(value, Mapping):
        return result
    for key in _INTS:
        v = value.get(key)
        if type(v) is int and -(2**31) <= v <= 2**63 - 1:
            result[key] = v
    for key in _BOOLS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in _CODES:
        v = label(value.get(key))
        if v is not None: result[key] = v
    for key in _HASHES:
        v = value.get(key)
        if isinstance(v, str) and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', v): result[key] = v
    value_time = value.get('backoff_until')
    if isinstance(value_time, str) and len(value_time) <= 40:
        try:
            parsed = datetime.fromisoformat(value_time.replace('Z', '+00:00'))
            if parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0:
                result['backoff_until'] = parsed.isoformat()
        except ValueError:
            pass
    return result


def safe_target(value: Mapping | None) -> dict:
    if not isinstance(value, Mapping): return {}
    result = {}
    if value.get('scope') in {'workspace', 'external', 'local', 'unknown'}:
        result['scope'] = value['scope']
    if label(value.get('category')) is not None:
        result['category'] = value['category']
    path = value.get('path')
    if isinstance(path, str):
        if len(path) > 256:
            result['path'] = '<hidden>'
            return result
        path = path.replace('\\', '/')
        parts = path.split('/')
        text, truncated = redact_output(path, 256)
        if (not path.startswith('/') and not any(p == '..' for p in parts)
                and not any(c in path for c in ':\0\r\n?') and not truncated and text == path):
            result['path'] = path
        else: result['path'] = '<hidden>'
    return result


def safe_resources(value: Mapping | None) -> dict:
    if not isinstance(value, Mapping): return {}
    return {k: v for k in RESOURCES if (v := label(value.get(k))) is not None}


def safe_argv(argv: list[str]) -> list[str]:
    if not argv or not isinstance(argv[0], str): return []
    program = argv[0][:4096].replace('\\', '/').rsplit('/', 1)[-1] if len(argv[0]) <= 4096 else '<program hidden>'
    program = label(program) or '<program hidden>'
    # Arbitrary code, shell bodies and positional strings remain opaque even
    # when the command policy trusts their executable prefix.
    result = [program]
    hide_next = False
    for arg in argv[1:33]:
        if not isinstance(arg, str): result.append('<hidden>'); continue
        if hide_next: result.append('<hidden>'); hide_next = False; continue
        if arg in ('-c', '-Command', '-EncodedCommand', '/c', '/C'):
            result.append(arg); hide_next = True
        elif re.fullmatch(r'--[a-zA-Z][a-zA-Z0-9-]{0,40}', arg):
            result.append(arg); hide_next = True
        elif arg in ('--version', '-V', '-q', '--help', 'status', 'diff', 'log'):
            result.append(arg)
        else: result.append('<hidden>')
    if len(argv) > 33: result.append('<arguments truncated>')
    return result


@dataclass(frozen=True)
class OperationContext:
    producer_id: str
    operation_id: str
    invocation_id: str
    parent_operation_id: str | None = None
    origin_invocation_id: str | None = None
    tool: str | None = None
    origin: str = 'mcp'
    target: Mapping = field(default_factory=lambda: MappingProxyType({}))
    resources: Mapping = field(default_factory=lambda: MappingProxyType({}))
    capture_mode: str = 'summary'
    root_operation_id: str | None = None


CURRENT_OPERATION: ContextVar[OperationContext | None] = ContextVar('execution_operation', default=None)


class EventSink:
    def __init__(self, store: 'ExecutionStore', settings: CaptureSettings | None = None) -> None:
        self.store = store
        self.settings = settings or store.settings
        self.started = time.monotonic()
        self.deadline = self.started + self.settings.detail_window_minutes * 60
        self._detail_totals: dict[str, dict[str, int]] = {}
        self._detail_buffers: dict[tuple[str, str], dict] = {}
        self._buffer_bytes = 0
        self._detail_entries = 0
        import threading
        self._detail_lock = threading.Lock()

    def begin_invocation(self, tool: str, target: Mapping | None = None, origin: str = 'mcp') -> OperationContext:
        context = OperationContext(self.store.producer_id, uuid.uuid4().hex, uuid.uuid4().hex,
            tool=label(tool), origin=origin if origin in ('mcp', 'local_admin', 'supervisor') else 'mcp',
            target=MappingProxyType(safe_target(target)), capture_mode=self.settings.mode)
        self.emit(context, 'tool', 'received', invocation_outcome='accepted')
        return context

    def child(self, context: OperationContext, resources: Mapping | None = None) -> OperationContext:
        return OperationContext(context.producer_id, uuid.uuid4().hex, context.invocation_id,
            parent_operation_id=context.operation_id, origin_invocation_id=context.origin_invocation_id,
            tool=context.tool, origin=context.origin, target=context.target,
            resources=MappingProxyType({**context.resources, **safe_resources(resources)}), capture_mode=context.capture_mode,
            root_operation_id=context.root_operation_id or context.operation_id)

    def emit(self, context: OperationContext, kind: str, phase: str, result: Mapping | None = None,
             *, invocation_outcome: str = 'unknown', operation_outcome: str = 'pending',
             evidence: str = 'observed', provider: str | None = None) -> str:
        try:
            if context.producer_id != self.store.producer_id or kind not in KINDS or phase not in PHASES:
                raise ValueError('Invalid observation binding')
            if operation_outcome not in OUTCOMES or evidence not in ('observed', 'inferred', 'unavailable'):
                raise ValueError('Invalid observation outcome')
            if invocation_outcome not in ('accepted', 'returned', 'error', 'unknown'):
                raise ValueError('Invalid invocation outcome')
            return self.store.publish({'operation_id': context.operation_id,
                'parent_operation_id': context.parent_operation_id, 'invocation_id': context.invocation_id,
                'origin_invocation_id': context.origin_invocation_id, 'resources': safe_resources(context.resources),
                'kind': kind, 'phase': phase, 'tool': context.tool, 'provider': label(provider),
                'target': safe_target(context.target), 'origin': {'channel': context.origin},
                'evidence': evidence, 'invocation_outcome': invocation_outcome,
                'operation_outcome': operation_outcome, 'result': safe_result(result),
                'capture': {'mode': context.capture_mode, 'payload_truncated': False,
                    'capture_disabled': context.capture_mode == 'detail' and time.monotonic() >= self.deadline}, 'detail_refs': []})
        except Exception as exc:
            self.store.note_failure(type(exc).__name__)
            return 'dropped'

    def detail(self, context: OperationContext, stream: str, text: str) -> str:
        if (context.capture_mode != 'detail' or self.settings.mode != 'detail'
                or time.monotonic() >= self.deadline or stream not in ('stdout', 'stderr')
                or context.producer_id != self.store.producer_id):
            return 'disabled'
        if not isinstance(text, str): return 'dropped'
        with self._detail_lock:
            # An explicit task completion should release this accounting using
            # finish_detail(). Refuse new identities at the bound, never evict
            # and accidentally reset an unfinished task's quota.
            root = context.root_operation_id or context.operation_id
            if root not in self._detail_totals and len(self._detail_totals) >= 1024:
                return 'dropped'
            totals = self._detail_totals.setdefault(root, {'all': 0})
            stream_key = context.operation_id + ':' + stream
            if stream_key not in totals and (len(totals) >= 257 or self._detail_entries >= 1024):
                return 'dropped'
            if stream_key not in totals: self._detail_entries += 1
            totals.setdefault(stream_key, 0)
            key = (context.operation_id, stream)
            state = self._detail_buffers.setdefault(key, {'text': '', 'hidden_line': False,
                'private_key': False, 'context': context, 'pending_truncated': False, 'quota_reported': False})
            remaining = min(64 * 1024 - totals.get(stream_key, 0), 1024 * 1024 - totals['all'])
            if remaining <= 0:
                if not state['quota_reported']:
                    self._publish_detail(context, stream, '', True, 0)
                    state['quota_reported'] = True
                return 'dropped'
            bounded, truncated = clip(text, remaining)
            size = len(bounded.encode('utf-8')); totals[stream_key] = totals.get(stream_key, 0) + size; totals['all'] += size
            if truncated:
                state['pending_truncated'] = True
                if not state['quota_reported']:
                    self._publish_detail(context, stream, '', True, 0)
                    state['quota_reported'] = True
            combined = state['text'] + bounded
            self._buffer_bytes -= len(state['text'].encode('utf-8')); state['text'] = ''
            outputs = []
            for line in combined.splitlines(keepends=True):
                if line.endswith(('\n', '\r')):
                    outputs.append((self._safe_line(state, line), len(line.encode('utf-8'))))
                    state['hidden_line'] = False
                elif len(line.encode('utf-8')) > 4096 or self._buffer_bytes + len(line.encode('utf-8')) > 1024 * 1024:
                    outputs.append(('[unterminated output line hidden]\n', len(line.encode('utf-8'))))
                    state['hidden_line'] = True; truncated = True
                else:
                    state['text'] = line; self._buffer_bytes += len(line.encode('utf-8'))
            blocks = []; block = ''; source_count = 0
            for value, count in outputs:
                if block and len(block.encode('utf-8')) + len(value.encode('utf-8')) > 12 * 1024:
                    blocks.append((block, source_count)); block = ''; source_count = 0
                block += value; source_count += count
            if block: blocks.append((block, source_count))
            outcomes = [self._publish_detail(context, stream, value, truncated, count) for value, count in blocks]
        return 'dropped' if 'dropped' in outcomes else 'accepted'

    @staticmethod
    def _safe_line(state: dict, line: str) -> str:
        if state['hidden_line']: return '[output continuation hidden]\n'
        if state['private_key'] or '-----BEGIN ' in line and 'PRIVATE KEY-----' in line:
            state['private_key'] = '-----END ' not in line
            return '[private key redacted]\n'
        return redact_output(line, 12 * 1024)[0]

    def _publish_detail(self, context: OperationContext, stream: str, safe: str, truncated: bool, source_bytes: int) -> str:
        safe, clipped = clip(safe, 12 * 1024)
        return self.store.publish({'operation_id': context.operation_id, 'parent_operation_id': context.parent_operation_id,
            'invocation_id': context.invocation_id, 'kind': 'process', 'phase': 'progress',
            'stream': stream, 'text': safe, 'source_bytes': source_bytes,
            'capture': {'mode': 'detail', 'payload_truncated': truncated or clipped}}, channel='detail')

    def finish_detail(self, context: OperationContext) -> None:
        with self._detail_lock:
            root = context.root_operation_id or context.operation_id
            keys = [k for k,s in self._detail_buffers.items()
                    if k[0] == context.operation_id or context.operation_id == root and (s['context'].root_operation_id or s['context'].operation_id) == root]
            for key in keys:
                state = self._detail_buffers.pop(key)
                self._buffer_bytes -= len(state['text'].encode('utf-8'))
                if state['text']:
                    self._publish_detail(state['context'], key[1], self._safe_line(state, state['text']),
                                         state['pending_truncated'], len(state['text'].encode('utf-8')))
            if context.operation_id == root:
                removed = self._detail_totals.pop(root, {})
                self._detail_entries -= max(0, len(removed) - 1)

    def health(self) -> dict:
        return self.store.health()
