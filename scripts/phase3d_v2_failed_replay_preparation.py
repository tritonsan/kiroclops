"""Preserve a pre-Session panel preparation closed by two actual replay failures.

Only named JSON receipts are read. No PNG, capture, model or repair is run.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.phase3d_v2_failed_preparation import ROOT, need
from kiroclops.local_engine.realistic_policy import input_history_check, replay_scenario
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json

MESSAGE = 'Two real captures did not match the fixed actual reference input intervals.'


def run(failed, previous, output):
    failed, previous, output = [Path(p).resolve() for p in (failed, previous, output)]
    need(not output.exists(), 'Keep previous closure evidence unchanged.')
    need(not any(output.is_relative_to(p) or p.is_relative_to(output) for p in (failed, previous)), 'Separate output is required.')
    consumed = {}

    def read(path):
        path = Path(path).resolve()
        value = read_json(path)
        consumed[str(path)] = file_sha256(path)
        return value

    failure, progress = read(failed / 'failure.json'), read(failed / 'progress.json')
    saved, cases, starts = read(previous / 'calibration.json'), read(previous / 'recipes.json'), read(previous / 'start-binding.json')
    need(saved.get('stage') == starts.get('stage') == 'panel' and starts.get('recipes_sha256') == digest(cases),
         'Bind the exact preceding panel calibration recipes.')
    need(failure.get('code') == 'input_history_mismatch' and failure.get('message') == MESSAGE
         and failure.get('model_started') is False and failure.get('previous_attempts_preserved') is True
         and progress.get('model_started') is False, 'Expected the preserved preparation replay failure.')
    session = failed.parents[1] / 's' / failed.name
    need(not session.exists() and all(not (failed / name).exists() for name in ('suite', 'result.json', 'calibration.json')),
         'No suite or Session may already have completed.')
    private = [case for case in cases if case.get('role') != 'calibration']
    need(len(private) == 24 and len({case['case_id'] for case in private}) == 24, 'Expected all 24 immutable private recipes.')
    complete, incomplete, records, fatal = [], [], [], None
    for ordinal, case in enumerate(private, 1):
        case_id = case['case_id']
        need(isinstance(case_id, str) and Path(case_id).name == case_id and '/' not in case_id and '\\' not in case_id,
             'Unsafe case identity.')
        present, absent = [], []
        for role in ('reference', 'original', 'before'):
            path = failed / role / case_id / 'record.json'
            if path.is_file():
                record = read(path)
                need(record.get('measurement_status') == 'ok' and record.get('scenario') == case['scenario'],
                     'Retained completed record has another recipe/status.')
                present.append(role)
                records.append({'case_id': case_id, 'role': role, 'source_role': role, 'record_path': str(path),
                    'record_file_sha256': consumed[str(path.resolve())], 'record_sha256': digest(record),
                    'source_sha256': record.get('source_sha256'), 'scope': 'retained completed receipt; no new acceptance'})
            else:
                absent.append(role)
            replay_path = path.parent / 'capture-replay.json'
            if not replay_path.is_file():
                continue
            replay = read(replay_path)
            if replay.get('status') != 'input_history_mismatch':
                continue
            need(fatal is None and role == 'before' and not path.exists(), 'One unselected before-role fatal replay is required.')
            need(replay.get('selected_attempt') is None and replay.get('duration_rescaled') is False
                 and [a.get('attempt') for a in replay.get('attempts', [])] == [1, 2], 'Retain the exact two actual attempts.')
            reference = read(failed / 'reference' / case_id / 'record.json')
            execution, proof = replay_scenario(case['scenario'], reference['observations'])
            attempts = []
            for item in replay['attempts']:
                directory = path.parent / f"attempt-{item['attempt']:02d}"
                need(Path(item['directory']).resolve() == directory.resolve(), 'Attempt directory is not the exact named capture.')
                receipt = read(directory / 'capture-attempt.json')
                record = receipt['record']
                start = read(directory / 'capture-start.json')
                need(digest(receipt) == item.get('receipt_sha256') and receipt.get('attempt') == item['attempt']
                     and Path(receipt.get('directory', '')).resolve() == directory.resolve()
                     and receipt.get('status') == item.get('status') == 'input_history_mismatch', 'Attempt receipt binding differs.')
                need(record.get('scenario') == case['scenario'] and record.get('execution_scenario') == execution
                     and record.get('input_replay') == proof and start.get('scenario') == execution
                     and record.get('source_sha256') == starts.get('parent_source_sha256')
                     and digest(start.get('source_hashes')) == record['source_sha256'], 'Attempt source or actual replay recipe differs.')
                history = input_history_check(reference['observations'], record['observations'])
                need(history == receipt.get('input_history') == item.get('input_history') and history.get('pass') is False
                     and history.get('duration_rescaled') is False, 'Actual input metadata does not independently reproduce the mismatch.')
                attempts.append({'attempt': item['attempt'], 'directory': str(directory),
                    'receipt_file_sha256': consumed[str((directory / 'capture-attempt.json').resolve())],
                    'receipt_sha256': digest(receipt), 'source_sha256': record['source_sha256'],
                    'input_history': history, 'failed_events': [row for row in history['differences'] if row['pass'] is False],
                    'source_hashes': start['source_hashes'], 'adapter_hashes': start.get('adapter_hashes')})
            fatal = {'case_id': case_id, 'ordinal': ordinal, 'role': role, 'directory': str(path.parent),
                'replay_file_sha256': consumed[str(replay_path.resolve())], 'source_sha256': attempts[0]['source_sha256'],
                'attempts': attempts, 'selected_attempt': None, 'duration_rescaled': False}
        row = {'ordinal': ordinal, 'case_id': case_id, 'split_role': case['role'], 'present_roles': present, 'missing_roles': absent}
        (complete if not absent else incomplete).append(row)
    need(len(complete) == 7 and len(incomplete) == 17 and len(records) == 23 and fatal is not None and fatal['ordinal'] == 8,
         'Named record completion differs from the actual interrupted eighth private case.')
    producer = {'path': str(Path(__file__).resolve()), 'sha256': file_sha256(__file__)}
    for relative in ('scripts/phase3d_v2_failed_preparation.py', 'scripts/phase3d_v2_prepare.py',
                     'kiroclops/local_engine/suite_web_v2.py', 'kiroclops/local_engine/realistic_policy.py',
                     'kiroclops/web_local/capture.py'):
        consumed[str((ROOT / relative).resolve())] = file_sha256(ROOT / relative)
    consumed[producer['path']] = producer['sha256']
    diagnostic = {'kind': 'ActualInputReplayFailureDiagnostic', 'schema_version': 1, 'stage': 'panel',
        'case_id': fatal['case_id'], 'role': fatal['role'], 'comparison_recomputed_from_actual_input_metadata': True,
        'attempt_count': 2, 'attempts': deepcopy(fatal['attempts']),
        'tolerance_rule': {'minimum_ms': 20, 'maximum_ms': 30, 'median_interval_multiplier': 1.5,
                           'pointer_tolerance_px': 1, 'relative_and_adjacent_intervals_both_required': True},
        'assessment': 'Both observed drag_end adjacent intervals differ beyond the frozen input comparability tolerance; relative-to-first times and pointer positions match. No Kiro behavior task began.',
        'thresholds_changed': False, 'replay_rescaled': False, 'capture_started': False, 'model_started': False,
        'raw_png_decoded_or_hashed': False, 'full_behavior_acceptance': False}
    closure = {'kind': 'FailedReferenceSuitePreparationClosure', 'schema_version': 1,
        'status': 'measurement_insufficient', 'stage': 'panel', 'preparation_id': failed.name,
        'failed_preparation_directory': str(failed), 'previous_calibration_directory': str(previous),
        'previous_calibration': str(previous), 'expected_session_directory': str(session),
        'attempt_directory': fatal['directory'], 'suite_created': False, 'session_created': False,
        'failure': failure, 'failure_classification': 'two_actual_input_history_mismatches',
        'measurement_status': 'fail', 'behavior_status': 'not_evaluated', 'model_started': False,
        'native_prompt_sent': False, 'new_model_generation': False, 'candidate_budget_consumed': False,
        'admission_started': False, 'initial_quality_started': False, 'full_behavior_acceptance': False,
        'private_case_count': 24, 'completed_case_count': 7, 'missing_case_count': 17,
        'completed_role_record_count': 23, 'completed_cases': complete, 'incomplete_cases': incomplete,
        'completed_records': records, 'failed_attempt': fatal, 'producer': producer,
        'historical_acceptance_changed': False, 'automatic_replacement_started': False, 'capture_restarted': False,
        'source_bindings_sha256': digest(consumed), 'diagnostic_sha256': digest(diagnostic),
        'scope': 'Preparation input-comparability failure; existing partial captures preserved, behavior not evaluated.'}
    output.mkdir(parents=True)
    write_json(output / 'closure.json', closure)
    write_json(output / 'diagnostic.json', diagnostic)
    write_json(output / 'evidence-bindings.json', {'kind': 'FailedPreparationEvidenceBindings', 'schema_version': 1,
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'scope': 'Named record/input/source metadata only; no raw PNG or held-out behavior comparison.'})
    write_json(output / 'receipt.json', {'kind': 'FailedReferenceSuitePreparationReceipt', 'schema_version': 1,
        'status': 'measurement_insufficient', 'closure_sha256': digest(closure),
        'output_file_hashes': {name: file_sha256(output / name) for name in ('closure.json', 'diagnostic.json', 'evidence-bindings.json')},
        'consumed_files_sha256': digest(consumed), 'producer': producer, 'full_behavior_acceptance': False})
    return closure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('failed', 'previous', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        value = run(args.failed, args.previous, args.output)
    except (VisualError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}))
        return 1
    print(json.dumps({key: value[key] for key in ('status', 'stage', 'completed_case_count', 'missing_case_count', 'behavior_status')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
