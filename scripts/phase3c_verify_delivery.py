"""Seal the bounded 3C delivery and verify preserved historical artifacts.

No captures, model turns, acceptance decisions or historical files are rewritten.
The report's current file inventory is refreshed after presentation QA; its
initial-build inventory remains as historical evidence.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse

from kiroclops.common import ROOT
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes


SOURCES = [
    'kiroclops/local_engine/backend.py', 'kiroclops/cloud/execution.py',
    'kiroclops/local_engine/intake.py', 'kiroclops/local_engine/intake_ui_server.py',
    'kiroclops/local_engine/workflow_status.py', 'kiroclops/visual/raw_event_review.py',
    'kiroclops/web_local/natural_capture.py', 'kiroclops/web_local/capture.py',
    'tests/test_local_backend.py', 'tests/test_raw_event_review.py',
    'tests/test_workflow_status.py', 'tests/test_web_natural_capture.py',
    'scripts/phase3c_panel_measurement_diagnosis.py',
    'scripts/phase3c_interaction_probes.py', 'scripts/phase3c_ui_check.py',
    'scripts/phase3c_user_flow_run.py', 'scripts/phase3c_report.py',
    'scripts/phase3c_report_check.py', 'scripts/phase3c_verify_delivery.py',
    'PHASE3C_PLAN.md', 'PHASE3C_STATUS.md', 'README.md', 'ROADMAP.md',
    'LOCAL_ENGINEERING.md',
    'kiroclops/local_engine/launcher.py', 'kiroclops/local_engine/review.py',
]


def verify(files):
    failures = []
    for name, expected in files.items():
        path = ROOT / name
        if not path.is_file():
            failures.append({'path': name, 'error': 'missing'})
        elif file_sha256(path) != expected:
            failures.append({'path': name, 'error': 'sha256_mismatch'})
    return failures


def seal():
    base = ROOT / 'artifacts/phase3c'
    old_path = ROOT / 'artifacts/phase3b/DELIVERY_MANIFEST.json'
    old = read_json(old_path)
    historical = {k: v for k, v in old['files'].items() if k.startswith('artifacts/')}
    historical_failures = verify(historical)
    preservation = {
        'kind': 'HistoricalArtifactPreservation',
        'historical_manifest_sha256': file_sha256(old_path),
        'artifact_files_checked': len(historical),
        'status': 'fail' if historical_failures else 'pass',
        'failures': historical_failures,
        'scope': 'Historical artifact hashes only. Current code/docs are sealed separately in 3C.',
        'changed_current_paths': [k for k, v in old['files'].items()
                                  if not k.startswith('artifacts/')
                                  and (not (ROOT / k).is_file() or file_sha256(ROOT / k) != v)],
    }
    write_json(base / 'historical-preservation.json', preservation)
    if historical_failures:
        raise ValueError('Historical artifact mismatch; inspect preservation receipt.')

    report = base / 'delivery-01'
    report_files = {p.relative_to(report).as_posix(): file_sha256(p)
                    for p in sorted(report.rglob('*'))
                    if p.is_file() and p != report / 'integrity.json'}
    write_json(report / 'integrity.json', {
        'kind': 'CurrentPresentationInventory', 'files': report_files,
        'scope': 'Report including presentation QA; original build inventory remains preserved.',
    })

    paths = {ROOT / name for name in SOURCES}
    paths.update(p for p in report.rglob('*') if p.is_file())
    paths.update(p for p in base.glob('*.json')
                 if p.name not in ('DELIVERY_MANIFEST.json', 'delivery-verification.json'))
    for folder in ['doc-history-01', 'panel-measurement-diagnosis-03', 'ui-check-04']:
        paths.update(p for p in (base / folder).glob('*') if p.is_file())

    sealed_sources = []
    for folder in ['interaction-probes-01', 'interaction-probes-panel-warmup-02']:
        directory = base / folder
        paths.update(p for p in directory.glob('*.json'))
        registration = read_json(directory / 'registration.json')
        binding = read_json(directory / 'registration-binding.json')
        result = read_json(directory / 'result.json')
        if digest(registration) != binding['sha256'] or result['registration_sha256'] != binding['sha256']:
            raise ValueError('Probe registration binding changed: ' + folder)
        for domain, sources in registration['sources'].items():
            for key in ['candidate', 'baseline', 'reference_source']:
                if not sources.get(key):
                    continue
                field = 'reference_sha256' if key == 'reference_source' else key + '_sha256'
                hashes = source_hashes(Path(sources[key]))
                if digest(hashes) != sources[field]:
                    raise ValueError('Sealed probe source changed: ' + domain + '/' + key)
                sealed_sources.append({'set': folder, 'domain': domain, 'role': key,
                                       'sha256': sources[field], 'files': len(hashes), 'status': 'pass'})
        for row in result['rows']:
            case = Path(row['path'])
            paths.add(case)

    real = base / 'user-flow-kiro-01'
    paths.update(p for p in real.glob('*') if p.is_file())
    result = read_json(real / 'result.json')
    session = Path(result['session'])
    state = read_json(session / 'session.json')
    summary = read_json(real / 'summary.json')
    report_state = session / 'report/report.json'
    if file_sha256(session / 'session.json') != summary['session_json_sha256']:
        raise ValueError('Real run session changed after summary.')
    if file_sha256(report_state) != summary['report_sha256']:
        raise ValueError('Real run report changed after summary.')
    source = session / 'candidates' / state['selected_candidate_id'] / 'source'
    selected = source_hashes(source)
    if digest(selected) != summary['candidate_sha256']:
        raise ValueError('Real run selected source changed.')
    paths.update(source / name for name in selected)
    paths.update(session / name for name in ['session.json', 'binding.json', 'report/report.json'])
    # Bounded native proof/config/log inventory. Hashing does not expose content.
    paths.update(p for p in (session / 'kiro').rglob('*') if p.is_file())
    for candidate in state['candidates']:
        directory = session / 'candidates' / candidate['candidate_id']
        paths.update(p for p in directory.glob('*.json'))

    files = {p.relative_to(ROOT).as_posix(): file_sha256(p) for p in sorted(paths)}
    manifest = {
        'kind': 'KiroClopsPhase3CDelivery', 'schema_version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'visibility': 'internal_evidence_inventory',
        'implementation_status': 'delivered', 'overall_acceptance': 'partial',
        'files': files, 'preserved_artifact_files': len(historical),
        'historical_manifest_sha256': file_sha256(old_path),
        'sealed_probe_sources': sealed_sources,
        'real_kiro_run': summary,
        'tests': read_json(base / 'regression-tests.json'),
        'presentation': {'scenarios': 10, 'comparison_videos': 24,
                         'ui_checks': 12, 'viewer_checks': 11, 'standalone_file_mode': 'pass'},
        'scope': {'new_model_runs': 1, 'new_aws_resources': False,
                  'desktop_or_ide_taken_over': False, 'full_3a_repeated': False,
                  'historical_acceptance_rewritten': False},
        'remaining': ['14-tick jump mismatch', 'panel interrupted-close mismatch',
                      'two historical panel event boundaries and onset uncertainty',
                      'independent visual/source review', 'full 3A/game chain acceptance'],
        'producer_sha256': file_sha256(Path(__file__)),
    }
    write_json(base / 'DELIVERY_MANIFEST.json', manifest)
    failures = verify(files)
    receipt = {
        'kind': 'Phase3CDeliveryVerification', 'status': 'fail' if failures else 'pass',
        'manifest_sha256': file_sha256(base / 'DELIVERY_MANIFEST.json'),
        'files_checked': len(files), 'historical_artifacts_checked': len(historical),
        'sealed_probe_source_checks': len(sealed_sources), 'failures': failures,
        'overall_acceptance': 'partial',
        'scope': 'Checks file/source integrity. Does not certify missing behavior or reviews.',
    }
    write_json(base / 'delivery-verification.json', receipt)
    if failures:
        raise ValueError('Delivery hash mismatch.')
    print({k: receipt[k] for k in ['status', 'files_checked', 'historical_artifacts_checked',
                                   'sealed_probe_source_checks', 'overall_acceptance']})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        manifest = read_json(ROOT / 'artifacts/phase3c/DELIVERY_MANIFEST.json')
        failures = verify(manifest['files'])
        print({'status': 'fail' if failures else 'pass', 'files': len(manifest['files']), 'failures': failures})
        if failures:
            raise SystemExit(1)
    else:
        seal()
