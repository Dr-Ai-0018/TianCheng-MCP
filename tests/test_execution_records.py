from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from tiancheng_mcp.execution_config import CaptureSettings
from tiancheng_mcp.execution_events import EventSink, safe_argv
from tiancheng_mcp.execution_paths import private_directory, producer_live
from tiancheng_mcp.execution_query import ExecutionQuery
from tiancheng_mcp.execution_store import ExecutionStore, StoreLimits


def complete(sink, context, code=7):
    assert sink.emit(context,'process','started')=='accepted'
    sink.finish_detail(context)
    assert sink.emit(context,'tool','completed',{'exit_code':code,'stdout':'must not persist'},
        invocation_outcome='returned',operation_outcome='failed' if code else 'succeeded')=='accepted'


def wait_written(store, count):
    deadline=time.monotonic()+5
    while store.health()['written_seq']<count and time.monotonic()<deadline: time.sleep(0.01)
    assert store.health()['written_seq']>=count,store.health()


@pytest.mark.parametrize('document',[{'mode':'invalid'},{'summaryMaxMiB':True},{'detailRetentionDays':31},
    {'storageRoot':'relative'},{'unknown':1},{'summaryRetentionDays':1}])
def test_invalid_capture_configuration(document):
    with pytest.raises((ValueError,TypeError)): CaptureSettings.parse(document)


def test_policy_document_round_trip_and_private_root(tmp_path):
    policy=CaptureSettings.parse({'mode':'detail','storageRoot':str(tmp_path/'records')})
    assert CaptureSettings.parse(policy.document())==policy
    store=ExecutionStore(policy.root(tmp_path),policy,workspace=tmp_path/'workspace')
    assert store.health()['storage_available'],store.health()
    assert producer_live(store.directory) is True
    assert store.close()=='drained'
    assert producer_live(store.directory) is False
    if os.name!='nt':
        assert store.root.stat().st_mode & 0o077==0
        assert all(p.stat().st_mode & 0o077==0 for p in store.directory.iterdir())


def test_failed_command_evidence_survives_new_producer(tmp_path):
    root=tmp_path/'records'; store=ExecutionStore(root); sink=EventSink(store)
    context=sink.begin_invocation('run_command',{'scope':'workspace','path':'tests'})
    complete(sink,context)
    assert store.close()=='drained',store.health()
    result=ExecutionQuery(root).show(context.operation_id)
    assert result['operation_outcome']=='failed'
    assert result['record_complete']
    assert result['items'][-1]['result']['exit_code']==7
    assert result['items'][-1]['invocation_outcome']=='returned'
    second=ExecutionStore(root); assert second.producer_id!=store.producer_id
    assert second.close()=='drained'
    assert ExecutionQuery(root).show(context.operation_id)['operation_outcome']=='failed'
    assert store.health()['synced_seq']==store.health()['written_seq']


def test_two_producers_and_concurrent_contexts_do_not_mix(tmp_path):
    root=tmp_path/'records'; stores=[ExecutionStore(root),ExecutionStore(root)]
    assert all(s.health()['storage_available'] for s in stores),[s.health() for s in stores]
    sinks=[EventSink(s) for s in stores]
    def work(i):
        sink=sinks[i%2]; c=sink.begin_invocation('run_command',{'path':f'task-{i}'})
        complete(sink,c,i)
        return c
    with ThreadPoolExecutor(max_workers=4) as pool: contexts=list(pool.map(work,range(16)))
    for store in stores: assert store.close()=='drained',store.health()
    for c in contexts:
        events=ExecutionQuery(root).show(c.operation_id)['items']
        assert len(events)==3
        assert {e['producer_id'] for e in events}=={c.producer_id}
        assert {e['invocation_id'] for e in events}=={c.invocation_id}


def test_summary_does_not_store_raw_inputs_and_detail_is_redacted(tmp_path):
    root=tmp_path/'records'; settings=CaptureSettings(mode='detail')
    store=ExecutionStore(root,settings); sink=EventSink(store)
    c=sink.begin_invocation('run_command',{'path':'https://user:password@host/path?signature=secret'})
    sink.emit(c,'tool','progress',{'prompt':'do not retain','environment':'do not retain','exit_code':7})
    assert sink.detail(c,'stdout','key=synthetic api_key=secret-value sk-synthetic123 https://user:pw@example.com/a?signature=abcdef')=='accepted'
    complete(sink,c); assert store.close()=='drained',store.health()
    raw=b''.join(p.read_bytes() for p in store.directory.glob('*.jsonl')).decode()
    for forbidden in ('do not retain','secret-value','sk-synthetic123','signature=abcdef','user:pw'):
        assert forbidden not in raw
    assert '<hidden>' in raw and '[redacted]' in raw
    shown=ExecutionQuery(root).show(c.operation_id,detail=True)
    assert any(e.get('stream')=='stdout' for e in shown['items'])
    assert not any('text' in e for e in ExecutionQuery(root).show(c.operation_id)['items'])


def test_unknown_payload_and_cross_producer_are_rejected(tmp_path):
    a=ExecutionStore(tmp_path/'a'); b=ExecutionStore(tmp_path/'b')
    sink=EventSink(a); context=EventSink(b).begin_invocation('read_text')
    assert sink.emit(context,'tool','completed')=='dropped'
    assert a.publish({'kind':'tool','phase':'received','prompt':'a raw secret'})=='dropped'
    assert a.health()['write_failure_count']>0
    a.close(); b.close()
    assert 'a raw secret' not in ''.join(p.read_text() for p in a.directory.glob('*.jsonl'))


def test_capture_off_and_detail_window_expiry(tmp_path):
    off=ExecutionStore(tmp_path/'off',CaptureSettings(mode='off'))
    sink=EventSink(off); c=sink.begin_invocation('run_command')
    assert sink.emit(c,'tool','completed')=='disabled'
    assert not off.root.exists()
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    c=sink.begin_invocation('run_command'); sink.deadline=0
    assert sink.detail(c,'stdout','never retain')=='disabled'
    complete(sink,c); store.close()
    assert not list(store.directory.glob('detail-*.jsonl'))


def test_detail_byte_budget_is_utf8_bounded(tmp_path):
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    c=sink.begin_invocation('run_command')
    for _ in range(12): sink.detail(c,'stdout','中文😀'*5000)
    complete(sink,c); store.close()
    records=ExecutionQuery(store.root).show(c.operation_id,detail=True)['items']
    output=[e for e in records if e.get('stream')=='stdout']
    assert sum(len(e['text'].encode()) for e in output)<=65536
    assert any(e['capture']['payload_truncated'] for e in output)


def test_storage_overlap_and_symlink_are_rejected(tmp_path):
    workspace=tmp_path/'workspace'; workspace.mkdir()
    for path in (workspace,workspace/'records',tmp_path):
        with pytest.raises(PermissionError): ExecutionStore(path,workspace=workspace)
    target=tmp_path/'target'; target.mkdir()
    link=tmp_path/'link'
    try: link.symlink_to(target,target_is_directory=True)
    except OSError: pytest.skip('Symlink creation is unavailable')
    with pytest.raises(PermissionError): ExecutionStore(link/'records')


def test_existing_permissions_are_never_relaxed(tmp_path):
    if os.name=='nt':
        # An ordinary pytest directory has inherited access, unlike a newly
        # created private record root. Refuse it rather than hardening in place.
        broad=tmp_path/'broad'; broad.mkdir()
        store=ExecutionStore(broad)
        if store.health()['storage_available']:
            store.close(); pytest.skip('Test parent already has a private ACL')
        assert store.health()['write_failure_count']>0
    else:
        broad=tmp_path/'broad'; broad.mkdir(mode=0o755); broad.chmod(0o755)
        store=ExecutionStore(broad)
        assert not store.health()['storage_available']
        assert broad.stat().st_mode & 0o077==0o055


def test_writer_failure_is_reported_and_shutdown_is_bounded(tmp_path,monkeypatch):
    store=ExecutionStore(tmp_path/'records'); sink=EventSink(store)
    wait_written(store,1)
    def failed(*args): raise OSError('signature=real-looking-secret must not be logged')
    monkeypatch.setattr(store,'_write_event',failed)
    c=sink.begin_invocation('run_command'); complete(sink,c)
    deadline=time.monotonic()+3
    while store.health()['write_failure_count']==0 and time.monotonic()<deadline: time.sleep(0.01)
    assert store.health()['write_failure_count']>0
    begin=time.monotonic(); store.close(); assert time.monotonic()-begin<2.2
    raw=b''.join(p.read_bytes() for p in store.directory.iterdir() if p.is_file()).decode(errors='replace')
    assert 'real-looking-secret' not in raw
    assert not ExecutionQuery(store.root).show(c.operation_id)['record_complete']


def test_queue_pressure_and_stalled_close_are_visible(tmp_path,monkeypatch):
    limits=StoreLimits(normal_items=2,critical_items=2,sync_seconds=0.02)
    store=ExecutionStore(tmp_path/'records',limits=limits); sink=EventSink(store)
    wait_written(store,1)
    gate=threading.Event(); original=store._write_event
    monkeypatch.setattr(store,'_write_event',lambda *args:(gate.wait(3),original(*args))[-1])
    c=sink.begin_invocation('run_command')
    for _ in range(30): sink.emit(c,'tool','progress')
    assert store.health()['dropped_summary']>0
    assert store.health()['queue_depth']<=4
    assert store.close(0)=='incomplete'
    gate.set(); assert store.close()=='drained',store.health()


def test_rotation_retention_and_evicted_cursor(tmp_path):
    root=tmp_path/'records'; limits=StoreLimits(segment_bytes=1400,summary_bytes=6000,sync_seconds=0.02)
    store=ExecutionStore(root,limits=limits); sink=EventSink(store)
    first=sink.begin_invocation('run_command'); complete(sink,first)
    wait_written(store,4)
    query=ExecutionQuery(root); page=query.page(limit=1)
    cursor=page['next_cursor']; assert cursor
    for i in range(24):
        c=sink.begin_invocation('run_command'); complete(sink,c,i)
        wait_written(store,store.health()['observed_seq'])
    assert store.close()=='drained',store.health()
    assert sum(p.stat().st_size for p in store.directory.glob('summary-*.jsonl'))<=6000
    resumed=query.page(cursor=cursor)
    assert resumed['cursor_gap']
    assert query.status()['retention_generation']>0


def test_crash_tail_is_preserved_as_incomplete(tmp_path):
    root=tmp_path/'records'
    script='''import sys,os,time
from pathlib import Path
from tiancheng_mcp.execution_store import ExecutionStore
from tiancheng_mcp.execution_events import EventSink
s=ExecutionStore(Path(sys.argv[1])); e=EventSink(s); c=e.begin_invocation('run_command')
while s.health()['synced_seq']<2: time.sleep(.01)
print(c.operation_id,flush=True); os._exit(0)
'''
    result=subprocess.run([sys.executable,'-c',script,str(root)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    operation=result.stdout.strip(); query=ExecutionQuery(root)
    assert not query.show(operation)['record_complete']
    path=next(root.glob('producers/*/summary-*.jsonl'))
    with path.open('ab') as stream: stream.write(b'{"incomplete":')
    report=query.show(operation)
    assert any(q['reason']=='tail_incomplete' for q in report['quality'])
    assert report['operation_outcome']=='unknown'


def test_cursor_binding_budget_and_no_overwrite_export(tmp_path):
    store=ExecutionStore(tmp_path/'records'); sink=EventSink(store)
    c=sink.begin_invocation('run_command'); complete(sink,c); store.close()
    query=ExecutionQuery(store.root); page=query.page(limit=1)
    small=query.page(max_bytes=1)
    assert small['next_cursor'] and small['scan_incomplete']
    assert not any(q['reason'].startswith('tail_') for q in small['quality'])
    resumed=query.page(cursor=small['next_cursor'])
    assert resumed['items'] and not resumed['cursor_gap']
    assert page['scan_incomplete'] and page['next_cursor']
    other=ExecutionQuery(tmp_path/'other')
    with pytest.raises(ValueError): other.page(cursor=page['next_cursor'])
    with pytest.raises(ValueError): query.page({'tool':'write_text'},page['next_cursor'])
    export_dir=private_directory(tmp_path/'export')
    output=export_dir/'evidence.json'
    result=query.export(c.operation_id,output)
    assert result['events']==3
    assert json.loads(output.read_bytes())['operation']==c.operation_id
    before=output.read_bytes()
    with pytest.raises(FileExistsError): query.export(c.operation_id,output)
    assert output.read_bytes()==before


def test_command_projection_hides_arbitrary_code_and_arguments():
    argv=safe_argv(['python','-c','password=supersecret','--api-key','sk-realish123','private-file.txt'])
    assert 'supersecret' not in repr(argv) and 'sk-realish123' not in repr(argv)
    assert 'private-file.txt' not in repr(argv)


def test_secret_and_private_key_split_across_output_chunks(tmp_path):
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    c=sink.begin_invocation('run_command')
    for chunk in ('api_', 'key=split-secret', '\nsk-', 'synthetic-token123\n',
                  '-----BEGIN PRIVATE KEY-----\n', 'unrecognizable-secret-body\n', '-----END PRIVATE KEY-----\n'):
        assert sink.detail(c,'stdout',chunk)=='accepted'
    complete(sink,c); assert store.close()=='drained'
    raw=''.join(p.read_text() for p in store.directory.glob('*.jsonl'))
    for secret in ('split-secret','synthetic-token123','unrecognizable-secret-body'):
        assert secret not in raw
    assert '[private key redacted]' in raw


def test_child_detail_quotas_share_one_root_task(tmp_path):
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    parent=sink.begin_invocation('agent_run'); child=sink.child(parent,{'run_id':'run_123abc'})
    assert child.root_operation_id==parent.operation_id
    for _ in range(10): sink.detail(child,'stdout','x'*10000+'\n')
    sink.finish_detail(child)
    root=sink._detail_totals[parent.operation_id]
    assert root['all']==65536 and root[child.operation_id+':stdout']==65536
    assert sink.detail(child,'stdout','new data\n')=='dropped'
    sink.finish_detail(parent); assert sink._detail_entries==0
    store.close()


def test_invalid_cursor_and_corrupt_records_are_not_executed(tmp_path):
    store=ExecutionStore(tmp_path/'records'); sink=EventSink(store)
    c=sink.begin_invocation('run_command'); complete(sink,c); store.close()
    path=next(store.directory.glob('summary-*.jsonl'))
    with path.open('ab') as stream: stream.write(b'not json\n{"schema_version":99}\n')
    query=ExecutionQuery(store.root); report=query.show(c.operation_id)
    assert not report['record_complete']
    assert any(q['reason']=='corrupt_or_unsupported_record' for q in report['quality'])
    with pytest.raises(ValueError): query.page(cursor='not-a-valid-cursor')


def test_local_query_cli_does_not_create_a_service(tmp_path,capsys):
    from tiancheng_mcp.observability_admin import main
    root=tmp_path/'records'; store=ExecutionStore(root); sink=EventSink(store)
    c=sink.begin_invocation('run_command'); complete(sink,c); store.close()
    main(['--project-root',str(tmp_path),'--storage-root',str(root),'show',c.operation_id,'--json'])
    value=json.loads(capsys.readouterr().out)
    assert value['operation_outcome']=='failed'
    assert len(list((root/'producers').iterdir()))==1


def test_local_config_capture_merge_is_validated(tmp_path):
    from tiancheng_mcp.runtime_config import launcher_config
    (tmp_path/'config').mkdir()
    (tmp_path/'config/launcher.defaults.json').write_text(json.dumps({'observability':{'mode':'summary','summaryMaxMiB':64}}))
    local=tmp_path/'local.json'; local.write_text(json.dumps({'observability':{'mode':'detail','storageRoot':str(tmp_path/'records')}}))
    policy=launcher_config(tmp_path,local,{})['observability']
    assert policy['mode']=='detail' and policy['summaryMaxMiB']==64
    local.write_text(json.dumps({'observability':{'summaryMaxMiB':False}}))
    with pytest.raises(ValueError): launcher_config(tmp_path,local,{})


def test_exact_detail_budget_has_explicit_truncation_without_extra_payload(tmp_path):
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    context=sink.begin_invocation('run_command')
    for _ in range(8):
        sink.detail(context,'stdout','x'*8191+'\n')
    assert sink.detail(context,'stdout','extra')=='dropped'
    assert sink.detail(context,'stdout','more')=='dropped'
    sink.finish_detail(context)
    assert store.close()=='drained'
    rows=ExecutionQuery(store.root).show(context.operation_id,detail=True)['items']
    details=[e for e in rows if e['channel']=='detail']
    assert sum(len(e['text'].encode()) for e in details)==65536
    markers=[e for e in details if e['text']=='' and e['capture']['payload_truncated']]
    assert len(markers)==1 and markers[0]['source_bytes']==0


def test_pending_line_keeps_quota_truncation_flag_until_flush(tmp_path):
    store=ExecutionStore(tmp_path/'records',CaptureSettings(mode='detail')); sink=EventSink(store)
    context=sink.begin_invocation('run_command')
    for _ in range(255):
        sink.detail(context,'stdout','a'*255+'\n')
    sink.detail(context,'stdout','z'*400)
    sink.finish_detail(context)
    assert store.close()=='drained'
    details=[e for e in ExecutionQuery(store.root).show(context.operation_id,detail=True)['items'] if e['channel']=='detail']
    pending=next(e for e in details if e['text']=='z'*256)
    assert pending['capture']['payload_truncated']
