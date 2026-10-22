"""Sequential reference-only pixel and actual comparator adequacy gate.

Usage (from the repository root, after captures and sources are stable):
  .venv-local/Scripts/python.exe -u -B -m kiroclops.local_engine.reference_observability --manifest artifacts/phase3a/shared-web-references-02/manifest.json --expected-manifest-sha256 SHA256 --output artifacts/phase3a/shared-web-reference-observability-02

This writes only a NEW artifact directory. It never captures, edits source,
changes thresholds, reads a model candidate, or claims behavior-transfer success.
All 32 records must pass both raw temporal support and the actual current
comparator replayed on their measured pixels. Frame-validity alone is insufficient.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import VisualError, read_json, write_json, file_sha256, digest
from kiroclops.local_engine.qualification import analyzer_sources
from kiroclops.local_engine.web_reference import (
    load_reference_split, verify_reference_pack, reference_time_coverage,
    check_reference_comparison_coverage, MIN_COMPARISON_COVERAGE, MAX_COMPARISON_GAP_S,
)
from kiroclops.local_engine.evaluation import _measure_record
from kiroclops.local_engine.session import source_hashes
from kiroclops.web_local.specs import fixture_project, scenarios

REQUIRED_COUNT = 32


def sources():
    result = analyzer_sources()
    for path in (ROOT / 'kiroclops/local_engine/web_reference.py',
                 ROOT / 'kiroclops/local_engine/session.py', Path(__file__).resolve()):
        result[path.relative_to(ROOT).as_posix()] = file_sha256(path)
    return result


def ratio(frames):
    return sum(bool(f['valid']) for f in frames) / len(frames) if frames else 0.


def first_bad(item, package_manifest, package):
    frame = next((f for f in item['frames'] if f.get('expected_active') and not f['valid']), None)
    if frame is None:
        return None
    raw = package_manifest['frames'][frame['index']]
    return {'index': frame['index'], 'time_s': frame['time_s'], 'reason': frame.get('reason'),
            'image': str(Path(package) / raw['path']), 'image_sha256': raw['sha256']}


def error(exc):
    return {'code': getattr(exc, 'code', 'reference_measurement_error'), 'message': str(exc)}


def self_failure(receipt):
    """Keep actual failed comparator gates, without reinterpreting its verdict."""
    windows = [row for row in receipt['coverage_windows']
               if row.get('valid_time_ratio') is None
               or row['valid_time_ratio'] < MIN_COMPARISON_COVERAGE
               or any(value is False for value in (row.get('gates') or {}).values())]
    return {'code': 'reference_comparator_self_consistency_failure',
            'message': 'The actual current comparison contract rejected the reference compared with itself.',
            'failed_windows': windows,
            'failed_observable_dynamics': [row for row in receipt['dynamics']
                                          if row.get('observable') and not row.get('pass')],
            'failed_relative_timing': [row for row in receipt['relative_timing'] if not row.get('pass')],
            'complete_comparator_diagnostics': 'self-comparison.json',
            'behavior_transfer_success_claimed': False}


def measure_one(raw, domain, split, number, output, expected_sha, check):
    identifier = raw['scenario']['id']
    directory = output / domain / split / identifier
    directory.mkdir(parents=True)
    package_manifest = read_json(Path(raw['package']) / 'manifest.json')
    errors, tracks, connections, camera = [], [], [], []
    measured = None
    event_diagnostics = None
    temporal = reference_time_coverage(package_manifest, expected_scenario=raw['scenario'])
    write_json(directory / 'temporal-support.json', temporal)
    consistency = None
    if temporal['status'] != 'pass':
        errors.append({'code': 'reference_window_coverage_failure',
                       'message': 'Necessary actual timestamp support did not pass.',
                       'diagnostic': 'temporal-support.json'})
    try:
        measured = _measure_record(raw, directory, check, lambda message: None)
        observations, events = measured['observations'], measured['events']
        errors.extend(deepcopy(observations.get('errors', [])))
        camera = observations['camera']
        if measured.get('measurement_status') != 'ok':
            errors.append({'code': 'reference_tracking_failure',
                           'message': 'Existing measurement contract rejected the recording.'})
        if not camera or any(frame['status'] != 'pass' for frame in camera):
            errors.append({'code': 'camera_motion', 'message': 'Static background observations did not pass.'})
        for obj in observations['objects']:
            intervals = []
            for low, high in obj['active_intervals_s']:
                active = [frame for frame in obj['frames'] if frame.get('expected_active')
                          and low - 1e-9 <= frame['time_s'] <= high + 1e-9]
                value = ratio(active)
                intervals.append({'time_interval_s': [low, high], 'frames': len(active), 'valid_ratio': value})
                if value < MIN_COMPARISON_COVERAGE:
                    errors.append({'code': 'tracking_failure', 'object_id': obj['id'],
                                   'time_interval_s': [low, high], 'valid_ratio': value})
            if not intervals:
                errors.append({'code': 'tracking_failure', 'object_id': obj['id'], 'message': 'No active intervals.'})
            tracks.append({'id': obj['id'], 'valid_ratio': obj['valid_ratio'], 'active_intervals': intervals,
                           'raw_example': first_bad(obj, package_manifest, raw['package'])})
        if not tracks:
            errors.append({'code': 'tracking_failure', 'message': 'No measured objects.'})
        for connection in observations.get('connections', []):
            connections.append({'id': connection['id'], 'valid_ratio': connection['valid_ratio'],
                                'raw_example': first_bad(connection, package_manifest, raw['package'])})
            if connection['valid_ratio'] < MIN_COMPARISON_COVERAGE:
                errors.append({'code': 'insufficient_observability', 'connection_id': connection['id'],
                               'valid_ratio': connection['valid_ratio']})
        event_diagnostics = {'events': len(events.get('events', [])),
            'relationships': len(events.get('relations', [])),
            'unobservable': deepcopy(events.get('unobservable', [])),
            'note': 'Secondary absent/censored events are diagnostic; required checks come from the unmodified comparator.'}
        # Mandatory actual contract replay, even when frame-validity equals 100%.
        # This helper consumes the measured pixels, not private DOM coordinates.
        consistency = check_reference_comparison_coverage(measured)
        write_json(directory / 'self-comparison.json', consistency)
        if consistency['status'] != 'pass':
            errors.append(self_failure(consistency))
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        if getattr(exc, 'code', '') == 'source_changed':
            raise
        errors.append(error(exc))
    check()
    evidence = {}
    for path in (directory / 'temporal-support.json', directory / 'selection.json',
                 directory / 'analysis.json', directory / 'self-comparison.json',
                 Path(raw['package']) / 'manifest.json'):
        if path.is_file():
            evidence[str(path)] = file_sha256(path)
    return {'index': number, 'domain': domain, 'split': split, 'scenario_id': identifier,
        'scenario_sha256': digest(raw['scenario']), 'package': raw['package'],
        'reference_pack_sha256': expected_sha, 'reference_package_sha256': raw['reference_package_sha256'],
        'status': 'pass' if not errors and consistency and consistency['status'] == 'pass' else 'fail',
        'errors': errors, 'objects': tracks, 'connections': connections,
        'camera_status': 'pass' if camera and all(frame['status'] == 'pass' for frame in camera) else 'fail',
        'temporal_support_status': temporal['status'],
        'comparator_self_consistency_status': consistency['status'] if consistency else 'not_completed',
        'comparator_coverage_windows': consistency['coverage_windows'] if consistency else [],
        'event_diagnostics': event_diagnostics,
        'private_capture_functional_status': raw['functional']['status'],
        'capture_timing_status': raw['timing']['status'],
        'functional_and_timing_scope': 'Previously bound recorder receipts, separate from pixel observability.',
        'analysis_path': str(directory / 'analysis.json') if measured else None,
        'analysis_sha256': file_sha256(directory / 'analysis.json') if measured else None,
        'observations_sha256': digest(measured['observations']) if measured else None,
        'events_sha256': digest(measured['events']) if measured else None,
        'self_comparison_path': str(directory / 'self-comparison.json') if consistency else None,
        'self_comparison_sha256': file_sha256(directory / 'self-comparison.json') if consistency else None,
        'evidence_hashes': evidence, 'behavior_transfer_success_claimed': False}


def run(manifest, expected_sha, output):
    manifest, output = manifest.resolve(), output.resolve()
    if output.exists():
        raise VisualError('output_exists', 'Use a fresh output; previous evidence must be preserved.')
    if len(expected_sha) != 64 or any(c not in '0123456789abcdef' for c in expected_sha):
        raise VisualError('invalid_sha256', 'An explicit lowercase SHA256 is required.')
    if file_sha256(manifest) != expected_sha:
        raise VisualError('reference_pack_changed', 'Reference manifest differs from the explicit expected SHA256.')
    before, started = sources(), time.time()
    output.mkdir(parents=True)
    write_json(output / 'registration.json', {
        'kind': 'ReferenceObservabilityRegistrationV2', 'manifest': str(manifest),
        'manifest_sha256': expected_sha, 'source_hashes': before, 'started_at': started,
        'parallel_analysis_workers': 1, 'required_count': REQUIRED_COUNT,
        'contract': {'min_valid_ratio': MIN_COMPARISON_COVERAGE,
                     'min_comparison_coverage': MIN_COMPARISON_COVERAGE,
                     'max_comparison_gap_s': MAX_COMPARISON_GAP_S,
                     'require_static_camera': True,
                     'event_options': {'connection_tolerance_px': 2., 'rest_duration_s': .25}},
        'mandatory_comparator': 'web_reference.check_reference_comparison_coverage',
        'scope': 'All32 reference pixels, before any model/candidate. No tuning; self-consistency is reference adequacy only.',
        'behavior_transfer_success_claimed': False})
    records, fatal = [], None

    def check():
        if sources() != before or file_sha256(manifest) != expected_sha:
            raise VisualError('source_changed', 'Measurement source/reference changed during the gate.')

    try:
        verified = verify_reference_pack(manifest)
        if verified['verified_records'] != REQUIRED_COUNT:
            raise VisualError('invalid_reference_pack', 'Exactly32 current reference records are required.')
        for domain in ('panel', 'svg'):
            for split in ('calibration', 'validation'):
                raw_records = load_reference_split(manifest, domain, split,
                    expected_source_hashes=source_hashes(fixture_project(domain, 'reference')),
                    expected_scenarios=scenarios(domain, split))
                for raw in raw_records:
                    check()
                    number, identifier = len(records) + 1, raw['scenario']['id']
                    print(f'{number}/32 {domain}/{split}/{identifier}', flush=True)
                    record = measure_one(raw, domain, split, number, output, expected_sha, check)
                    write_json(output / domain / split / identifier / 'gate.json', record)
                    records.append(record)
                    write_json(output / 'progress.json', {'completed': len(records), 'required_count': REQUIRED_COUNT,
                                                         'records': records, 'finished_at': None})
                    print(record['status'], [item.get('code') for item in record['errors']], flush=True)
        check()
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        fatal = error(exc)
    after = sources()
    source_unchanged = after == before and file_sha256(manifest) == expected_sha
    passed = sum(record['status'] == 'pass' for record in records)
    self_passed = sum(record['comparator_self_consistency_status'] == 'pass' for record in records)
    result = {'kind': 'ReferenceObservabilityGateV2',
        'status': 'pass' if not fatal and source_unchanged and passed == self_passed == REQUIRED_COUNT else 'fail',
        'manifest': str(manifest), 'manifest_sha256': expected_sha,
        'source_hashes': before, 'completed_source_hashes': after,
        'source_binding_status': 'pass' if source_unchanged else 'fail',
        'count': len(records), 'required_count': REQUIRED_COUNT, 'passed': passed,
        'actual_comparator_self_consistency_passed': self_passed, 'fatal_error': fatal, 'records': records,
        'started_at': started, 'finished_at': time.time(), 'elapsed_s': time.time() - started,
        'model_executed': False, 'capture_executed': False, 'behavior_transfer_success_claimed': False,
        'private_dom_used_for_pixel_measurement': False,
        'independent_visual_review_status': 'not_claimed_by_this_automated_gate'}
    write_json(output / 'result.json', result)
    print(result['status'], passed, '/32; actual self-consistency', self_passed,
          '/32;', file_sha256(output / 'result.json'), flush=True)
    return 0 if result['status'] == 'pass' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    return run(args.manifest, args.expected_manifest_sha256, args.output)


if __name__ == '__main__':
    raise SystemExit(main())
