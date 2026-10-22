"""Prospective SVG acceptance of an existing source, without an authoring run.

The worker's ``candidate_sha256`` is a pixel producer identity. It is not a
claim that a model produced new code. This workflow creates no Session, prompt,
candidate selection, editor hook, or model-generation receipt.
"""
from __future__ import annotations

from copy import deepcopy
import html
import json
from pathlib import Path
import time

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from . import evidence, media_suite, suite_revision, suite_web
from .session import _ordinary, copy_project, source_hashes
from .suite_evidence import _binding, _source_expectation
from .execution_policy import resolve_policy, validate_policy, budget_from_policy, REVISED_ID

VERSION = 1
INITIAL_MEASUREMENT_SECONDS = 1800
REPAIR_BUDGET = {'max_candidates': 3, 'max_seconds': 1800, 'max_validation_seconds': 1800}
NON_BEHAVIOR_FAILURES = {
    'required_order_unobservable', 'comparison_coverage_failure',
    'baseline_comparison_coverage_failure', 'comparison_measurement_failure',
    'insufficient_observability',
}


def _suite_policy(suite):
    if suite.get('schema_version') == 1:
        return resolve_policy()
    if suite.get('schema_version') == 2:
        policy = validate_policy(suite.get('execution_policy'), stage=suite.get('stage'))
        _need(policy['policy_id'] == REVISED_ID, 'A revised suite requires the revised execution policy.')
        return policy
    raise VisualError('invalid_initial_acceptance', 'Unknown reference-suite execution protocol.')


def _plan_policy(plan, suite):
    policy = _suite_policy(suite)
    revised = policy['policy_id'] == REVISED_ID
    _need(plan.get('schema_version') == (2 if revised else 1), 'Initial plan has the wrong execution protocol version.')
    if revised:
        _need(plan.get('execution_policy') == policy and plan.get('execution_policy_sha256') == digest(policy),
              'Initial measurement execution policy changed.', 'initial_plan_changed')
    return policy


def _need(value, message, code='invalid_initial_acceptance'):
    if not value:
        raise VisualError(code, message)


def _code_sources():
    paths = [Path(__file__).resolve()]
    script = media_suite.ROOT / 'scripts/phase3d_svg_initial_acceptance.py'
    if script.is_file():
        paths.append(script)
    return {str(path): file_sha256(path) for path in paths}


def _matrix(suite):
    """The approved 3D SVG protocol is exact, including repeats and inputs."""
    _need(suite.get('domain') == suite.get('stage') == 'svg', 'This workflow accepts the existing SVG source only.')
    scenario_provider = suite_web
    if suite.get('schema_version') == 2:
        from . import suite_web_v2
        scenario_provider = suite_web_v2
    expected = {(role, scenario['case_id']): scenario
        for split, role in (('calibration', 'calibration'), ('known_regression', 'regression'), ('validation', 'validation'))
        for scenario in scenario_provider.scenarios('svg', split)}
    actual = {(case['role'], case['case_id']): case for case in suite['cases']}
    _need(len(actual) == len(suite['cases']) and set(actual) == set(expected),
          'Keep all 4 calibration, 12 known regression and 12 new validation cases.', 'needs_reference')
    _need(all(case['stage'] == 'svg' and case.get('purpose', 'primary') == 'primary'
          and case['scenario'] == expected[key] and case['input_provenance'] == 'recorder_verified'
          for key, case in actual.items()), 'SVG recipes or their declared input authority differ from the approved protocol.')
    _suite_policy(suite)
    _need(suite.get('heldout_status') == 'provided'
          and (suite.get('schema_version') != 1 or suite.get('limits') == media_suite.LIMITS),
          'The frozen protocol needs its validation pool and registered thresholds.', 'needs_reference')
    return [actual[key] for key in expected]


def _historical(session, candidate_id, expected_source, expected_original):
    """Link genuine historical generation; never promote it to a new run."""
    session = _ordinary(session)
    state = read_json(session / 'session.json')
    chosen = [item for item in state.get('candidates', []) if item.get('candidate_id') == candidate_id]
    _need(state.get('domain') == 'svg' and len(chosen) == 1 and chosen[0].get('status') == 'succeeded'
          and chosen[0].get('source_sha256') == digest(expected_source),
          'Historical candidate does not bind this unchanged source.', 'wrong_historical_source')
    source = session / 'candidates' / candidate_id / 'source'
    _need(source_hashes(source) == expected_source, 'Historical candidate sources changed.', 'wrong_historical_source')
    _need(state.get('baseline_hashes') == expected_original and source_hashes(session / 'baseline') == expected_original,
          'The suite original must remain the historical clean original comparison source.', 'wrong_historical_original')
    files = {str(session / 'session.json'): file_sha256(session / 'session.json')}
    # Optional completed generation files remain historical links, not a new
    # producer proof. The explicit session/source identity is always required.
    job_id = chosen[0].get('job_id')
    if isinstance(job_id, str) and job_id and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in job_id):
        for name in ('job.json', 'result.json'):
            path = session / 'jobs' / job_id / name
            if path.is_file():
                files[str(path)] = file_sha256(path)
    return {'kind': 'HistoricalExistingCandidateGeneration', 'historical_session': str(session),
        'historical_candidate_id': candidate_id, 'historical_job_id': job_id,
        'source_sha256': digest(expected_source), 'original_source_sha256': digest(expected_original),
        'files': files, 'new_model_run': False,
        'scope': 'Historical generation evidence only; no new model identity or prompt is asserted.'}


def register_initial_acceptance(suite_directory, authoring_parent, output, *,
                                historical_session, historical_candidate_id,
                                max_measurement_seconds=None):
    """Freeze the unchanged source and exact matrix before any new capture."""
    suite_directory, authoring_parent, output = map(_ordinary, (suite_directory, authoring_parent, output))
    _need(not output.exists(), 'Retain every previous initial acceptance attempt.', 'output_exists')
    _need(not output.is_relative_to(suite_directory) and not output.is_relative_to(authoring_parent)
          and not suite_directory.is_relative_to(output) and not authoring_parent.is_relative_to(output),
          'Acceptance evidence must be outside source and frozen suite trees.', 'unsafe_path')
    suite = media_suite.load_suite(suite_directory)
    policy = _suite_policy(suite)
    if max_measurement_seconds is None:
        max_measurement_seconds = policy['max_initial_measurement_seconds']
    _need(type(max_measurement_seconds) is int and 1 <= max_measurement_seconds <= policy['max_initial_measurement_seconds'],
          'Initial measurement has its own frozen deadline; it cannot enlarge the authoring budget.')
    cases = _matrix(suite)
    parent_hashes = source_hashes(authoring_parent)
    _need(parent_hashes == suite['authoring_parent_source_hashes'],
          'The existing SVG source must be the exact frozen authoring parent.', 'wrong_authoring_parent')
    _need(isinstance(historical_candidate_id, str) and media_suite._name(historical_candidate_id),
          'An explicit historical candidate identity is required.')
    generation = _historical(historical_session, historical_candidate_id, parent_hashes, suite['original_source_hashes'])
    source = output / 'unchanged-source'
    copy_project(authoring_parent, source)
    plan = {'kind': 'NoCodeChangeInitialAcceptancePlan', 'schema_version': 2 if policy['policy_id'] == REVISED_ID else 1, 'created_at': time.time(),
        'domain': 'svg', 'stage': 'svg', 'suite': str(suite_directory), 'suite_sha256': digest(suite),
        'authoring_parent': str(authoring_parent), 'authoring_parent_source_hashes': parent_hashes,
        'unchanged_source': str(source), 'unchanged_source_sha256': digest(parent_hashes),
        'original_source_hashes': suite['original_source_hashes'],
        'cases': [{key: deepcopy(case[key]) for key in ('case_id', 'role', 'stage', 'purpose', 'scenario')} for case in cases],
        'limits': deepcopy(suite['limits']), 'event_contract': deepcopy(suite['event_contract']),
        'measurement_source_hashes': suite_revision.measurement_source_hashes('svg'),
        'orchestration_source_hashes': media_suite.analysis_source_hashes(), 'workflow_source_hashes': _code_sources(),
        'historical_generation': generation, 'max_measurement_seconds': max_measurement_seconds,
        'model_started': False, 'new_model_generation': False, 'authoring_started': False,
        'source_changes_allowed': False, 'controller_changes_allowed': False,
        'baseline_roles': {'improvement': 'original_comparison_target', 'preservation': 'exact_existing_authoring_parent'},
        'repair_prompt': suite['goal'], 'repair_prompt_sha256': digest(suite['goal']),
        'prompt_binding_scope': 'registered_goal_and_calibration_case_allowlist',
        'actual_repair_prompt_verified': False,
        'repair_rule': 'Only a complete source-bound behavior failure can preregister a separate repair.'}
    if policy['policy_id'] == REVISED_ID:
        plan.update(execution_policy=policy, execution_policy_sha256=digest(policy))
    write_json(output / 'initial-plan.json', plan)
    write_json(output / 'initial-plan-binding.json', {'plan_sha256': digest(plan)})
    load_plan(output)
    return plan


def load_plan(output, *, verify_code=True):
    output = _ordinary(output)
    plan = read_json(output / 'initial-plan.json')
    _need(plan.get('kind') == 'NoCodeChangeInitialAcceptancePlan' and plan.get('schema_version') in (1, 2)
          and read_json(output / 'initial-plan-binding.json').get('plan_sha256') == digest(plan),
          'Initial acceptance registration changed.', 'initial_plan_changed')
    suite = media_suite.load_suite(plan['suite'], verify_analysis_code=verify_code)
    policy = _plan_policy(plan, suite)
    cases = _matrix(suite)
    _need(digest(suite) == plan['suite_sha256'] and plan['limits'] == suite['limits']
          and plan['event_contract'] == suite['event_contract']
          and plan['cases'] == [{key: deepcopy(case[key]) for key in ('case_id', 'role', 'stage', 'purpose', 'scenario')} for case in cases],
          'Registered suite or cases changed.', 'initial_plan_changed')
    _need(_ordinary(plan['unchanged_source']) == output / 'unchanged-source'
          and source_hashes(plan['unchanged_source']) == source_hashes(plan['authoring_parent'])
          == plan['authoring_parent_source_hashes'] == suite['authoring_parent_source_hashes']
          and plan['unchanged_source_sha256'] == digest(plan['authoring_parent_source_hashes'])
          and plan['original_source_hashes'] == suite['original_source_hashes'],
          'Initial acceptance source is no longer the exact parent.', 'source_changed')
    generation = plan['historical_generation']
    _need(_historical(generation['historical_session'], generation['historical_candidate_id'],
                      plan['authoring_parent_source_hashes'], plan['original_source_hashes']) == generation,
          'Historical link changed.', 'historical_evidence_changed')
    _need(plan.get('model_started') is False and plan.get('new_model_generation') is False
          and plan.get('authoring_started') is False and plan.get('source_changes_allowed') is False
          and plan.get('controller_changes_allowed') is False,
          'Initial acceptance cannot contain an authoring or model-generation claim.')
    _need(type(plan.get('max_measurement_seconds')) is int and 1 <= plan['max_measurement_seconds'] <= policy['max_initial_measurement_seconds']
          and plan.get('repair_prompt') == suite['goal'] and plan.get('repair_prompt_sha256') == digest(suite['goal']),
          'Registered deadline or calibration-only goal changed.', 'initial_plan_changed')
    if verify_code:
        _need(plan['measurement_source_hashes'] == suite_revision.measurement_source_hashes('svg')
              and plan['orchestration_source_hashes'] == media_suite.analysis_source_hashes()
              and plan['workflow_source_hashes'] == _code_sources(),
              'Frozen measurement or orchestration changed.', 'analysis_source_changed')
    return plan, suite


def _qualified(record, case, suite, expected_source=None, *, sources):
    _need(record.get('scenario') == case['scenario'], 'Exact acceptance recipe changed.', 'suite_recipe_mismatch')
    _need(record.get('measurement_status') == 'ok', 'Required pixels do not qualify.', 'tracking_failure')
    if expected_source is not None:
        _need(record.get('source_sha256') == digest(expected_source['sources']),
              'Pixel source differs from its immutable producer.', 'source_changed')
    package = load_package(record['package'], verify='hashes')
    _binding(record, case, suite, expected_source=expected_source,
             reference_package=package['package_sha256'] if expected_source is None else None)
    suite_revision.verify_measurement_record(record, 'svg', expected_sources=sources)
    return record


def _measurement_ok(row):
    if row.get('measurement_pass') is not True or row.get('baseline_measurement_pass') is not True:
        return False
    if row.get('baseline_coverage_failures') or row.get('failure_class') in NON_BEHAVIOR_FAILURES:
        return False
    geometry = row.get('svg_geometry_preservation')
    if isinstance(geometry, dict) and geometry.get('measurement_status') != 'ok':
        return False
    return row.get('required_order_status') not in ('insufficient_observability', 'unobservable')


def _persist_result(output, result):
    write_json(output / 'result.json', result)
    title = 'SVG unchanged-source initial acceptance'
    rows = ''.join('<tr><td>'+html.escape(role)+'</td><td>'+str(group['passed'])+'/'+str(group['required'])+'</td></tr>'
                   for role, group in result.get('groups', {}).items())
    document = '<!doctype html><meta charset="utf-8"><title>'+title+'</title><h1>'+title+'</h1>'
    document += '<p>'+html.escape(result['status'])+'</p><p>No new code or model generation. Historical generation stays historical.</p>'
    document += '<table><thead><tr><th>Role</th><th>Passed / required</th></tr></thead><tbody>'+rows+'</tbody></table>'
    document += '<p>Independent raw-pixel and source review: pending. Full behavior acceptance: false.</p>'
    document += '<p><a href="result.json">Bound numerical result</a> · <a href="initial-plan.json">Frozen registration</a></p>'
    (output / 'report.html').write_text(document, encoding='utf-8')
    # Preserve partial/failing records too. A retry requires another registration.
    files = source_hashes(output)
    files.pop('result-binding.json', None)
    write_json(output / 'result-binding.json', {'kind': 'NoCodeChangeInitialAcceptanceReceipt',
        'schema_version': result['schema_version'], 'result_sha256': digest(result), 'file_hashes': files,
        'model_started': False, 'new_model_generation': False})
    return result


def run_initial_acceptance(output, *, check=lambda: None, progress=lambda *args: None, cancel=None):
    """Measure each frozen recipe unchanged. The caller owns the capture lease.

    This does not export validation to an editor, submit a model job, or change
    a Session budget. Native capture and functional checks keep their existing
    clocks/controllers. Existing baseline caches retain their original roles.
    """
    output = _ordinary(output)
    _need(not (output / 'run-start.json').exists() and not (output / 'result.json').exists(),
          'An initial result is immutable; register a distinct attempt.', 'initial_already_run')
    plan, suite = load_plan(output)
    started = time.time()
    with (output / 'run-start.json').open('x', encoding='utf-8') as stream:
        json.dump({'kind': 'NoCodeChangeInitialAcceptanceStart', 'plan_sha256': digest(plan),
            'started_at': started, 'deadline_at': started + plan['max_measurement_seconds'], 'model_started': False}, stream)
    cases = _matrix(suite)
    behavior_options = ({'acceptance_policy': suite['acceptance_policy']}
                        if suite.get('acceptance_policy') is not None else {})
    indexes = {role: [] for role in ('reference', 'original', 'before', 'unchanged')}
    comparisons, preservation = [], []
    functional = None
    exposure_log = {'kind': 'InitialOperatorMeasurementActions', 'schema_version': plan['schema_version'],
        'plan_sha256': digest(plan), 'workflow_actions': [], 'model_started_by_workflow': False,
        'media_exported_by_workflow': False, 'session_created_by_workflow': False,
        'scope': 'Actions of this workflow only; independent exposure attestation is required before private validation reuse.'}
    write_json(output / 'exposure-log.json', exposure_log)

    def guard():
        check()
        if cancel and cancel():
            raise VisualError('job_cancelled', 'Unchanged-source acceptance was cancelled.')
        if time.time() > started + plan['max_measurement_seconds']:
            raise VisualError('initial_measurement_timeout', 'Initial measurement reached its separate deadline.')
        _need(source_hashes(plan['authoring_parent']) == source_hashes(plan['unchanged_source'])
              == plan['authoring_parent_source_hashes'], 'Sources changed during acceptance.', 'source_changed')

    def save(role, record, case):
        record = {**record, 'case_id': case['case_id'], 'acceptance_source_role': role,
                  'new_model_generation': False}
        indexes[role].append(record)
        write_json(output / 'indexes' / (role + '.json'), indexes[role])
        exposure_log['workflow_actions'].append({'action': 'private_measurement_retained', 'case_id': case['case_id'],
            'role': role, 'destination': str(_ordinary(record['package'])), 'exported_to_model': False})
        write_json(output / 'exposure-log.json', exposure_log)
        return record

    try:
        original = _ordinary(plan['suite']) / suite['original']
        projects = {'original': original, 'before': _ordinary(plan['authoring_parent']),
                    'unchanged': _ordinary(plan['unchanged_source'])}
        expectations = {role: _source_expectation(project, suite['original_source_hashes'] if role == 'original'
                       else plan['authoring_parent_source_hashes'], 'svg') for role, project in projects.items()}
        cached = {(item['role'], item['case_id']): item for item in
                  suite.get('initial_records', []) + suite.get('baseline_records', [])}
        _need(len(cached) == len(suite.get('initial_records', [])) + len(suite.get('baseline_records', [])),
              'Duplicate fixed baseline role/case.', 'wrong_baseline')
        for index, case in enumerate(cases):
            guard()
            progress('unchanged SVG ' + case['case_id'], index, len(cases))
            reference = media_suite.record_from_suite(plan['suite'], case)
            _qualified(reference, case, suite, sources=plan['measurement_source_hashes'])
            reference = save('reference', media_suite.clone_record(reference, output / 'records/reference' / case['case_id']), case)
            rows = {}
            for role in ('original', 'before', 'unchanged'):
                guard()
                destination = output / 'records' / role / case['case_id']
                if role != 'unchanged' and (role, case['case_id']) in cached:
                    record = media_suite.record_from_suite(plan['suite'], cached[role, case['case_id']])
                    _qualified(record, case, suite, expectations[role], sources=plan['measurement_source_hashes'])
                    record = media_suite.clone_record(record, destination)
                    record['initial_acceptance_capture_basis'] = 'retained_frozen_baseline'
                else:
                    captured_at = time.time()
                    transaction = {'case_id': case['case_id'], 'role': role, 'capture_started_at': captured_at,
                        'source_sha256': digest(expectations[role]['sources']), 'scenario_sha256': digest(case['scenario']),
                        'measurement_source_hashes': plan['measurement_source_hashes'], 'new_model_generation': False}
                    write_json(output / 'capture-bindings' / role / (case['case_id'] + '.json'), transaction)
                    # "candidate" is the native pixel producer role, never a
                    # newly authored/model-selected candidate in this workflow.
                    record = media_suite._capture(projects[role], case, destination, guard, progress, cancel,
                                                   'candidate' if role == 'unchanged' else 'before', reference,
                                                   **behavior_options)
                    guard()
                    record = suite_revision.stamp_new_measurement(record, 'svg',
                        expected_sources=plan['measurement_source_hashes'], capture_started_at=captured_at)
                    record['capture_transaction'] = transaction
                    record['initial_acceptance_capture_basis'] = 'fresh_unchanged_source' if role == 'unchanged' else 'fresh_fixed_baseline'
                _qualified(record, case, suite, expectations[role], sources=plan['measurement_source_hashes'])
                write_json(destination / 'record.json', record)
                rows[role] = save(role, record, case)
            result = media_suite._compare(reference, rows['original'], rows['unchanged'], 'svg', require_improvement=True,
                                          **behavior_options)
            prior = media_suite._parent_acceptance(reference, rows['original'], rows['before'], 'svg', **behavior_options)
            preserved = media_suite._compare(rows['before'], rows['before'], rows['unchanged'], 'svg', require_improvement=False,
                                             **behavior_options)
            preserved = {**preserved, 'case_id': case['case_id'], 'role': case['role'],
                         'accepted_parent_reference_gates': prior['pass'], 'parent_reference_result': prior,
                         'preservation_required': True}
            comparisons.append({**result, 'case_id': case['case_id'], 'role': case['role']})
            preservation.append(preserved)
            write_json(output / 'comparisons.json', comparisons)
            write_json(output / 'preservation.json', preservation)
        guard()
        functional = media_suite._checks(plan['unchanged_source'], plan['authoring_parent'], 'svg',
                                         output / 'functional.json', guard, cancel)
        guard()
        load_plan(output)
        measurement_ok = all(_measurement_ok(row) for row in comparisons + preservation)
        functional_qualified = isinstance(functional, dict) and functional.get('status') in ('pass', 'fail') \
            and isinstance(functional.get('checks'), list) and bool(functional['checks']) \
            and functional.get('source_sha256') == digest(expectations['unchanged']['sources'])
        numerical_pass = measurement_ok and functional_qualified and functional['status'] == 'pass' \
            and all(row['pass'] for row in comparisons + preservation)
        behavior_failure = measurement_ok and functional_qualified and not numerical_pass
        status = 'numerical_pass_pending_review' if numerical_pass else 'behavior_fail_pending_review' if behavior_failure else 'measurement_failed'
        groups = {role: {'required': sum(case['role'] == role for case in cases),
            'passed': sum(row['role'] == role and row['pass'] for row in comparisons),
            'pass': all(row['pass'] for row in comparisons if row['role'] == role)} for role in media_suite.ROLES}
        result = {'kind': 'NoCodeChangeInitialAcceptance', 'schema_version': plan['schema_version'], 'status': status,
            'complete': True, 'plan_sha256': digest(plan), 'suite_sha256': plan['suite_sha256'],
            'source_sha256': plan['unchanged_source_sha256'], 'source_unchanged': True,
            'model_started': False, 'new_model_generation': False, 'authoring_started': False,
            'historical_generation': deepcopy(plan['historical_generation']), 'groups': groups,
            'comparisons': comparisons, 'parent_preservation': preservation, 'functional': functional,
            'measurement_qualified': measurement_ok and functional_qualified,
            'numerical_pass': bool(numerical_pass), 'behavior_failure_observed': bool(behavior_failure),
            'repair_preregistration_allowed': bool(behavior_failure), 'heldout_status': suite['heldout_status'],
            'independent_review_status': 'pending', 'source_review_status': 'pending',
            'full_behavior_acceptance': False, 'numerical_acceptance_only': True,
            'started_at': started, 'finished_at': time.time(), 'candidate_count': 0,
            'authoring_budget_consumed': False, 'previous_results_preserved': True}
    except (VisualError, OSError, KeyError, TypeError, ValueError, KeyboardInterrupt) as error:
        result = {'kind': 'NoCodeChangeInitialAcceptance', 'schema_version': plan['schema_version'], 'status': 'measurement_failed',
            'complete': False, 'plan_sha256': digest(plan), 'suite_sha256': plan['suite_sha256'],
            'source_sha256': plan['unchanged_source_sha256'], 'source_unchanged': False if getattr(error, 'code', '') == 'source_changed' else None,
            'code': 'job_cancelled' if isinstance(error, KeyboardInterrupt) else getattr(error, 'code', 'initial_acceptance_failed'),
            'message': 'Initial measurement interrupted.' if isinstance(error, KeyboardInterrupt) else str(error),
            'comparisons': comparisons, 'parent_preservation': preservation, 'functional': functional,
            'measurement_qualified': False, 'numerical_pass': False, 'behavior_failure_observed': False,
            'repair_preregistration_allowed': False, 'full_behavior_acceptance': False,
            'model_started': False, 'new_model_generation': False, 'authoring_started': False,
            'historical_generation': deepcopy(plan['historical_generation']), 'candidate_count': 0,
            'authoring_budget_consumed': False, 'previous_results_preserved': True,
            'started_at': started, 'finished_at': time.time(), 'independent_review_status': 'pending', 'source_review_status': 'pending'}
    if plan['schema_version'] == 2:
        result.update(execution_policy=deepcopy(plan['execution_policy']),
            acceptance_profile=suite['acceptance_profile'],
            acceptance_policy_sha256=(suite.get('acceptance_policy') or {}).get('policy_sha256'),
            source_epoch=suite['source_epoch'])
    return _persist_result(output, result)


def load_result(output):
    """Verify the durable initial result without granting review or acceptance."""
    output = _ordinary(output)
    plan, suite = load_plan(output)
    result, receipt = read_json(output / 'result.json'), read_json(output / 'result-binding.json')
    files = source_hashes(output)
    files.pop('result-binding.json', None)
    _need(receipt.get('kind') == 'NoCodeChangeInitialAcceptanceReceipt' and receipt.get('schema_version') == plan['schema_version']
          and receipt.get('result_sha256') == digest(result) and receipt.get('file_hashes') == files
          and result.get('kind') == 'NoCodeChangeInitialAcceptance' and result.get('schema_version') == plan['schema_version']
          and result.get('plan_sha256') == digest(plan) and result.get('suite_sha256') == digest(suite)
          and result.get('source_sha256') == plan['unchanged_source_sha256']
          and result.get('model_started') is False and result.get('new_model_generation') is False
          and result.get('authoring_started') is False and result.get('candidate_count') == 0
          and result.get('full_behavior_acceptance') is False,
          'The initial acceptance receipt or its evidence changed.', 'initial_result_changed')
    if result.get('complete') is True:
        cases = {case['case_id']: case for case in _matrix(suite)}
        for role in ('reference', 'original', 'before', 'unchanged'):
            rows = read_json(output / 'indexes' / (role + '.json'))
            _need(isinstance(rows, list) and len(rows) == len(cases)
                  and {row.get('case_id') for row in rows} == set(cases),
                  'A complete initial result needs every exact recording role/case.', 'initial_result_changed')
            for record in rows:
                case = cases[record['case_id']]
                expected_sha = case.get('source_sha256') if role == 'reference' else digest(
                    plan['original_source_hashes'] if role == 'original' else plan['authoring_parent_source_hashes'])
                _need(record.get('scenario') == case['scenario'] and record.get('acceptance_source_role') == role
                      and record.get('measurement_status') == 'ok' and record.get('source_sha256') == expected_sha
                      and record.get('new_model_generation') is False,
                      'A retained role changed recipe, source or generation authority.', 'initial_result_changed')
        for field in ('comparisons', 'parent_preservation'):
            rows = result.get(field, [])
            _need(len(rows) == len(cases) and {row.get('case_id') for row in rows} == set(cases)
                  and all(row.get('role') == cases[row['case_id']]['role'] for row in rows),
                  'Initial comparisons omit or replace a required case.', 'initial_result_changed')
    return result


def required_observation_bindings(output):
    """Exact raw-review matrix, including fresh unchanged pixels and parents."""
    output = _ordinary(output)
    result = load_result(output)
    _need(result.get('complete') is True, 'Incomplete acceptance cannot establish a full review matrix.', 'incomplete_review')
    bindings = []
    for role in ('reference', 'original', 'before', 'unchanged'):
        for record in read_json(output / 'indexes' / (role + '.json')):
            observed, events = evidence.observations(record), evidence.event_set(record)
            bindings.append({'case_id': record['case_id'], 'role': role,
                'package_sha256': load_package(record['package'], verify='hashes')['package_sha256'],
                'observations_sha256': digest(observed), 'events_sha256': digest(events),
                'source_sha256': record.get('source_sha256')})
    return bindings


def attest_validation_exposure(initial_output, output, *, reviewer, inspected_logs,
                               private_evidence, model_exposure, validation_exported,
                               repair_prompt, exposed_case_ids):
    """Bind an actual operator's inspection; never infer external non-exposure.

    This is an explicit local attestation, not a signature or an automatic
    review. The repair prompt must stay unchanged and only calibration cases
    may be designated for model exposure.
    """
    initial_output, output = map(_ordinary, (initial_output, output))
    result = load_result(initial_output)
    plan, suite = load_plan(initial_output)
    _need(not output.exists() and not output.is_relative_to(initial_output),
          'Exposure attestations live separately from the immutable initial result.', 'output_exists')
    _need(not output.is_relative_to(_ordinary(plan['authoring_parent']))
          and not output.is_relative_to(_ordinary(plan['suite'])),
          'An exposure attestation cannot change an application or frozen reference tree.', 'unsafe_path')
    _need(isinstance(reviewer, str) and bool(reviewer.strip()), 'An actual reviewer identity is required.')
    _need(all(type(value) is bool for value in (private_evidence, model_exposure, validation_exported)),
          'Exposure authority must be explicitly declared.')
    required_logs = {'initial-plan.json', 'run-start.json', 'exposure-log.json', 'result.json', 'result-binding.json'}
    _need(isinstance(inspected_logs, list) and len(inspected_logs) == len(set(inspected_logs))
          and set(inspected_logs) == required_logs, 'Inspect every bound registration, action and result log.')
    calibration = {case['case_id'] for case in suite['cases'] if case['role'] == 'calibration'}
    _need(isinstance(exposed_case_ids, list) and len(exposed_case_ids) == len(set(exposed_case_ids))
          and all(isinstance(case, str) for case in exposed_case_ids), 'Declare the actual planned model-visible case identities.')
    _need(repair_prompt == plan['repair_prompt'] and digest(repair_prompt) == plan['repair_prompt_sha256'],
          'The repair prompt remains the preregistered calibration-only goal.', 'repair_prompt_changed')
    actions = read_json(initial_output / 'exposure-log.json')
    _need(actions.get('plan_sha256') == digest(plan) and actions.get('model_started_by_workflow') is False
          and actions.get('session_created_by_workflow') is False and actions.get('media_exported_by_workflow') is False
          and all(item.get('exported_to_model') is False
              and _ordinary(item['destination']).is_relative_to(initial_output) for item in actions['workflow_actions']),
          'Initial workflow actions contain a model start or evidence export.', 'validation_exposure_unknown')
    eligible = private_evidence and not model_exposure and not validation_exported and set(exposed_case_ids) == calibration
    attestation = {'kind': 'InitialSVGValidationExposureAttestation', 'schema_version': plan['schema_version'],
        'reviewer': reviewer.strip(), 'created_at': time.time(), 'initial_result_sha256': digest(result),
        'initial_receipt_sha256': file_sha256(initial_output / 'result-binding.json'),
        'inspected_logs': sorted(inspected_logs),
        'log_hashes': {name: file_sha256(initial_output / name) for name in sorted(required_logs)},
        'private_evidence': private_evidence, 'validation_previously_scored': True,
        'model_exposure': model_exposure, 'validation_exported': validation_exported,
        'repair_prompt': repair_prompt, 'repair_prompt_sha256': digest(repair_prompt),
        'exposed_case_ids': sorted(exposed_case_ids), 'calibration_only_prompt': set(exposed_case_ids) == calibration,
        'prompt_binding_scope': 'registered_goal_and_calibration_case_allowlist', 'actual_repair_prompt_verified': False,
        'private_pool_reuse_allowed': eligible,
        'scope': 'Explicit reviewer assertion about external exposure; workflow logs alone cannot prove it.'}
    write_json(output, attestation)
    return attestation


def _exposure(attestation_path, initial_output, result, plan, suite):
    if attestation_path is None:
        return None
    attestation_path = _ordinary(attestation_path)
    value = read_json(attestation_path)
    required_logs = {'initial-plan.json', 'run-start.json', 'exposure-log.json', 'result.json', 'result-binding.json'}
    calibration = {case['case_id'] for case in suite['cases'] if case['role'] == 'calibration'}
    _need(value.get('kind') == 'InitialSVGValidationExposureAttestation' and value.get('schema_version') == plan['schema_version']
          and isinstance(value.get('reviewer'), str) and bool(value['reviewer'].strip())
          and value.get('initial_result_sha256') == digest(result)
          and value.get('initial_receipt_sha256') == file_sha256(initial_output / 'result-binding.json')
          and set(value.get('inspected_logs', [])) == required_logs
          and value.get('log_hashes') == {name: file_sha256(initial_output / name) for name in sorted(required_logs)}
          and value.get('repair_prompt') == plan['repair_prompt']
          and value.get('repair_prompt_sha256') == plan['repair_prompt_sha256'],
          'Exposure attestation is incomplete or belongs to another initial result.', 'validation_exposure_unknown')
    eligible = value.get('private_evidence') is True and value.get('model_exposure') is False \
        and value.get('validation_exported') is False and set(value.get('exposed_case_ids', [])) == calibration
    _need(value.get('private_pool_reuse_allowed') is eligible and value.get('calibration_only_prompt') is (set(value.get('exposed_case_ids', [])) == calibration),
          'Exposure attestation contradicts its source facts.', 'validation_exposure_unknown')
    return {'path': str(attestation_path), 'sha256': file_sha256(attestation_path), 'private_pool_reuse_allowed': eligible,
            'model_exposure': value['model_exposure'], 'validation_exported': value['validation_exported'],
            'reviewer': value['reviewer'], 'validation_previously_scored': True}


def _recipe_identity(scenario):
    # Case labels/repeats cannot make previously evaluated inputs unseen again.
    return digest({key: scenario.get(key) for key in ('domain', 'viewport', 'events', 'duration_s',
        'initial_node_count', 'expected_final_node_count', 'expected_selected_id')})


def preregister_repair(initial_output, output, *, repair_suite_directory=None, authoring_parent=None,
                      exposure_attestation=None):
    """Retain the failure and freeze a separate repair declaration; no model.

    Operator-scored validation can remain blind to the repair model only with
    an explicit private-exposure attestation and unchanged calibration-only
    prompt. Never claim it has never been scored by the project. If a model
    saw validation, a different prospective pool is required.
    """
    initial_output, output = map(_ordinary, (initial_output, output))
    result = load_result(initial_output)
    plan, suite = load_plan(initial_output)
    policy = _plan_policy(plan, suite)
    _need(result.get('complete') is True and result.get('measurement_qualified') is True
          and result.get('source_unchanged') is True and result.get('behavior_failure_observed') is True
          and result.get('repair_preregistration_allowed') is True and result.get('numerical_pass') is False,
          'Only a completed behavior failure permits a separate repair.', 'repair_not_permitted')
    _need(not output.exists(), 'Separate repair declarations never overwrite initial or previous results.', 'output_exists')
    _need(not output.is_relative_to(initial_output) and not initial_output.is_relative_to(output)
          and not output.is_relative_to(_ordinary(plan['authoring_parent']))
          and not output.is_relative_to(_ordinary(plan['suite'])), 'Use a separate repair evidence tree.', 'unsafe_path')
    if authoring_parent is None:
        authoring_parent = plan['authoring_parent']  # Explicitly frozen in the first registration.
    authoring_parent = _ordinary(authoring_parent)
    _need(source_hashes(authoring_parent) == plan['authoring_parent_source_hashes'],
          'Repair must begin from the exact unchanged existing source.', 'wrong_authoring_parent')
    seen = {_recipe_identity(case['scenario']) for case in suite['cases']}
    exposure = _exposure(exposure_attestation, initial_output, result, plan, suite)
    reuse = exposure is not None and exposure['private_pool_reuse_allowed']
    registered_suite = None
    missing = ['Provide an inspected private-exposure attestation or a separate prospective validation pool.']
    if repair_suite_directory is None and reuse:
        repair_suite_directory = plan['suite']
    if repair_suite_directory is not None:
        repair_suite_directory = _ordinary(repair_suite_directory)
        registered_suite = media_suite.load_suite(repair_suite_directory)
        _need(registered_suite['domain'] == registered_suite['stage'] == 'svg'
              and registered_suite['authoring_parent_source_hashes'] == plan['authoring_parent_source_hashes']
              and registered_suite['original_source_hashes'] == plan['original_source_hashes']
              and registered_suite['limits'] == plan['limits'] and registered_suite['event_contract'] == plan['event_contract']
              and _suite_policy(registered_suite) == policy,
              'Repair must keep parent, original comparison, event policy and thresholds.', 'wrong_repair_suite')
        fresh = [case for case in registered_suite['cases'] if case['role'] == 'validation']
        _need(fresh and all(case.get('input_provenance') == 'recorder_verified'
              and (reuse or _recipe_identity(case['scenario']) not in seen) for case in fresh),
              'Previously model-exposed inputs cannot become unseen through new labels.', 'validation_already_seen')
        prior_cal = {_recipe_identity(case['scenario']) for case in suite['cases'] if case['role'] == 'calibration'}
        new_cal = {_recipe_identity(case['scenario']) for case in registered_suite['cases'] if case['role'] == 'calibration'}
        _need(prior_cal <= new_cal, 'Repair retains both initial calibration behaviors.', 'needs_reference')
        missing = []
    declaration = {'kind': 'SeparateSVGRepairPreregistration', 'schema_version': plan['schema_version'], 'created_at': time.time(),
        'initial_result': str(initial_output), 'initial_result_sha256': digest(result),
        'initial_receipt_sha256': file_sha256(initial_output / 'result-binding.json'),
        'initial_plan_sha256': digest(plan), 'failure_retained': True,
        'authoring_parent': str(authoring_parent), 'authoring_parent_source_hashes': plan['authoring_parent_source_hashes'],
        'original_source_hashes': plan['original_source_hashes'], 'budget': budget_from_policy(policy),
        'limits': deepcopy(plan['limits']), 'event_contract': deepcopy(plan['event_contract']),
        'repair_suite': str(repair_suite_directory) if repair_suite_directory is not None else None,
        'repair_suite_sha256': digest(registered_suite) if registered_suite is not None else None,
        'reference_status': 'provided' if registered_suite is not None else 'needs_reference',
        'heldout_status': 'provided' if registered_suite is not None else 'not_provided',
        'missing_references': missing, 'validation_previously_scored': True,
        'validation_previously_scored_by': 'operator', 'validation_blind_to_repair_model': True if reuse else None,
        'model_exposure': False if reuse else exposure['model_exposure'] if exposure else None,
        'validation_exposure_attestation': exposure,
        'repair_prompt': plan['repair_prompt'], 'repair_prompt_sha256': plan['repair_prompt_sha256'],
        'prompt_binding_scope': 'registered_goal_and_calibration_case_allowlist', 'actual_repair_prompt_verified': False,
        'model_visible_case_ids': [case['case_id'] for case in suite['cases'] if case['role'] == 'calibration'],
        'raw_admission_status': 'pending', 'execution_ready': False, 'model_started': False,
        'new_model_generation': False, 'source_changes_applied': False,
        'scope': 'Declaration only. A real new Session, independent admission and actual Kiro run remain separate.'}
    if policy['policy_id'] == REVISED_ID:
        declaration.update(execution_policy=policy, execution_policy_sha256=digest(policy))
    write_json(output / 'repair-plan.json', declaration)
    write_json(output / 'repair-plan-binding.json', {'repair_plan_sha256': digest(declaration)})
    return declaration
