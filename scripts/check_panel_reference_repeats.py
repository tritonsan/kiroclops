"""Stored panel reference repeatability only; no new captures or verdict edits."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.evaluation import _contract, _dynamic_checks, _relative_timing_checks
from kiroclops.visual.comparison import compare_observations


def main():
    base = ROOT/'artifacts/phase3a'
    source = base/'completion-paired-cohort-01/sessions/panel-baseline-01/panel/measurements/1b91c51697ab40848aeecd711b671e2e/reference'
    output = base/'completion-20260927/panel-reference-repeat-diagnostic.json'
    assert not output.exists()
    hashes = {}
    records = {}
    for folder in sorted(source.iterdir()):
        if not folder.is_dir():
            continue
        paths = [folder/'package/manifest.json', folder/'measurement/analysis.json',
                 folder/'measurement/selection.json', folder/'measurement/reuse-origin.json']
        for path in paths:
            hashes[str(path)] = file_sha256(path)
        package, analysis, selection, origin = map(read_json, paths)
        assert analysis['package_sha256'] == package['package_sha256'] == analysis['observations']['package_sha256']
        assert analysis['candidate_sha256'] == digest(package['metadata']['source_hashes'])
        assert origin['original_analysis_sha256'] == file_sha256(paths[1])
        assert origin['observations_sha256'] == digest(analysis['observations'])
        assert selection == analysis['observations']['selection']
        assert package['metadata']['scenario']['id'] == folder.name
        records[folder.name] = {'scenario':package['metadata']['scenario'],
            'source_sha256':analysis['candidate_sha256'], 'selection':selection,
            'observations':analysis['observations'], 'events':analysis['events']}
    assert len(records) == 12
    rows = []
    for scenario in ('interrupt','drag-release','reverse'):
        for width in (768,1280):
            sid = f'{scenario}-w{width}'
            a,b = (records[f'{sid}-r{r}'] for r in (1,2))
            assert a['source_sha256'] == b['source_sha256']
            assert a['scenario']['repeat'] == 1 and b['scenario']['repeat'] == 2
            assert {k:v for k,v in a['scenario'].items() if k not in {'id','repeat'}} == {k:v for k,v in b['scenario'].items() if k not in {'id','repeat'}}
            for reverse in (False,True):
                ref,target = (b,a) if reverse else (a,b)
                contract = _contract(ref,target)
                pixel = compare_observations(ref['observations'],target['observations'],contract)
                dynamics = _dynamic_checks(ref,target,contract)
                relative = _relative_timing_checks(ref,target,contract)
                rows.append({'scenario':sid, 'direction':'r2-to-r1' if reverse else 'r1-to-r2',
                    'pass':pixel['success'] and all(x['pass'] for x in dynamics if x.get('observable')) and all(x['pass'] for x in relative),
                    'measurement_pass':pixel['measurement_status'] == 'pass',
                    'failed_windows':[{k:x[k] for k in ('reference_id','window_id','gates','position_rmse','size_rmse','scale_px','limits')}
                        for x in pixel['object_windows'] if not x['pass']],
                    'failed_dynamics':[x for x in dynamics if x.get('observable') and not x['pass']],
                    'failed_relative_timing':[x for x in relative if not x['pass']]})
    assert all(file_sha256(path) == sha for path,sha in hashes.items())
    result = {'kind':'StoredReferenceRepeatDiagnostic', 'domain':'panel',
        'passed':sum(r['pass'] for r in rows),'required':len(rows),'rows':rows,
        'evidence_hashes':hashes,'producer_sha256':file_sha256(__file__),
        'comparator_sha256':file_sha256(ROOT/'kiroclops/local_engine/evaluation.py'),
        'independent_repeat_recordings':True,'recaptured':False,'model_run':False,
        'operator_feedback_to_kiro':False,'primary_outcome_modified':False,
        'scope':'Supplementary stored-observation comparison. No new image accuracy review or replacement of the original reference gate.'}
    write_json(output,result)
    print({'passed':result['passed'],'required':len(rows),'failures':[r for r in rows if not r['pass']]})


if __name__ == '__main__':
    main()
