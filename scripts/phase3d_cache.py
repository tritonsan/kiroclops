"""Capture private fixed baselines before authoring; no model or budget change."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine import evidence, media_suite
from kiroclops.local_engine.reliability import prepare_runtime
from kiroclops.local_engine.session import Session, _ordinary, source_hashes
from kiroclops.local_engine.suite_evidence import _source_expectation
from kiroclops.local_engine.suite_revision import (
    measurement_source_hashes, stamp_new_measurement, verify_measurement_record,
)
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


def _need(value, message, code='wrong_baseline'):
    if not value:
        raise VisualError(code, message)


def _capture(project, case, destination, reference, check, progress, cancel, runtime_root):
    if case['stage'] in ('jump', 'ground'):
        from kiroclops.local_engine.suite_game import capture_target
        return capture_target(project, case['scenario'], destination, check, progress, cancel,
                              stage=case['stage'], variant='before', runtime_root=Path(runtime_root) / 'runtime/game')
    if reference.get('intake_binding'):
        from kiroclops.local_engine.suite_intake_binding import capture_record
        return capture_record(project, destination, case['scenario'], check, progress, cancel, 'before', reference=reference)
    from kiroclops.local_engine.suite_web import capture_record
    return capture_record(project, destination, case['scenario'], check, progress, cancel, 'before',
                          reference_selection=reference['selection'])


def _baseline_binding(record, case, expected):
    _need(record.get('scenario') == case['scenario'] and record.get('measurement_status') == 'ok',
          'Private baseline must keep its exact scenario and qualified measurement.')
    _need(record.get('source_sha256') == digest(expected.get('session_sources', expected['sources'])),
          'Private baseline belongs to another immutable source.')
    evidence._record_source(record, load_package(record['package'], verify='hashes'), expected)


def cache_baselines(previous_suite, output, authoring_parent, *, runtime_root=Path('C:/kc3d'),
                    check=lambda: None, progress=lambda *args: None, cancel=None):
    """Capture every missing non-calibration original/parent role exactly once.

    Caller owns the common capture lease. Existing evidence is retained, not
    restamped. Unstamped/older retained measurements require a later revision.
    The newly copied suite remains private and cannot establish admission.
    """
    previous_suite, output, authoring_parent = map(_ordinary, (previous_suite, output, authoring_parent))
    _need(not output.exists() and not output.is_relative_to(previous_suite)
          and not output.is_relative_to(authoring_parent), 'Use a fresh cache output outside source/evidence.', 'output_exists')
    suite = media_suite.load_suite(previous_suite, verify_analysis_code=False)
    _need(source_hashes(authoring_parent) == suite['authoring_parent_source_hashes'],
          'Authoring parent changed.', 'wrong_authoring_parent')
    original = previous_suite / suite['original']
    producers = {
        'original': _source_expectation(original, suite['original_source_hashes'], suite['domain']),
        'before': _source_expectation(authoring_parent, suite['authoring_parent_source_hashes'], suite['domain']),
    }
    cases = {case['case_id']: case for case in suite['cases']}
    noncal = [case for case in suite['cases'] if case['role'] in ('regression', 'validation')]
    expected_keys = {(role, case['case_id']) for case in noncal for role in ('original', 'before')}
    cached, retained = {}, []
    for item in suite.get('baseline_records', []):
        key = item['role'], item['case_id']
        _need(key in expected_keys and key not in cached, 'Existing cache contains a foreign or duplicate case.')
        record = media_suite.record_from_suite(previous_suite, item)
        _baseline_binding(record, cases[item['case_id']], producers[item['role']])
        if record.get('measurement_source_hashes') is not None or record.get('measurement_provenance') is not None:
            verify_measurement_record(record, cases[item['case_id']]['stage'])
        cached[key] = record
        retained.append({**deepcopy(item), 'record': record})
    stages = {case['stage'] for case in suite['cases']}
    snapshots = {stage: measurement_source_hashes(stage) for stage in stages}
    executable = media_suite.analysis_source_hashes()
    old_sha = digest(suite)
    output.mkdir(parents=True)
    write_json(output / 'start-binding.json', {'kind': 'PrivateFixedBaselinePreparation', 'schema_version': 1,
        'previous_suite_sha256': old_sha, 'original_source_sha256': digest(suite['original_source_hashes']),
        'authoring_parent_source_sha256': digest(suite['authoring_parent_source_hashes']),
        'required_baseline_records': len(expected_keys), 'retained_baseline_records': len(retained),
        'missing_records': sorted(expected_keys - set(cached)), 'model_started': False,
        'exposed_to_model': False, 'candidate_captured': False, 'thresholds_changed': False, 'budgets_changed': False})
    fresh = []
    missing = [(case, role) for case in noncal for role in ('original', 'before') if (role, case['case_id']) not in cached]
    for index, (case, role) in enumerate(missing):
        check()
        reference = media_suite.record_from_suite(previous_suite, case)
        stage = case['stage']
        snapshot = measurement_source_hashes(stage)
        _need(snapshot == snapshots[stage], 'Measurement code changed before capture.', 'analysis_source_changed')
        capture_started_at = time.time()
        transaction = {'case_id': case['case_id'], 'role': role, 'scenario_sha256': digest(case['scenario']),
                       'measurement_source_hashes': snapshot, 'capture_started_at': capture_started_at,
                       'source_sha256': digest(suite['original_source_hashes'] if role == 'original' else suite['authoring_parent_source_hashes'])}
        write_json(output / 'capture-bindings' / role / (case['case_id'] + '.json'), transaction)
        progress('private ' + role + ' ' + case['case_id'], index, len(missing))
        destination = output / 'captures' / role / case['case_id']
        project = original if role == 'original' else authoring_parent
        record = _capture(project, case, destination, reference, check, progress, cancel, runtime_root)
        _baseline_binding(record, case, producers[role])
        record['capture_transaction'] = deepcopy(transaction)
        record = stamp_new_measurement(record, stage, expected_sources=snapshot, capture_started_at=capture_started_at)
        write_json(destination / 'record.json', record)
        fresh.append({'case_id': case['case_id'], 'role': role, 'record': record})
        cached[role, case['case_id']] = record
    check()
    _need(set(cached) == expected_keys, 'Private baseline matrix is incomplete.')
    _need(snapshots == {stage: measurement_source_hashes(stage) for stage in stages}
          and executable == media_suite.analysis_source_hashes(),
          'Measurement or orchestration changed during cache preparation.', 'analysis_source_changed')
    _need(digest(media_suite.load_suite(previous_suite, verify_analysis_code=False)) == old_sha,
          'Previous suite changed during cache preparation.')
    records = [{**deepcopy(case), 'record': media_suite.record_from_suite(previous_suite, case)} for case in suite['cases']]
    initial = [{**deepcopy(item), 'record': media_suite.record_from_suite(previous_suite, item)} for item in suite.get('initial_records', [])]
    for item in initial:
        _baseline_binding(item['record'], cases[item['case_id']], producers[item['role']])
    new = media_suite.create_suite(output / 'suite', domain=suite['domain'], stage=suite['stage'], records=records,
        required_cases=suite['required_cases'], original=original, authoring_parent=authoring_parent,
        goal=suite['goal'], event_contract=suite['event_contract'], preservation=suite['preservation_contract'],
        initial_records=initial, baseline_records=retained + fresh)
    # A prospective canonical revision checks/reanalyzes all retained records.
    # This cache wrapper never certifies their historical measurement code.
    result = {'status': 'pending_measurement_revision', 'suite': str(output / 'suite'), 'suite_sha256': digest(new),
        'previous_suite_sha256': old_sha, 'reference_cases': len(records), 'initial_cases': len(initial),
        'private_baseline_records': len(cached), 'new_captures': len(fresh), 'retained_captures': len(retained),
        'private_baseline_cache_complete': True, 'measurement_revision_required': True,
        'model_started': False, 'exposed_to_model': False, 'candidate_captured': False,
        'reference_pool_changed': False, 'thresholds_changed': False, 'budgets_changed': False,
        'reference_raw_review_status': 'pending', 'initial_raw_review_status': 'pending'}
    write_json(output / 'cache.json', result)
    return result


def run(previous, identifier, root=Path('C:/kc3d')):
    _need(isinstance(identifier, str) and re.fullmatch(r'[a-z0-9-]{1,40}', identifier),
          'Use a short lowercase cache identifier.', 'unsafe_path')
    previous, root = _ordinary(previous), _ordinary(root)
    output, session = root / 'p' / identifier, root / 's' / identifier
    _need(not output.exists() and not session.exists(), 'Keep earlier cache attempts unchanged.', 'output_exists')
    saved = Session(previous)
    state = saved._load()
    _need(state.get('experiment', {}).get('reference_mode') == 'media_suite', 'Expected a reference suite session.', 'invalid_suite')
    _need(state.get('started_at') is None and not state.get('candidates'), 'Private baseline cache must precede model work.', 'session_started')
    runtime = prepare_runtime(root, browser_path=state['experiment'].get('runtime_environment', {}).get('PLAYWRIGHT_BROWSERS_PATH'))
    old_environment = {key: os.environ.get(key) for key in (*runtime['environment'], 'KIROCLOPS_ANALYSIS_BACKEND')}
    os.environ.update(runtime['environment'])
    os.environ['KIROCLOPS_ANALYSIS_BACKEND'] = 'local'

    def progress(phase, completed=0, total=1):
        print(phase, flush=True)
        write_json(output / 'progress.json', {'phase': phase, 'completed': completed, 'total': total,
                                            'model_started': False, 'exposed_to_model': False})

    try:
        with _lock(Path(runtime['capture_lock']), timeout=60), _lock(previous / 'session.lock', timeout=60):
            state = saved._load()
            _need(state.get('started_at') is None and not state.get('candidates'),
                  'Private baseline cache must precede model work.', 'session_started')
            _need(source_hashes(state['reference']) == state['reference_hashes']
                  and source_hashes(state['baseline']) == state['baseline_hashes']
                  and digest(media_suite.load_suite(state['reference'], verify_analysis_code=False)) == state['experiment']['suite_sha256'],
                  'Session source or reference changed.', 'source_changed')
            for job in (previous / 'jobs').glob('*/job.json'):
                _need(read_json(job).get('status') not in ('queued', 'running'), 'Wait for current session work.', 'job_running')
            old_ready = read_json(previous / 'environment-readiness.json')
            result = cache_baselines(state['reference'], output, state['baseline'], runtime_root=root, progress=progress)
            media_suite.create_suite_session(session, result['suite'], state['baseline'], runtime=runtime)
            write_json(session / 'environment-readiness.json', {**old_ready, 'runtime': runtime,
                'suite_sha256': result['suite_sha256'], 'status': 'pending_raw_review', 'reference_revision': str(previous),
                'private_baseline_records': result['private_baseline_records'], 'private_baseline_cache_complete': True,
                'measurement_revision_required': True, 'initial_record_count': result['initial_cases'],
                'reference_raw_review_status': 'pending', 'initial_raw_review_status': 'pending', 'model_started': False})
            result = {**result, 'session': str(session)}
            write_json(output / 'result.json', result)
            return result
    finally:
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    args = parser.parse_args()
    try:
        result = run(args.previous, args.id, args.root)
    except (VisualError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', 'cache_failed'), 'message': str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
