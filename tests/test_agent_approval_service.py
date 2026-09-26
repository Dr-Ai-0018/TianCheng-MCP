"""Process-level manual transport checks using a deterministic local peer."""
import json
import sys
import time

import pytest

from tiancheng_mcp.agents import AgentProfileRegistry
from tiancheng_mcp.service import TianChengService

PEER = r'''
import json,sys
def send(x): print(json.dumps(x,ensure_ascii=False),flush=True)
thread='thread-fixture';turn='turn-fixture'
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if method=='initialize': send({'id':m['id'],'result':{}})
    elif method in ['thread/start','thread/resume']:
        assert m['params']['approvalsReviewer']=='user'
        send({'id':m['id'],'result':{'thread':{'id':thread,'modelProvider':'fixture'}}})
    elif method=='turn/start':
        send({'id':m['id'],'result':{'turn':{'id':turn}}})
        send({'id':20,'method':'item/commandExecution/requestApproval','params':{
            'threadId':thread,'turnId':turn,'itemId':'item-fixture','command':"Write-Output '审批'"}})
    elif m.get('id')==20:
        decision=m['result']['decision']
        if decision=='accept':
            send({'method':'item/completed','params':{'threadId':thread,'turnId':turn,
                'item':{'type':'agentMessage','id':'answer','text':'审批完成'}}})
        send({'method':'turn/completed','params':{'threadId':thread,'turn':{
            'id':turn,'status':'interrupted' if decision=='cancel' else 'completed'}}})
'''


def wait_request(service, sid, rid):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        r = service.agent_approval_list(sid, rid)
        if r['requests']: return r['requests'][0]
        assert r['state'] == 'running', r
        time.sleep(.02)
    raise AssertionError('No request')


def wait_result(service, sid, rid):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        r = service.agent_run_result(sid, rid)
        if r['result_ready']: return r
        time.sleep(.02)
    raise AssertionError('No completion')


@pytest.mark.parametrize('decision', ['accept', 'cancel'])
def test_service_roundtrip_resume_and_binding(tmp_path, decision):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    script = tmp_path / 'peer.py'; script.write_text(PEER,encoding='utf-8')
    service = TianChengService(workspace, tmp_path / 'audit', allow_exec=True, enable_agent_catalog=False)
    prefix = [sys.executable,str(script)]
    service._exec_commands['codex'] = prefix
    service._agent_only_commands.pop('codex',None)
    service.agent_profiles = AgentProfileRegistry(['codex'])
    try:
        s = service.agent_session_create(codex_defaults={'manual_approval':True})
        sid = s['session_id']
        for _ in range(2):  # New thread and native resume.
            r = service.agent_run_start(sid,'fixture',max_runtime_seconds=20);rid=r['run_id']
            request = wait_request(service,sid,rid)
            assert request['details']['command'] == "Write-Output '审批'"
            other = service.agent_session_create()
            with pytest.raises(FileNotFoundError):
                service.agent_approval_respond(other['session_id'],rid,request['approval_id'],decision)
            reply = service.agent_approval_respond(sid,rid,request['approval_id'],decision)
            assert reply['status']=='submitted'
            with pytest.raises((FileNotFoundError,PermissionError)):
                service.agent_approval_respond(sid,rid,request['approval_id'],decision)
            result=wait_result(service,sid,rid)
            assert result['state']==('succeeded' if decision=='accept' else 'cancelled')
            assert result['native_session_id']=='thread-fixture'
            if decision=='accept': assert result['result']=='审批完成'
            assert service.agent_approval_list(sid,rid)['count']==0
        r=service.agent_run_start(sid,'fixture',max_runtime_seconds=20)
        request=wait_request(service,sid,r['run_id'])
        service.agent_run_cancel(sid,r['run_id'])
        with pytest.raises(PermissionError):
            service.agent_approval_respond(sid,r['run_id'],request['approval_id'],'accept')
        assert service.agent_approval_list(sid,r['run_id'])['count']==0
        if decision == 'accept':
            r=service.agent_run_start(sid,'fixture',max_runtime_seconds=3)
            request=wait_request(service,sid,r['run_id'])
            # Simulate an expired approval after the native process timed out:
            # attempted expiry delivery must not overwrite timed_out with failed.
            pending=service._get_agent_run(sid,r['run_id'])[1].parser.pending
            pending[request['approval_id']]['deadline']=time.monotonic()-1
            time.sleep(3.5)
            assert wait_result(service,sid,r['run_id'])['state']=='timed_out'
            assert service.agent_approval_list(sid,r['run_id'])['count']==0
    finally:
        service.shutdown()
