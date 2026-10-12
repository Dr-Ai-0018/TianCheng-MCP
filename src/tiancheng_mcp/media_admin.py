"""Local-only media configuration edits; never exposed as an MCP tool."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .environment_admin import read_local, MAX_CONFIG_BYTES
from .command_policy import trusted_path
from .media_config import MediaSettings
from .policy import _atomic_write_bytes
from .runtime_config import launcher_config


def explain(project_root: Path, local: Path) -> dict:
    read_local(local)
    settings = MediaSettings.parse(launcher_config(project_root, local)['media'])
    return {**settings.document(), 'configuration_role': 'next_start', 'runtime_configuration': 'unknown',
            'experimental': ['exports', 'attachments'], 'restart_required': True,
            'limits': {'image_source_bytes': 32*1024*1024, 'export_bytes': 4*1024*1024,
                       'attachment_bytes': 32*1024*1024, 'result_ttl_seconds': 900}}


def edit(project_root: Path, local: Path, action: str, value: str, hosts: list[str] | None = None,
         proxy_url: str | None = None) -> dict:
    config = launcher_config(project_root, local)
    workspace = Path(config['workspace']) if config.get('workspace') else None
    current = read_local(local, workspace)
    document = MediaSettings.parse(config['media']).document()
    if action in ('images', 'exports'):
        if value not in ('on', 'off'): raise ValueError('Media switch must be on/off')
        document[action] = value == 'on'
    elif action == 'attachments':
        document['attachments'] = {'mode': value, 'hosts': hosts if hosts is not None else document['attachments']['hosts']}
        saved_proxy = proxy_url if proxy_url is not None else config['media']['attachments'].get('proxy_url')
        if saved_proxy is not None:
            document['attachments']['proxy_url'] = saved_proxy
    else:
        raise ValueError('Unknown media configuration action')
    current['media'] = MediaSettings.parse(document).document()
    payload = (json.dumps(current, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(payload) > MAX_CONFIG_BYTES: raise ValueError('Launcher config exceeds byte limit')
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        backup = trusted_path(Path(str(local) + '.media.bak'), workspace, 'Launcher backup')
        _atomic_write_bytes(backup, local.read_bytes())
    _atomic_write_bytes(local, payload)
    return explain(project_root, local)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--local', type=Path, required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    for action in ('images', 'exports', 'attachments'):
        sub = commands.add_parser(action)
        sub.add_argument('value')
        if action == 'attachments':
            sub.add_argument('--host', action='append', default=None)
            sub.add_argument('--proxy-url', default=None)
    args = parser.parse_args()
    try:
        result = explain(args.project_root.absolute(), args.local.absolute()) if args.action == 'status' else edit(
            args.project_root.absolute(), args.local.absolute(), args.action, args.value, getattr(args, 'host', None),
            getattr(args, 'proxy_url', None))
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'reason': '媒体配置校验或保存失败；未输出配置内容。'}, ensure_ascii=True))
        raise SystemExit(1)


if __name__ == '__main__': main()
