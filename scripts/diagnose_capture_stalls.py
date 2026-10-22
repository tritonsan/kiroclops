"""Prospective, models-free recorder diagnostics after the frozen cohort.

The probe does not relax deadlines or retry captures. It retains timings in
memory and writes them after recording. No analysis VideoPackage is produced.
Observed overlap is reported as correlation, never proof of a root cause.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import ExitStack
import gc
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TimingTrace:
    def __init__(self, capacity=20000):
        self.started = time.perf_counter()
        self.rows = deque(maxlen=capacity)
        self.total = 0
        self.capture_thread = threading.get_ident()

    def add(self, name, start, *, error=None, **fields):
        end = time.perf_counter()
        self.total += 1
        self.rows.append({'name': name, 'start_s': start-self.started,
                         'end_s': end-self.started, 'duration_s': end-start,
                         'capture_thread': threading.get_ident() == self.capture_thread,
                         'error_type': error, **fields})

    def sync(self, name, function):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            error = None
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self.add(name, start, error=error)
        return wrapped

    def asynchronous(self, name, function):
        async def wrapped(*args, **kwargs):
            start = time.perf_counter()
            error = None
            try:
                return await function(*args, **kwargs)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self.add(name, start, error=error)
        return wrapped

    async def observe(self, operation):
        ready = asyncio.Event()
        async def heartbeat():
            interval = .010
            due = time.perf_counter() + interval
            ready.set()
            while True:
                await asyncio.sleep(max(0, due-time.perf_counter()))
                now = time.perf_counter()
                self.add('event_loop_lateness', due, lateness_s=max(0, now-due))
                due = now + interval
        task = asyncio.create_task(heartbeat())
        await ready.wait()
        try:
            return await operation()
        finally:
            # One loop cycle lets a blocked timer record its actual wake time.
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def snapshot(self):
        rows = list(self.rows)
        peaks = sorted((r for r in rows if r['name'] == 'event_loop_lateness'),
                       key=lambda r: r['lateness_s'], reverse=True)[:10]
        for peak in peaks:
            peak['overlapping_operations'] = [r['name'] for r in rows
                if r is not peak and r['name'] != 'event_loop_lateness'
                and r['start_s'] <= peak['end_s'] and r['end_s'] >= peak['start_s']]
        return {'event_count': self.total, 'dropped_old_events': self.total-len(rows),
                'events': rows, 'largest_loop_delays': peaks,
                'interpretation': 'In-memory timings; overlaps do not establish causation. Instrumentation can affect runtime.'}


def require_no_running_trials(progress):
    if not progress.is_file():
        raise ValueError('Known cohort progress is required before diagnostic captures')
    state = json.loads(progress.read_text('utf-8'))
    if any(row.get('status') in {'running', 'registered'} or
           any(stage.get('status') == 'running' for stage in row.get('stages', []))
           for row in state['rows']):
        raise ValueError('The preregistered cohort must finish before diagnostic captures')


def require_review_preparation_finished(path):
    state = json.loads(path.read_text('utf-8'))
    if state.get('status') != 'preparation_finished_independent_inspection_required':
        raise ValueError('Heavy post-cohort review preparation must finish before diagnostic captures')


def staged_static_policy():
    """Explicit diagnostic override; never silently replace frozen code."""
    from kiroclops.visual.schema import read_json, file_sha256
    from kiroclops.web_local import natural_capture
    folder = ROOT/'artifacts/phase3a/static-sampling-compatibility-01'
    record = read_json(folder/'result.json')
    proposed = folder/'natural-capture-proposed.py'
    if (record.get('status') != 'staged_synthetic_pass_native_pending'
            or file_sha256(natural_capture.__file__) != record['production_source_sha256']
            or file_sha256(proposed) != record['proposed_source_sha256']):
        raise ValueError('Static-sampling proposal is not bound to current frozen sources')
    expected = {**natural_capture.POLICY, 'static_image_stale_s': .010,
                'static_pixel_cadence_policy': 'earlier_real_png_v2'}
    if record['policy'] != expected:
        raise ValueError('Unexpected diagnostic policy change')
    return record['policy'], {'proposal_sha256': file_sha256(proposed),
                             'test_receipt_sha256': file_sha256(folder/'result.json')}


def run(domain, output, *, static_policy='frozen', scenario_id=None):
    from kiroclops.visual.schema import write_json, file_sha256
    from kiroclops.web_local import capture, natural_capture
    from kiroclops.web_local.specs import fixture_project, capture_scenarios
    from playwright.async_api import CDPSession, Page

    progress = ROOT / 'artifacts/phase3a/completion-paired-cohort-01/progress.json'
    require_no_running_trials(progress)
    require_review_preparation_finished(ROOT/'artifacts/phase3a/completion-postcohort-reviews-01/pipeline.json')
    if static_policy not in ('frozen', 'earlier-real-png'):
        raise ValueError('Unknown diagnostic policy')
    override = staged_static_policy() if static_policy == 'earlier-real-png' else None
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Fresh diagnostic output required')
    output.mkdir(parents=True)
    trace = TimingTrace()
    sources = {str(p.relative_to(ROOT)): file_sha256(p) for p in
               [Path(capture.__file__), Path(natural_capture.__file__), Path(__file__)]}
    gc_starts = {}
    def gc_event(phase, info):
        key = (threading.get_ident(), info.get('generation'))
        if phase == 'start':
            gc_starts[key] = time.perf_counter()
        elif key in gc_starts:
            trace.add('python_gc', gc_starts.pop(key), generation=key[1])
    original_run = capture.run_capture
    original_send = CDPSession.send
    async def send(self, method, *args, **kwargs):
        return await trace.asynchronous('cdp.'+method, original_send)(self, method, *args, **kwargs)
    def run_observed(factory, receipt):
        return original_run(lambda: trace.observe(factory), receipt)
    cancel_path = output / 'cancel-request.json'
    cancelled = trace.sync('cancel_file_exists', cancel_path.exists)
    result, failure = None, None
    # Fixture target only. A named validation input may diagnose the recorder,
    # but does not run, tune or replace any model's selected candidate.
    scenarios = capture_scenarios(domain, 'calibration', 'qualified-natural-20260927')
    if scenario_id:
        scenarios += capture_scenarios(domain, 'validation', 'qualified-natural-20260927')
        matches = [s for s in scenarios if s['id'] == scenario_id]
        if len(matches) != 1:
            raise ValueError('Unknown or ambiguous diagnostic scenario')
        scenario = matches[0]
    else:
        scenario = scenarios[0]
    gc.callbacks.append(gc_event)
    try:
        with ExitStack() as stack:
            if override:
                stack.enter_context(patch.object(natural_capture, 'POLICY', override[0]))
            stack.enter_context(patch.object(capture, 'run_capture', run_observed))
            stack.enter_context(patch.object(capture, '_write', trace.sync('capture_sync_file_write', capture._write)))
            stack.enter_context(patch.object(capture, 'visible_hit_box', trace.asynchronous('visible_hit_box', capture.visible_hit_box)))
            stack.enter_context(patch.object(natural_capture, 'screenshot_image', trace.asynchronous('screenshot_image', natural_capture.screenshot_image)))
            stack.enter_context(patch.object(CDPSession, 'send', send))
            stack.enter_context(patch.object(Page, 'evaluate', trace.asynchronous('page_evaluate', Page.evaluate)))
            result = capture.capture_run(fixture_project(domain, 'target'), output/'capture', scenario,
                variant='diagnostic_only_not_acceptance', cancel=cancelled, build_package=False)
    except Exception as exc:
        failure = {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}
    finally:
        gc.callbacks.remove(gc_event)
        unchanged = all(file_sha256(ROOT/name) == sha for name, sha in sources.items())
        write_json(output/'timing-probe.json', {'kind': 'ProspectiveCaptureTimingProbe',
            'status': 'failed' if failure else 'captured', 'error': failure, 'domain': domain,
            'source_hashes': sources, 'source_unchanged': unchanged,
            'diagnostic_static_policy': static_policy, 'scenario_id': scenario['id'],
            'runtime_policy_override': override[1] if override else None,
            'actual_sampling_policy': override[0] if override else dict(natural_capture.POLICY),
            'acceptance_run': False, 'model_run': False, 'failed_trial_replaced': False,
            'deadlines_changed': False, 'retries': 0, 'analysis_package_produced': False,
            'timing': result.get('timing') if result else None, 'trace': trace.snapshot()})
    return {'status': 'failed' if failure else 'captured', 'error': failure,
            'receipt': str(output/'timing-probe.json'), 'acceptance_run': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', choices=['panel', 'svg'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--static-policy', choices=['frozen', 'earlier-real-png'], default='frozen')
    parser.add_argument('--scenario', help='Optional exact scenario ID; fixture target only, never a selected model candidate')
    args = parser.parse_args()
    print(json.dumps(run(args.domain, args.output, static_policy=args.static_policy, scenario_id=args.scenario), ensure_ascii=False))
