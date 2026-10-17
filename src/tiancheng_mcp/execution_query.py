"""Bounded read-only history queries. Never import or start the MCP service."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .execution_events import KINDS, OUTCOMES, PHASES, label, redact_output, safe_resources, safe_result, safe_target
from .execution_paths import checked_path, private_open, producer_live, validate_private, validate_root
from .execution_store import _SEGMENT, _UUID, encoded, read_json


def _safe_event(event: object) -> dict:
    if not isinstance(event, dict) or type(event.get('schema_version')) is not int or event['schema_version'] != 1:
        raise ValueError('unsupported_schema')
    if event.get('kind') not in KINDS or event.get('phase') not in PHASES:
        raise ValueError('corrupt_record')
    for key in ('producer_id','event_id','operation_id','parent_operation_id','invocation_id','origin_invocation_id'):
        value = event.get(key)
        if key in ('producer_id', 'event_id') and value is None:
            raise ValueError('corrupt_record')
        if value is not None and (not isinstance(value,str) or not _UUID.fullmatch(value)):
            raise ValueError('corrupt_record')
    if type(event.get('seq')) is not int or not 0 < event['seq'] < 2**63:
        raise ValueError('corrupt_record')
    channel=event.get('channel')
    if channel not in ('summary','detail'): raise ValueError('corrupt_record')
    safe={k:event.get(k) for k in ('schema_version','producer_id','event_id','seq','operation_id',
        'parent_operation_id','invocation_id','origin_invocation_id','kind','phase','channel')}
    for key in ('occurred_at','recorded_at'):
        value=event.get(key)
        if not isinstance(value,str) or len(value)>64:
            raise ValueError('corrupt_record')
        datetime.fromisoformat(value); safe[key]=value
    safe['elapsed_ms']=event.get('elapsed_ms') if type(event.get('elapsed_ms')) is int else None
    capture=event.get('capture',{})
    if not isinstance(capture,dict): raise ValueError('corrupt_record')
    safe['capture']={k:v for k,v in capture.items() if k=='mode' and v in ('summary','detail','off') or k in ('payload_truncated','capture_disabled') and type(v) is bool}
    if channel=='detail':
        if event.get('stream') not in ('stdout','stderr') or not isinstance(event.get('text'),str): raise ValueError('corrupt_record')
        safe.update(stream=event['stream'],text=redact_output(event['text'],12*1024)[0],
                    source_bytes=event.get('source_bytes') if type(event.get('source_bytes')) is int else None)
    else:
        origin = event.get('origin', {})
        safe['origin'] = {'channel': origin.get('channel') if isinstance(origin, dict)
                          and origin.get('channel') in ('mcp', 'local_admin', 'supervisor') else 'unknown'}
        safe.update(tool=label(event.get('tool')),provider=label(event.get('provider')),
            target=safe_target(event.get('target')),resources=safe_resources(event.get('resources')),
            result=safe_result(event.get('result')),evidence=event.get('evidence') if event.get('evidence') in ('observed','inferred','unavailable') else 'unavailable',
            invocation_outcome=event.get('invocation_outcome') if event.get('invocation_outcome') in ('accepted','returned','error','unknown') else 'unknown',
            operation_outcome=event.get('operation_outcome') if event.get('operation_outcome') in OUTCOMES else 'unknown')
    return safe


class ExecutionQuery:
    def __init__(self, root: Path, *, workspace: Path | None = None) -> None:
        self.root=validate_root(root,workspace)
        self.store_id=hashlib.sha256(str(self.root).encode()).hexdigest()

    def _generation(self) -> int:
        path=self.root/'retention.json'
        return int(read_json(path).get('generation',0)) if path.exists() else 0

    @staticmethod
    def _segment_order(name: str) -> tuple[bool, str]:
        return name.startswith('detail-'), name

    def _sources(self, state: dict | None, deadline: float, quality: list[dict], progress: dict):
        """Walk one producer at a time; never pre-scan every private ACL.

        Producer names have a bounded lexical order. Segments retain append
        order within each channel. The query promises scanned-range ordering,
        not a global newest-first index. Each filesystem call is indivisible;
        the deadline is checked between calls and processing steps.
        """
        if not self.root.exists():
            return
        validate_private(self.root, directory=True)
        producers = self.root / 'producers'
        if not producers.exists():
            return
        validate_private(producers, directory=True)
        names = []
        for entry in producers.iterdir():
            if len(names) >= 2048:
                quality.append({'reason': 'producer_scan_limit'})
                progress['incomplete'] = True
                break
            names.append(entry.name)
        names.sort()
        start = state.get('producer') if state else None
        if start is not None and start not in names:
            progress['gap'] = True
        segment_count = 0
        for name in names:
            if start is not None and name < start:
                continue
            if not _UUID.fullmatch(name):
                quality.append({'reason': 'unexpected_producer'})
                continue
            selected = state if name == start else None
            resume_segment = selected.get('segment') if selected else None
            resume_offset = selected.get('offset', 0) if selected else 0
            progress['resume'] = (name, resume_segment, resume_offset)
            if time.monotonic() >= deadline:
                progress['incomplete'] = True
                return
            producer = producers / name
            try:
                validate_private(producer, directory=True)
                manifest = read_json(producer / 'manifest.json')
                if manifest.get('schema_version') != 1 or manifest.get('producer_id') != name:
                    quality.append({'producer_id': name, 'reason': 'unsupported_manifest'})
                    continue
                if not manifest.get('closed'):
                    live = producer_live(producer)
                    quality.append({'producer_id': name, 'reason': 'active' if live is True
                                    else 'unclosed' if live is False else 'liveness_unknown'})
                health = manifest.get('health', {})
                if isinstance(health, dict) and (health.get('dropped_summary') or health.get('dropped_detail')
                                                or health.get('write_failure_count') or health.get('shutdown_incomplete')):
                    quality.append({'producer_id': name, 'reason': 'capture_loss'})
                segments = []
                for index, entry in enumerate(producer.iterdir()):
                    if index >= 1024:
                        quality.append({'reason': 'segment_scan_limit'})
                        progress['incomplete'] = True
                        break
                    if _SEGMENT.fullmatch(entry.name):
                        segments.append(entry.name)
                segments.sort(key=self._segment_order)
                if resume_segment is not None and resume_segment not in segments:
                    progress['gap'] = True
                for segment in segments:
                    if resume_segment is not None and self._segment_order(segment) < self._segment_order(resume_segment):
                        continue
                    offset = resume_offset if segment == resume_segment else 0
                    progress['resume'] = (name, segment, offset)
                    if time.monotonic() >= deadline:
                        progress['incomplete'] = True
                        return
                    segment_count += 1
                    if segment_count > 4096:
                        quality.append({'reason': 'segment_scan_limit'})
                        progress['incomplete'] = True
                        progress['resume'] = None
                        return
                    yield producer / segment, offset
            except FileNotFoundError:
                progress['gap'] = True
                quality.append({'producer_id': name, 'reason': 'metadata_evicted'})
            except (OSError, ValueError, TypeError):
                quality.append({'producer_id': name, 'reason': 'metadata_unavailable'})
        progress['resume'] = None

    def _cursor(self, path: Path, offset: int, generation: int, filters: dict, detail: bool,
                *, inventory: bool = False) -> str:
        state = {'store': self.store_id, 'producer': path.name if inventory else path.parent.name,
                 'segment': None if inventory else path.name, 'offset': offset,
                 'generation': generation, 'filters': filters, 'detail': detail}
        return base64.urlsafe_b64encode(encoded(state)).decode()

    def _resume_cursor(self, resume: tuple | None, generation: int, filters: dict, detail: bool) -> str | None:
        if resume is None:
            return None
        producer, segment, offset = resume
        path = self.root / 'producers' / producer
        return self._cursor(path / segment if segment else path, offset, generation,
                            filters, detail, inventory=segment is None)

    def _decode_cursor(self, cursor: str | None, filters: dict, detail: bool) -> dict | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError('Invalid history cursor')
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('Invalid history cursor') from None
        if (not isinstance(state, dict) or state.get('store') != self.store_id
                or state.get('filters') != filters or state.get('detail') is not detail
                or not _UUID.fullmatch(str(state.get('producer', '')))
                or type(state.get('offset')) is not int or state['offset'] < 0
                or type(state.get('generation')) is not int or state['generation'] < 0
                or (state.get('segment') is None and state['offset'] != 0)
                or (state.get('segment') is not None and not _SEGMENT.fullmatch(str(state['segment'])))):
            raise ValueError('Invalid history cursor binding')
        return state

    def status(self, *, cursor: str | None = None, seconds: float = 0.5) -> dict:
        if type(seconds) not in (int, float) or not 0 < seconds <= 0.5:
            raise ValueError('Query budget outside supported range')
        deadline = time.monotonic() + seconds
        filters = {'query': 'inventory'}
        state = self._decode_cursor(cursor, filters, True)
        generation = self._generation() if self.root.exists() else 0
        quality = []
        progress = {'resume': None, 'incomplete': False,
                    'gap': bool(state and state['generation'] != generation)}
        counts = {'summary': 0, 'detail': 0}
        found = 0
        for path, _ in self._sources(state, deadline, quality, progress):
            try:
                validate_private(path)
                counts[_SEGMENT.fullmatch(path.name)[1]] += path.stat().st_size
                found += 1
            except FileNotFoundError:
                progress['gap'] = True
                quality.append({'reason': 'segment_evicted'})
            except (OSError, ValueError):
                quality.append({'reason': 'segment_unavailable'})
        return {'schema_version': 1, 'available': self.root.exists(), 'segment_count': found,
                'bytes': counts, 'retention_generation': generation, 'quality': quality[:256],
                'scan_incomplete': progress['incomplete'], 'cursor_gap': progress['gap'],
                'next_cursor': self._resume_cursor(progress['resume'], generation, filters, True),
                'counts_scope': 'scanned_page'}

    def page(self, filters: dict | None = None, cursor: str | None = None, *, detail: bool = False,
             limit: int = 500, max_bytes: int = 8*1024*1024, max_events: int = 10000,
             seconds: float = 0.5) -> dict:
        filters=filters or {}
        if set(filters)-{'operation','tool','provider','status','since'} or any(not isinstance(v,str) or len(v)>128 for v in filters.values()):
            raise ValueError('Invalid history filters')
        since = datetime.fromisoformat(filters['since']) if 'since' in filters else None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if type(limit) is not int or not 1<=limit<=500 or type(max_bytes) is not int or not 1<=max_bytes<=8*1024*1024:
            raise ValueError('Query budget outside supported range')
        if type(max_events) is not int or not 1<=max_events<=10000 or not 0<seconds<=0.5:
            raise ValueError('Query budget outside supported range')
        deadline = time.monotonic() + seconds
        state = self._decode_cursor(cursor, filters, detail)
        generation = self._generation() if self.root.exists() else 0
        quality = []
        if generation:
            quality.append({'reason': 'retention_observed'})
        progress = {'resume': None, 'incomplete': False,
                    'gap': bool(state and state['generation'] != generation)}
        items=[]; used=seen=0
        next_cursor=None; stopped=False
        for path, offset in self._sources(state, deadline, quality, progress):
            if not detail and path.name.startswith('detail-'):
                continue
            try:
                validate_private(path)
                with path.open('rb') as stream:
                    if offset > path.stat().st_size:
                        progress['gap'] = True
                        offset = 0
                    if offset:
                        stream.seek(offset-1)
                        if stream.read(1) != b'\n':
                            raise ValueError('Invalid history cursor offset')
                    stream.seek(offset)
                    while True:
                        position=stream.tell()
                        if used>=max_bytes or seen>=max_events or time.monotonic()>=deadline or len(items)>=limit:
                            next_cursor=self._cursor(path,position,generation,filters,detail); stopped=True; break
                        raw=stream.readline(min(16385,max_bytes-used+1))
                        if not raw: break
                        if len(raw) > max_bytes-used:
                            next_cursor=self._cursor(path,position,generation,filters,detail)
                            stopped=True
                            break
                        used+=len(raw); seen+=1
                        if not raw.endswith(b'\n'):
                            reason='tail_pending' if producer_live(path.parent) is True else 'tail_incomplete'
                            quality.append({'producer_id':path.parent.name,'reason':reason})
                            # Do not resume past an incomplete active append.
                            if reason=='tail_pending': next_cursor=self._cursor(path,position,generation,filters,detail); stopped=True
                            break
                        try:
                            event=_safe_event(json.loads(raw))
                            if event['producer_id']!=path.parent.name or event['channel']!=_SEGMENT.fullmatch(path.name)[1]: raise ValueError('corrupt_record')
                        except (ValueError,TypeError,UnicodeError):
                            quality.append({'producer_id':path.parent.name,'reason':'corrupt_or_unsupported_record'}); continue
                        if filters.get('operation') and filters['operation'] not in (event.get('operation_id'),event.get('parent_operation_id'),*(event.get('resources') or {}).values()): continue
                        if any(filters.get(key) and filters[key]!=event.get(key) for key in ('tool','provider')): continue
                        if filters.get('status') and filters['status']!=event.get('operation_outcome'): continue
                        if since is not None:
                            occurred = datetime.fromisoformat(event['occurred_at'])
                            if occurred.tzinfo is None:
                                occurred = occurred.replace(tzinfo=timezone.utc)
                            if occurred < since: continue
                        items.append(event)
            except FileNotFoundError:
                progress['gap']=True; quality.append({'reason':'segment_evicted'})
            except PermissionError:
                quality.append({'reason':'segment_unavailable'})
            if stopped: break
        if next_cursor is None and progress['incomplete']:
            next_cursor = self._resume_cursor(progress['resume'], generation, filters, detail)
        stopped = stopped or progress['incomplete']
        if len(quality)>256: quality=quality[:255]+[{'reason':'quality_truncated'}]
        return {'schema_version':1,'items':items,'next_cursor':next_cursor,'cursor_gap':progress['gap'],
                'scan_incomplete':stopped,'scanned_bytes':used,'scanned_events':seen,'quality':quality}

    def recent(self, *, limit: int = 50, filters: dict | None = None, cursor: str | None = None) -> dict:
        if type(limit) is not int or not 1<=limit<=500: raise ValueError('Invalid recent limit')
        page=self.page(filters,cursor,limit=500)
        operations={}
        for event in sorted(page['items'],key=lambda e:(e['producer_id'],e['seq'])):
            operation=event.get('operation_id')
            if operation:
                key=(event['producer_id'],operation)
                previous=operations.get(key,{})
                row={**previous,**event}
                # Invocation handoff does not override an observed business terminal.
                if previous.get('operation_outcome') in ('succeeded','failed','denied','timed_out','cancelled') and event.get('operation_outcome') in ('pending', 'unknown'):
                    row['operation_outcome']=previous['operation_outcome']
                operations[key]=row
        page['operations']=sorted(operations.values(),key=lambda e:e['occurred_at'],reverse=True)[:limit]
        return page

    def show(self, operation: str, *, detail: bool = False, cursor: str | None = None) -> dict:
        page=self.page({'operation':operation},cursor,detail=detail)
        page['items'].sort(key=lambda e:(e['producer_id'],e['seq']))
        matches = {(e['producer_id'], e['operation_id']) for e in page['items']
                   if e.get('operation_id') == operation}
        if not matches:
            matches = {(e['producer_id'], e['operation_id']) for e in page['items']
                       if e.get('operation_id') and operation in (e.get('resources') or {}).values()}
            child_matches = {(e['producer_id'], e['operation_id']) for e in page['items']
                             if (e['producer_id'], e.get('parent_operation_id')) in matches}
            matches -= child_matches
        incomplete = bool(page['scan_incomplete'] or page['cursor_gap']
                          or any(q['reason'] not in ('active',) for q in page['quality']))
        resolved = []
        for producer, identity in sorted(matches):
            own = [e for e in page['items'] if (e['producer_id'], e.get('operation_id')) == (producer, identity)]
            terminals = [e for e in own if e.get('operation_outcome') in ('succeeded','failed','denied','timed_out','cancelled')]
            starts = [e for e in own if e['phase'] in ('received','started')]
            resolved.append({'producer_id': producer, 'operation_id': identity,
                             'operation_outcome': terminals[-1]['operation_outcome'] if terminals else 'unknown',
                             'record_complete': bool(terminals and starts and not incomplete)})
        page['resolved_operations'] = resolved
        page['ambiguous_matches'] = len(resolved) > 1
        page['operation_outcome'] = resolved[0]['operation_outcome'] if len(resolved) == 1 else 'unknown'
        page['record_complete'] = len(resolved) == 1 and resolved[0]['record_complete']
        page['not_found_in_scanned_range'] = not page['items']
        return page

    def _export_snapshot(self, operation: str, *, detail: bool = False) -> tuple[bytes, dict]:
        maximum=16*1024*1024; used=0; cursor=None; items=[]; quality=[]; gaps=False
        deadline=time.monotonic()+10
        payload_bytes=0
        # Query budgets are per page; the complete export also has a total scan
        # ceiling and never silently exports a partial operation as complete.
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('Export scan exceeded its time budget; narrow the requested range')
            page=self.page({'operation':operation},cursor,detail=detail,max_bytes=1024*1024)
            used+=page['scanned_bytes']
            items.extend(page['items']); quality.extend(page['quality']); gaps|=page['cursor_gap']
            payload_bytes+=sum(len(encoded(item)) for item in page['items'])
            if len(quality)>256: quality=quality[:255]+[{'reason':'quality_truncated'}]
            if used>maximum or payload_bytes>maximum:
                raise ValueError('Export exceeds its budget; narrow the requested range')
            next_cursor=page['next_cursor']
            if not next_cursor:
                if page['scan_incomplete']:
                    raise ValueError('Export scan is incomplete; narrow the requested range')
                break
            if next_cursor==cursor or any(q['reason']=='tail_pending' for q in page['quality']):
                raise ValueError('Export snapshot is incomplete; retry after the operation stops')
            cursor=next_cursor
        from . import __version__
        report={'schema_version':1,'product_version':__version__,'operation':operation,'detail':detail,'items':items,
                'cursor_gap':gaps,'quality':quality[:256]}
        payload=encoded(report)
        if len(payload)>maximum: raise ValueError('Export exceeds its budget')
        return payload, {'bytes':len(payload),'events':len(items),
                         'sha256':hashlib.sha256(payload).hexdigest(),
                         'operation':operation,'detail':detail,'quality':quality[:256],'cursor_gap':gaps}

    def preview_export(self, operation: str, *, detail: bool = False) -> dict:
        _, receipt = self._export_snapshot(operation, detail=detail)
        return {**receipt, 'preview':True}

    def export(self, operation: str, output: Path, *, detail: bool = False) -> dict:
        payload, receipt = self._export_snapshot(operation, detail=detail)
        output=checked_path(output)
        if output.exists(): raise FileExistsError('Export output already exists')
        validate_private(output.parent,directory=True)
        temporary=output.with_name('.execution-export-'+os.urandom(16).hex())
        try:
            with private_open(temporary) as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            # Hard-link commit provides no-overwrite atomically on both platforms.
            os.link(temporary,output)
        finally:
            temporary.unlink(missing_ok=True)
        return receipt
