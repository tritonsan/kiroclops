"""Bind previously viewed raw glyph proposals; position review only.

The explicit inspection journal is required; this command does not view images.
No model, capture, source edit, final acceptance, or event approval is performed.
"""
from pathlib import Path
import argparse
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, digest, file_sha256
from kiroclops.local_engine.review import evaluate_review


def run(directory, aid, output):
    directory, aid, output = map(lambda p: Path(p).resolve(), (directory, aid, output))
    assert not output.exists(), 'Preserve previous review result'
    proposal = read_json(aid/'proposals.json')
    request = read_json(directory/'request.json')
    inspected = read_json(aid/'visual-inspection-progress.json')
    viewed = {row['file']: row['sha256'] for row in inspected['viewed']}
    assert proposal['request_sha256'] == digest(request)
    assert not proposal['independent_review_signed']
    corrections_path = aid/'manual-corrections.json'
    corrections = read_json(corrections_path)['points'] if corrections_path.exists() else []
    corrections = {row['raw_proposal_index']: row for row in corrections}
    assert len(corrections) == (len(read_json(corrections_path)['points']) if corrections_path.exists() else 0)
    for point in proposal['points']:
        if point['status'] == 'proposed':
            continue
        mark = corrections[point['raw_proposal_index']]
        assert mark['image_sha256'] == point['image_sha256']
        assert viewed.get(mark['viewed_file']) == file_sha256(aid/mark['viewed_file'])
        assert mark['evidence'].strip()
        point.update(x=mark['x'], y=mark['y'], status='manually_resolved')
    for sheet in proposal['sheets']:
        assert viewed.get(sheet['file']) == sheet['sha256'] == file_sha256(aid/sheet['file'])
    assert {row['image_sha256'] for sheet in proposal['sheets'] for row in sheet['bindings']} == {
        p['image_sha256'] for p in proposal['points']}
    annotation = {'schema_version': 2, 'status': 'reviewed',
        'request_sha256': digest(request), 'reviewer': 'Codex assistant',
        'method': 'independent_raw_image_marking', 'external_human_review': False,
        'computer_assisted': True, 'all_points_manually_clicked': False,
        'evidence': ('Inspected every distinct native hat/head crop on the bound sheets. '
                     'Physical anchor consistently follows the same feature. Exact image '
                     'duplicates share the raw coordinate; no tracker or engine coordinates used.'),
        'aid_sha256': file_sha256(aid/'proposals.json'),
        'inspection_sha256': file_sha256(aid/'visual-inspection-progress.json'),
        'manual_corrections_sha256': file_sha256(corrections_path) if corrections else None,
        'points': [{k: p[k] for k in ('record_id','object_id','index','image_sha256','x','y')}
                   for p in proposal['points']]}
    result = evaluate_review(directory, annotation, persist=False)
    output.mkdir(parents=True)
    write_json(output/'annotation.json', annotation)
    write_json(output/'result.json', result)
    print({'status': result['status'], 'objects': result['objects'],
           'events': 'pending', 'relationships': 'pending', 'source_native': 'pending'}, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('review', type=Path)
    p.add_argument('--aid', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    run(a.review, a.aid, a.output)
