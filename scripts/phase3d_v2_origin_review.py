"""Verify historical Kiro authorship and an unchanged selected source snapshot.

This is a source/authorship axis only. It replays historical evidence using its
existing validator and never starts a model, captures images, edits code, or
grants full behavior acceptance to the new measurement Session.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import review
from kiroclops.local_engine.backend import _validate_job_id
from kiroclops.local_engine.execution_policy import REVISED_ID, policy_from_state
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, Session, _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_unchanged as unchanged

UNCHANGED_RUNNER_SHA256 = '47539bbe72068368bd85a2fb56201e68614908b3428810cd8496c875ba7adfd2'


def _need(condition, message, code='invalid_origin_review'):
    if not condition:
        raise VisualError(code, message)


def _files(binding):
    _need(isinstance(binding, dict) and binding, 'A nonempty source evidence binding is required.')
    for name, sha in binding.items():
        path = _ordinary(name)
        _need(path.is_file() and file_sha256(path) == sha, 'Source evidence changed: ' + str(path), 'source_changed')


def _phase_time(value):
    return type(value) in (int, float) and math.isfinite(value)


def _job(directory, item, state, expected, consumed):
    operation, job_id = item['operation'], _validate_job_id(item['job_id'])
    path = directory / 'jobs' / job_id
    job, binding, result = (read_json(path / name) for name in ('job.json', 'binding.json', 'result.json'))
    _need(job.get('job_id') == job_id and job.get('operation') == operation and job.get('status') == 'succeeded'
        and job.get('source_hashes') == expected and job.get('source_sha256') == digest(expected)
        and binding.get('sha256') == digest({key: job[key] for key in JOB_BINDING_FIELDS})
        and job.get('result_sha256') == digest(result), 'Canonical job source/result binding differs.', 'wrong_job')
    source = _ordinary(state['baseline']) if operation == 'prepare' else directory / 'candidates' / state['selected_candidate_id'] / 'source'
    _need(_ordinary(job['source_path']) == source, 'A job used an unexpected source directory.', 'wrong_job')
    for key in ('job_id', 'operation', 'status', 'candidate_id', 'source_sha256', 'created_at', 'started_at', 'finished_at', 'error'):
        _need(item.get(key) == job.get(key), 'Terminal job snapshot differs: ' + key, 'wrong_job')
    _need(item.get('result') == result and all(_phase_time(job.get(key)) for key in ('created_at', 'started_at', 'finished_at'))
        and state['started_at'] <= job['created_at'] <= job['started_at'] <= job['finished_at'],
        'Terminal job source/time/result snapshot differs.', 'wrong_job')
    deadline = job['created_at'] + state['budget']['max_validation_seconds'] if operation == 'finalize' else state['deadline_at']
    _need(job['finished_at'] <= deadline, 'A recorded job exceeded its frozen phase deadline.', 'phase_timeout')
    for name in ('job.json', 'binding.json', 'result.json'):
        consumed[str(path / name)] = file_sha256(path / name)
    return job, result


def run(directory, stage_result, origin, origin_sha256, historical_source_review, output):
    directory, stage_result, origin, historical_source_review, output = map(_ordinary,
        (directory, stage_result, origin, historical_source_review, output))
    _need(not output.exists(), 'Never overwrite an origin/source review.', 'output_exists')
    for protected in (directory, stage_result, origin, historical_source_review):
        _need(not output.is_relative_to(protected) and not protected.is_relative_to(output),
            'Keep this receipt outside the Session and input evidence.', 'unsafe_path')
    _need(file_sha256(unchanged.__file__) == UNCHANGED_RUNNER_SHA256,
        'The exact inspected unchanged runner source revision is required.', 'runner_changed')
    result = read_json(stage_result / 'result.json')
    receipt = read_json(stage_result / 'result-binding.json')
    intent = read_json(stage_result / 'intent.json')
    intent_binding = read_json(stage_result / 'intent-binding.json')
    files = source_hashes(stage_result)
    del files['result-binding.json']
    _need(receipt.get('kind') == 'NoCodeChangeStageResultReceipt' and receipt.get('schema_version') == 1
        and receipt.get('result_sha256') == digest(result) and receipt.get('output_file_hashes') == files,
        'The frozen unchanged-stage result receipt changed.', 'result_changed')
    _need(intent.get('kind') == 'NoCodeChangeStageIntent' and intent.get('schema_version') == 1
        and result.get('kind') == 'NoCodeChangeStageResult' and result.get('schema_version') == 1
        and result.get('intent_sha256') == digest(intent)
        and intent_binding.get('kind') == 'NoCodeChangeStageIntentReceipt'
        and intent_binding.get('intent_sha256') == digest(intent)
        and intent_binding.get('intent_file_sha256') == file_sha256(stage_result / 'intent.json')
        and intent_binding.get('session_intent_sha256') == file_sha256(directory / 'unchanged-stage-intent.json')
        and read_json(directory / 'unchanged-stage-intent.json') == intent
        and intent.get('runner_sha256') == UNCHANGED_RUNNER_SHA256,
        'The operator-declared unchanged stage intent changed.', 'intent_changed')
    _need(result.get('status') in ('numerical_pass_pending_reviews', 'numerical_failure')
        and result.get('error') is None and result.get('source_unchanged') is True
        and result.get('calibration_eligible') is True and result.get('final_submitted') is True
        and all(result.get(key) is False for key in ('model_started', 'new_model_generation', 'code_written',
            'automatic_repair', 'automatic_restart', 'full_behavior_acceptance'))
        and intent.get('model_started') is False and intent.get('new_model_generation') is False
        and intent.get('code_change_allowed') is False,
        'Only a completed unchanged source snapshot can receive this source-axis review.', 'incomplete_unchanged_stage')
    session = Session(directory)
    state = session._load()  # Never reconcile/spawn a job during source inspection.
    execution = policy_from_state(state)
    _need(state.get('schema_version') == 2 and execution['policy_id'] == REVISED_ID
        and execution['stage'] in ('panel', 'svg') and execution == intent.get('execution_policy')
        and state.get('status') == 'finalized' and state.get('clock_origin') == 'unchanged_initial_measurement'
        and state.get('active_job_id') is None and state.get('prepared') is True
        and state.get('session_id') == intent.get('session_id') == result.get('session_id')
        and _ordinary(intent['session_directory']) == directory,
        'The source receipt belongs to another measurement Session or execution protocol.', 'wrong_session')
    unchanged._no_native(session)
    suite = read_json(_ordinary(state['reference']) / 'suite.json')
    _need(suite.get('schema_version') == 2 and digest(suite) == state['experiment'].get('suite_sha256')
        == intent.get('suite_sha256') == result.get('suite_sha256')
        and suite.get('source_epoch') == intent.get('source_epoch')
        and state['experiment'].get('stage') == intent.get('stage') == result.get('stage') == suite.get('stage'),
        'Frozen suite/source epoch identity differs.', 'wrong_suite')
    expected = suite['authoring_parent_source_hashes']
    _need(intent.get('source_hashes') == expected and intent.get('source_sha256') == digest(expected)
        and result.get('source_sha256') == digest(expected), 'Intent measured another authoring parent.', 'wrong_source')
    unchanged._source_unchanged(session, expected)
    selected = state.get('selected_candidate_id')
    _need(selected == result.get('candidate_id') == 'candidate-01' and len(state.get('candidates', [])) == 1
        and state['candidates'][0].get('candidate_id') == selected
        and state['candidates'][0].get('status') == 'succeeded' and state['candidates'][0].get('eligible') is True
        and _phase_time(state.get('started_at')) and _phase_time(state.get('sealed_at'))
        and state['started_at'] <= state['sealed_at'] < state['deadline_at'],
        'The exact unchanged candidate was not sealed in its authoring budget.', 'wrong_candidate')
    canonical = read_json(stage_result / 'canonical-bindings.json')
    _need(canonical.get('kind') == 'NoCodeChangeCanonicalEvidence' and canonical.get('schema_version') == 1
        and canonical.get('status') == 'complete' and canonical.get('errors') == [],
        'The unchanged stage has incomplete canonical source evidence.', 'source_changed')
    _files(canonical['files'])
    origin_binding = unchanged._origin(origin, origin_sha256, suite)
    _need(intent.get('historical_origin') == origin_binding, 'The stage intent named another historical origin.', 'wrong_origin')
    initial = unchanged._initial_quality(Path(intent['initial_quality']['directory']), suite)
    _need(initial == intent['initial_quality'], 'Initial-quality evidence changed.', 'initial_quality_changed')
    historical = _ordinary(origin_binding['historical_session'])
    historical_state = Session(historical)._load()
    _need(historical_state.get('selected_candidate_id') == origin_binding['historical_candidate_id'],
        'Historical source review must bind the exact selected generation candidate.', 'wrong_origin')
    validated = review.validate_source_review(historical_source_review, historical, candidate_sha256=digest(expected))
    _need(validated.get('status') == 'pass' and validated.get('full_behavior_acceptance') is False
        and validated.get('source_hashes') == expected
        and (validated.get('session_directory') is None or _ordinary(validated['session_directory']) == historical),
        'Historical source inspection cannot transfer full behavior acceptance.', 'historical_source_not_qualified')
    native = validated['native_validation']
    _need(native.get('status') == 'pass' and native.get('model') == 'claude-opus-5.5' and native.get('effort') == 'max',
        'The recorded historical model/effort evidence is not the accepted origin.', 'historical_native_not_qualified')
    consumed = {str(path): file_sha256(path) for path in (Path(__file__), Path(unchanged.__file__), Path(review.__file__),
        origin, historical_source_review, directory / 'session.json', directory / 'binding.json',
        directory / 'unchanged-stage-intent.json', _ordinary(state['reference']) / 'suite.json')}
    consumed.update(canonical['files'])
    consumed.update(origin_binding['historical_binding_files'])
    consumed.update({str(stage_result / name): sha for name, sha in source_hashes(stage_result).items()})
    consumed.update({str(historical / name): sha for name, sha in native['evidence_hashes'].items()})
    completed = result.get('completed_jobs', [])
    _need(len(completed) == 3 and [row.get('operation') for row in completed] == ['prepare', 'evaluate', 'finalize']
        and result.get('owned_job_ids') == [row['job_id'] for row in completed],
        'Exactly the canonical three measurement jobs must be retained.', 'wrong_job')
    jobs = [_job(directory, row, state, expected, consumed) for row in completed]
    _need(jobs[0][0]['job_id'] == state.get('prepare_job_id')
        and jobs[1][0]['job_id'] == state['candidates'][0].get('job_id')
        and jobs[2][0]['job_id'] == state.get('final_job_id') == (state.get('final') or {}).get('job_id')
        and jobs[1][1].get('eligible') is True
        and result.get('numerical_pass') == (jobs[2][1].get('pass') is True)
        and (state.get('final') or {}).get('pass') == (jobs[2][1].get('pass') is True),
        'Selected candidate/calibration/final result bindings disagree.', 'wrong_job')
    # The original source review keeps its original clean baseline. The new
    # measurement Session begins from that historical candidate, so copying
    # the old review into the new Session would assert incorrect role bindings.
    launch = read_json(historical / 'kiro/launch.json')
    _files(consumed)
    unchanged._source_unchanged(session, expected)
    unchanged._no_native(session)
    source_receipt = {'kind': 'UnchangedHistoricalSourceAxisReview', 'schema_version': 1, 'status': 'pass',
        'source_axis_status': 'pass', 'source_sha256': digest(expected), 'source_hashes': deepcopy(expected),
        'new_session_directory': str(directory), 'new_session_id': state['session_id'],
        'selected_measurement_candidate_id': selected, 'final_job_id': state['final_job_id'],
        'suite_sha256': digest(suite), 'source_epoch': suite['source_epoch'],
        'unchanged_stage_result_sha256': digest(result), 'unchanged_stage_intent_sha256': digest(intent),
        'historical_generation_scope': {'historical_session': str(historical),
            'historical_candidate_id': origin_binding['historical_candidate_id'],
            'source_review': str(historical_source_review), 'source_review_sha256': file_sha256(historical_source_review),
            'source_validation_status': 'pass', 'recorded_model': native['model'], 'recorded_effort': native['effort'],
            'recorded_native_session_id': native['native_session_id'], 'recorded_cli_version': launch.get('cli_version'),
            'recorded_kas_version': launch.get('kas_version'), 'historical_native_replay_status': 'pass',
            'native_evidence_hashes': deepcopy(native['evidence_hashes']), 'historical_only': True,
            'scope': 'Read-only verification of the earlier recorded Kiro generation and its source inspection; no new model generation.'},
        'historical_clean_baseline_hashes': deepcopy(historical_state['baseline_hashes']),
        'new_measurement_baseline_hashes': deepcopy(state['baseline_hashes']),
        'new_model_generation': False, 'new_session_native_generation_audited': False,
        'model_started': False, 'app_or_capture_started': False, 'code_written': False,
        'candidate_origin': 'measurement_snapshot_of_existing_kiro_code',
        'numerical_behavior_status': 'pass' if result['numerical_pass'] else 'fail',
        'independent_raw_review_status': 'not_decided_here', 'functional_acceptance_status': 'not_decided_here',
        'full_behavior_acceptance': False,
        'scope': 'Source/authorship axis only: historical Kiro authorship plus byte-identical baseline, target, selected candidate and measured job sources. Numerical, functional and independent raw acceptance remain separate.'}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'source-axis.json', source_receipt)
    write_json(output / 'evidence-bindings.json', {'kind': 'UnchangedHistoricalSourceAxisBindings', 'schema_version': 1,
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed), 'source_axis_sha256': digest(source_receipt)})
    write_json(output / 'receipt.json', {'kind': 'UnchangedHistoricalSourceAxisReceipt', 'schema_version': 1,
        'status': 'pass', 'source_axis_sha256': digest(source_receipt), 'output_file_hashes': source_hashes(output),
        'consumed_files_sha256': digest(consumed), 'new_model_generation': False, 'full_behavior_acceptance': False})
    return source_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--stage-result', required=True, type=Path)
    parser.add_argument('--origin', required=True, type=Path)
    parser.add_argument('--origin-sha256', required=True)
    parser.add_argument('--historical-source-review', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(args.session, args.stage_result, args.origin, args.origin_sha256, args.historical_source_review, args.output)
    except (VisualError, OSError, KeyError, ValueError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'code': getattr(error, 'code', 'invalid_origin_review'), 'message': str(error)}))
        return 1
    print(json.dumps({key: result[key] for key in ('status', 'source_sha256', 'source_axis_status', 'new_model_generation', 'full_behavior_acceptance')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
