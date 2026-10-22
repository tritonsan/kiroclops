"""Bind completed raw-image inspection to a frozen selected SVG candidate kit.

This only writes review annotations. The frozen checker must still validate all
scopes, source evidence and candidate identity; no acceptance badge is granted.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest


def run(review, aid):
    review, aid = Path(review).resolve(), Path(aid).resolve()
    prepared = read_json(review/'preparation.json')
    assert digest(prepared) == read_json(review/'preparation-binding.json')['sha256']
    inventory = read_json(aid/'inventory.json')
    assert inventory['source_preparation_sha256'] == file_sha256(review/'preparation.json')
    assert inventory['candidate_sha256'] == prepared['candidate_sha256']
    assert read_json(aid/'unsigned-ready.json')['inventory_sha256'] == file_sha256(aid/'inventory.json')
    requested = {e['directory']: e for e in prepared['requests']}
    viewed = {r['file']: r['sha256'] for r in read_json(aid/'visual-inspection-progress.json')['viewed']}
    required = list((aid/'raw').glob('points-*.png'))
    required += list((aid/'event-boards').glob('*/events-*.png'))
    required += list(aid.glob('lifecycle-*.png'))
    required += list((aid/'endpoint-inspection').glob('*.png'))
    for rec in prepared['fingerprint']['records']:
        assert rec['mechanic'] == 'svg'
        recipe = rec['origins'][0]['scenario']['recipe']
        page = '02' if recipe == 'five-to-six-drag' else '01'
        required.append(aid/'raw'/rec['id']/f'geometry-{page}.png')
    for path in required:
        assert viewed.get(path.relative_to(aid).as_posix()) == file_sha256(path), f'Not inspected: {path}'
    lifetime = read_json(aid/'lifecycle-check.json')
    assert lifetime['complete'] and lifetime['all_pass']
    assert read_json(aid/'agreement.json')['complete']
    manual_path = aid/'manual-relationship-marks.json'
    manual = read_json(manual_path) if manual_path.exists() else {'status': 'complete', 'marks': []}
    assert manual['status'] == 'complete'
    marks = {(m['record_id'], m['task_id']): m for m in manual['marks']}
    assert len(marks) == len(manual['marks'])
    used = set()
    raw = read_json(aid/'raw/position-proposals.json')
    req = read_json(review/'positions/svg/request.json')
    assert raw['request_sha256'] == digest(req) == requested['positions/svg']['request_sha256']
    common = {'reviewer': 'Codex assistant', 'status': 'completed', 'external_human_review': False,
              'inspection_journal_sha256': file_sha256(aid/'visual-inspection-progress.json')}
    annotations = {'positions/svg': common | {'request_sha256': digest(req),
        'method': 'independent_raw_image_marking', 'points': raw['points'],
        'method_detail': 'Independent raw glyph alpha-mass estimates; all declared point crop sheets actually viewed. No tracker/DOM coordinates as marking input. Computer-assisted review, not individual manual point clicks.'}}
    for record in inventory['records']:
        assert file_sha256(record['record_path']) == record['record_sha256']
        name = record['id']
        for difference in read_json(aid/'records'/name/'disagreements.json'):
            assert difference['scope'] == 'relationships' and (name, difference['id']) in marks, difference
        for scope in ('events', 'relationships'):
            relative = f'records/{name}/{scope}'
            draft = read_json(aid/relative/'draft.json')
            req = read_json(review/relative/'request.json')
            assert draft['request_sha256'] == digest(req) == requested[relative]['request_sha256']
            assert draft['raw_sha256'] == file_sha256(aid/'raw'/name/'proposals.json')
            checks = []
            for item in draft['checks']:
                value = item['observed']
                key = name, item['id']
                evidence = 'Independent raw pixel sequence estimates; actual event/lifetime pages and one raw/overlay geometry sequence per recording inspected. Numerical disagreements require separate native-pixel evidence.'
                if key in marks:
                    mark = marks[key]
                    assert item['frame_sha256s'] == [mark['image_sha256']]
                    value, evidence = mark['observed'], mark['evidence']
                    used.add(key)
                else:
                    assert item['agreement'], item
                checks.append({'id': item['id'], 'verdict': 'pass', 'evidence': evidence,
                    'frame_sha256s': item['frame_sha256s'],
                    ('observed_interval_s' if scope == 'events' else 'observed'): value})
            annotations[relative] = common | {'request_sha256': digest(req),
                'method': 'independent_raw_sequence_review', 'checks': checks}
    assert used == set(marks) and set(annotations) == set(requested)
    output = review/'completed-annotations'
    assert not output.exists(), 'Preserve previous annotations'
    for relative, annotation in annotations.items():
        write_json(output/relative/'annotation.json', annotation)
    receipt = {'status': 'annotations_completed_frozen_check_required',
        'review_sha256': file_sha256(review/'preparation.json'), 'position_points': len(raw['points']),
        'scopes': len(annotations), 'inspected_pages': len(required), 'manual_marks': len(marks),
        'acceptance_granted': False, 'external_human_review': False}
    write_json(aid/'annotation-binding.json', receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', required=True, type=Path)
    parser.add_argument('--aid', required=True, type=Path)
    args = parser.parse_args()
    print(run(args.review, args.aid), flush=True)
