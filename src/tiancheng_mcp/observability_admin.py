"""Local history CLI; viewing records never starts or controls a service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .execution_config import CaptureSettings
from .execution_query import ExecutionQuery
from .execution_view import event_lines, report_lines
from .runtime_config import launcher_config


def main(argv: list[str] | None = None) -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--local',type=Path)
    parser.add_argument('--storage-root',type=Path,help='Explicit local administrator path; never read from record payloads')
    commands=parser.add_subparsers(dest='action',required=True)
    status=commands.add_parser('status'); status.add_argument('--cursor')
    recent=commands.add_parser('recent'); recent.add_argument('--limit',type=int,default=50)
    for name in ('tool','provider','status','since'): recent.add_argument('--'+name)
    recent.add_argument('--cursor')
    outputs = [status, recent]
    for name in ('show','follow','export'):
        cmd=commands.add_parser(name); cmd.add_argument('operation'); cmd.add_argument('--detail',action='store_true')
        if name=='export':
            cmd.add_argument('--output',type=Path)
            cmd.add_argument('--preview',action='store_true',help='Scan the same bounded export without writing a file')
        if name=='show': cmd.add_argument('--cursor')
        outputs.append(cmd)
    for cmd in outputs:
        output = cmd.add_mutually_exclusive_group()
        output.add_argument('--json',action='store_true',help='Machine-readable JSON (default)')
        output.add_argument('--text',action='store_true',help='Terminal-safe readable presentation')
        cmd.add_argument('--view',action='store_true',help='Include readable lines in the JSON report for local menus')
    args=parser.parse_args(argv)
    if os.name == 'nt' and args.text and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if args.action == 'export' and not args.preview and args.output is None:
        parser.error('export requires --output unless --preview is used')
    try:
        config=launcher_config(args.project_root,args.local,{})
        settings=CaptureSettings.parse(config.get('observability'))
        root=args.storage_root or settings.root(args.project_root)
        query=ExecutionQuery(root,workspace=Path(config['workspace']) if config.get('workspace') else None)
        if args.action=='status':
            result=query.status(cursor=args.cursor)
            result['capture_config']={'scope':'next_start','active_mode':'unknown',**settings.document()}
        elif args.action=='recent':
            result=query.recent(limit=args.limit,cursor=args.cursor,filters={k:getattr(args,k) for k in ('tool','provider','status','since') if getattr(args,k)})
        elif args.action=='show': result=query.show(args.operation,detail=args.detail,cursor=args.cursor)
        elif args.action=='export':
            result=(query.preview_export(args.operation,detail=args.detail) if args.preview
                    else query.export(args.operation,args.output,detail=args.detail))
        else:
            cursor=None; seen={}
            while True:
                page=query.show(args.operation,detail=args.detail,cursor=cursor)
                for event in page['items']:
                    key=(event['producer_id'],event['channel'])
                    if event['seq'] > seen.get(key,0):
                        print('\n'.join(event_lines(event)) if args.text else json.dumps(event,ensure_ascii=True),flush=True)
                        seen[key]=event['seq']
                        if len(seen)>4096: seen.pop(next(iter(seen)))
                if page['quality'] or page['cursor_gap']:
                    print(json.dumps({'quality':page['quality'],'cursor_gap':page['cursor_gap']},ensure_ascii=True),flush=True)
                cursor=page['next_cursor']
                time.sleep(0.25)
        if args.view:
            result['display_lines']=report_lines(args.action,result)
        print('\n'.join(report_lines(args.action,result)) if args.text else json.dumps(result,ensure_ascii=True))
    except KeyboardInterrupt:
        return
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'reason':'本机执行记录查询失败；未输出原始异常。'},ensure_ascii=True))
        raise SystemExit(1)


if __name__=='__main__': main()
