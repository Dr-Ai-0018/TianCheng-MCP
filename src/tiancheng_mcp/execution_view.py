"""Terminal-safe presentation of the shared local history query model."""
from __future__ import annotations

import json
import re
import unicodedata


def terminal_text(value: object) -> str:
    text = str(value)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf', 'Cs') else c for c in text)


def event_lines(event: dict) -> list[str]:
    identity = f"{event.get('producer_id', '?')} / {event.get('operation_id') or '-'}"
    lines = [f"{event.get('occurred_at', '?')}  {identity}  #{event.get('seq', '?')}"]
    if event.get('channel') == 'detail':
        lines.append(f"  {event.get('stream', '?')} · 仅显示已捕获的受限详情")
        lines.extend('  ' + line for line in event.get('text', '').splitlines())
    else:
        lines.append(f"  {event.get('tool') or event.get('kind', '?')} / {event.get('phase', '?')}"
                     f" · 调用 {event.get('invocation_outcome', 'unknown')}"
                     f" · 操作 {event.get('operation_outcome', 'unknown')}"
                     f" · 证据 {event.get('evidence', 'unavailable')}")
        for key in ('provider', 'parent_operation_id', 'target', 'resources', 'result', 'capture'):
            if event.get(key):
                lines.append(f"  {key}: {json.dumps(event[key], ensure_ascii=False, sort_keys=True)}")
    return [terminal_text(line) for line in lines]


def report_lines(action: str, report: dict) -> list[str]:
    lines = []
    if action == 'recent':
        lines.append('执行记录 · 仅在本页扫描范围内排序；翻页不代表全局最新排序。')
        for event in report.get('operations', []):
            lines.extend(event_lines(event))
        if not report.get('operations'):
            lines.append('本页未找到操作；不代表其他页没有记录。')
    elif action == 'show':
        lines.append(f"操作结果 {report.get('operation_outcome', 'unknown')}"
                     f" · 记录完整 {report.get('record_complete', False)}"
                     f" · 多重匹配 {report.get('ambiguous_matches', False)}")
        if report.get('not_found_in_scanned_range'):
            lines.append('本页扫描范围内未找到；不能据此判断任务未执行。')
        for event in report.get('items', []):
            lines.extend(event_lines(event))
    else:
        lines.append(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    for quality in report.get('quality', []):
        lines.append('记录质量: ' + json.dumps(quality, ensure_ascii=False, sort_keys=True))
    if report.get('cursor_gap'):
        lines.append('游标存在缺口：记录可能已淘汰或改变。')
    if report.get('scan_incomplete'):
        lines.append('扫描尚未完成；本页结果不能代表完整历史。')
    if report.get('next_cursor'):
        lines.append('下一页游标: ' + report['next_cursor'])
    # Apply the same boundary to metadata and to detail output, including bidi.
    return [terminal_text(part) for line in lines for part in line.splitlines()]
