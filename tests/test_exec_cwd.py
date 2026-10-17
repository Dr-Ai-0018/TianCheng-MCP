"""Regression coverage for authorized ordinary-command working directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from tiancheng_mcp.access_context import AccessContext
from tiancheng_mcp.policy import AccessPolicy, AccessRule, AccessPolicyError
from tiancheng_mcp.security import WorkspaceSecurityError
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp import service as service_module


@pytest.fixture
def external(tmp_path):
    path=tmp_path/'approved';path.mkdir()
    (path/'child').mkdir()
    return path


def service_for(workspace, external, **kwargs):
    policy=kwargs.pop('access_policy', AccessPolicy(workspace,[AccessRule(workspace,'full'),AccessRule(external,'full',allow_exec=True)]))
    return TianChengService(workspace,None,allow_exec=kwargs.pop('allow_exec',True),access_policy=policy,**kwargs)


@pytest.mark.parametrize('child',['','child'])
def test_bounded_command_uses_explicitly_authorized_absolute_cwd(workspace,external,child):
    service=service_for(workspace,external)
    try:
        target=external/child
        result=service.run_command('python',['-c','import os;print(os.getcwd())'],cwd=str(target))
        assert result['exit_code']==0 and Path(result['stdout'].strip())==target.resolve()
        assert result['cwd'].startswith('<policy>/')
        assert str(external) not in result['cwd']
        assert service.external_grants.enabled is False
    finally:service.shutdown()


def test_managed_command_runs_and_remains_owned_by_parent_service(workspace,external):
    service=service_for(workspace,external)
    try:
        result=service.start_process('python',['-c','import os;print(os.getcwd(),flush=True)'],cwd=str(external),max_runtime_seconds=10)
        identity=result['process_id'];deadline=time.monotonic()+10
        while service.process_status(identity)['running'] and time.monotonic()<deadline:time.sleep(.02)
        state=service.process_status(identity)
        assert state['exit_code']==0 and state['resources_released']
        assert Path(service.process_output(identity)['stdout'].strip())==external.resolve()
        assert service.list_processes()['count']==1 and service._process_slots.active_count==0
        assert state['cwd'].startswith('<policy>/')
    finally:service.shutdown()


@pytest.mark.parametrize('managed',[False,True])
@pytest.mark.parametrize('rule',['missing','no-exec','deny','approval','disabled','child-deny','child-approval','child-no-exec'])
def test_unapproved_cwd_never_spawns(workspace,external,monkeypatch,managed,rule):
    rules=[AccessRule(workspace,'full')]
    if rule!='missing':
        rules.append(AccessRule(external,'deny' if rule=='deny' else 'full',allow_exec=rule!='no-exec',
                                require_approval=rule=='approval',enabled=rule!='disabled'))
    target=external/'child' if rule.startswith('child-') else external
    if rule.startswith('child-'):
        rules.append(AccessRule(target,'deny' if rule=='child-deny' else 'full',allow_exec=rule!='child-no-exec',require_approval=rule=='child-approval'))
    service=service_for(workspace,external,access_policy=AccessPolicy(workspace,rules))
    monkeypatch.setattr(service_module.subprocess,'Popen',lambda *a,**kw:pytest.fail('unauthorized cwd spawned'))
    try:
        with pytest.raises(PermissionError):
            if managed:service.start_process('python',['--version'],cwd=str(target))
            else:service.run_command('python',['--version'],cwd=str(target))
        assert service._process_slots.active_count==0 and not service._processes
    finally:service.shutdown()


@pytest.mark.parametrize('managed',[False,True])
def test_global_exec_and_command_policy_still_apply(workspace,external,tmp_path,managed):
    service=service_for(workspace,external,allow_exec=False)
    try:
        with pytest.raises(PermissionError,match='execution is disabled'):
            (service.start_process if managed else service.run_command)('python',['--version'],cwd=str(external))
    finally:service.shutdown()
    path=tmp_path/'commands.json';path.write_text(json.dumps({'schema_version':1,'disable':['python']}))
    service=service_for(workspace,external,command_policy_path=path)
    try:
        with pytest.raises(PermissionError):
            (service.start_process if managed else service.run_command)('python',['--version'],cwd=str(external))
    finally:service.shutdown()


@pytest.mark.parametrize('managed',[False,True])
def test_policy_change_between_initial_validation_and_spawn_is_rechecked(workspace,external,monkeypatch,managed):
    service=service_for(workspace,external)
    initial=service._validated_exec_request
    def change(*args):
        result=initial(*args)
        service.access_policy=AccessPolicy(workspace,[AccessRule(workspace,'full'),AccessRule(external,'deny')])
        return result
    monkeypatch.setattr(service,'_validated_exec_request',change)
    monkeypatch.setattr(service_module.subprocess,'Popen',lambda *a,**kw:pytest.fail('revoked cwd spawned'))
    try:
        with pytest.raises(PermissionError):
            (service.start_process if managed else service.run_command)('python',['--version'],cwd=str(external))
        assert service._process_slots.active_count==0
    finally:service.shutdown()


def test_relative_absolute_workspace_and_parent_escape_contract(workspace,external):
    service=service_for(workspace,external)
    try:
        for cwd in ('.',str(workspace),'',None):
            assert service._execution_working_directory(cwd)==workspace.resolve()
        for cwd in ('..',str(external/'..'/'approved')):
            with pytest.raises((WorkspaceSecurityError,AccessPolicyError)):
                service.run_command('python',['--version'],cwd=cwd)
        with pytest.raises(FileNotFoundError):
            service.run_command('python',['--version'],cwd=str(external/'missing'))
    finally:service.shutdown()


def test_symbolic_link_cwd_is_rejected(workspace,external,tmp_path):
    link=external/'link'
    try:link.symlink_to(workspace,target_is_directory=True)
    except OSError:pytest.skip('symlink privilege unavailable')
    service=service_for(workspace,external)
    try:
        with pytest.raises((WorkspaceSecurityError,AccessPolicyError)):
            service.run_command('python',['--version'],cwd=str(link))
    finally:service.shutdown()


def test_delegated_context_cannot_jump_to_another_approved_rule(workspace,external,tmp_path):
    other=tmp_path/'other';other.mkdir()
    policy=AccessPolicy(workspace,[AccessRule(workspace,'full'),AccessRule(external,'full',allow_exec=True),AccessRule(other,'full',allow_exec=True)])
    parent=service_for(workspace,external,access_policy=policy)
    scoped=parent._scoped_service(AccessContext(external,'exec',lambda:parent.access_policy),allow_exec=True)
    try:
        assert scoped.run_command('python',['--version'],cwd=str(external))['exit_code']==0
        with pytest.raises(WorkspaceSecurityError):scoped.run_command('python',['--version'],cwd=str(other))
    finally:scoped.shutdown();parent.shutdown()


def test_prepared_process_policy_root_cannot_bypass_exec_rule(workspace,external):
    service=service_for(workspace,external,access_policy=AccessPolicy(workspace,[AccessRule(workspace,'full')]))
    try:
        prefix=service._prepare_exec_command('python',['--version'])
        with pytest.raises(PermissionError):
            service._start_managed_process_prepared('python',prefix,external,policy_root=external,max_runtime_seconds=10,output_limit_bytes=4096)
        assert service._process_slots.active_count==0
    finally:service.shutdown()


def test_external_cwd_records_remain_bounded_and_private(workspace,external,tmp_path):
    root=tmp_path/'records'
    service=service_for(workspace,external,observability_config={'mode':'summary','storageRoot':str(root)})
    try:
        result=service.run_command('python',['--version'],cwd=str(external))
        assert result['exit_code']==0 and result['cwd'].startswith('<policy>/')
    finally:service.shutdown()
    documents=[json.loads(line) for path in root.rglob('*.jsonl') for line in path.read_text(encoding='utf-8').splitlines()]
    def strings(value):
        if isinstance(value,str):yield value
        elif isinstance(value,dict):
            for child in value.values():yield from strings(child)
        elif isinstance(value,list):
            for child in value:yield from strings(child)
    assert documents
    assert not any(str(external) in value for doc in documents for value in strings(doc))


def test_external_cwd_cannot_select_execution_record_storage(workspace,external,tmp_path):
    root=tmp_path/'records'
    policy=AccessPolicy(workspace,[AccessRule(workspace,'full'),AccessRule(root,'full',allow_exec=True)])
    service=service_for(workspace,external,access_policy=policy,observability_config={'mode':'summary','storageRoot':str(root)})
    try:
        with pytest.raises(PermissionError,match='reserved'):
            service.run_command('python',['--version'],cwd=str(root))
    finally:service.shutdown()
