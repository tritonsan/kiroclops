"""Prospective regression of two failed web recording seams, without Kiro.

The formerly private recipes are now known recorder regressions. This script
does not claim new held-out behavior, promote an old job, or change a project.
"""
from pathlib import Path
import argparse
import json
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.reliability import qualify_warmups
from kiroclops.local_engine.session import copy_project, source_hashes
from kiroclops.local_engine.suite_revision import measurement_source_hashes, stamp_new_measurement
from kiroclops.local_engine.source_inventory import full_byte_inventory
from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json
from kiroclops.web_local.specs import fixture_project
from scripts.phase3d_v2_prepare import Capture, recipes, sources


CASES = {
    'panel': ('realistic-suite-known-panel-drag-release-w1280-r1',
              'realistic-suite-known-panel-drag-release-w1280-r2'),
    'svg': ('realistic-suite-svg-validation-five-to-six-drag-new-w1280-r1',
            'realistic-suite-svg-validation-five-to-six-drag-new-w1280-r2'),
}


def selected_cases(stage):
    indexed = {case['case_id']: case for case in recipes(stage)}
    return [indexed[name] for name in CASES[stage]]


def run(stage, output, root=Path('C:/kc3d')):
    output = Path(output).resolve()
    if output.exists():
        raise VisualError('output_exists', 'Preserve all recording attempts.')
    parent, original, historical = sources(stage)
    reference = fixture_project(stage, 'reference')
    cases = selected_cases(stage)
    expected = measurement_source_hashes(stage)
    project_hashes = {role: source_hashes(path) for role, path in
                      (('reference', reference), ('original', original), ('before', parent))}
    output.mkdir(parents=True)
    copy_project(parent, output / 'parent')
    if source_hashes(output / 'parent') != project_hashes['before']:
        raise VisualError('source_changed', 'Copied parent differs from pinned candidate.')
    spec = {'kind': 'RecorderRepairQualification', 'schema_version': 1,
            'stage': stage, 'cases': cases, 'measurement_source_hashes': expected,
            'sources': project_hashes, 'historical_candidate': historical,
            'producer_sha256': file_sha256(__file__),
            'maximum_seconds': 3600, 'model_started': False,
            'formerly_private_recipes_now_known': True,
            'full_behavior_acceptance': False}
    write_json(output / 'spec.json', spec)
    (output / 'producer.py').write_bytes(Path(__file__).read_bytes())
    started = time.time()
    cap = Capture(stage, output, root)
    records, warmups, failure = [], None, None
    def check():
        if time.time() - started >= spec['maximum_seconds']:
            raise VisualError('time_budget_exhausted', 'Qualification deadline reached.')
    def cancellation_probe():
        check()
        return False
    def capture(project, case, destination, variant='before', reference_record=None):
        from kiroclops.local_engine.suite_web_v2 import capture_record
        check()
        if file_sha256(__file__) != spec['producer_sha256']:
            raise VisualError('source_changed', 'Qualification producer changed.')
        snapshot = measurement_source_hashes(stage)
        capture_started = time.time()
        if snapshot != expected:
            raise VisualError('analysis_source_changed', 'Qualification sources changed.')
        role = 'reference' if variant == 'reference' else 'original' if Path(project) == original else 'before'
        record = capture_record(project, destination, case['scenario'], check, cap.progress,
                                cancellation_probe, variant,
                                reference_selection=(reference_record or {}).get('selection'),
                                reference_record=reference_record)
        check()
        if record.get('source_sha256') != digest(project_hashes[role]):
            raise VisualError('source_changed', 'Recording belongs to another producer.')
        record = stamp_new_measurement(record, stage, expected_sources=snapshot,
                                       capture_started_at=capture_started)
        write_json(Path(destination) / 'record.json', record)
        return record
    try:
        with _lock(Path(cap.runtime['capture_lock']), timeout=60):
            def warmup(path):
                record = capture(output / 'parent', cases[0], path / 'capture')
                write_json(path / 'record.json', record)
                return {'status': 'ok', 'timing': record.get('timing', {'status': 'pass', 'errors': []}),
                        'measurement_status': record['measurement_status'], 'record_sha256': digest(record)}
            warmups = qualify_warmups(warmup, output / 'warmups')
            for case in cases:
                check()
                ref = capture(reference, case, output / 'reference' / case['case_id'], 'reference')
                records.append({'case_id': case['case_id'], 'role': 'reference', 'record': ref})
                for role, project in (('original', original), ('before', output / 'parent')):
                    record = capture(project, case, output / role / case['case_id'], reference_record=ref)
                    records.append({'case_id': case['case_id'], 'role': role, 'record': record})
            if expected != measurement_source_hashes(stage):
                raise VisualError('analysis_source_changed', 'Qualification sources changed.')
            for role, path in (('reference', reference), ('original', original), ('before', parent),
                               ('before', output / 'parent')):
                if source_hashes(path) != project_hashes[role]:
                    raise VisualError('source_changed', 'A pinned project changed.')
            check()
            if file_sha256(__file__) != spec['producer_sha256']:
                raise VisualError('source_changed', 'Qualification producer changed.')
    except Exception as exc:
        failure = {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}
    finally:
        cap.close()
    result = {'kind': 'RecorderRepairQualificationResult', 'schema_version': 1,
              'status': 'pass' if failure is None and len(records) == 6 else 'fail',
              'stage': stage, 'spec_sha256': digest(spec), 'records': records,
              'required_record_count': 6, 'completed_record_count': len(records),
              'warmups': warmups, 'error': failure, 'started_at': started,
              'finished_at': time.time(), 'model_started': False,
              'full_behavior_acceptance': False, 'thresholds_changed': False}
    write_json(output / 'result.json', result)
    write_json(output / 'receipt.json', {'kind': 'RecorderRepairQualificationReceipt',
               'schema_version': 1,
               'result_sha256': digest(result), 'spec_sha256': digest(spec),
               'producer_sha256': file_sha256(__file__),
               'output_file_hashes': full_byte_inventory(output, ignored=(), excluded_suffixes=()),
               'measurement_source_hashes': expected, 'project_source_hashes': project_hashes})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=tuple(CASES), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.stage, args.output)
    print(json.dumps({key: result[key] for key in
                      ('status', 'stage', 'completed_record_count', 'error', 'model_started')}))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
