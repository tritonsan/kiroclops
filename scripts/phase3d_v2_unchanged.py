"""Measure and seal an unchanged historical Kiro candidate in a fresh v2 session.

This runner never starts a model, writes movement code, repairs a failed trial,
or grants full behavior acceptance. It submits the normal durable Session jobs
so final pixel/functional evidence can be reviewed by the existing reviewers.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import media_suite, realistic_policy, suite_readiness
from kiroclops.local_engine.backend import TERMINAL, _lock
from kiroclops.local_engine.execution_policy import REVISED_ID, policy_from_state, validate_policy
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, Session, _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

SAFETY_SECONDS = 180
POLL_SECONDS = 10
ORIGIN_KIND = 'ExistingKiroCandidateOrigin'


def _need(condition, message, code='invalid_unchanged_stage'):
    if not condition:
        raise VisualError(code, message)


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _verify_files(files):
    _need(isinstance(files, dict) and files and all(isinstance(name, str) and _sha(sha)
        for name, sha in files.items()), 'Expected a nonempty full-byte file binding.')
    for name, sha in files.items():
        path = _ordinary(name)
        _need(path.is_file() and file_sha256(path) == sha, 'Bound evidence changed: ' + name, 'source_changed')


def _initial_quality(directory, suite):
    result = read_json(directory / 'result.json')
    plan = read_json(directory / 'plan.json')
    binding = read_json(directory / 'result-binding.json')
    inputs = read_json(directory / 'source-bindings.json')
    files = source_hashes(directory)
    del files['result-binding.json']
    _need(binding.get('kind') == 'ExistingParentInitialQualityReceipt' and binding.get('schema_version') == 1
        and binding.get('result_sha256') == digest(result) and binding.get('output_file_hashes') == files,
        'The append-only initial-quality receipt or output changed.', 'initial_quality_changed')
    _need(result.get('kind') == 'ExistingParentInitialQuality' and result.get('schema_version') == 1
        and result.get('status') == 'sufficient_initial_quality'
        and result.get('complete') is True and result.get('measurement_and_source_qualified') is True
        and result.get('initial_quality_pass') is True and result.get('repair_required') is False
        and result.get('error') is None, 'Existing-source quality is not sufficient.', 'initial_quality_not_sufficient')
    for key in ('validation_opened', 'model_started', 'new_model_generation', 'new_candidate_created',
                'candidate_budget_consumed', 'app_or_capture_started', 'new_functional_checks_run', 'full_behavior_acceptance'):
        _need(result.get(key) is False, 'Initial-quality evidence must remain pre-model and pre-validation.')
    _need(plan.get('kind') == 'ExistingParentInitialQualityPlan' and plan.get('schema_version') == 1
        and result.get('plan_sha256') == digest(plan) and plan.get('suite_sha256') == digest(suite)
        and result.get('suite_sha256') == digest(suite)
        and plan.get('execution_policy') == suite['execution_policy']
        and plan.get('acceptance_policy_sha256') == digest(suite['acceptance_policy'])
        and result.get('source_epoch') == suite['source_epoch']
        and result.get('acceptance_profile') == suite['acceptance_profile']
        and result.get('authoring_parent_source_sha256') == digest(suite['authoring_parent_source_hashes']),
        'Initial-quality evidence belongs to another suite, policy or source.', 'wrong_initial_quality')
    selected = [{key: deepcopy(case[key]) for key in ('case_id', 'role', 'stage', 'purpose', 'scenario')}
        for case in suite['cases'] if case['role'] in ('calibration', 'regression')]
    rows = result.get('comparisons', [])
    _need(plan.get('selected_cases') == selected and len(rows) == len(selected)
        and {(row.get('case_id'), row.get('role')) for row in rows} == {(case['case_id'], case['role']) for case in selected}
        and all((row.get('comparison') or {}).get('pass') is True
            and row['comparison'].get('measurement_pass') is True
            and row['comparison'].get('baseline_measurement_pass') is True for row in rows),
        'Every calibration and known-regression comparison must have passed.', 'initial_quality_not_sufficient')
    for row in rows:
        comparison = read_json(safe_path(directory, 'comparisons/' + row['case_id'] + '.json'))
        _need(comparison == row, 'An initial-quality comparison changed.', 'initial_quality_changed')
    consumed = inputs.get('consumed_files')
    _need(inputs.get('validation_files_opened') == [] and binding.get('input_bindings_sha256') == digest(consumed),
        'Initial-quality input receipt changed.', 'initial_quality_changed')
    producer = Path(__file__).with_name('phase3d_v2_initial_quality.py').resolve()
    _need(isinstance(consumed, dict) and consumed.get(str(producer)) == file_sha256(producer),
        'Initial-quality producer identity is missing.', 'initial_quality_changed')
    _verify_files(consumed)
    return {'directory': str(directory), 'result_sha256': digest(result),
        'receipt_sha256': file_sha256(directory / 'result-binding.json'),
        'output_file_hashes': source_hashes(directory), 'input_bindings_sha256': digest(consumed)}


def _origin(path, expected_sha256, suite):
    _need(_sha(expected_sha256) and file_sha256(path) == expected_sha256,
        'Supply the exact byte SHA-256 of the operator-provided origin manifest.', 'origin_changed')
    value = read_json(path)
    expected = suite['authoring_parent_source_hashes']
    _need(value.get('kind') == ORIGIN_KIND and value.get('schema_version') == 1
        and value.get('source_hashes') == expected and value.get('source_sha256') == digest(expected)
        and value.get('new_model_generation') is False,
        'Origin must bind the exact existing historical candidate without a new generation claim.', 'wrong_origin')
    historical = _ordinary(value['historical_session'])
    candidate_id = value.get('historical_candidate_id')
    _need(isinstance(candidate_id, str) and re.fullmatch('candidate-[0-9]{2}', candidate_id) is not None,
        'Historical candidate identifier is malformed.', 'wrong_origin')
    historical_state = Session(historical)._load()  # Binding-only read; no jobs are reconciled or started.
    candidates = [item for item in historical_state['candidates'] if item['candidate_id'] == candidate_id]
    _need(len(candidates) == 1 and candidates[0].get('status') == 'succeeded'
        and candidates[0].get('source_sha256') == digest(expected), 'Historical candidate identity differs.', 'wrong_origin')
    source = historical / 'candidates' / candidate_id / 'source'
    _need(source_hashes(source) == expected, 'Historical candidate source changed.', 'source_changed')
    job_directory = historical / 'jobs' / candidates[0]['job_id']
    job = read_json(job_directory / 'job.json')
    job_result = read_json(job_directory / 'result.json')
    _need(job.get('status') == 'succeeded' and job.get('operation') == 'evaluate'
        and job.get('candidate_id') == candidate_id and job.get('source_hashes') == expected
        and job.get('source_sha256') == digest(expected) and job.get('result_sha256') == digest(job_result)
        and read_json(job_directory / 'binding.json').get('sha256') == digest({key: job[key] for key in JOB_BINDING_FIELDS}),
        'Historical source/result job binding changed.', 'wrong_origin')
    required = {str(historical / name): file_sha256(historical / name) for name in ('session.json', 'binding.json')}
    required.update({str(job_directory / name): file_sha256(job_directory / name)
                     for name in ('job.json', 'binding.json', 'result.json')})
    required.update({str(source / name): sha for name, sha in expected.items()})
    files = value.get('historical_binding_files')
    _need(isinstance(files, dict) and all(files.get(name) == sha for name, sha in required.items()),
        'Origin must bind historical session, source and evaluation receipts.', 'wrong_origin')
    _verify_files(files)
    return {'manifest': str(path), 'manifest_sha256': expected_sha256, 'manifest_document_sha256': digest(value),
        'historical_session': str(historical), 'historical_candidate_id': candidate_id,
        'historical_binding_files': deepcopy(files), 'source_sha256': digest(expected),
        'native_authorship_review_status': 'pending', 'new_model_generation': False}


def _source_unchanged(session, expected):
    state = session._load()
    _need(state['baseline_hashes'] == expected and source_hashes(state['baseline']) == expected
        and session._verify_sources(state) == expected and source_hashes(state['target']) == expected,
        'This stage permits no target or baseline code change.', 'source_changed')
    for candidate in state['candidates']:
        source = session.directory / 'candidates' / candidate['candidate_id'] / 'source'
        _need(candidate.get('source_sha256') == digest(expected) and source_hashes(source) == expected,
            'An unchanged measurement candidate differs from its existing parent.', 'source_changed')
    return state


def _no_native(session):
    _need(not (session.directory / 'kiro' / 'launch.json').exists()
        and not (session.directory / 'kiro' / 'task-prompt.json').exists()
        and not (session.directory / 'kiro' / 'events.jsonl').exists(),
        'An unchanged measurement stage cannot contain native model-run evidence.', 'model_already_started')


def _preflight(session, quality, origin, origin_sha):
    state = session._load()
    execution = policy_from_state(state)
    _need(state.get('schema_version') == 2 and execution['policy_id'] == REVISED_ID
        and state.get('domain') in ('web_panel', 'svg') and state.get('mode') == 'tools',
        'Use a fresh panel/SVG v2 tools session.', 'unsupported_schema')
    experiment = state['experiment']
    _need(experiment.get('reference_mode') == 'media_suite' and experiment.get('stage') in ('panel', 'svg'),
        'The session must use the frozen revised reference suite.')
    _need(state.get('status') == 'new' and state.get('started_at') is None and state.get('deadline_at') is None
        and state.get('clock_origin') is None and state.get('prepared') is False
        and state.get('prepare_job_id') is None and not state.get('candidates') and state.get('active_job_id') is None
        and state.get('selected_candidate_id') is None and state.get('sealed_at') is None
        and state.get('final_job_id') is None and state.get('final') is None,
        'Never reuse a started session, measurement attempt or candidate.', 'session_not_fresh')
    _no_native(session)
    suite_directory = _ordinary(state['reference'])
    suite = media_suite.load_suite(suite_directory)
    _need(suite.get('schema_version') == 2 and suite['stage'] == experiment['stage']
        and suite['domain'] == state['domain'] and digest(suite) == experiment.get('suite_sha256')
        and validate_policy(suite.get('execution_policy'), stage=suite['stage']) == execution,
        'Session and suite execution/source bindings differ.', 'wrong_suite')
    policy = realistic_policy.validate_policy(suite.get('acceptance_policy'), domain=suite['stage'])
    _need(experiment.get('acceptance_profile') == suite.get('acceptance_profile') == policy['id']
        and experiment.get('acceptance_policy_sha256') == policy['policy_sha256']
        and experiment.get('source_epoch') == suite.get('source_epoch')
        and suite['analysis_source_hashes'] == media_suite.analysis_source_hashes(2),
        'Revised acceptance policy or analysis code changed.', 'wrong_suite')
    _source_unchanged(session, suite['authoring_parent_source_hashes'])
    # Prepared Kiro configuration is allowed and is verified by Session. No
    # configure or launch function is imported or called by this script.
    _need(state.get('controls_hashes') == session._controls(), 'Prepared controls are not bound.', 'control_changed')
    quality_binding = _initial_quality(quality, suite)
    origin_binding = _origin(origin, origin_sha, suite)
    admission = suite_readiness.validate(session.directory, suite)
    _source_unchanged(session, suite['authoring_parent_source_hashes'])
    return state, suite, quality_binding, origin_binding, admission


def _poll(session, submitted, operation, deadline, output):
    job_id = submitted['job_id']
    last = None
    outer_deadline = deadline + SAFETY_SECONDS
    while True:
        _no_native(session)
        _need(time.time() < outer_deadline, 'Owned-job transport safety deadline expired.', 'transport_timeout')
        last = session.get_job(job_id, wait_seconds=min(POLL_SECONDS, max(0, deadline - time.time())),
                               allow_final_result=True)
        _need(last.get('job_id') == job_id and last.get('operation') == operation,
            'The returned job does not belong to this submitted operation.', 'wrong_job')
        write_json(output / 'jobs' / (operation + '-latest.json'), last)
        if last.get('status') in TERMINAL:
            _need(type(last.get('finished_at')) in (int, float) and math.isfinite(last['finished_at']),
                'A terminal job needs its durable completion time.', 'invalid_job')
            _need(last['finished_at'] <= deadline, 'The job completed outside its frozen phase budget.', 'phase_timeout')
            return last
        _need(time.time() < deadline, 'Frozen measurement/validation budget expired.', 'phase_timeout')


def run(directory, quality, origin, origin_sha256, output):
    directory, quality, origin, output = map(_ordinary, (directory, quality, origin, output))
    _need(not output.exists(), 'Keep every prior attempt unchanged.', 'output_exists')
    for protected in (directory, quality, origin):
        _need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
            'Output must be separate from the session and input receipts.', 'unsafe_path')
    session = Session(directory)
    state, suite, quality_binding, origin_binding, admission = _preflight(session, quality, origin, origin_sha256)
    expected = suite['authoring_parent_source_hashes']
    execution = suite['execution_policy']
    intent = {'kind': 'NoCodeChangeStageIntent', 'schema_version': 1, 'stage': suite['stage'],
        'session_id': state['session_id'], 'session_directory': str(directory), 'suite_sha256': digest(suite),
        'source_epoch': suite['source_epoch'], 'source_sha256': digest(expected), 'source_hashes': deepcopy(expected),
        'acceptance_profile': suite['acceptance_profile'], 'acceptance_policy_sha256': suite['acceptance_policy']['policy_sha256'],
        'execution_policy': deepcopy(execution), 'safety_seconds': SAFETY_SECONDS,
        'initial_quality': quality_binding, 'historical_origin': origin_binding,
        'admission_sha256': digest(admission), 'admission_file_sha256': file_sha256(directory / 'admission.json'),
        'runner_sha256': file_sha256(__file__), 'clock_origin': 'unchanged_initial_measurement',
        'new_model_generation': False, 'model_started': False, 'code_change_allowed': False,
        'maximum_measurement_candidates': 1, 'automatic_repair': False, 'automatic_restart': False,
        'candidate_meaning': 'Measurement snapshot of existing historical Kiro code, not a newly generated solution.',
        'full_behavior_acceptance': False, 'created_at': time.time()}
    # A separate orchestration lease prevents two copies of this runner from
    # accepting the same fresh session. Session owns all actual job processes.
    with _lock(directory / 'unchanged-stage.lock', timeout=1):
        _need(not (directory / 'unchanged-stage-intent.json').exists(), 'This session already has an unchanged-stage intent.', 'session_not_fresh')
        # Recheck after lease acquisition before any clock/job mutation.
        _need(session._load().get('started_at') is None and not session._load().get('candidates'),
              'Session changed during admission.', 'session_not_fresh')
        output.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'unchanged-stage-intent.json', intent)
        write_json(output / 'intent.json', intent)
        write_json(output / 'intent-binding.json', {'kind': 'NoCodeChangeStageIntentReceipt', 'schema_version': 1,
            'intent_sha256': digest(intent), 'intent_file_sha256': file_sha256(output / 'intent.json'),
            'session_intent_sha256': file_sha256(directory / 'unchanged-stage-intent.json')})
        owned, completed = [], []
        active = None
        failure = None
        calibration_eligible = numeric_pass = False
        candidate_id = None
        final_submitted = False
        cleanup = []
        started = time.time()
        try:
            clock = session.start_clock(origin='unchanged_initial_measurement')
            _need(clock['deadline_at'] == clock['started_at'] + execution['max_seconds'],
                'Canonical authoring clock differs from frozen policy.', 'invalid_execution_policy')
            authoring_deadline = clock['deadline_at']
            for operation in ('prepare', 'evaluate', 'finalize'):
                _source_unchanged(session, expected)
                _no_native(session)
                _need(time.time() < authoring_deadline, 'Do not seal after the frozen authoring deadline.', 'phase_timeout')
                submitted = session.submit(operation, candidate_id=candidate_id if operation == 'finalize' else None)
                active = submitted['job_id']
                owned.append(active)
                if operation == 'evaluate':
                    candidate_id = submitted.get('candidate_id')
                    _need(candidate_id == 'candidate-01', 'Exactly one unchanged measurement candidate is allowed.', 'wrong_candidate')
                if operation == 'finalize':
                    final_submitted = True
                    final_state = session._load()
                    _need(final_state.get('selected_candidate_id') == candidate_id
                        and type(final_state.get('sealed_at')) in (int, float)
                        and final_state['sealed_at'] < authoring_deadline, 'Canonical finalization did not seal the unchanged candidate.', 'wrong_candidate')
                    durable = read_json(directory / 'jobs' / active / 'job.json')
                    deadline = durable['created_at'] + execution['max_validation_seconds']
                else:
                    deadline = authoring_deadline
                result = _poll(session, submitted, operation, deadline, output)
                completed.append(deepcopy(result))
                active = None
                _source_unchanged(session, expected)
                _need(result['status'] == 'succeeded', 'Owned ' + operation + ' job did not succeed.',
                    (result.get('error') or {}).get('code', operation + '_failed'))
                _need(result.get('source_sha256') == digest(expected), 'Job measured different source bytes.', 'wrong_candidate')
                if operation == 'evaluate':
                    measured = session._load()['candidates']
                    calibration_eligible = len(measured) == 1 and measured[0].get('eligible') is True
                    _need(calibration_eligible and (result.get('result') or {}).get('eligible') is True,
                        'Unchanged calibration failed; preserve the failure and do not repair.', 'calibration_failed')
                if operation == 'finalize':
                    numeric_pass = (result.get('result') or {}).get('pass') is True
            _source_unchanged(session, expected)
            _no_native(session)
            _verify_files(origin_binding['historical_binding_files'])
            _need(file_sha256(origin) == origin_sha256, 'Operator origin manifest changed.', 'origin_changed')
            _need(_initial_quality(quality, suite) == quality_binding, 'Initial-quality evidence changed.', 'initial_quality_changed')
        except (VisualError, OSError, KeyError, ValueError, TypeError, KeyboardInterrupt) as error:
            failure = {'code': getattr(error, 'code', 'unchanged_stage_interrupted' if isinstance(error, KeyboardInterrupt)
                                      else 'unchanged_stage_failed'), 'message': str(error)}
            if active is not None and active in owned:
                try:
                    cleanup.append(session.cancel_job(active))
                except (VisualError, OSError, KeyError, ValueError, TypeError) as cleanup_error:
                    cleanup.append({'job_id': active, 'status': 'cleanup_failed', 'error': str(cleanup_error)})
        try:
            _source_unchanged(session, expected)
            _no_native(session)
            unchanged = True
        except (VisualError, OSError, KeyError, ValueError, TypeError) as source_error:
            unchanged = False
            failure = failure or {'code': getattr(source_error, 'code', 'source_changed'), 'message': str(source_error)}
        result = {'kind': 'NoCodeChangeStageResult', 'schema_version': 1,
            'status': 'numerical_pass_pending_reviews' if numeric_pass and unchanged and failure is None
                else 'numerical_failure' if final_submitted and failure is None else 'unchanged_stage_failed',
            'intent_sha256': digest(intent), 'suite_sha256': digest(suite), 'session_id': state['session_id'],
            'stage': suite['stage'], 'source_sha256': digest(expected), 'source_unchanged': unchanged,
            'initial_quality_status': 'sufficient_initial_quality', 'calibration_eligible': calibration_eligible,
            'final_submitted': final_submitted, 'numerical_pass': numeric_pass and unchanged and failure is None,
            'candidate_id': candidate_id, 'owned_job_ids': owned, 'completed_jobs': completed, 'cleanup': cleanup,
            'error': failure, 'started_at': started, 'finished_at': time.time(),
            'clock_origin': 'unchanged_initial_measurement', 'model_started': False, 'new_model_generation': False,
            'code_written': False, 'automatic_repair': False, 'automatic_restart': False,
            'candidate_origin': 'measurement_snapshot_of_existing_kiro_code',
            'heldout_comparison_started': final_submitted, 'independent_raw_review_status': 'pending',
            'source_review_status': 'pending', 'functional_review_status': 'pending',
            'native_origin_review_status': 'pending', 'full_behavior_acceptance': False}
        write_json(output / 'result.json', result)
        canonical_files, canonical_errors = {}, []

        def bind_file(path):
            try:
                canonical_files[str(path)] = file_sha256(path)
            except (VisualError, OSError, ValueError) as error:
                canonical_errors.append({'path': str(path), 'code': getattr(error, 'code', 'evidence_unavailable'), 'message': str(error)})

        for name in ('session.json', 'binding.json', 'admission.json', 'unchanged-stage-intent.json'):
            bind_file(directory / name)
        for job_id in owned:
            for name in ('job.json', 'binding.json', 'result.json'):
                path = directory / 'jobs' / job_id / name
                if path.is_file():
                    bind_file(path)
        for candidate in session._load()['candidates']:
            source = directory / 'candidates' / candidate['candidate_id'] / 'source'
            try:
                canonical_files.update({str(source / name): sha for name, sha in source_hashes(source).items()})
            except (VisualError, OSError, ValueError) as error:
                canonical_errors.append({'path': str(source), 'code': getattr(error, 'code', 'evidence_unavailable'), 'message': str(error)})
        write_json(output / 'canonical-bindings.json', {'kind': 'NoCodeChangeCanonicalEvidence', 'schema_version': 1,
            'status': 'complete' if not canonical_errors else 'incomplete', 'files': canonical_files,
            'errors': canonical_errors, 'new_model_generation': False, 'full_behavior_acceptance': False})
        write_json(output / 'result-binding.json', {'kind': 'NoCodeChangeStageResultReceipt', 'schema_version': 1,
            'result_sha256': digest(result), 'output_file_hashes': source_hashes(output)})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--initial-quality', required=True, type=Path)
    parser.add_argument('--origin', required=True, type=Path)
    parser.add_argument('--origin-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(args.session, args.initial_quality, args.origin, args.origin_sha256, args.output)
    except (VisualError, OSError, KeyError, ValueError, TypeError) as error:
        print(json.dumps({'status': 'rejected_before_start', 'code': getattr(error, 'code', 'invalid_unchanged_stage'), 'message': str(error)}))
        return 1
    print(json.dumps({key: result[key] for key in ('status', 'candidate_id', 'numerical_pass', 'new_model_generation', 'full_behavior_acceptance')}))
    return 0 if result['numerical_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
