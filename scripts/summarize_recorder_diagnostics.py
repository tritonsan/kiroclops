"""Summarize all14 declared fixture probes without rescoring the frozen study.

Missing attempts remain missing. This reads receipts only: no model, capture,
retry, threshold change or causal claim is produced.
"""
from pathlib import Path
import argparse
import math
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, VisualError

FINISHED = 'diagnostics_finished_interpretation_required'
METRICS = ('median_interval_s', 'p95_interval_s', 'maximum_motion_gap_s',
           'clock_uncertainty_s')


def need(condition, message):
    if not condition:
        raise VisualError('diagnostic_evidence_invalid', message)


def summarize_receipt(value):
    need(value.get('kind') == 'ProspectiveCaptureTimingProbe', 'Unexpected probe kind')
    need(value.get('source_unchanged') is True, 'Probe source changed')
    for key in ('acceptance_run', 'model_run', 'failed_trial_replaced', 'deadlines_changed'):
        need(value.get(key) is False, 'Probe was not an unchanged fixture diagnostic')
    need(value.get('retries') == 0, 'Undeclared retry')
    timing = value.get('timing') or {}
    need(value.get('status') in ('captured', 'failed'), 'Probe is not terminal')
    need(timing.get('status') in (None, 'pass', 'fail'), 'Unknown timing verdict')
    error = value.get('error')
    # A completed capture may still have invalid timing. Conversely a failed
    # capture without timings is not a measured motion mismatch.
    status = ('capture_failure' if value['status'] == 'failed' or error else
              'timing_pass' if timing.get('status') == 'pass' else
              'timing_failure' if timing.get('status') == 'fail' else 'timing_unavailable')
    measures = {}
    for key in METRICS:
        v = timing.get(key)
        need(v is None or (type(v) in (int, float) and math.isfinite(v) and v >= 0),
             'Invalid timing value')
        measures[key] = v
    operations = {}
    for event in value.get('trace', {}).get('events', []):
        name, duration = event.get('name'), event.get('duration_s')
        need(isinstance(name, str) and type(duration) in (int, float)
             and math.isfinite(duration) and duration >= 0, 'Invalid trace operation')
        item = operations.setdefault(name, {'count': 0, 'maximum_duration_s': 0.0})
        item['count'] += 1
        item['maximum_duration_s'] = max(item['maximum_duration_s'], duration)
    return {'status': status, 'timing_status': timing.get('status'),
            'metrics_s': measures, 'timing_errors': timing.get('errors', []),
            'error': error, 'operations': operations,
            'trace_dropped_events': value.get('trace', {}).get('dropped_old_events'),
            'causality_established': False}


def declared_cases():
    first = []
    for domain, scenario, policies in (
        ('panel', 'open-w960-r1', ('frozen', 'earlier-real-png')),
        ('panel', 'open-close-w960-r1', ('earlier-real-png', 'frozen')),
        ('panel', 'reverse-w1280-r2', ('frozen', 'earlier-real-png')),
        ('svg', 'three-to-four-w960-r1', ('earlier-real-png', 'frozen')),
    ):
        first += [(f'{domain}-{scenario}-{p}', domain, scenario, p, None) for p in policies]
    second = [(p, 'svg', 'three-to-five-w1280-r1', p, None)
              for p in ('frozen', 'earlier-real-png')]
    third = [(f'{i+1:02}-{mode}', 'svg', 'three-to-five-w1280-r1', 'frozen', mode)
             for i, mode in enumerate(('none', 'durable', 'durable', 'none'))]
    return [('completion-postcohort-recorder-diagnostics-01', first),
            ('completion-final-svg-recorder-diagnostics-01', second),
            ('completion-durable-progress-diagnostics-01', third)]


def run(base, output):
    base, output = Path(base).resolve(), Path(output).resolve()
    need(not output.exists(), 'Preserve previous summaries; use a fresh output')
    bindings, rows = {}, []

    def bound(path):
        value = read_json(path)
        bindings[str(path)] = file_sha256(path)
        return value

    for directory, cases in declared_cases():
        folder = base/directory
        pipeline = bound(folder/'pipeline.json')
        need(pipeline['status'] == FINISHED, 'Finish every declared probe before interpretation')
        need(len(pipeline['rows']) == len(cases), 'Missing or extra declared attempt')
        need(pipeline.get('primary_outcomes_modified') is False
             and pipeline.get('thresholds_changed') is False, 'Experiment was changed')
        for (name, domain, scenario, policy, mode), entry in zip(cases, pipeline['rows']):
            if mode is None:
                need(entry.get('policy') == policy, 'Probe policy or ordering differs')
                if 'id' in entry:
                    need(entry['id'] == name, 'Probe identity differs')
            else:
                need(entry.get('mode') == mode, 'Durable callback order differs')
            need(entry.get('owned_tree_closed', entry.get('owned_process_tree_closed')) is True,
                 'Owned process cleanup is not confirmed')
            receipt = folder/name/'timing-probe.json'
            row = {'group': directory, 'id': name, 'domain': domain,
                   'scenario': scenario, 'policy': policy, 'progress_callback': mode}
            if not receipt.exists():
                need(entry['status'] in ('probe_incomplete', 'incomplete'),
                     'Recorded result has disappeared')
                row.update(status='incomplete', error={'code': 'probe_incomplete'},
                           metrics_s={}, timing_status=None, causality_established=False)
            else:
                need(entry['status'] == 'recorded', 'Unregistered receipt')
                value = bound(receipt)
                need(bindings[str(receipt)] == entry['receipt_sha256'], 'Probe receipt changed')
                need((value['domain'], value['scenario_id'], value['diagnostic_static_policy'])
                     == (domain, scenario, policy), 'Receipt belongs to a different probe')
                row.update(summarize_receipt(value))
                row['receipt_sha256'] = bindings[str(receipt)]
            if mode is not None:
                sink = folder/(name+'-sink')/'progress-io.json'
                if sink.exists():
                    io = bound(sink)
                    need(io['mode'] == mode and io['causality_established'] is False,
                         'Wrong durable I/O receipt')
                    peaks = {}
                    for op in io['operations']:
                        duration = op['duration_s']
                        need(type(duration) in (int, float) and math.isfinite(duration)
                             and duration >= 0, 'Invalid progress I/O duration')
                        peaks[op['operation']] = max(peaks.get(op['operation'], 0), duration)
                    row['progress_io_maximum_s'] = peaks
                else:
                    row['progress_io_evidence'] = 'unavailable'
            rows.append(row)
    need(len(rows) == 14, 'All14 original diagnostic attempts required')
    need(all(file_sha256(path) == sha for path, sha in bindings.items()), 'Evidence changed during reading')
    result = {'kind': 'RecorderDiagnosticInterpretation', 'status': 'completed', 'rows': rows,
              'counts': {s: sum(r['status'] == s for r in rows) for s in
                         ('timing_pass', 'timing_failure', 'capture_failure', 'timing_unavailable', 'incomplete')},
              'primary_outcomes_modified': False, 'causality_established': False,
              'reliability_established': False, 'production_patch_authorized_by_this_report': False,
              'scope': 'Fixture-only diagnostic observations, not model trials or repaired old outcomes. '
                       'Small ordered samples and instrumented timings cannot establish general reliability or root cause.',
              'inputs': bindings}
    write_json(output/'result.json', result)
    lines = ['# Recorder diagnostic experiments', '', result['scope'], '',
             '| Experiment | Sampling | Durable progress write | Result | p95 / largest motion gap (ms) |',
             '|---|---|---|---|---|']
    for row in rows:
        metric = row['metrics_s']
        fmt = lambda k: '—' if metric.get(k) is None else f'{1000*metric[k]:.2f}'
        lines.append(f"| {row['domain']} / {row['scenario']} | {row['policy']} | {row['progress_callback'] or 'none'} | {row['status']} | {fmt('p95_interval_s')} / {fmt('maximum_motion_gap_s')} |")
    lines += ['', 'Missing recordings are not counted as successful. The six old recording failures are not considered fixed by these diagnostics. '
              'Overlapping operations are not evidence of a root cause. The source and acceptance thresholds were not changed.', '',
              '[Linked numerical data](result.json)', '']
    (output/'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    print({'status': result['status'], 'counts': result['counts'], 'trials_replaced': False})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=ROOT/'artifacts/phase3a')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.base, args.output)
