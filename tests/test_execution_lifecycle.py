from __future__ import annotations

import json
import sys
import time

import pytest

from tiancheng_mcp.agent_adapters import CodexJsonlParser, ClaudeJsonlParser
from tiancheng_mcp.agents import AgentProfileRegistry
from tiancheng_mcp.execution_events import EventSink
from tiancheng_mcp.execution_lifecycle import AgentObserver
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore
from tiancheng_mcp.service import TianChengService


def service_for(workspace, tmp_path, mode='detail'):
    return TianChengService(workspace, None, allow_exec=True, enable_agent_catalog=False,
        observability_config={'mode': mode, 'storageRoot': str(tmp_path / 'records')})


def wait_finished(service, pid):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        record=service._get_managed_process(pid)
        if record.finished.wait(.02):
            return service._managed_process_status(record)
    raise AssertionError('Process did not finish')


def all_rows(root):
    rows=[];cursor=None
    for _ in range(50):
        page=ExecutionQuery(root).page(detail=True,cursor=cursor)
        rows.extend(page['items']);cursor=page['next_cursor']
        if not cursor: return rows
    raise AssertionError('History did not finish')


def test_managed_exit7_output_and_terminal_survive_history_eviction(workspace,tmp_path,monkeypatch):
    service=service_for(workspace,tmp_path)
    try:
        started=service.start_process('python',['-c',"import sys; print('process-marker'); print('token=sk-synthetic123456'); sys.exit(7)"],output_limit_bytes=4096)
        status=wait_finished(service,started['process_id'])
        assert status['exit_code']==7 and status['resources_released']
        output=service.process_output(started['process_id'])
        assert 'token=sk-synthetic123456' in output['stdout']
        monkeypatch.setattr('tiancheng_mcp.service.MAX_PROCESS_HISTORY',0)
        service._prune_process_history_locked()
        with pytest.raises(FileNotFoundError): service.process_status(started['process_id'])
    finally:
        service.shutdown()
    rows=all_rows(tmp_path/'records')
    process=[r for r in rows if r['kind']=='process' and r['channel']=='summary']
    assert len([r for r in process if r['phase']=='started'])==1
    terminal=next(r for r in process if r['phase']=='completed')
    assert terminal['operation_outcome']=='failed'
    assert terminal['result']['exit_code']==7 and terminal['result']['resources_released']
    assert ExecutionQuery(tmp_path/'records').show(started['process_id'])['operation_outcome']=='failed'
    raw=b''.join(p.read_bytes() for p in (tmp_path/'records').rglob('*.jsonl'))
    assert b'process-marker' in raw and b'sk-synthetic123456' not in raw


def test_process_capture_budget_not_reset_when_start_invocation_returns(workspace,tmp_path):
    service=service_for(workspace,tmp_path)
    try:
        started=service.start_process('python',['-c',"import time; print('a'*40000, flush=True); time.sleep(.5); print('b'*40000, flush=True)"],output_limit_bytes=200000)
        wait_finished(service,started['process_id'])
        output=service.process_output(started['process_id'],max_bytes=200000)
        assert output['stdout'].count('a')==40000 and output['stdout'].count('b')==40000
    finally: service.shutdown()
    details=[r for r in all_rows(tmp_path/'records') if r['channel']=='detail']
    assert sum(r['source_bytes'] for r in details)<=65536
    assert any(r['capture']['payload_truncated'] for r in details)


@pytest.mark.parametrize('timeout',[False,True])
def test_process_cancel_and_timeout_terminal_are_observed(workspace,tmp_path,timeout):
    service=service_for(workspace,tmp_path)
    try:
        started=service.start_process('python',['-c','import time; time.sleep(20)'],max_runtime_seconds=1 if timeout else 30)
        if not timeout: service.stop_process(started['process_id'],force=True)
        wait_finished(service,started['process_id'])
    finally: service.shutdown()
    report=ExecutionQuery(tmp_path/'records').show(started['process_id'])
    assert report['operation_outcome']==('timed_out' if timeout else 'cancelled')
    final=next(r for r in report['items'] if r['phase']=='completed')
    assert final['result']['termination_observed'] and final['result']['resources_released']
    assert any(r['phase']=='cancel_requested' for r in report['items'])


def test_observer_fault_cannot_change_process_result(workspace,tmp_path,monkeypatch):
    service=service_for(workspace,tmp_path)
    def fail(*args,**kwargs): raise OSError('synthetic observer fault')
    monkeypatch.setattr(service.execution_sink,'emit',fail)
    try:
        started=service.start_process('python',['-c',"print('original-result')"])
        assert wait_finished(service,started['process_id'])['exit_code']==0
        assert 'original-result' in service.process_output(started['process_id'])['stdout']
    finally: service.shutdown()
    assert service.execution_store.health()['write_failure_count']>0


AGENT = r"""
import json,sys
for item in [
 {'type':'thread.started','thread_id':'thread-fixture'},
 {'type':'item.started','item':{'id':'item-real-1','type':'command_execution','command':'command-body-never-in-journal'}},
 {'type':'item.completed','item':{'id':'item-real-1','type':'command_execution','status':'failed','exit_code':7,'aggregated_output':'provider-output-never-in-journal'}},
 {'type':'item.completed','item':{'id':'file-real-1','type':'file_change','changes':[{'path':'hidden-file-body','diff':'diff-body-never-in-journal'}]}},
 {'type':'item.completed','item':{'type':'agent_message','text':'message-body-never-in-journal'}},
]: print(json.dumps(item),flush=True)
print('stderr-body-never-in-journal',file=sys.stderr,flush=True)
"""


def fake_agent(service,tmp_path,code=AGENT):
    script=tmp_path/'peer.py';script.write_text(code,encoding='utf-8')
    service._exec_commands['codex']=[sys.executable,str(script)]
    service._agent_only_commands.pop('codex',None)
    service.agent_profiles=AgentProfileRegistry(['codex'])


def wait_agent(service,sid,rid):
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        payload=service.agent_run_result(sid,rid)
        if payload['result_ready']: return payload
        time.sleep(.02)
    raise AssertionError('Agent did not finish')


def test_agent_exit0_does_not_hide_command7_or_persist_protocol_bodies(workspace,tmp_path):
    service=service_for(workspace,tmp_path)
    fake_agent(service,tmp_path)
    try:
        session=service.agent_session_create();sid=session['session_id']
        started=service.agent_run_start(sid,'prompt-body-never-in-journal')
        result=wait_agent(service,sid,started['run_id'])
        assert result['state']=='succeeded' and result['exit_code']==0
        assert result['result']=='message-body-never-in-journal'
        assert result['outcomes']['commands']['failed_events']==1
        assert result['outcomes']['task']=='unverified'
        legacy=service.agent_run_events(sid,started['run_id'])
        sequences=[e['seq'] for e in legacy['events']]
        assert sequences==sorted(set(sequences))
    finally: service.shutdown()
    rows=all_rows(tmp_path/'records')
    command=[r for r in rows if r.get('resources',{}).get('native_item_id')=='item-real-1']
    assert [r['phase'] for r in command]==['started','completed']
    assert command[-1]['operation_outcome']=='failed' and command[-1]['result']['exit_code']==7
    assert command[-1]['result']['source_seq'] in sequences
    agent=next(r for r in rows if r['phase']=='completed' and r.get('result',{}).get('scope')=='agent_lifecycle')
    assert agent['result']['process_outcome']=='succeeded'
    assert agent['result']['command_failures']==1 and agent['result']['task_outcome']=='unverified'
    assert agent['operation_outcome']=='unknown'
    assert not [r for r in rows if r['channel']=='detail']
    raw=b''.join(p.read_bytes() for p in (tmp_path/'records').rglob('*.jsonl'))
    for secret in ('prompt-body','command-body','provider-output','message-body','stderr-body','diff-body','hidden-file-body'):
        assert secret.encode() not in raw


def test_parser_observation_preserves_public_events_and_seq():
    lines=[json.dumps({'type':'item.completed','item':{'id':'native-123','type':'command_execution','status':'completed','exit_code':0}})]
    baseline=CodexJsonlParser();observed=CodexJsonlParser();copies=[]
    observed.source_observer=lambda kind,metadata:copies.append((kind,metadata))
    for line in lines:
        before=baseline.feed_line(line);after=observed.feed_line(line)
        assert (before.seq,before.type,before.data,before.summary)==(after.seq,after.type,after.data,after.summary)
    assert copies[0][1]['native_item_id']=='native-123' and copies[0][1]['source_seq']==0
    observed.source_observer=lambda *args:(_ for _ in ()).throw(OSError('observer'))
    assert observed.feed_line(lines[0]).seq==1
    assert ClaudeJsonlParser().synthetic_event('status','original').seq==0


@pytest.mark.parametrize('action', ['accept', 'expire', 'withdraw', 'send_failure'])
def test_manual_approval_observation_delivery_and_privacy(workspace, tmp_path, monkeypatch, action):
    from test_agent_approval_service import PEER, wait_request, wait_result
    service = service_for(workspace, tmp_path)
    fake_agent(service, tmp_path, PEER)
    try:
        sid = service.agent_session_create(codex_defaults={'manual_approval': True})['session_id']
        rid = service.agent_run_start(sid, 'private-prompt-observation-test', max_runtime_seconds=20)['run_id']
        request = wait_request(service, sid, rid)
        aid = request['approval_id']
        run = service._get_agent_run(sid, rid)[1]
        if action == 'withdraw':
            service.agent_run_cancel(sid, rid)
        elif action == 'expire':
            with run.lock:
                run.parser.pending[aid]['deadline'] = time.monotonic() - 1
            service.agent_approval_list(sid, rid)
        elif action == 'send_failure':
            record = service._get_managed_process(run.process_id)
            original = record.send_input
            def fail_reply(data, **kwargs):
                if data and json.loads(data).get('id') == 20:
                    raise OSError('private-transport-error-body')
                return original(data, **kwargs)
            monkeypatch.setattr(record, 'send_input', fail_reply)
            with pytest.raises(RuntimeError, match='could not be delivered'):
                service.agent_approval_respond(sid, rid, aid, 'accept')
        else:
            service.agent_approval_respond(sid, rid, aid, 'accept')
        result = wait_result(service, sid, rid)
        assert result['outcomes']['task'] == 'unverified'
        assert not run.parser.observation_delivery
    finally:
        service.shutdown()
    rows = all_rows(tmp_path / 'records')
    approvals = [r for r in rows if r['kind'] == 'approval']
    phases = [r['phase'] for r in approvals]
    assert phases[0] == 'received'
    expected = {'accept': ['received', 'decision_submitted', 'response_delivered'],
                'expire': ['received', 'expired', 'response_delivered'],
                'withdraw': ['received', 'withdrawn'],
                'send_failure': ['received', 'decision_submitted', 'response_failed']}
    assert phases == expected[action]
    assert len({r['operation_id'] for r in approvals}) == 1
    assert all(r['resources']['approval_id'] == aid and
               r['resources']['run_id'] == rid and
               r['resources']['native_item_id'] == 'item-fixture' for r in approvals)
    assert all(r['operation_outcome'] != 'succeeded' for r in approvals)
    raw = b''.join(p.read_bytes() for p in (tmp_path / 'records').rglob('*.jsonl'))
    for secret in ('private-prompt-observation-test', 'Write-Output', 'private-transport-error-body',
                   'clientInfo', 'approvalsReviewer', '审批完成'):
        assert secret.encode() not in raw
    assert not [r for r in rows if r['channel'] == 'detail']


def test_provider_tracking_is_bounded_and_pi_events_keep_original_sequence(workspace, tmp_path):
    from tiancheng_mcp.pi_adapter import PiJsonlParser
    service = service_for(workspace, tmp_path, mode='summary')
    try:
        parent = service.execution_sink.begin_invocation('agent_run_start')
        observer = AgentObserver(service.execution_sink, parent,
            session_id='session-bound', run_id='run-bound', provider='pi')
        parser = PiJsonlParser()
        parser.source_observer = observer.source
        public = parser.synthetic_event('status', 'private-pi-event-body')
        assert public.seq == 0 and public.summary == 'private-pi-event-body'
        for index in range(260):
            observer.source('command_completed', {'source_seq': index + 1,
                'native_item_id': f'item-{index}', 'state': 'completed', 'exit_code': 0})
            observer.approval('received', f'approval-{index}', {})
        assert len(observer._commands) == 256 and len(observer._approvals) == 256
        observer.finished(state='succeeded', code=0, commands={}, provider_errors=0, output_gap=False)
        assert not observer._commands and not observer._approvals
        observer.source('command_started', {'native_item_id': 'late-item'})
        assert not observer._commands
    finally:
        service.shutdown()
    rows = all_rows(tmp_path / 'records')
    assert any(r.get('result', {}).get('reason_code') == 'native_item_tracking_limit' for r in rows)
    assert 'private-pi-event-body' not in json.dumps(rows)


def test_observer_fault_preserves_original_business_exception(workspace, tmp_path, monkeypatch):
    service = service_for(workspace, tmp_path)
    def broken(*args, **kwargs):
        raise OSError('observer fault')
    def denied():
        raise PermissionError('original policy refusal')
    monkeypatch.setattr(service.execution_sink, 'emit', broken)
    try:
        with pytest.raises(PermissionError, match='original policy refusal'):
            service.audited('start_process', None, denied)
        assert service.run_with_fallback('stat', None, lambda: {'original': True}) == {'original': True}
    finally:
        service.shutdown()
