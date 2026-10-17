"""Private lifecycle observers. Never consume Agent raw protocol or prompts."""
from __future__ import annotations

import codecs
from dataclasses import replace
from functools import wraps
import threading

from .execution_events import EventSink, OperationContext, label


def isolated(method):
    @wraps(method)
    def observe(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            try:
                self.sink.store.note_failure(type(exc).__name__)
            except Exception:
                pass
            return None
    return observe


def detached(sink: EventSink, parent: OperationContext, resources: dict) -> OperationContext:
    context = sink.child(parent, resources)
    # The launch invocation can finish while this operation is still alive.
    return replace(context, root_operation_id=context.operation_id)


class ProcessObserver:
    def __init__(self, sink: EventSink, parent: OperationContext, *, process_id: str,
                 owner: str, program: str, provider: str | None = None):
        self.sink = sink
        resources = {'process_id': process_id, 'process_owner': owner}
        self.context = (detached(sink, parent, resources) if owner == 'process'
                        else sink.child(parent, resources))
        self.owner, self.program, self.provider = owner, program, provider
        self._lock = threading.RLock()
        self._closed = False
        self._decoders = {name: codecs.getincrementaldecoder('utf-8')('replace')
                          for name in ('stdout', 'stderr')}

    @isolated
    def started(self):
        self.sink.emit(self.context, 'process', 'started',
                       {'program': self.program, 'scope': 'managed_process'}, provider=self.provider)

    @isolated
    def output(self, stream: str, chunk: bytes):
        # Agent pipes contain protocol, messages, approvals and possibly secrets.
        # Their allowed metadata is projected by AgentObserver instead.
        if self.owner != 'process' or self.context.capture_mode != 'detail':
            return
        with self._lock:
            if self._closed:
                return
            text = self._decoders[stream].decode(chunk)
            self.sink.detail(self.context, stream, text)

    @isolated
    def cancel_requested(self, reason_code: str = 'caller'):
        self.sink.emit(self.context, 'process', 'cancel_requested',
                       {'cancel_requested': True, 'reason_code': reason_code,
                        'scope': 'managed_process'}, provider=self.provider)

    @isolated
    def finished(self, facts: dict):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.owner == 'process':
                for stream, decoder in self._decoders.items():
                    self.sink.detail(self.context, stream, decoder.decode(b'', final=True))
                self.sink.finish_detail(self.context)
            code = facts.get('exit_code')
            outcome = ('unknown' if type(code) is not int else
                       'timed_out' if facts.get('timeout') else
                       'cancelled' if facts.get('cancel_requested') else
                       'failed' if type(code) is int and code != 0 else
                       'succeeded' if type(code) is int else 'unknown')
            self.sink.emit(self.context, 'process', 'completed',
                {**facts, 'program': self.program, 'scope': 'managed_process',
                 'termination_observed': type(code) is int},
                operation_outcome=outcome, provider=self.provider)


class AgentObserver:
    def __init__(self, sink: EventSink, parent: OperationContext, *, session_id: str,
                 run_id: str, provider: str):
        self.sink = sink
        self.context = detached(sink, parent, {'agent_session_id': session_id, 'run_id': run_id})
        self.provider = provider
        self._closed = False
        self._commands: dict[tuple[str, str], OperationContext] = {}
        self._approvals: dict[str, OperationContext] = {}
        self._lock = threading.RLock()

    @isolated
    def validated(self):
        self.sink.emit(self.context, 'agent', 'validated', {'scope': 'agent_lifecycle'}, provider=self.provider)

    @isolated
    def started(self):
        self.sink.emit(self.context, 'agent', 'started', {'scope': 'agent_lifecycle'}, provider=self.provider)

    @isolated
    def launch_failed(self, error_type: str):
        self.sink.emit(self.context, 'agent', 'failed',
            {'reason_code': error_type, 'scope': 'agent_lifecycle'},
            operation_outcome='failed', provider=self.provider)

    @isolated
    def source(self, event_type: str, metadata: dict):
        # The parser supplies only this explicit projection, never an item/raw dict.
        with self._lock:
            if self._closed:
                return
            seq = metadata.get('source_seq')
            facts = {'source_seq': seq, 'scope': 'provider_event', 'state': event_type}
            if event_type not in ('command_started', 'command_completed', 'file_changed'):
                self.sink.emit(self.context, 'agent', 'progress', facts, provider=self.provider)
                return
            scope = 'provider_file_metadata' if event_type == 'file_changed' else 'provider_command'
            facts['scope'] = scope
            facts['count'] = metadata.get('count')
            identity = label(metadata.get('native_item_id'))
            if event_type == 'command_completed':
                code, state = metadata.get('exit_code'), metadata.get('state')
                outcome = ('failed' if state == 'failed' or type(code) is int and code != 0 else
                           'succeeded' if state == 'completed' and type(code) is int else 'unknown')
                facts.update(exit_code=code, command_outcome=outcome)
            else:
                outcome = 'pending' if event_type == 'command_started' else 'unknown'
            if identity is None:
                self.sink.emit(self.context, 'agent', 'progress',
                    {**facts, 'reason_code': 'native_item_id_unavailable'}, provider=self.provider)
                return
            key = (scope, identity)
            context = self._commands.get(key)
            if context is None:
                if len(self._commands) >= 256:
                    self.sink.emit(self.context, 'agent', 'progress',
                        {**facts, 'reason_code': 'native_item_tracking_limit'}, provider=self.provider)
                    return
                context = self.sink.child(self.context, {'native_item_id': identity})
                self._commands[key] = context
            self.sink.emit(context, 'agent', 'started' if event_type == 'command_started' else 'completed',
                           facts, operation_outcome=outcome, provider=self.provider)

    @isolated
    def approval(self, phase: str, approval_id: str, metadata: dict):
        with self._lock:
            context = self._approvals.get(approval_id)
            if context is None:
                if phase != 'received' or self._closed or len(self._approvals) >= 256:
                    return
                context = self.sink.child(self.context, {'approval_id': approval_id,
                    'native_item_id': metadata.get('native_item_id')})
                self._approvals[approval_id] = context
            outcome = ('failed' if phase == 'response_failed' else
                       'cancelled' if phase == 'withdrawn' else 'unknown')
            self.sink.emit(context, 'approval', phase,
                {'scope': 'approval_delivery', 'decision': metadata.get('decision'),
                 'state': phase, 'task_outcome': 'unverified'},
                operation_outcome=outcome, provider=self.provider)

    @isolated
    def finished(self, *, state: str, code: int | None, commands: dict, provider_errors: int,
                 output_gap: bool):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            outcome = state if state in ('failed', 'cancelled', 'timed_out') else (
                'cancelled' if state == 'aborted_on_shutdown' else 'unknown')
            self.sink.finish_detail(self.context)
            self.sink.emit(self.context, 'agent', 'completed',
                {'scope': 'agent_lifecycle', 'state': state, 'exit_code': code,
                 'process_outcome': 'succeeded' if state == 'succeeded' else outcome,
                 'task_outcome': 'unverified', 'commands_status': commands.get('status'),
                 'commands_observed': commands.get('completed_events'),
                 'command_failures': commands.get('failed_events'),
                 'commands_unknown': commands.get('unknown_events'),
                 'provider_errors': provider_errors, 'output_truncated': output_gap},
                operation_outcome=outcome, provider=self.provider)
            self._commands.clear()
            self._approvals.clear()
