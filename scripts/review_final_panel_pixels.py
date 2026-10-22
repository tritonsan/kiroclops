"""Diagnose independent panel marks, and bind only actually viewed aids.

No threshold, measurement or session is edited. The unsigned diagnosis is not
a visual acceptance receipt. Completion requires all declared crop/event pages.
"""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.review import evaluate_review, evaluate_scope_review


def run(review, aid, complete=False, scope_root=None):
    review, aid = Path(review).resolve(), Path(aid).resolve()
    scope_root = Path(scope_root).resolve() if scope_root else aid
    scope_root.relative_to(aid)  # All visual pages must share the inspection journal.
    prepared = read_json(review / 'preparation.json')
    assert digest(prepared) == read_json(review / 'preparation-binding.json')['sha256']
    assert file_sha256(review / 'preparation.json') == read_json(aid / 'preparation.json')['source_preparation_sha256']
    records = prepared['fingerprint']['records']
    entries = {e['directory']: e for e in prepared['requests']}
    assert all(r['mechanic'] == 'panel' for r in records)
    # Existing board renderer receives only raw pixel curves and scope drafts.
    if not (scope_root / 'event-board-index.json').exists():
        write_json(scope_root / 'inventory.json', {'records': records})
        import panel_completion_boards as boards
        boards.R, boards.RAW = scope_root, aid / 'raw'
        boards.run()
    required = sorted((aid / 'raw').glob('position-*.png'))
    required += sorted((scope_root / 'event-boards').glob('*.png'))
    required += [aid / 'raw' / r['id'] / 'geometry-01.png' for r in records]
    rows = {}
    manifest = read_json(aid / 'raw/manifest.json')
    for p in manifest['proposals']:
        assert file_sha256(p['path']) == p['sha256']
        raw = read_json(p['path'])
        rows.update({(raw['record_id'], r['index']): r for r in raw['rows']})
    req = read_json(review / 'positions/panel/request.json')
    points = []
    for t in req['tasks']:
        row = rows[t['record_id'], t['index']]
        assert row['image_sha256'] == t['image_sha256']
        point = row['points'][t['object_id']]
        points.append({k: t[k] for k in ('record_id', 'object_id', 'index', 'image_sha256')} |
                      {'x': point['x'], 'y': point['y']})
    common = {'reviewer': 'Codex assistant', 'status': 'unsigned', 'external_human_review': False}
    annotations = [('positions/panel', common | {'request_sha256': digest(req),
        'method': 'independent_raw_image_marking', 'points': points,
        'method_detail': 'Independent foreground color/alpha-mass estimates from raw PNGs. No tracker/DOM coordinates as marking input. Computer-assisted inspection, not individual manual point clicks.'}, evaluate_review)]
    failures = []
    for r in read_json(scope_root / 'drafts/summary.json')['records']:
        counts = [c for c in r['event_counts'] if c['required'] != c['independent']]
        if counts:
            failures.append({'record': r['record'], 'event_counts': counts})
    for rec in records:
        for scope in ('events', 'relationships'):
            draft = read_json(scope_root / 'drafts' / rec['id'] / (scope + '.json'))
            checks = []
            for c in draft['checks']:
                value = c.get('independent_observation')
                checks.append({'id': c['id'], 'verdict': 'pass' if value is not None else 'fail',
                    'frame_sha256s': c['frame_sha256s'],
                    'evidence': 'Independent full raw pixel sequence estimate; actual crop, geometry and event page inspection is separately required for completion.',
                    ('observed_interval_s' if scope == 'events' else 'observed'): value})
            relative = f"records/{rec['id']}/{scope}"
            annotations.append((relative, common | {'request_sha256': draft['request_sha256'],
                'method': 'independent_raw_sequence_review', 'checks': checks}, evaluate_scope_review))
    results = {}
    # Completion only emits annotations for the mandatory frozen check. Reuse
    # the unsigned diagnosis solely when every exact annotation digest agrees;
    # the checker still revalidates current raw files and source bindings.
    cached = read_json(scope_root / 'unsigned-numeric-diagnosis.json') if complete else None
    if cached is not None:
        assert cached['review_preparation_sha256'] == file_sha256(review / 'preparation.json')
        assert cached['numeric_agreement'] == 'pass'
    for path, marks, fn in annotations:
        assert digest(read_json(review / path / 'request.json')) == entries[path]['request_sha256'] == marks['request_sha256']
        if cached is None:
            result = fn(review / path, marks, persist=False)
        else:
            result = cached['results'][path]
            assert result['annotation_sha256'] == digest(marks)
            assert result['request_sha256'] == marks['request_sha256']
        results[path] = result
        if result['status'] != 'pass':
            failures.append({'directory': path, 'result': result})
    summary = {'status': 'unsigned', 'numeric_agreement': 'fail' if failures else 'pass',
        'failures': failures, 'results': results, 'review_preparation_sha256': file_sha256(review / 'preparation.json'),
        'required_visual_files': [p.relative_to(aid).as_posix() for p in required]}
    if not complete:
        write_json(scope_root / 'unsigned-numeric-diagnosis.json', summary)
        return {'status': 'unsigned', 'disagreeing_scopes': len(failures), 'required_sheets': len(required), 'position_points': len(points)}
    assert not failures, 'Independent marks disagree; inspect and preserve diagnosis'
    viewed = {r['file']: r['sha256'] for r in read_json(aid / 'visual-inspection-progress.json')['viewed']}
    for p in required:
        assert viewed.get(p.relative_to(aid).as_posix()) == file_sha256(p), str(p)
    out = review / 'completed-annotations'
    assert not out.exists(), 'Existing annotations are immutable'
    for path, marks, fn in annotations:
        marks.update(status='completed', inspection_journal_sha256=file_sha256(aid / 'visual-inspection-progress.json'))
        write_json(out / path / 'annotation.json', marks)
    return {'status': 'annotations_completed_check_required', 'output': str(out), 'scopes': len(annotations)}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--aid', type=Path, required=True)
    p.add_argument('--complete', action='store_true')
    p.add_argument('--scope-root', type=Path, help='Fresh versioned drafts and event boards inside the aid folder; preserves earlier diagnoses.')
    a = p.parse_args()
    print(run(a.review, a.aid, a.complete, a.scope_root), flush=True)
