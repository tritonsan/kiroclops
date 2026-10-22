"""Unsigned pixel-only bridge for the frozen selected panel review kit."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest


def run(review, output):
    review, output = Path(review).resolve(), Path(output).resolve()
    assert not output.exists(), 'Preserve prior aids'
    prepared = read_json(review/'preparation.json')
    assert digest(prepared) == read_json(review/'preparation-binding.json')['sha256']
    entries = {e['directory']: e for e in prepared['requests']}
    bridge = output/'scope-adapter'
    request = read_json(review/'positions/panel/request.json')
    assert digest(request) == entries['positions/panel']['request_sha256']
    write_json(bridge/'panel/positions/request.json', request)
    records = []
    for original in prepared['fingerprint']['records']:
        assert original['mechanic'] == 'panel'
        analysis = Path(original['analysis_path'])
        assert file_sha256(analysis) == prepared['fingerprint']['files'][str(analysis)]
        data = read_json(analysis)
        obs = data.get('observations')
        if obs is None:
            path = analysis.parent/'observations.json'
            assert file_sha256(path) == prepared['fingerprint']['files'][str(path)]
            obs = read_json(path)
        assert digest(obs) == original['observations_sha256']
        assert digest(obs['selection']) == original['selection_sha256']
        name = original['id']
        folder = bridge/'inputs'/name
        write_json(folder/'analysis.json', {'observations': obs})
        write_json(folder/'selection.json', obs['selection'])
        records.append({'id':name, 'domain':'panel', 'role':original['role'],
            'package':original['package'], 'package_sha256':original['package_sha256'],
            'observations_sha256':original['observations_sha256'],
            'analysis_path':str(folder/'analysis.json'), 'selection_sha256':original['selection_sha256']})
        for scope in ('events','relationships'):
            relative = f'records/{name}/{scope}'
            request = read_json(review/relative/'request.json')
            assert digest(request) == entries[relative]['request_sha256']
            write_json(bridge/'panel'/relative/'request.json', request)
    write_json(bridge/'preparation.json', {'records':records,
        'source_preparation_sha256':file_sha256(review/'preparation.json'),
        'candidate_sha256':prepared['candidate_sha256'], 'status':'adapter_only_not_reviewed'})
    import review_panel_raw09
    import prepare_panel_scope_raw10_segments
    raw = review_panel_raw09.run(bridge, output/'raw')
    scopes = prepare_panel_scope_raw10_segments.run(bridge, output/'raw', output/'drafts')
    receipt = {'status':'unsigned_not_reviewed', 'raw':raw, 'records':scopes,
        'unresolved':sum(r['unresolved'] for r in scopes),
        'source_preparation_sha256':file_sha256(review/'preparation.json'),
        'candidate_sha256':prepared['candidate_sha256'], 'producer_sha256':file_sha256(__file__)}
    write_json(output/'preparation.json', receipt)
    return receipt


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    result=run(a.review,a.output)
    print({'status':result['status'],'unresolved':result['unresolved']},flush=True)
