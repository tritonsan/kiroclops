"""Require pixel/comparator reference adequacy before a new cohort can start.

Frame-validity percentages alone are not an adequate reference gate. This
replays the stored, source-bound measurements through the current comparator;
it neither runs CV nor releases held-out measurements into a model workspace.
"""
from pathlib import Path

from kiroclops.visual.schema import VisualError, read_json, file_sha256, digest

ROOT = Path(__file__).resolve().parents[2]


def _require(condition, message):
    if not condition:
        raise VisualError('reference_not_qualified', message)


def _bound(path, expected):
    path = Path(path).resolve()
    _require(path.is_file() and file_sha256(path) == expected,
             'A reference adequacy evidence file changed or is missing.')
    return path


def selection_matches_package(selection, expected_sha256):
    """Accept both existing selection formats without hiding conflicting IDs.

    Fixture auto-selection binds ``reference_package_sha256``; confirmed video
    intake binds ``package_sha256``. These are package identities, not optional
    hints: at least one must exist and every supplied identity must agree.
    The caller still checks the entire unmodified selection against the worker.
    """
    if not isinstance(selection, dict):
        return False
    identities = [selection[name] for name in ('reference_package_sha256', 'package_sha256')
                  if name in selection]
    return bool(identities) and all(value == expected_sha256 for value in identities)


def validate_reference_gate(manifest, gate):
    from .qualification import analyzer_sources
    from .web_reference import verify_reference_pack, check_reference_comparison_coverage

    _require(gate is not None,
             'Supply the completed 32-record pixel and comparator gate with --reference-gate.')
    manifest, path = Path(manifest).resolve(), Path(gate).resolve()
    proof = read_json(path)
    manifest_sha = file_sha256(manifest)
    _require(proof.get('kind') == 'ReferenceObservabilityGateV2'
             and proof.get('status') == 'pass' and proof.get('fatal_error') is None
             and proof.get('count') == proof.get('passed')
             == proof.get('actual_comparator_self_consistency_passed')
             == proof.get('required_count') == 32,
             'All 32 reference records must pass pixel and actual comparator checks.')
    _require(proof.get('manifest_sha256') == manifest_sha,
             'Reference adequacy belongs to a different raw reference corpus.')
    sources = proof.get('source_hashes', {})
    _require(sources and sources == proof.get('completed_source_hashes')
             and proof.get('source_binding_status') == 'pass',
             'Reference adequacy requires unchanged measurement sources.')
    expected_sources = analyzer_sources()
    for name in ('web_reference.py', 'session.py'):
        source = ROOT / 'kiroclops/local_engine' / name
        expected_sources[source.relative_to(ROOT).as_posix()] = file_sha256(source)
    _require(all(sources.get(name) == sha for name, sha in expected_sources.items()),
             'Reference adequacy was produced by a different measurement version.')
    for name, sha in sources.items():
        source = (ROOT / name).resolve()
        _require(source.is_relative_to(ROOT), 'Reference source binding escapes the project.')
        _bound(source, sha)

    # This single full pack replay checks exact domains, splits, scenario
    # counts, every capture content hash and every PNG. The comparator gate
    # consumes those verified records without replaying the whole pack again.
    verified = verify_reference_pack(manifest, include_splits=True)
    _require(verified['manifest_sha256'] == manifest_sha
             and verified['verified_records'] == 32,
             'Reference pack is not the exact 32-record corpus used by this gate.')
    raw = {}
    for (domain, split), records in verified['verified_splits'].items():
        for record in records:
            raw[domain, split, record['scenario']['id']] = record
    rows = proof.get('records', [])
    keys = [(row.get('domain'), row.get('split'), row.get('scenario_id')) for row in rows]
    _require(len(keys) == len(set(keys)) == 32 and set(keys) == set(raw),
             'Reference adequacy omits or duplicates a required scenario.')
    for key, row in zip(keys, rows):
        record = raw[key]
        _require(row.get('status') == row.get('camera_status')
                 == row.get('temporal_support_status')
                 == row.get('comparator_self_consistency_status') == 'pass'
                 and not row.get('errors'), 'A required reference has an unresolved measurement failure.')
        _require(row.get('reference_pack_sha256') == manifest_sha
                 and row.get('reference_package_sha256') == record['reference_package_sha256']
                 and row.get('scenario_sha256') == digest(record['scenario']),
                 'Reference measurement is bound to another package or scenario.')
        for name, sha in row.get('evidence_hashes', {}).items():
            _bound(name, sha)
        analysis_path = _bound(row['analysis_path'], row['analysis_sha256'])
        analysis = read_json(analysis_path)
        observations, events = analysis['observations'], analysis['events']
        _require(analysis.get('measurement_status') == 'ok'
                 and analysis.get('package_sha256') == record['reference_package_sha256']
                 and analysis.get('candidate_sha256') == record.get('source_sha256')
                 and digest(observations) == row.get('observations_sha256')
                 and digest(events) == row.get('events_sha256')
                 and events.get('observation_sha256') == digest(observations)
                 and events.get('package_sha256') == record['reference_package_sha256']
                 and observations.get('package_sha256') == record['reference_package_sha256'],
                 'Reference pixel observations changed or lack a valid package binding.')
        selection_path = analysis_path.parent / 'selection.json'
        _require(row.get('evidence_hashes', {}).get(str(selection_path)) == file_sha256(selection_path),
                 'Reference selection is not bound to the gate.')
        selection = read_json(selection_path)
        _require(selection == observations.get('selection')
                 and selection_matches_package(selection, record['reference_package_sha256']),
                 'Reference selection does not belong to these measured pixels.')
        measured = {**record, 'selection': selection,
                    'measurement_status': analysis['measurement_status'],
                    'observations': observations, 'events': events}
        replay = check_reference_comparison_coverage(measured)
        stored = read_json(_bound(row['self_comparison_path'], row['self_comparison_sha256']))
        _require(replay.get('status') == 'pass' and replay == stored,
                 'The actual reference comparator no longer reproduces its passing result.')
    return {'path': str(path), 'sha256': file_sha256(path), 'records': len(rows),
            'manifest_sha256': manifest_sha, 'status': 'pass',
            'scope': 'Reference adequacy only; no transfer or independent visual review claim.'}
