"""Live manual approval acceptance in a synthetic Git workspace.

Accept requires prior human authorization of COMMAND and --authorized.
This script never changes production policy, profiles or credentials.
"""
import argparse
import asyncio
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters
from tiancheng_mcp.cli import PROJECT_ROOT

INNER = "printf 'TQ_B7_MANUAL\\n' > approval-marker.txt"
COMMAND = '/bin/bash -lc "' + INNER + '"'
CONTENT = "TQ_B7_MANUAL\n"


def assess(report):
    response = report.get('response', {})
    request = report.get('request', {})
    if (report.get('decision') not in ('accept', 'cancel')
            or not request.get('approval_id') or response.get('approval_id') != request['approval_id']
            or request.get('method') != 'item/commandExecution/requestApproval'
            or response.get('decision') != report.get('decision') or response.get('status') != 'submitted'
            or not request_matches(request, report.get('workspace'))
            or not report.get('complete_events') or not report.get('session_closed')):
        return False
    commands = report.get('commands', [])
    if report['decision'] == 'accept':
        return (report.get('state') == 'succeeded' and len(commands) == 1
                and commands[0].get('status') == 'completed' and commands[0].get('exit_code') == 0
                and report.get('independent_mcp_content') == CONTENT)
    # The native transport emits a terminal command item even when approval
    # was cancelled.  A null exit code is not evidence of command execution.
    return (report.get('state') == 'cancelled' and report.get('marker_absent') is True
            and all(command.get('status') == 'unknown' and command.get('exit_code') is None
                    for command in commands))


def expected(command):
    value = command
    for _ in range(4):
        if value == INNER:
            return True
        try:
            parts = shlex.split(value) if isinstance(value, str) else value
        except ValueError:
            return False
        if (not isinstance(parts, list) or len(parts) != 3 or
                parts[0] not in ('/bin/bash', '/usr/bin/bash', 'bash') or parts[1] != '-lc'):
            return False
        value = parts[2]
    return value == INNER


def request_matches(request, workspace):
    details = request.get('details', {})
    cwd = details.get('cwd')
    if not isinstance(cwd, str) or not isinstance(workspace, str):
        return False
    try:
        return Path(cwd).resolve() == Path(workspace).resolve() and expected(details.get('command'))
    except (OSError, ValueError):
        return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--decision', choices=['cancel', 'accept'], required=True)
    parser.add_argument('--authorized', action='store_true')
    args = parser.parse_args()
    if args.decision == 'accept' and not args.authorized:
        parser.error('accept requires explicit human authorization of the exact command')
    root = args.root.resolve()
    if not args.root.is_absolute() or root == PROJECT_ROOT or PROJECT_ROOT in root.parents or root in PROJECT_ROOT.parents:
        parser.error('--root must be absolute and outside the source tree')
    args.root = root
    workspace = args.root / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / 'approval-marker.txt'
    if marker.exists():
        raise RuntimeError('refusing to reuse an existing marker')
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    config = args.root / 'config'
    parameters = StdioServerParameters(command=sys.executable, cwd=str(args.root), args=[
        '-m', 'tiancheng_mcp', '--workspace', str(workspace), '--allow-exec',
        '--audit-dir', str(args.root / 'audit'), '--access-policy', str(config / 'policy.json'),
        '--agent-profiles', str(config / 'profiles.json'), '--agent-env-file', str(config / 'agent.env'),
        '--agent-sources', str(config / 'sources.json'), '--agent-catalog', str(config / 'catalog.sqlite3'),
        '--launcher-local-config', str(config / 'launcher.json'),
    ], encoding='utf-8')
    report = {'decision': args.decision, 'workspace': str(workspace), 'verified': False}
    async with Client(parameters, mode='legacy', raise_exceptions=True) as client:
        async def call(tool, **arguments):
            result = await client.call_tool(tool, arguments)
            if result.is_error or not isinstance(result.structured_content, dict):
                raise RuntimeError(f'{tool} failed')
            return result.structured_content

        session = await call('agent_session', action='create', profile='codex-default', sandbox='read-only')
        sid = session['session_id']
        try:
            run = await call('agent_run', action='start', session_id=sid, max_runtime_seconds=180,
                             codex_options={'manual_approval': True}, prompt=(
                f'Run exactly this command once in the current cwd: {COMMAND}\n'
                'This is an authorized test in a synthetic repository. Request escalation '
                'with sandbox_permissions=require_escalated so the human approval request '
                'is surfaced. Do not run without approval, add cd, change the command, '
                'edit files with another tool, or retry after cancellation.'))
            rid = run['run_id']
            deadline = time.monotonic() + 170
            request = None
            while time.monotonic() < deadline:
                approvals = await call('agent_approval', action='list', session_id=sid, run_id=rid)
                pending = approvals.get('requests', [])
                if pending:
                    if len(pending) != 1:
                        raise RuntimeError('unexpected multiple requests')
                    request = pending[0]
                    break
                detail = await call('agent_run', action='inspect', session_id=sid, run_id=rid)
                if detail['state'] not in ('queued', 'running'):
                    raise RuntimeError('run ended without an approval request')
                await asyncio.sleep(1)
            if request is None:
                raise TimeoutError('no approval request')
            report['request'] = request
            if not request_matches(request, str(workspace)):
                await call('agent_approval', action='respond', session_id=sid, run_id=rid,
                           approval_id=request['approval_id'], decision='cancel')
                raise RuntimeError('request command differs from the authorized command')
            response = await call('agent_approval', action='respond', session_id=sid, run_id=rid,
                                  approval_id=request['approval_id'], decision=args.decision)
            report['response'] = response
            while time.monotonic() < deadline:
                detail = await call('agent_run', action='inspect', session_id=sid, run_id=rid)
                if detail['state'] not in ('queued', 'running'):
                    break
                await asyncio.sleep(1)
            page = await call('agent_run', action='events', session_id=sid, run_id=rid,
                              after_seq=0, limit=100, max_bytes=65536)
            commands = [event['data'] for event in page.get('events', [])
                        if event['type'] == 'command_completed']
            report.update(state=detail['state'], commands=commands,
                          complete_events=not page.get('has_more') and not page.get('cursor_gap'))
            if args.decision == 'accept':
                read = await call('read_text', path='approval-marker.txt')
                report['independent_mcp_content'] = read['content']
            else:
                report['marker_absent'] = not marker.exists()
        finally:
            closed = await call('agent_session', action='close', session_id=sid)
            report['session_closed'] = closed.get('closed') is True
            report['verified'] = assess(report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({'verified': False, 'error_type': type(exc).__name__, 'error': str(exc)}), flush=True)
        sys.exit(1)
