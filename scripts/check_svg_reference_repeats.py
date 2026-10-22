"""Compare already captured independent reference repeats, without recapture.

Read-only diagnostic of stored measurements. Image correctness is not inferred
from this comparison; no primary trial or reference qualification is rewritten.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.evaluation import _contract, _dynamic_checks
from kiroclops.visual.comparison import compare_observations


def main():
    base = ROOT/'artifacts/phase3a'
    measurements = base/'completion-paired-cohort-01/sessions/svg-baseline-01/svg/measurements/dea94a69d4dd438fa8d82468c0bd71ca/reference'
    output = base/'completion-20260927/svg-reference-repeat-diagnostic.json'
    assert not output.exists()
    bound = {}
    def load(sid):
        folder = measurements/sid
        paths = [folder/'package/manifest.json', folder/'measurement/analysis.json',
                 folder/'measurement/selection.json', folder/'measurement/reuse-origin.json']
        for path in paths:
            bound[str(path)] = file_sha256(path)
        package, analysis, selection, origin = map(read_json, paths)
        assert analysis['package_sha256'] == package['package_sha256']
        assert analysis['observations']['package_sha256'] == package['package_sha256']
        assert analysis['candidate_sha256'] == digest(package['metadata']['source_hashes'])
        assert origin['original_analysis_sha256'] == file_sha256(paths[1])
        assert origin['observations_sha256'] == digest(analysis['observations'])
        assert selection == analysis['observations']['selection']
        assert package['metadata']['scenario']['id'] == sid
        return {'scenario': package['metadata']['scenario'], 'source_sha256': analysis['candidate_sha256'],
                'selection': selection,
                'measurement_status': analysis['measurement_status'],
                'observations': analysis['observations'], 'events': analysis['events']}
    rows = []
    for scenario in ('three-to-five', 'five-to-six-drag', 'six-to-four'):
        for width in (768, 1280):
            sid = f'{scenario}-w{width}'
            a, b = [load(f'{sid}-r{r}') for r in (1, 2)]
            assert a['source_sha256'] == b['source_sha256']
            assert a['scenario']['repeat'] == 1 and b['scenario']['repeat'] == 2
            assert {k:v for k,v in a['scenario'].items() if k not in {'id','repeat'}} == {k:v for k,v in b['scenario'].items() if k not in {'id','repeat'}}
            for reverse in (False, True):
                ref, target = (b,a) if reverse else (a,b)
                contract = _contract(ref, target)
                result = compare_observations(ref['observations'], target['observations'], contract)
                dynamics = _dynamic_checks(ref, target, contract)
                rows.append({'scenario':sid, 'direction':'r2-to-r1' if reverse else 'r1-to-r2',
                    'pass':result['success'] and all(w['pass'] for w in dynamics if w.get('observable')),
                    'measurement_pass':result['measurement_status'] == 'pass',
                    'failed_windows': [{k:w[k] for k in ('reference_id','window_id','gates','position_rmse','size_rmse','scale_px','limits')}
                                       for w in result['object_windows'] if not w['pass']],
                    'failed_observable_dynamics':[w for w in dynamics if w.get('observable') and not w['pass']]})
    assert all(file_sha256(path) == sha for path, sha in bound.items())
    receipt = {'kind':'StoredReferenceRepeatDiagnostic',
        'passed':sum(r['pass'] for r in rows), 'required':len(rows), 'rows':rows,
        'evidence_hashes':bound, 'producer_sha256':file_sha256(__file__),
        'comparator_sha256':file_sha256(ROOT/'kiroclops/local_engine/evaluation.py'),
        'independent_repeat_recordings':True, 'recaptured':False, 'model_run':False,
        'operator_feedback_to_kiro':False, 'primary_outcome_modified':False,
        'scope':'Supplementary stored-observation comparison. Not a replacement reference gate, fresh raw-image accuracy review, or motion-transfer success.'}
    write_json(output,receipt)
    print({'reference_repeat_comparisons_passed':receipt['passed'],'required':receipt['required'],
           'failures':[{'scenario':r['scenario'],'direction':r['direction'],'windows':r['failed_windows']} for r in rows if not r['pass']]})


if __name__ == '__main__':
    main()
