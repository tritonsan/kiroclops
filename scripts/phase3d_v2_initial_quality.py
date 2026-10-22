"""Score existing authoring-parent pixels without opening private validation.

Only calibration and known-regression records are read. No model, browser,
capture, Session submission or new candidate is created by this command.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import media_suite, realistic_policy, suite_revision, suite_web_v2
from kiroclops.local_engine.execution_policy import validate_policy as validate_execution_policy, REVISED_ID
from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.local_engine.suite_evidence import _binding, _source_expectation
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

ROLES = ('calibration', 'regression')
NON_BEHAVIOR_FAILURES = {'input_history_mismatch', 'required_order_unobservable',
    'comparison_measurement_failure', 'comparison_coverage_failure', 'baseline_comparison_coverage_failure',
    'insufficient_observability'}


def _need(condition, message, code='invalid_initial_quality'):
    if not condition:
        raise VisualError(code, message)


def _manifest(directory, parent, consumed):
    # Do not call load_suite: its full evidence verification opens held-out
    # packages. Bind the manifest now and verify only the selected subtrees.
    path, binding_path = directory / 'suite.json', directory / 'suite-binding.json'
    value, binding = read_json(path), read_json(binding_path)
    _need(value.get('kind') == 'ReferenceMediaSuite' and value.get('schema_version') == 2,
          'This prospective initial check accepts ReferenceMediaSuite v2 only.', 'unsupported_schema')
    _need(binding.get('suite_sha256') == digest(value) and value.get('calibration_frozen') is True,
          'Frozen suite manifest changed.', 'suite_changed')
    stage = value.get('stage')
    _need(stage in ('panel', 'svg') and value.get('domain') == ('web_panel' if stage == 'panel' else 'svg'),
          'Initial browser-quality checks accept panel and SVG only.', 'unsupported_domain')
    execution = validate_execution_policy(value.get('execution_policy'), stage=stage)
    _need(execution['policy_id'] == REVISED_ID, 'The revised execution policy is required.')
    policy = realistic_policy.validate_policy(value.get('acceptance_policy'), domain=stage)
    limits = {**media_suite.LIMITS, 'required_improvement_fraction': None, 'web_curve_error_fraction': .05}
    _need(value.get('limits') == limits and value.get('acceptance_profile') == policy['id'],
          'Frozen realistic quality profile differs.', 'invalid_acceptance_policy')
    expected_code = media_suite.analysis_source_hashes(2)
    _need(value.get('analysis_source_hashes') == expected_code and value.get('source_epoch') == digest(expected_code),
          'Frozen analysis source epoch differs.', 'analysis_source_changed')
    parent_sources = source_hashes(parent)
    _need(parent_sources == value.get('authoring_parent_source_hashes'),
          'Use the exact frozen existing authoring parent.', 'wrong_authoring_parent')
    _need(value.get('original') == 'comparison-original', 'Original comparison source has the wrong suite path.', 'wrong_original')
    original = safe_path(directory, value['original'], must_exist=False)
    _need(original.is_dir(), 'Original comparison source directory is missing.', 'wrong_original')
    original_sources = source_hashes(original)
    _need(original_sources == value.get('original_source_hashes'), 'Original comparison target changed.', 'wrong_original')
    for root, hashes in ((parent, parent_sources), (original, original_sources)):
        consumed.update({str(root / relative): sha for relative, sha in hashes.items()})
    consumed.update({str(path): file_sha256(path), str(binding_path): file_sha256(binding_path)})
    root = Path(media_suite.ROOT)
    consumed.update({str(root / relative): sha for relative, sha in expected_code.items()})
    cases = value.get('cases')
    _need(isinstance(cases, list) and all(isinstance(case, dict) for case in cases), 'Case metadata is malformed.')
    selected = [case for case in cases if case.get('role') in ROLES]
    _need(selected and {case['role'] for case in selected} == set(ROLES), 'Both calibration and known regression are required.', 'needs_reference')
    ids = [case.get('case_id') for case in selected]
    _need(len(ids) == len(set(ids)) and all(isinstance(name, str) and media_suite._name(name) == name for name in ids),
          'Selected case identifiers repeat or are unsafe.')
    _need(all(case.get('stage') == stage and case.get('purpose') == 'primary' for case in selected),
          'Initial quality cases must be primary cases of the selected stage.')
    expected_cases = {(scenario['case_id'], role): scenario
        for split, role in (('calibration', 'calibration'), ('known_regression', 'regression'))
        for scenario in suite_web_v2.scenarios(stage, split)}
    _need(len(selected) == len(expected_cases) and {(case['case_id'], case['role']) for case in selected} == set(expected_cases)
          and all(case['scenario'] == expected_cases[case['case_id'], case['role']] for case in selected),
          'Keep every exact calibration and known-regression recipe.', 'needs_reference')
    required = [(item.get('case_id'), item.get('role')) for item in value.get('required_cases', []) if item.get('role') in ROLES]
    _need(len(required) == len(selected) and set(required) == {(case['case_id'], case['role']) for case in selected},
          'Selected mandatory cases are incomplete.', 'needs_reference')
    return value, selected, original


def _selected_record(directory, item, case, suite, role, prefix, expected_source, sources, consumed):
    subtree = safe_path(directory, prefix, must_exist=False)
    _need(subtree.is_dir(), 'Selected role/case directory is missing.', 'needs_reference')
    raw = item.get('record')
    _need(isinstance(raw, dict), 'Selected measurement record is missing.', 'needs_reference')
    for key in ('package', 'analysis', 'observations', 'events', 'selection'):
        value = raw.get(key)
        if isinstance(value, str):
            path = safe_path(directory, value, must_exist=key != 'package')
            _need(path.is_relative_to(subtree), 'Selected record points outside its exact role/case subtree.', 'wrong_record_role')
    actual = source_hashes(subtree)
    expected = {name[len(prefix) + 1:]: sha for name, sha in suite.get('file_hashes', {}).items()
                if name.startswith(prefix + '/')}
    _need(actual and actual == expected, 'Selected role/case files changed.', 'suite_changed')
    consumed.update({str(subtree / relative): sha for relative, sha in actual.items()})
    record = media_suite.record_from_suite(directory, item)
    _need(record.get('scenario') == case['scenario'] and record.get('measurement_status') == 'ok',
          'Selected recipe or measurement status differs.', 'measurement_not_ready')
    expected_sha = case.get('source_sha256') if role == 'reference' else digest(expected_source['sources'])
    _need(record.get('source_sha256') == expected_sha, 'Selected pixels came from a different source.', 'wrong_record_source')
    package = load_package(record['package'], verify='hashes')
    if role == 'reference':
        _need(package['package_sha256'] == case.get('package_sha256')
              and digest(package['inputs']) == case.get('timeline_sha256')
              and digest(record.get('selection')) == case.get('selection_sha256')
              and package['inputs']['provenance'] == case.get('input_provenance'),
              'Reference pixel, input or selection binding changed.', 'suite_changed')
    _binding(record, case, suite, expected_source=expected_source,
        reference_package=package['package_sha256'] if role == 'reference' else None)
    suite_revision.verify_measurement_record(record, suite['stage'], expected_sources=sources)
    return record


def run(suite_directory, authoring_parent, output):
    suite_directory, authoring_parent, output = map(_ordinary, (suite_directory, authoring_parent, output))
    _need(not output.exists(), 'Never overwrite any prior initial-quality result.', 'output_exists')
    _need(not output.is_relative_to(suite_directory) and not suite_directory.is_relative_to(output)
          and not output.is_relative_to(authoring_parent) and not authoring_parent.is_relative_to(output),
          'Output must be separate from the suite and application.', 'unsafe_path')
    consumed = {str(Path(__file__).resolve()): file_sha256(__file__)}
    suite, cases, original = _manifest(suite_directory, authoring_parent, consumed)
    started = time.time()
    plan = {'kind': 'ExistingParentInitialQualityPlan', 'schema_version': 1, 'suite_sha256': digest(suite),
        'stage': suite['stage'], 'authoring_parent_source_sha256': digest(suite['authoring_parent_source_hashes']),
        'original_source_sha256': digest(suite['original_source_hashes']), 'acceptance_profile': suite['acceptance_profile'],
        'acceptance_policy_sha256': digest(suite['acceptance_policy']), 'execution_policy': deepcopy(suite['execution_policy']),
        'source_epoch': suite['source_epoch'], 'selected_cases': [{key: deepcopy(case[key]) for key in
            ('case_id', 'role', 'stage', 'purpose', 'scenario')} for case in cases],
        'validation_opened': False, 'new_candidate_created': False, 'candidate_budget_consumed': False,
        'new_model_generation': False, 'model_started': False, 'app_or_capture_started': False}
    write_json(output / 'plan.json', plan)
    sources = suite_revision.measurement_source_hashes(suite['stage'])
    parent_expectation = _source_expectation(authoring_parent, suite['authoring_parent_source_hashes'], suite['domain'])
    original_expectation = _source_expectation(original, suite['original_source_hashes'], suite['domain'])
    comparisons = []
    try:
        for case in cases:
            name = case['case_id']
            pool = suite.get('initial_records', []) if case['role'] == 'calibration' else suite.get('baseline_records', [])
            originals = [item for item in pool if item.get('role') == 'original' and item.get('case_id') == name]
            parents = [item for item in pool if item.get('role') == 'before' and item.get('case_id') == name]
            _need(len(originals) == len(parents) == 1, 'Selected original/authoring-parent pixels are missing or duplicated.', 'needs_reference')
            prefix = 'initial' if case['role'] == 'calibration' else 'fixed-baselines'
            reference = _selected_record(suite_directory, case, case, suite, 'reference', 'clips/' + name,
                                        None, sources, consumed)
            baseline = _selected_record(suite_directory, originals[0], case, suite, 'original', prefix + '/original/' + name,
                                       original_expectation, sources, consumed)
            parent = _selected_record(suite_directory, parents[0], case, suite, 'before', prefix + '/before/' + name,
                                     parent_expectation, sources, consumed)
            compared = media_suite._compare(reference, baseline, parent, suite['stage'], require_improvement=False,
                                            acceptance_policy=suite['acceptance_policy'])
            parent_quality = media_suite._parent_acceptance(reference, baseline, parent, suite['stage'],
                                                           acceptance_policy=suite['acceptance_policy'])
            _need(compared == parent_quality, 'Existing-parent quality helper disagrees with direct comparison.', 'comparison_inconsistent')
            row = {'case_id': name, 'role': case['role'], 'comparison': compared,
                'source_sha256': parent['source_sha256'], 'producer_role': 'existing_authoring_parent',
                'functional_check_basis': 'retained_capture_oracle_only', 'recorded_functional': deepcopy(parent.get('functional')),
                'new_functional_check_run': False, 'new_model_generation': False}
            comparisons.append(row)
            write_json(output / 'comparisons' / (name + '.json'), row)
        _need(all(file_sha256(path) == sha for path, sha in consumed.items()),
              'Source or selected evidence changed during comparison.', 'source_changed')
        ready = all(row['comparison'].get('measurement_pass') is True
            and row['comparison'].get('baseline_measurement_pass') is True
            and row['comparison'].get('failure_class') not in NON_BEHAVIOR_FAILURES for row in comparisons)
        qualified = ready and all(isinstance(row['recorded_functional'], dict)
            and row['recorded_functional'].get('status') in ('pass', 'fail') for row in comparisons)
        passed = qualified and all(row['comparison'].get('pass') is True for row in comparisons)
        status = 'sufficient_initial_quality' if passed else 'requires_repair' if qualified else 'initial_evidence_incomplete'
        failure = None
    except (VisualError, OSError, KeyError, ValueError, TypeError) as error:
        qualified = passed = False
        status = 'initial_evidence_incomplete'
        failure = {'code': getattr(error, 'code', 'initial_quality_failed'), 'message': str(error)}
    result = {'kind': 'ExistingParentInitialQuality', 'schema_version': 1, 'status': status,
        'plan_sha256': digest(plan), 'suite_sha256': digest(suite), 'source_epoch': suite['source_epoch'],
        'acceptance_profile': suite['acceptance_profile'], 'authoring_parent_source_sha256': plan['authoring_parent_source_sha256'],
        'complete': len(comparisons) == len(cases) and failure is None, 'measurement_and_source_qualified': qualified,
        'initial_quality_pass': passed, 'repair_required': status == 'requires_repair',
        'groups': {role: {'required': sum(case['role'] == role for case in cases),
                         'compared': sum(row['role'] == role for row in comparisons),
                         'passed': sum(row['role'] == role and row['comparison'].get('pass') is True for row in comparisons)} for role in ROLES},
        'comparisons': comparisons, 'error': failure, 'started_at': started, 'finished_at': time.time(),
        'validation_opened': False, 'model_started': False, 'new_model_generation': False,
        'new_candidate_created': False, 'candidate_budget_consumed': False, 'app_or_capture_started': False,
        'new_functional_checks_run': False, 'full_behavior_acceptance': False,
        'scope': 'Existing-source pixel quality on calibration and known regressions only. Retained capture oracles are reused; final functional, private validation and independent raw/source reviews remain required.'}
    write_json(output / 'source-bindings.json', {'consumed_files': consumed, 'validation_files_opened': [],
        'scope': 'Manifest metadata may describe private validation; its records/packages were not opened.'})
    write_json(output / 'result.json', result)
    files = source_hashes(output)
    write_json(output / 'result-binding.json', {'kind': 'ExistingParentInitialQualityReceipt', 'schema_version': 1,
        'result_sha256': digest(result), 'output_file_hashes': files, 'input_bindings_sha256': digest(consumed)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', required=True, type=Path)
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(args.suite, args.parent, args.output)
    except (VisualError, OSError, KeyError, ValueError, TypeError) as error:
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'initial_quality_failed'), 'message': str(error)}))
        return 1
    print(json.dumps({key: result[key] for key in ('status', 'complete', 'groups', 'model_started', 'validation_opened')}))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
