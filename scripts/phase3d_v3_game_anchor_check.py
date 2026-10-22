"""Machine check that every contact-sheet point is the declared physical anchor.

For each selected sheet frame this re-derives the anchor from the hash-verified
raw PNG with the frozen review helper, and checks that the reviewed point equals
it and sits at the declared fixed offset from the detected feature corner:
Tiny Crate (228x128) pink hat corner + (1, 2); Heart (320x180) skull top-row
corner + (3.5, 3). It reads the review and raw frames only and writes a new
folder; it changes no review file, point or verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.package import frame_paths, load_package, read_image  # noqa: E402
from scripts.prepare_game_raw_review_aid import anchor  # noqa: E402

OFFSETS = {(228, 128): ('pink_hat_top_left', (1., 2.)), (320, 180): ('white_head_top_left', (3.5, 3.))}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--preparation', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {args.output}')
    selection = json.loads((args.review / 'contact-selection.json').read_text(encoding='utf-8'))
    calibration = json.loads((args.preparation / 'calibration.json').read_text(encoding='utf-8'))
    packages = {item['source_role'] + '-' + item['case_id']: item['record']['package'] for item in calibration['records']}
    rows_by_record, paths_by_record, failures, checked = {}, {}, [], []
    for item in selection['selected_images']:
        record_id, index = item['record_id'], item['index']
        if record_id not in rows_by_record:
            package = packages[record_id]
            manifest = load_package(package)
            paths_by_record[record_id] = (frame_paths(package, manifest), manifest)
            rows_by_record[record_id] = json.loads(
                (args.review / 'raw' / record_id / 'points.json').read_text(encoding='utf-8'))['rows']
        paths, manifest = paths_by_record[record_id]
        path = paths[index]
        digest = _sha(path)
        problem = []
        if digest != item['image_sha256'] or digest != manifest['frames'][index]['sha256']:
            problem.append('raw_frame_hash')
        image = read_image(path)
        size = (image.shape[1], image.shape[0])
        found = anchor(image)
        points = list(rows_by_record[record_id][index]['points'].values())
        point = points[0] if len(points) == 1 else None
        feature, offset = OFFSETS.get(size, (None, None))
        if feature is None:
            problem.append('unknown_frame_size')
        if len(found) != 1:
            problem.append('anchor_not_unique')
        if point is None:
            problem.append('no_single_review_point')
        if not problem:
            proposal = found[0]
            if (point['x'], point['y'], point['feature'], list(point['feature_xy'])) != (
                    proposal['x'], proposal['y'], proposal['feature'], list(proposal['feature_xy'])):
                problem.append('point_differs_from_rederived_anchor')
            if point['feature'] != feature:
                problem.append('feature_name')
            if (point['x'] - point['feature_xy'][0], point['y'] - point['feature_xy'][1]) != offset:
                problem.append('declared_offset')
        entry = {'record_id': record_id, 'index': index, 'image_sha256': digest, 'size': list(size),
                 'point': point, 'problems': problem}
        checked.append(entry)
        if problem:
            failures.append(entry)
    args.output.mkdir(parents=True)
    result = {'kind': 'GameAnchorDerivationCheck', 'schema_version': 1, 'review': str(args.review),
              'review_result_sha256': _sha(args.review / 'result.json'),
              'contact_selection_sha256': _sha(args.review / 'contact-selection.json'),
              'helper_sha256': _sha(Path(__file__).resolve().parents[0] / 'prepare_game_raw_review_aid.py'),
              'producer_sha256': _sha(Path(__file__)),
              'declared_offsets': {f'{w}x{h}': {'feature': f, 'offset_px': list(o)} for (w, h), (f, o) in OFFSETS.items()},
              'frames_checked': len(checked), 'failures': failures, 'status': 'pass' if checked and not failures else 'fail',
              'frames': checked,
              'scope': 'Post-hoc diagnostic for the calibration raw review; it does not change the review or its verdict.'}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'frames_checked': len(checked), 'failures': len(failures),
                      'sizes': sorted({tuple(e['size']) for e in checked})}))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
