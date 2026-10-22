"""Fail a revised measurement stage closed after two actual replay mismatches.

The inference is read-only and uses only jobs already owned by this Session.
It does not restart a model, create candidates, change budgets or accept behavior.
"""
from __future__ import annotations

from pathlib import Path

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path
from .backend import _validate_job_id
from .execution_policy import REVISED_ID, policy_from_state
from .session import JOB_BINDING_FIELDS, _ordinary

CLOSED_OPERATIONS = frozenset({'prepare', 'evaluate', 'finalize'})


def _need(condition, message):
    if not condition:
        raise VisualError('stage_closure_evidence_invalid', message)


def _bound_json(path, files):
    path = _ordinary(path)
    value = read_json(path)
    files[str(path)] = file_sha256(path)
    return value


def _owned_jobs(state):
    jobs = []
    for operation, key in (('prepare', 'prepare_job_id'), ('finalize', 'final_job_id')):
        if state.get(key):
            jobs.append((operation, _validate_job_id(state[key]), None))
    for candidate in state.get('candidates', []):
        jobs.append(('evaluate', _validate_job_id(candidate['job_id']), candidate))
    _need(len({row[1] for row in jobs}) == len(jobs), 'A Session job identity is duplicated.')
    return jobs


def _reference(suite_directory, case, suite, files):
    record = case['record']
    observed = record.get('observations')
    if isinstance(observed, str):
        path = safe_path(suite_directory, observed)
        _need(file_sha256(path) == suite['file_hashes'].get(observed), 'Frozen reference observations changed.')
        observed = _bound_json(path, files)
    _need(isinstance(observed, dict) and observed.get('package_sha256') == case.get('package_sha256'),
          'Reference observations belong to another package.')
    _need(digest(observed.get('inputs')) == case.get('timeline_sha256'), 'Reference actual input history changed.')
    return observed


def _verify_capture(root, case, role, expected_source, reference, files):
    from .realistic_policy import input_history_check, replay_scenario

    proof = _bound_json(root / 'capture-replay.json', files)
    attempts = proof.get('attempts')
    _need(proof.get('status') == 'input_history_mismatch' and proof.get('selected_attempt') is None
          and proof.get('duration_rescaled') is False and isinstance(attempts, list) and len(attempts) == 2,
          'Fatal closure requires exactly two unselected, unscaled real mismatches.')
    expected_execution, expected_replay = replay_scenario(case['scenario'], reference)
    outputs = []
    for index, item in enumerate(attempts, 1):
        directory = _ordinary(root / f'attempt-{index:02d}')
        _need(item.get('attempt') == index and _ordinary(item['directory']) == directory
              and item.get('status') == 'input_history_mismatch', 'Capture attempts are reordered or belong to another root.')
        receipt = _bound_json(directory / 'capture-attempt.json', files)
        _need(receipt.get('attempt') == index and _ordinary(receipt['directory']) == directory
              and receipt.get('status') == 'input_history_mismatch' and item.get('receipt_sha256') == digest(receipt),
              'Actual mismatch receipt identity changed.')
        record = receipt.get('record') or {}
        observed = record.get('observations')
        _need(isinstance(observed, dict) and record.get('scenario') == case['scenario']
              and record.get('execution_scenario') == expected_execution and record.get('input_replay') == expected_replay
              and record.get('source_sha256') == expected_source,
              'Actual capture does not bind the fixed recipe, replay or source.')
        package_path = _ordinary(record['package'])
        _need(package_path.is_relative_to(directory), 'Actual capture package escapes its owned attempt.')
        package = load_package(package_path, verify='hashes')
        files[str(package_path / 'manifest.json')] = file_sha256(package_path / 'manifest.json')
        for frame in package['frames']:
            files[str(safe_path(package_path, frame['path']))] = frame['sha256']
        _need(observed.get('package_sha256') == package['package_sha256'] and observed.get('inputs') == package['inputs'],
              'Mismatch input history is not bound to its recorded pixels.')
        history = input_history_check(reference, observed)
        _need(history.get('status') == 'input_history_mismatch' and history.get('pass') is False
              and receipt.get('input_history') == history and item.get('input_history') == history,
              'Both actual histories must independently reproduce the mismatch.')
        outputs.append({'attempt': index, 'directory': str(directory), 'receipt_sha256': digest(receipt),
            'package_sha256': package['package_sha256'], 'input_history_sha256': digest(history)})
    return {'case_id': case['case_id'], 'role': role, 'attempts': outputs,
            'selected_attempt': None, 'duration_rescaled': False}


def _fatal_proof(directory, state, operation, job, candidate):
    files = {}
    job_directory = directory / 'jobs' / job['job_id']
    current = _bound_json(job_directory / 'job.json', files)
    binding = _bound_json(job_directory / 'binding.json', files)
    _need(current == job and binding.get('sha256') == digest({key: job[key] for key in JOB_BINDING_FIELDS}),
          'Fatal job scope/source binding changed.')
    _need(job.get('operation') == operation and job.get('status') == 'failed'
          and job.get('error', {}).get('code') == 'input_history_mismatch'
          and job.get('source_sha256') == digest(job.get('source_hashes')),
          'Expected a failed input-history job with its exact source binding.')
    if candidate is not None:
        _need(job.get('candidate_id') == candidate.get('candidate_id')
              and candidate.get('source_sha256') == job.get('source_sha256'), 'Fatal job belongs to another candidate.')
    suite_directory = _ordinary(state['reference'])
    suite = _bound_json(suite_directory / 'suite.json', files)
    suite_binding = _bound_json(suite_directory / 'suite-binding.json', files)
    _need(suite.get('kind') == 'ReferenceMediaSuite' and suite.get('schema_version') == 2
          and suite.get('stage') == state['experiment']['stage']
          and digest(suite) == state['experiment'].get('suite_sha256') == suite_binding.get('suite_sha256')
          and suite.get('execution_policy') == state['experiment'].get('execution_policy'), 'Fatal capture suite changed.')
    roles = ('calibration',) if operation != 'finalize' else ('regression', 'validation')
    fatal = []
    for case in suite['cases']:
        if case['role'] not in roles:
            continue
        for role in ('original', 'before', 'candidate'):
            root = directory / 'measurements' / job['job_id'] / role / case['case_id']
            path = root / 'capture-replay.json'
            if not path.is_file() or read_json(path).get('status') != 'input_history_mismatch':
                continue
            _need(operation != 'prepare' or role != 'candidate', 'Preparation cannot contain a candidate capture.')
            _need(operation != 'evaluate' or role == 'candidate', 'Evaluation must use frozen baseline records.')
            reference = _reference(suite_directory, case, suite, files)
            expected = job['source_sha256'] if role == 'candidate' else digest(
                suite['original_source_hashes'] if role == 'original' else suite['authoring_parent_source_hashes'])
            fatal.append(_verify_capture(root, case, role, expected, reference, files))
    _need(len(fatal) == 1, 'A terminal replay error needs exactly one fatal two-attempt capture proof.')
    _need(all(file_sha256(name) == sha for name, sha in files.items()), 'Fatal evidence changed during verification.')
    return {'capture': fatal[0], 'evidence_file_hashes': files, 'evidence_sha256': digest(files),
            'suite_sha256': digest(suite), 'source_epoch': suite.get('source_epoch')}


def inspect_closure(directory, state, *, current_job_id=None):
    """Return an honest closure; malformed fatal evidence also blocks new work.

    Legacy and non-browser policies are unchanged. No global history scan, new
    capture, result publication, state mutation or candidate allocation occurs.
    """
    if state.get('schema_version') != 2:
        return None
    policy = policy_from_state(state)
    experiment = state.get('experiment', {})
    if policy['policy_id'] != REVISED_ID or experiment.get('reference_mode') != 'media_suite' or policy['stage'] not in ('panel', 'svg'):
        return None
    directory = _ordinary(directory)
    for operation, job_id, candidate in _owned_jobs(state):
        if job_id == current_job_id:
            continue
        path = directory / 'jobs' / job_id / 'job.json'
        job = read_json(_ordinary(path))
        if job.get('status') != 'failed' or (job.get('error') or {}).get('code') != 'input_history_mismatch':
            continue
        closure = {'kind': 'StageMeasurementClosure', 'schema_version': 1, 'status': 'measurement_insufficient',
            'stage': policy['stage'], 'fatal_job_id': job_id, 'fatal_operation': operation,
            'fatal_candidate_id': job.get('candidate_id'), 'source_sha256': job.get('source_sha256'),
            'reason': 'input_history_mismatch_after_two_captures', 'evidence_status': 'verified',
            'closed_operations': sorted(CLOSED_OPERATIONS), 'available_operations': ['inspect', 'get_job', 'cancel_job', 'report'],
            'new_capture_allowed': False, 'new_candidate_allowed': False, 'automatic_restart': False,
            'behavior_failure_inferred': False, 'full_behavior_acceptance': False}
        try:
            _need(job.get('job_id') == job_id, 'Fatal job identity differs from this Session.')
            closure.update(_fatal_proof(directory, state, operation, job, candidate))
        except (VisualError, OSError, KeyError, TypeError, ValueError) as error:
            closure.update(reason='fatal_input_history_evidence_incomplete', evidence_status='invalid',
                error={'code': getattr(error, 'code', 'stage_closure_evidence_invalid'), 'message': str(error)})
        return closure
    return None


def guard_operation(directory, state, operation):
    if operation not in CLOSED_OPERATIONS:
        return None
    closure = inspect_closure(directory, state)
    if closure is not None:
        raise VisualError('stage_measurement_insufficient',
            'This measurement stage is closed after fatal input-history failure in job '
            + closure['fatal_job_id'] + '; keep its evidence and do not create a repair/replacement trial. '
            + 'Closure evidence status: ' + closure['evidence_status'] + '.')
    return None


def guard_execution(directory, state, job):
    if job.get('operation') == 'report':
        return None
    closure = inspect_closure(directory, state, current_job_id=job['job_id'])
    if closure is not None:
        raise VisualError('stage_measurement_insufficient',
            'A prior fatal input-history measurement closed this stage; no queued capture may start. '
            + 'Fatal job: ' + closure['fatal_job_id'] + '. Evidence status: ' + closure['evidence_status'] + '.')
    return None
