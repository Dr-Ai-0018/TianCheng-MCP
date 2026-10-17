"""Bounded live Codex acceptance against synthetic files and durable records.

Consumes model quota. Never changes production settings, approves requests,
or saves prompts/provider output in the report. Keep the fresh test root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.policy import AccessPolicy
from tiancheng_mcp.service import TianChengService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Fresh absolute local test directory')
    parser.add_argument('--profile', default='codex-default')
    args = parser.parse_args()
    if not args.root.is_absolute() or args.root.exists():
        parser.error('--root must be absolute and must not already exist')
    root = args.root.resolve()
    root.mkdir(parents=True)
    workspace = root / 'workspace'
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True, capture_output=True)
    nonce = 'SYNTHETIC-' + uuid.uuid4().hex
    (workspace / 'fixture.txt').write_text(nonce, encoding='utf-8')
    (workspace / 'AGENTS.md').write_text(
        'This is a synthetic acceptance workspace. Only read fixture.txt and '
        'create result.txt when the user requests it. Do not inspect parent '
        'directories, configuration, credentials, the network or other files.\n', encoding='utf-8')
    service = TianChengService(workspace, root / 'audit', allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        access_policy_path=root/'access-policy.json', enable_agent_catalog=False,
        agent_env_file=root/'absent.env', command_policy_path=root/'command-policy.json',
        observability_config={'mode':'detail','storageRoot':str(root/'records')})
    checks = []
    sessions = []
    runs = []
    public_completed = 0
    public_gaps = False
    def check(name, ok):
        checks.append({'check':name,'ok':bool(ok)})
        print(json.dumps(checks[-1]), flush=True)
    try:
        for sandbox in ('read-only', 'workspace-write'):
            session = service.agent_session_create(profile=args.profile, sandbox=sandbox)
            sid = session['session_id']
            sessions.append(sid)
            prompt = ('Use one shell command to read fixture.txt in this working directory. '
                      'Do not write anything. Reply with its exact contents.' if sandbox == 'read-only' else
                      'Use a shell command to read fixture.txt and write its exact bytes to result.txt '
                      'in this working directory. Do not modify any other file. Reply DONE.')
            run = service.agent_run_start(sid, prompt, max_runtime_seconds=120)
            rid = run['run_id']
            runs.append(rid)
            deadline = time.monotonic() + 135
            cursor = 0
            completed = []
            seen = []
            gap = False
            while time.monotonic() < deadline:
                page = service.agent_run_events(sid, rid, after_seq=cursor, limit=100, wait_ms=1000)
                cursor = page['next_seq']
                gap |= page['cursor_gap']
                for event in page['events']:
                    seen.append(event['seq'])
                    if event['type'] == 'command_completed':
                        completed.append(event)
                if page['state'] not in ('queued','running') and not page['has_more']:
                    break
            else:
                service.agent_run_cancel(sid, rid)
                raise TimeoutError('bounded provider acceptance did not finish')
            result = service.agent_run_result(sid, rid)
            public_completed += len(completed)
            public_gaps |= gap
            check(sandbox+'.process_succeeded', result['state'] == 'succeeded')
            check(sandbox+'.real_command_exit0', any(e.get('data',{}).get('exit_code') == 0 for e in completed))
            check(sandbox+'.public_sequence', not gap and seen == sorted(set(seen)))
            check(sandbox+'.task_unverified', result.get('outcomes',{}).get('task') == 'unverified')
            if sandbox == 'read-only':
                check('read-only.independent_fixture_and_response',
                      (workspace/'fixture.txt').read_text(encoding='utf-8') == nonce
                      and nonce in str(result.get('result','')) and not (workspace/'result.txt').exists())
            else:
                check('workspace-write.independent_bytes',
                      (workspace/'result.txt').is_file() and (workspace/'result.txt').read_bytes() == nonce.encode())
            service.agent_session_close(sid)
    except Exception as exc:
        checks.append({'check':'live_exception','ok':False,'error_type':type(exc).__name__})
    finally:
        for sid in sessions:
            try:
                service.agent_session_close(sid)
            except (FileNotFoundError, ValueError, RuntimeError, PermissionError):
                pass
        service.shutdown()
    query = ExecutionQuery(root/'records')
    cursor = None
    events = []
    quality = []
    while True:
        page = query.page(cursor=cursor, detail=True)
        events.extend(page['items'])
        quality.extend(page['quality'])
        public_gaps |= page['cursor_gap']
        if not page['next_cursor']:
            check('durable_scan_complete', not page['scan_incomplete'])
            break
        cursor = page['next_cursor']
    native = [e for e in events if e['kind']=='agent' and e['phase']=='completed'
              and e.get('result',{}).get('scope')=='provider_command']
    check('durable_native_items', len(native) == public_completed and public_completed >= 2
          and all(e.get('resources',{}).get('native_item_id') for e in native))
    check('durable_runs', set(runs) <= {e.get('resources',{}).get('run_id') for e in events})
    check('durable_no_capture_loss', not public_gaps and not quality)
    raw = b''.join(p.read_bytes() for p in (root/'records').rglob('*.jsonl'))
    check('no_provider_body_persisted', nonce.encode() not in raw
          and not any(e['channel']=='detail' for e in events))
    report = {'checks':checks,'ok':all(c['ok'] for c in checks),
              'provider_completed_commands':public_completed,'durable_completed_commands':len(native),
              'event_count':len(events),'record_sha256':hashlib.sha256(raw).hexdigest(),
              'scope':'local synthetic Codex read-only read and workspace-write; no approval acceptance or denial proof'}
    (root/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'ok':report['ok'],'checks':len(checks)}), flush=True)
    if not report['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
