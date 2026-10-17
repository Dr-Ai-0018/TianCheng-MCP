"""Isolated, reproducible observation measurement; never opens formal config.

Run with the repo venv and a NEW output directory under .tmp. Raw timings,
RSS samples, source hashes and synthetic journals stay local. No deletion,
service restart, deployment, network access or real credentials are required.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time
from datetime import datetime, timezone

from tiancheng_mcp.execution_config import CaptureSettings
from tiancheng_mcp.execution_events import EventSink
from tiancheng_mcp.execution_store import ExecutionStore
from tiancheng_mcp.policy import AccessPolicy
from tiancheng_mcp.service import TianChengService


if os.name == 'nt':
    class WindowsCounters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
             'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
             'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _psapi = ctypes.WinDLL('psapi', use_last_error=True)
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(WindowsCounters), wintypes.DWORD]


def rss_bytes():
    if os.name == 'nt':
        counters = WindowsCounters()
        counters.cb = ctypes.sizeof(counters)
        if not _psapi.GetProcessMemoryInfo(_kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError('RSS measurement unavailable')
        return int(counters.WorkingSetSize)
    resident = int(Path('/proc/self/statm').read_text().split()[1])
    return resident * os.sysconf('SC_PAGE_SIZE')


def distribution(samples):
    ordered = sorted(samples)
    def percentile(fraction):
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]
    return {'count': len(ordered), 'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
            'p99_ms': percentile(.99), 'max_ms': max(ordered)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('summary', 'detail'), required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--calls', type=int, default=200)
    parser.add_argument('--interval-ms', type=float, default=20)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--stress', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10 or not 20 <= args.calls <= 2000 or not 5 <= args.interval_ms <= 100:
        parser.error('Measurement sizes outside bounded range')
    if not 1 <= args.duration <= 600:
        parser.error('Sustained duration must be 1..600 seconds')
    output = args.output.resolve()
    if output.exists():
        parser.error('Use a new output directory; existing evidence is never overwritten')
    output.mkdir(parents=True)
    workspace = output / 'workspace'
    workspace.mkdir()
    (workspace / 'fixture.txt').write_text('synthetic-stat-fixture', encoding='utf-8')
    report = {'schema_version': 1, 'mode': args.mode, 'started_at': datetime.now(timezone.utc).isoformat(),
              'python': platform.python_version(), 'platform': platform.platform(), 'rounds': [],
              'latency_method': 'paired same-service stat dispatch; sink disabled vs enabled; jobs disabled for short calls',
              'rss_method': 'current resident working set; same warmed service before observer activation',
              'configuration': {'calls': args.calls, 'interval_ms': args.interval_ms, 'duration_seconds': args.duration}}
    project = Path(__file__).resolve().parents[1]
    report['source_sha256'] = {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((project / 'src/tiancheng_mcp').glob('execution_*.py'))}
    report['source_sha256']['src/tiancheng_mcp/service.py'] = hashlib.sha256((project / 'src/tiancheng_mcp/service.py').read_bytes()).hexdigest()
    report['source_sha256']['scripts/benchmark_execution_records.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['source_sha256']['src/tiancheng_mcp/jobs.py'] = hashlib.sha256((project / 'src/tiancheng_mcp/jobs.py').read_bytes()).hexdigest()
    service = TianChengService(workspace, None, allow_exec=True, enable_agent_catalog=False,
        access_policy=AccessPolicy.default(workspace), access_policy_path=output / 'policy.json',
        agent_source_policy_path=output / 'sources.json', agent_profile_config_path=output / 'profiles.json',
        agent_env_file=output / 'agent.env', command_policy_path=output / 'commands.json',
        observability_config={'mode': 'off', 'storageRoot': str(output / 'records')})
    jobs = service.jobs
    service.jobs = None
    for _ in range(30):
        service.run_with_fallback('stat', 'fixture.txt', lambda: service.stat('fixture.txt'))
    gc.collect()
    rss_before = rss_bytes()
    settings = CaptureSettings(mode=args.mode, storage_root=str(output / 'records'))
    store = ExecutionStore(settings.root(project), settings, workspace=workspace)
    sink = EventSink(store)
    service.capture_settings = settings
    service.execution_record_root = store.root
    service.execution_store = store
    service.execution_sink = sink
    report['rss_before_observer'] = rss_before
    report['rss_samples'] = []
    report['queue_peak'] = {'depth': 0, 'bytes': 0}
    if not store.health()['storage_available']:
        raise RuntimeError('Isolated capture activation failed')

    def sample():
        health = store.health()
        report['rss_samples'].append({'elapsed': time.monotonic() - started, 'bytes': rss_bytes()})
        report['queue_peak']['depth'] = max(report['queue_peak']['depth'], health['queue_depth'])
        report['queue_peak']['bytes'] = max(report['queue_peak']['bytes'], health['queue_bytes'])

    started = time.monotonic()
    sample()
    try:
        reference = service.stat('fixture.txt')
        for index in range(args.rounds):
            rows = {}
            order = ('off', 'capture') if index % 2 == 0 else ('capture', 'off')
            for label in order:
                service.execution_sink = sink if label == 'capture' else None
                durations = []
                for number in range(args.calls):
                    call_started = time.perf_counter_ns()
                    result = service.run_with_fallback('stat', 'fixture.txt', lambda: service.stat('fixture.txt'))
                    durations.append((time.perf_counter_ns() - call_started) / 1e6)
                    if result != reference:
                        raise AssertionError('Original stat return changed')
                    if number % 10 == 0:
                        sample()
                    time.sleep(max(0, args.interval_ms / 1000 - durations[-1] / 1000))
                rows[label] = {'distribution': distribution(durations), 'samples_ms': durations}
            rows['added_p95_ms'] = rows['capture']['distribution']['p95_ms'] - rows['off']['distribution']['p95_ms']
            report['rounds'].append(rows)
            print(json.dumps({'phase':'short_round','mode':args.mode,'round':index+1,
                              'added_p95_ms':rows['added_p95_ms']}, ensure_ascii=True), flush=True)
        service.execution_sink = sink
        report['steady_health'] = store.health()
        report['rss_steady_peak_increment'] = max(row['bytes'] for row in report['rss_samples']) - rss_before
        service.jobs = jobs
        service.interactive_timeout_seconds = .01
        sustain_start = time.monotonic()
        target = sustain_start + args.duration
        handles = []
        source_totals = {}
        source_lock = threading.Lock()
        def task(name, outputting):
            total = returned = chunks = 0
            preserved = True
            while time.monotonic() < target:
                chunk_seconds = min(200, target - time.monotonic())
                if chunk_seconds <= 0:
                    break
                if outputting:
                    code = ("import time\nend=time.monotonic()+" + repr(chunk_seconds)
                        + "\nline='synthetic-line token=sk-benchmark-only123456 ' + 'x'*470\n"
                          "while time.monotonic()<end:\n print(line, flush=True)\n time.sleep(.25)\n")
                else:
                    code = 'import time; time.sleep(' + repr(chunk_seconds) + ')'
                result = service.run_command('python', ['-c', code], timeout_seconds=300, max_output_bytes=1024*1024)
                if result['exit_code'] != 0 or result['timeout'] or result['stdout_truncated']:
                    raise AssertionError('Synthetic business command did not complete unchanged')
                total += result['stdout_bytes_total']
                returned += len(result['stdout'].encode('utf-8'))
                if outputting:
                    preserved = preserved and 'sk-benchmark-only123456' in result['stdout']
                chunks += 1
            with source_lock:
                source_totals[name] = {'source_stdout_bytes': total, 'returned_utf8_bytes': returned,
                                       'commands': chunks, 'original_marker_preserved': preserved}
            return {'count': chunks}
        for name, outputting in [('background_a', False), ('background_b', False), ('continuous_output', True)]:
            handle = service.run_with_fallback('run_command', '.', lambda n=name, o=outputting: task(n, o))
            handles.append(service.jobs.get(handle['job_id']))
        last_progress = sustain_start
        print(json.dumps({'phase':'sustain_started','mode':args.mode,'seconds':args.duration}), flush=True)
        while not all(record.done.is_set() for record in handles):
            sample()
            if time.monotonic() > target + 15:
                raise TimeoutError('Synthetic sustained jobs exceeded verification deadline')
            if time.monotonic()-last_progress >= 60:
                print(json.dumps({'phase':'sustain_progress','mode':args.mode,
                                  'elapsed_seconds':round(time.monotonic()-sustain_start,1),
                                  'health':store.health()}), flush=True)
                last_progress=time.monotonic()
            time.sleep(.1)
        report['sustained_job_states'] = [{'state': r.state, 'error_type': r.error_type} for r in handles]
        if any(record.state != 'succeeded' for record in handles):
            raise AssertionError('Sustained jobs failed')
        report['sustained'] = {'elapsed_seconds': time.monotonic() - sustain_start,
                               'source_totals': source_totals, 'health': store.health()}
        report['rss_sustained_peak_increment'] = max(row['bytes'] for row in report['rss_samples']) - rss_before
        if args.stress:
            gate = threading.Event()
            entered = threading.Event()
            original_write = store._write_event
            def blocked(channel, event):
                entered.set()
                if not gate.wait(60):
                    raise TimeoutError('Synthetic slow-writer gate')
                return original_write(channel, event)
            store._write_event = blocked
            try:
                service.run_with_fallback('stat', 'fixture.txt', lambda: service.stat('fixture.txt'))
                if not entered.wait(2):
                    raise AssertionError('Writer pressure injection did not activate')
                samples = []
                for index in range(500):
                    call_started = time.perf_counter_ns()
                    result = service.run_with_fallback('stat', 'fixture.txt', lambda: service.stat('fixture.txt'))
                    samples.append((time.perf_counter_ns() - call_started) / 1e6)
                    if result != reference:
                        raise AssertionError('Writer pressure changed business return')
                    if index % 10 == 0:
                        sample()
                report['pressure'] = {'health_while_blocked': store.health(), 'distribution': distribution(samples),
                                      'samples_ms': samples, 'original_result_preserved': True}
            finally:
                gate.set()
                store._write_event = original_write
        sample()
    finally:
        service.execution_sink = sink
        service.jobs = jobs
        service.shutdown()
        report['final_health'] = store.health()
        report['elapsed_seconds'] = time.monotonic() - started
        report['rss_peak_increment_including_pressure'] = max(row['bytes'] for row in report['rss_samples']) - rss_before
        report['journal_bytes'] = {channel: sum(p.stat().st_size for p in store.root.rglob(channel + '-*.jsonl'))
                                   for channel in ('summary', 'detail')}
        details = []
        detail_secret_free = True
        for path in store.root.rglob('detail-*.jsonl'):
            raw = path.read_bytes()
            detail_secret_free &= b'sk-benchmark-only123456' not in raw
            details.extend(json.loads(line) for line in raw.splitlines())
        report['detail_capture'] = {'events': len(details),
            'source_bytes': sum(e.get('source_bytes', 0) for e in details),
            'text_utf8_bytes': sum(len(e.get('text', '').encode()) for e in details),
            'truncated_events': sum(bool(e.get('capture', {}).get('payload_truncated')) for e in details),
            'synthetic_token_redacted': detail_secret_free}
        report['limits'] = {'normal_items': store.limits.normal_items, 'critical_items': store.limits.critical_items,
                            'normal_bytes': store.limits.normal_bytes, 'critical_bytes': store.limits.critical_bytes,
                            'summary_bytes': settings.summary_max_mib*1024*1024, 'detail_bytes': settings.detail_max_mib*1024*1024}
        report['target_scope'] = {'short_p95_le_5ms': 'required for summary; detail measured for comparison',
                                  'rss_le_16mib': 'conservative in-process increment including profiler and business buffers'}
        report['gates'] = {'short_p95_le_5ms': all(r['added_p95_ms'] <= 5 for r in report['rounds']),
                          'steady_rss_le_16mib': report.get('rss_steady_peak_increment', 2**63) <= 16*1024*1024,
                          'sustained_rss_le_16mib': report.get('rss_sustained_peak_increment', 2**63) <= 16*1024*1024,
                          'steady_no_loss': not any(report.get('steady_health', {}).get(k, 1) for k in ('dropped_summary', 'dropped_detail', 'write_failure_count')),
                          'sustained_no_loss': not any(report.get('sustained', {}).get('health', {}).get(k, 1) for k in ('dropped_summary', 'dropped_detail', 'write_failure_count')),
                          'queue_and_disk_bounded': report['queue_peak']['depth'] <= store.limits.normal_items+store.limits.critical_items
                              and report['queue_peak']['bytes'] <= store.limits.normal_bytes+store.limits.critical_bytes
                              and all(report['journal_bytes'][c] <= report['limits'][c+'_bytes'] for c in ('summary','detail'))}
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output / 'result.json'), 'gates': report['gates'],
                      'final_health': report['final_health']}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
