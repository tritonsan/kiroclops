"""Append a metadata-only closure for an interrupted v2 SVG private preparation.

No image capture, model, measurement revision, admission or Session is run.
Completed record receipts are counted, not promoted into behavior acceptance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = 'Visible counter does not belong to this image observation interval'


def need(value, message):
    if not value:
        raise VisualError('invalid_failed_preparation', message)


def run(failed, previous, output, *, diagnostic=None):
    failed, previous, output = [Path(p).resolve() for p in (failed, previous, output)]
    need(not output.exists(), 'Preserve all previous closure outputs.')
    need(not any(output.is_relative_to(p) or p.is_relative_to(output) for p in (failed, previous)),
         'Closure output must be separate from the preparation.')
    consumed = {}

    def read(path):
        path = Path(path).resolve()
        value = read_json(path)
        consumed[str(path)] = file_sha256(path)
        return value

    failure, progress = read(failed / 'failure.json'), read(failed / 'progress.json')
    saved, cases, starts = read(previous / 'calibration.json'), read(previous / 'recipes.json'), read(previous / 'start-binding.json')
    need(saved.get('stage') == starts.get('stage') == 'svg', 'This narrow closure is SVG only.')
    need(starts.get('recipes_sha256') == digest(cases), 'Previous immutable recipe binding changed.')
    need(failure.get('code') == 'capture_timing_failure' and failure.get('message') == MESSAGE
         and failure.get('model_started') is False and failure.get('previous_attempts_preserved') is True,
         'Expected the recorded visual-clock preparation failure.')
    need(progress.get('model_started') is False, 'Preparation cannot claim a started model.')
    session = failed.parents[1] / 's' / failed.name
    need(not session.exists() and all(not (failed / name).exists() for name in ('suite', 'result.json', 'calibration.json')),
         'Do not close a complete or admitted preparation as this failure.')
    private = [case for case in cases if case.get('role') != 'calibration']
    need(len(private) == 24 and len({case['case_id'] for case in private}) == 24,
         'Keep the exact 24 private SVG recipe identities.')
    complete, incomplete, records, failed_attempt = [], [], [], None
    for index, case in enumerate(private, 1):
        case_id = case['case_id']
        need(isinstance(case_id, str) and Path(case_id).name == case_id and '/' not in case_id and '\\' not in case_id,
             'Unsafe recipe identity.')
        present, absent = [], []
        for role in ('reference', 'original', 'before'):
            path = failed / role / case_id / 'record.json'
            if not path.is_file():
                absent.append(role)
                continue
            record = read(path)
            need(record.get('measurement_status') == 'ok' and record.get('scenario') == case['scenario'],
                 'Completed receipt changed its recipe or measurement status.')
            records.append({'case_id': case_id, 'role': role, 'source_role': role, 'record_path': str(path),
                            'record_file_sha256': consumed[str(path.resolve())],
                            'record_sha256': digest(record), 'source_sha256': record.get('source_sha256'),
                            'scope': 'retained completed record receipt; not a new raw/behavior acceptance'})
            present.append(role)
        row = {'ordinal': index, 'case_id': case_id, 'split_role': case['role'], 'present_roles': present, 'missing_roles': absent}
        (complete if not absent else incomplete).append(row)
        attempt = failed / 'reference' / case_id / 'attempt-01'
        if (attempt / 'capture-error.json').is_file():
            need(failed_attempt is None, 'This receipt expects exactly one interrupted reference attempt.')
            error, start = read(attempt / 'capture-error.json'), read(attempt / 'capture-start.json')
            need(error.get('code') == failure['code'] and error.get('message') == failure['message']
                 and start.get('scenario') == case['scenario'] and start.get('variant') == 'reference',
                 'Failed reference capture is not bound to this recipe/error.')
            failed_attempt = {'case_id': case_id, 'ordinal': index, 'directory': str(attempt),
                              'capture_error_sha256': consumed[str((attempt / 'capture-error.json').resolve())],
                              'capture_start_sha256': consumed[str((attempt / 'capture-start.json').resolve())],
                              'source_hashes': start.get('source_hashes'), 'adapter_hashes': start.get('adapter_hashes')}
            for name in ('received-evidence.json', 'frame-receipts.json', 'clock-runtime.json', 'progress-runtime.json',
                         'transport-context.json', 'transport-order.json', 'stream-baseline.json', 'preflight.json'):
                read(attempt / 'private' / name)
    need(len(complete) == 18 and len(incomplete) == 6 and len(records) == 54
         and failed_attempt is not None and failed_attempt['ordinal'] == 19,
         'Observed named-path completion differs from the declared interrupted stage.')
    need(progress['phase'].startswith(failed_attempt['case_id'] + ':'), 'Progress does not identify the failing recipe.')
    for relative in ('scripts/phase3d_v2_prepare.py', 'kiroclops/local_engine/suite_web_v2.py',
                     'kiroclops/web_local/capture.py', 'kiroclops/web_local/visual_clock.py'):
        path = ROOT / relative
        consumed[str(path.resolve())] = file_sha256(path)
    producer = {'path': str(Path(__file__).resolve()), 'sha256': file_sha256(__file__)}
    consumed[producer['path']] = producer['sha256']
    if diagnostic is not None:
        need(diagnostic.get('kind') == 'VisibleClockFailureDiagnostic' and diagnostic.get('case_id') == failed_attempt['case_id']
             and diagnostic.get('full_behavior_acceptance') is False and diagnostic.get('capture_started') is False,
             'Diagnostic must be a read-only observation of this failed attempt.')
    closure = {'kind': 'FailedReferenceSuitePreparationClosure', 'schema_version': 1,
        'status': 'measurement_insufficient', 'stage': 'svg', 'preparation_id': failed.name,
        'failed_preparation_directory': str(failed), 'previous_calibration_directory': str(previous),
        'previous_calibration': str(previous), 'expected_session_directory': str(session),
        'attempt_directory': failed_attempt['directory'], 'suite_created': False, 'session_created': False,
        'failure': failure, 'failure_classification': 'visual_clock_capture_timing_failure',
        'measurement_status': 'fail', 'behavior_status': 'not_evaluated', 'model_started': False,
        'native_prompt_sent': False, 'new_model_generation': False, 'candidate_budget_consumed': False,
        'admission_started': False, 'initial_quality_started': False, 'full_behavior_acceptance': False,
        'private_case_count': len(private), 'completed_case_count': len(complete), 'missing_case_count': len(incomplete),
        'completed_role_record_count': len(records), 'completed_cases': complete, 'incomplete_cases': incomplete,
        'completed_records': records, 'failed_attempt': failed_attempt,
        'historical_acceptance_changed': False, 'automatic_replacement_started': False, 'capture_restarted': False,
        'producer': producer, 'source_bindings_sha256': digest(consumed),
        'scope': 'Preparation clock failure only; retained partial records are preserved and no behavior result is inferred.'}
    output.mkdir(parents=True)
    if diagnostic is not None:
        write_json(output / 'diagnostic.json', diagnostic)
        closure['diagnostic_sha256'] = digest(diagnostic)
    write_json(output / 'closure.json', closure)
    write_json(output / 'evidence-bindings.json', {'kind': 'FailedPreparationEvidenceBindings', 'schema_version': 1,
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'scope': 'Named metadata files only; no raw image/package rescan or behavior comparison.'})
    generated = {name: file_sha256(output / name) for name in ('closure.json', 'evidence-bindings.json')}
    if diagnostic is not None:
        generated['diagnostic.json'] = file_sha256(output / 'diagnostic.json')
    write_json(output / 'receipt.json', {'kind': 'FailedReferenceSuitePreparationReceipt', 'schema_version': 1,
        'status': 'measurement_insufficient', 'closure_sha256': digest(closure), 'output_file_hashes': generated,
        'consumed_files_sha256': digest(consumed), 'producer': producer, 'full_behavior_acceptance': False})
    return closure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--failed', required=True, type=Path)
    parser.add_argument('--previous', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--diagnostic', type=Path)
    args = parser.parse_args()
    try:
        value = run(args.failed, args.previous, args.output,
                    diagnostic=read_json(args.diagnostic) if args.diagnostic else None)
    except (VisualError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}))
        return 1
    print(json.dumps({key: value[key] for key in ('status', 'stage', 'completed_case_count', 'missing_case_count',
                                               'measurement_status', 'behavior_status', 'model_started')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
