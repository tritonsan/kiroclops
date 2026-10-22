"""Private engineering controls for the registered wall/coyote preservation gate.

Historical controllers are fixed test fixtures, never target seeds or model input.
Both mechanics must reject both known regressions and accept the protected source.
"""
from pathlib import Path
from kiroclops.common import ROOT
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, write_json
from .session import digest, ordinary

VERSION = 'heart-wall-coyote-qualification-v1'
CONTROLS = {
    ('jump', '01'): ('candidate-03', '0e67d1f2284feef725bdbdd47f821921c5c3a9526c83ca9103542896c73b9090'),
    ('jump', '02'): ('candidate-02', '884e7df71b61b94a3940f9008aa511b33682ad211fe9aacd6fec8a636fbc94f4'),
    ('jump', '03'): ('candidate-03', 'a7d9061c0860b695ef6f131271495ec5225bb6c2702adb9be6bf7102cf4bbafa'),
    ('ground', '01'): ('candidate-01', '335fc2f25c01fee391a84053a5a16e71a637f85f4fd03c61b07b28d4a492fa9c'),
    ('ground', '02'): ('candidate-01', '8e68864156bab8c9c236df1f47a118397d90d4542e42b6d6e09ce7c500b5c1b3'),
    ('ground', '03'): ('candidate-01', '6fe6ef8905306f409809375d580642356c7ff24f9ff62f185f7c6100ddf43ff2'),
}


def _need(value, message):
    if not value:
        raise ValueError(message)


def _historical(stage, chain):
    candidate, expected = CONTROLS[stage, chain]
    project = ordinary(ROOT / f'sessions/heart-heart-02-{chain}-{stage}/candidates/{candidate}/project')
    _need(digest(project_hashes(project)) == expected, 'Pinned historical control source changed')
    return project


def collect_controls(original_baseline, output, *, progress=print, cancel=None):
    from .integrity import run_checks
    original_baseline, output = ordinary(original_baseline), ordinary(output)
    _need(not output.exists(), 'Engineering controls need a new output directory')
    _need(not output.is_relative_to(original_baseline) and not original_baseline.is_relative_to(output),
          'Engineering outputs must be separate from original source')
    original_sources = project_hashes(original_baseline)
    output.mkdir(parents=True)
    copies = {}
    for stage, chain in CONTROLS:
        copies[stage, chain] = copy_project(_historical(stage, chain), output/'controllers'/f'{stage}-{chain}')
        _need(digest(project_hashes(copies[stage, chain])) == CONTROLS[stage, chain][1],
              'Historical control copy changed')
    results = []
    for stage, chain in CONTROLS:
        expected = 'pass' if chain == '02' else 'fail'
        baseline = original_baseline if stage == 'jump' else copies['jump', '02']
        directory = output/'runs'/f'{stage}-{chain}'
        progress(f'{stage} wall/coyote historical-{chain}; expected {expected}')
        result = run_checks(copies[stage, chain], baseline, directory, stage, cancel=cancel)
        component = directory/'wall-coyote/comparison.json'
        _need(component.is_file(), 'Missing wall/coyote engineering comparison')
        results.append({'id': f'{stage}-{chain}', 'stage': stage, 'chain': chain, 'expected': expected,
                        'candidate_project': str(copies[stage, chain]), 'baseline_project': str(baseline),
                        'path': str(directory), 'comparison_sha256': sha256(directory/'comparison.json'),
                        'wall_comparison_sha256': sha256(component), 'observed': result.get('status')})
    receipt = {'schema_version': 1, 'version': VERSION, 'engineering_only': True,
               'model_input': False, 'original_project': str(original_baseline),
               'original_sources': original_sources, 'controls': results}
    path = output/'controls.json'
    write_json(path, receipt)
    return str(path)


def verify_controls(receipt_path, *, expected_original_sources):
    from .integrity import verify_checks
    try:
        receipt_path = ordinary(receipt_path)
        root = receipt_path.parent
        receipt = read_json(receipt_path)
        _need(receipt.get('version') == VERSION and receipt.get('schema_version') == 1
              and receipt.get('engineering_only') is True and receipt.get('model_input') is False,
              'Wrong control receipt identity or visibility')
        original = ordinary(receipt['original_project'])
        _need(project_hashes(original) == receipt['original_sources'] == expected_original_sources,
              'Original baseline source binding differs')
        rows = receipt['controls']
        _need(len(rows) == 6 and {(r['stage'], r['chain']) for r in rows} == set(CONTROLS),
              'Exactly six distinct planned controls are required')
        cases = []
        for row in rows:
            stage, chain = row['stage'], row['chain']
            expected = 'pass' if chain == '02' else 'fail'
            _need(row['id'] == f'{stage}-{chain}' and row['expected'] == expected,
                  'Control identity or expected verdict substituted')
            candidate = ordinary(row['candidate_project'])
            baseline = ordinary(row['baseline_project'])
            _need(candidate == root/'controllers'/f'{stage}-{chain}', 'Control source path escaped')
            _need(baseline == (original if stage == 'jump' else root/'controllers/jump-02'),
                  'Control compared against a different baseline')
            candidate_sources, baseline_sources = project_hashes(candidate), project_hashes(baseline)
            _need(digest(candidate_sources) == CONTROLS[stage, chain][1], 'Wrong historical controller')
            _need(stage == 'jump' or digest(baseline_sources) == CONTROLS['jump', '02'][1],
                  'Ground control needs the protected jump source')
            directory = ordinary(row['path'])
            _need(directory == root/'runs'/f'{stage}-{chain}', 'Control result path escaped')
            comparison = directory/'comparison.json'
            component = directory/'wall-coyote/comparison.json'
            _need(sha256(comparison) == row['comparison_sha256']
                  and sha256(component) == row['wall_comparison_sha256'], 'Control evidence changed')
            result, wall = read_json(comparison), read_json(component)
            _need(result.get('stage') == wall.get('stage') == stage, 'Wrong control stage')
            _need(result.get('sources') == wall.get('sources') == {
                'baseline': baseline_sources, 'candidate': candidate_sources}, 'Wrong control source maps')
            _need(result.get('wall_coyote') == {'path': str(component), 'sha256': sha256(component)},
                  'Composite result is not bound to its wall/coyote component')
            replay = verify_checks(directory, expected_candidate_sources=candidate_sources,
                                   expected_baseline_sources=baseline_sources)
            _need(replay.get('status') == 'pass', 'Control native/source replay failed')
            _need(result.get('status') == row.get('observed') == replay.get('preservation_status') == expected,
                  'Whole-control outcome differs from preregistered expectation')
            _need(wall.get('status') == expected, 'Wall/coyote gate itself did not distinguish the known source')
            cases.append({'id': row['id'], 'status': 'pass', 'expected_behavior_verdict': expected,
                          'wall_behavior_verdict': wall['status']})
        return {'status': 'pass', 'count': 6, 'required': 6, 'cases': cases,
                'receipt_sha256': sha256(receipt_path)}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return {'status': 'fail', 'count': 0, 'required': 6, 'reason': str(exc)}
