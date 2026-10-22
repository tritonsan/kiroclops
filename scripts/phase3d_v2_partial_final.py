"""Read-only numerical diagnosis of the failed, sealed v2 jump final job.

This is not a final result, replay, repair, new trial or behavior acceptance.
The canonical failed job and all old evidence stay unchanged. Only a fresh
outside directory receives a diagnosis of its already completed pixel records.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import statistics
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import media_suite, suite_evidence
from kiroclops.local_engine.execution_policy import policy_from_state, REVISED_ID
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, _ordinary, source_hashes
from kiroclops.local_engine.source_inventory import full_byte_inventory
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

ROLES = ('reference', 'original', 'before', 'final')
ERROR = {'code': 'execution_error', 'message': "'function' object has no attribute 'is_set'"}


def _need(value, message, code='invalid_partial_final'):
    if not value:
        raise VisualError(code, message)


def _terminal(state, job, binding, result_exists):
    """Accept only the actual failed callback seam, never a succeeded job."""
    _need(state.get('schema_version') == 2 and state.get('domain') == 'game'
          and state.get('experiment', {}).get('reference_mode') == 'media_suite'
          and state.get('experiment', {}).get('stage') == 'jump',
          'This diagnostic accepts the revised jump suite only.', 'unsupported_domain')
    policy = policy_from_state(state)
    _need(policy['policy_id'] == REVISED_ID, 'The exact revised execution policy is required.')
    chosen = [c for c in state.get('candidates', [])
              if c.get('candidate_id') == state.get('selected_candidate_id')]
    _need(len(chosen) == 1 and chosen[0].get('status') == 'succeeded'
          and chosen[0].get('eligible') is True, 'An eligible, sealed source must exist.', 'unsealed_source')
    chosen = chosen[0]
    _need(job.get('job_id') == state.get('final_job_id') and job.get('operation') == 'finalize'
          and job.get('status') == 'failed' and job.get('error') == ERROR
          and job.get('result_applied') is True and not result_exists
          and state.get('status') == 'finalization_failed' and state.get('active_job_id') is None
          and state.get('final', {}).get('status') == 'failed'
          and state.get('final', {}).get('pass') is False
          and state.get('final', {}).get('job_id') == job.get('job_id')
          and state.get('final', {}).get('error') == ERROR,
          'Retain the original failed job without a replacement result.', 'wrong_terminal_job')
    _need(job.get('candidate_id') == chosen['candidate_id']
          and job.get('source_sha256') == chosen.get('source_sha256')
          and digest(job.get('source_hashes')) == chosen.get('source_sha256')
          and binding.get('sha256') == digest({key: job.get(key) for key in JOB_BINDING_FIELDS}),
          'Selected source or job identity changed.', 'wrong_candidate')
    timestamps = [job.get(k) for k in ('created_at', 'started_at', 'finished_at')]
    _need(all(type(v) in (int, float) for v in (*timestamps, state.get('sealed_at'), state.get('deadline_at')))
          and timestamps[0] <= state['sealed_at'] < state['deadline_at']
          and timestamps[0] <= timestamps[1] <= timestamps[2],
          'The final source was not sealed within the authoring deadline.', 'invalid_chronology')
    return chosen, policy


def _inventory(root, consumed):
    root = _ordinary(root)
    hashes = full_byte_inventory(root, ignored=(), excluded_suffixes=())
    consumed.update({str(root / name): sha for name, sha in hashes.items()})
    return hashes


def _matrix(cases, records):
    """Use precisely the same numerical and parent-preservation rules as the worker."""
    comparisons, preservation = [], []
    for case in cases:
        name = case['case_id']
        ref, original, prior, final = (records[role][name] for role in ROLES)
        result = media_suite._compare(ref, original, final, case['stage'],
                                     require_improvement=case['purpose'] == 'primary', acceptance_policy=None)
        result.update(case_id=name, scenario_id=name, role=case['role'], purpose=case['purpose'])
        parent = media_suite._compare(prior, prior, final, case['stage'],
                                     require_improvement=False, acceptance_policy=None)
        parent.update(case_id=name, role=case['role'], purpose='authoring_parent_preservation')
        prior_quality = media_suite._parent_acceptance(ref, original, prior, case['stage'], acceptance_policy=None)
        parent['parent_reference_acceptance'] = prior_quality
        parent['previously_accepted_case'] = prior_quality.get('pass') is True
        parent['retained_reference_behavior'] = not parent['previously_accepted_case'] or result.get('pass') is True
        comparisons.append(result)
        preservation.append(parent)
    return comparisons, preservation


def run(session_directory, output):
    directory, output = map(_ordinary, (session_directory, output))
    _need(not output.exists(), 'Preserve every previous diagnostic.', 'output_exists')
    _need(not output.is_relative_to(directory) and not directory.is_relative_to(output),
          'Diagnostic output must be outside the original session.', 'unsafe_path')
    started = time.time()
    state_path = directory / 'session.json'
    state = read_json(state_path)
    job_id = state.get('final_job_id')
    _need(isinstance(job_id, str) and media_suite._name(job_id) == job_id, 'Final job identity is missing.')
    job_root = directory / 'jobs' / job_id
    job, binding = read_json(job_root / 'job.json'), read_json(job_root / 'binding.json')
    chosen, policy = _terminal(state, job, binding, (job_root / 'result.json').exists())
    consumed = {str(Path(__file__).resolve()): file_sha256(__file__),
                str(state_path): file_sha256(state_path),
                str(job_root / 'job.json'): file_sha256(job_root / 'job.json'),
                str(job_root / 'binding.json'): file_sha256(job_root / 'binding.json')}
    suite_root = _ordinary(state['reference'])
    _need(not output.is_relative_to(suite_root) and not suite_root.is_relative_to(output),
          'Output must also be outside the frozen suite.', 'unsafe_path')
    suite = media_suite.load_suite(suite_root)
    _need(suite.get('schema_version') == 2 and suite.get('domain') == 'game' and suite.get('stage') == 'jump'
          and suite.get('acceptance_profile') == 'legacy-game-v1' and suite.get('acceptance_policy') is None
          and suite.get('execution_policy') == policy
          and digest(suite) == state['experiment'].get('suite_sha256')
          and state['baseline_hashes'] == suite['authoring_parent_source_hashes'],
          'Frozen suite/source/policy bindings differ.', 'suite_changed')
    consumed.update({str(suite_root / name): sha for name, sha in source_hashes(suite_root).items()})
    consumed.update({str(media_suite.ROOT / name): sha for name, sha in suite['analysis_source_hashes'].items()})
    selected_source = directory / 'candidates' / chosen['candidate_id'] / 'source'
    _need(_ordinary(job['source_path']) == selected_source and source_hashes(selected_source) == job['source_hashes'],
          'Final source path/bytes differ from the sealed candidate.', 'wrong_candidate')
    projects = {
        'original': (safe_path(suite_root, suite['original'], must_exist=False), suite['original_source_hashes']),
        'before': (_ordinary(state['baseline']), state['baseline_hashes']),
        'final': (selected_source, job['source_hashes']),
    }
    expectations = {}
    for role, (project, sources) in projects.items():
        expectations[role] = suite_evidence._source_expectation(project, sources, 'game')
        consumed.update({str(project / name): sha for name, sha in sources.items()})
    cases = [case for case in suite['cases'] if case['role'] in ('regression', 'validation')]
    from kiroclops.local_engine import suite_game
    expected = {s['case_id']: s for split in ('regression', 'validation') for s in suite_game.scenarios('jump', split)}
    _need(len(cases) == 24 and len(expected) == 24 and {c['case_id'] for c in cases} == set(expected)
          and all(c['stage'] == 'jump' and c['purpose'] == 'primary' and c['scenario'] == expected[c['case_id']] for c in cases),
          'The exact 12 known plus 12 new jump cases are required.', 'wrong_matrix')
    measurement_root = directory / 'measurements' / job_id
    _need(not any((measurement_root / name).exists() for name in
                  ('comparison.json', 'parent-preservation.json', 'visual-boundary.json', 'normal-level')),
          'The recorded failure point differs or later outputs appeared.', 'failure_point_changed')
    records, pixel_bindings, record_bindings = {}, set(), []
    for role in ROLES:
        index = measurement_root / (role + '.json')
        rows = suite_evidence._records(index, cases, complete=True)
        _need([case['case_id'] for _, case in rows] == [case['case_id'] for case in cases],
              'Saved role order differs from the original worker.', 'wrong_matrix')
        consumed[str(index)] = file_sha256(index)
        records[role] = {}
        for record, case in rows:
            name = case['case_id']
            suite_evidence._origin(directory, job_id, role, record, case)
            package = load_package(record['package'], verify='hashes')
            if role == 'reference':
                frozen = media_suite.record_from_suite(suite_root, case)
                expected_binding = suite_evidence._binding(frozen, case, suite, reference_package=case['package_sha256'])
                actual_binding = suite_evidence._binding(record, case, suite, reference_package=case['package_sha256'])
                _need(actual_binding == expected_binding and record.get('source_sha256') == case.get('source_sha256')
                      and record.get('raw_run') == frozen.get('raw_run'),
                      'Copied reference differs from the immutable suite.', 'wrong_record_source')
            else:
                actual_binding = suite_evidence._binding(record, case, suite, expected_source=expectations[role],
                    expected_job_id=job_id if role == 'final' else None)
                expected_sha = digest(projects[role][1])
                _need(record.get('source_sha256') == record.get('candidate_source_sha256') == expected_sha,
                      'Saved role pixels came from another source.', 'wrong_record_source')
            # Include immutable packages, analysis/worker receipts and real raw
            # process/input/frame files in the append-only diagnostic receipt.
            record_root = measurement_root / ('candidate' if role == 'final' else role) / name
            _inventory(record_root, consumed)
            _inventory(record['raw_run'], consumed)
            pixel_bindings.add(actual_binding)
            record_bindings.append({'case_id': name, 'role': role, 'record_sha256': digest(record),
                'package_sha256': package['package_sha256'], 'observation_binding': list(actual_binding),
                'source_sha256': record.get('source_sha256')})
            records[role][name] = record
    comparisons, preservation = _matrix(cases, records)
    functional_path = measurement_root / 'functional/comparison.json'
    functional = read_json(functional_path)
    _inventory(measurement_root / 'functional', consumed)
    groups = {role: {'passed': sum(c.get('pass') is True for c in comparisons if c['role'] == role),
                     'required': sum(c['role'] == role for c in comparisons)} for role in ('regression', 'validation')}
    scores = [c['score'] for c in comparisons]
    result = {'kind': 'FailedFinalNumericalDiagnostic', 'schema_version': 1,
        'status': 'partial_numerical_diagnostic', 'started_at': started, 'finished_at': time.time(),
        'session': str(directory), 'session_id': state['session_id'], 'stage': 'jump',
        'original_job_id': job_id, 'original_job_status': 'failed', 'original_job_error': deepcopy(ERROR),
        'failure_classification': 'tool_callback_interface_failure', 'candidate_id': chosen['candidate_id'],
        'source_sha256': chosen['source_sha256'], 'suite_sha256': digest(suite),
        'source_epoch': suite['source_epoch'], 'execution_policy': policy,
        'groups': groups, 'required': 24, 'passed': sum(c.get('pass') is True for c in comparisons),
        'mean_curve_error_body': statistics.mean(scores), 'minimum_curve_error_body': min(scores),
        'maximum_curve_error_body': max(scores),
        'numerical_status': 'pass' if all(c.get('pass') is True for c in comparisons) else 'fail',
        'retained_parent_behavior': all(c['retained_reference_behavior'] is True for c in preservation),
        'recorded_functional_status': functional.get('status'), 'new_functional_check_run': False,
        'validated_role_records': len(record_bindings), 'unique_pixel_bindings': len(pixel_bindings),
        'visual_boundary_status': 'not_evaluated', 'normal_level_status': 'not_evaluated',
        'independent_visual_review_status': 'pending', 'source_review_status': 'pending',
        'full_behavior_acceptance': False, 'canonical_result_created': False,
        'original_job_rewritten': False, 'new_model_generation': False, 'new_capture_started': False,
        'failed_trial_replaced': False,
        'scope': 'Existing sealed failed-job pixels only. Numerical observations do not complete missing boundary, normal-level or independent acceptance gates.'}
    _need(all(file_sha256(path) == sha for path, sha in consumed.items()),
          'Consumed evidence/source changed during diagnosis.', 'evidence_changed')
    output.mkdir(parents=True)
    write_json(output / 'diagnostic.json', result)
    write_json(output / 'comparisons.json', comparisons)
    write_json(output / 'parent-preservation.json', preservation)
    write_json(output / 'record-bindings.json', record_bindings)
    bindings = {'kind': 'FailedFinalDiagnosticBindings', 'schema_version': 1,
                'consumed_files': consumed, 'consumed_files_sha256': digest(consumed)}
    write_json(output / 'evidence-bindings.json', bindings)
    outputs = {name: file_sha256(output / name) for name in
               ('diagnostic.json', 'comparisons.json', 'parent-preservation.json', 'record-bindings.json', 'evidence-bindings.json')}
    receipt = {'kind': 'FailedFinalNumericalDiagnosticReceipt', 'schema_version': 1,
        'status': 'partial_numerical_diagnostic', 'producer': {'path': str(Path(__file__).resolve()), 'sha256': file_sha256(__file__)},
        'original_job_status': 'failed', 'diagnostic_sha256': outputs['diagnostic.json'],
        'output_file_hashes': outputs, 'consumed_files_sha256': digest(consumed), 'full_behavior_acceptance': False}
    write_json(output / 'receipt.json', receipt)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = run(args.session, args.output)
    print({k: value[k] for k in ('status', 'groups', 'mean_curve_error_body', 'numerical_status', 'original_job_status', 'full_behavior_acceptance')})


if __name__ == '__main__':
    main()
