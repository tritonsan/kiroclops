"""Numeric check that every contact-sheet tile carries its green marker at the independent point.

Post-hoc root check (11 October 2026) for the V4 final game raw review. At sheet
scale the 7-px marker cross shrinks to a few pixels, and the Haiku first pass
often could not see it. This tool reads only the review's own sheets, contact
selection and independent points. For every tile it:

- finds the right-hand (marked) frame as ``suite_generic_raw_review._tile``
  renders it: 230x600 tiles, frame at y=27 and x=305, scale min(290/w, 195/h),
  nearest-neighbour resize;
- maps the independent point into that frame;
- counts marker-green pixels (50, 255, 50) near the mapped point, elsewhere in
  the marked frame, and anywhere in the raw (left) frame;
- counts pure red (wall-edge ring) pixels on every sheet. Only wall records may
  have them.

It also re-renders every tile with the frozen ``suite_generic_raw_review._tile``
from the raw frame (re-hashed) and the independent point, then compares the
result with the sheet byte for byte.

The renderer resizes Heart frames by 0.906 with nearest-neighbour sampling, so
some source rows and columns are never sampled. When both the row and the column
of the 1-px cross fall on unsampled lines, the cross vanishes from the sheet
tile, though the point exists and the magnified page shows it.

A tile passes when it is identical to its fresh render and either has marker
pixels near its point and none anywhere else, or has no marker pixels at all
because both cross lines are unsampled. No game sprite uses that green. The tool
judges no image by eye and changes no review file.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import cv2
import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.suite_generic_raw_review import _tile  # noqa: E402
from kiroclops.visual.package import read_image  # noqa: E402
from kiroclops.visual.schema import file_sha256, read_json, write_json  # noqa: E402

GREEN = np.array([50, 255, 50], np.uint8)
RED = np.array([0, 0, 255], np.uint8)      # wall-edge rings (BGR)
TILE_H, TILE_W, TOP, LEFT, RIGHT_X, BOX_W, BOX_H = 230, 600, 27, 5, 305, 290, 195
NATIVE = {'white_head_top_left': (320, 180), 'pink_hat_top_left': (228, 128), 'pink_hat_top_right': (228, 128)}


def _unsampled(value, source, target):
    """True when OpenCV's nearest-neighbour resize from source to target never samples this line."""
    ramp = np.arange(source, dtype=np.float32)[None, :]
    return value not in set(cv2.resize(ramp, (target, 1), interpolation=cv2.INTER_NEAREST)[0].astype(int))


def check(review):
    review = Path(review)
    result = read_json(review / 'result.json')
    selection = read_json(review / 'contact-selection.json')
    tiles = {}
    for item in selection['selected_images']:
        tiles.setdefault(item['record_id'], []).append(item['index'])
    receipt = read_json(review / 'evidence-receipt.json')
    frames_by_hash = {sha: path for path, sha in receipt['source_files'].items() if path.lower().endswith('.png')}
    rows_cache, counts, failures, near_counts = {}, Counter(), [], Counter()
    identical = erased = 0
    red_outside_walls = {}
    for sheet in result['sheets']:
        if file_sha256(review / sheet['path']) != sheet['sha256']:
            raise SystemExit('Sheet bytes changed: ' + sheet['path'])
        record, page = Path(sheet['path']).stem.rsplit('-', 1)
        frames = sorted(tiles[record])[int(page) * 12:int(page) * 12 + 12]
        rows = rows_cache.setdefault(record, read_json(review / 'raw' / record / 'points.json')['rows'])
        image = read_image(review / sheet['path'])
        green = np.all(image == GREEN, axis=2)
        red = int(np.all(image == RED, axis=2).sum())
        if red and not record.startswith('boundary'):
            red_outside_walls[sheet['path']] = red
        for k, index in enumerate(frames):
            y0, x0 = (k // 3) * TILE_H, (k % 3) * TILE_W
            point = next(iter(rows[index]['points'].values()))
            width, height = NATIVE[point['feature']]
            scale = min(BOX_W / width, BOX_H / height)
            marked = green[y0 + TOP:y0 + TOP + round(height * scale), x0 + RIGHT_X:x0 + RIGHT_X + round(width * scale)]
            raw = green[y0 + TOP:y0 + TOP + round(height * scale), x0 + LEFT:x0 + LEFT + round(width * scale)]
            u, v = (round(point['x']) + .5) * scale - .5, (round(point['y']) + .5) * scale - .5
            reach = 3.5 * scale + 1.5
            ys, xs = np.nonzero(marked)
            near = int(np.sum((np.abs(xs - u) <= reach) & (np.abs(ys - v) <= reach)))
            far = len(xs) - near
            kind = point['feature']
            counts[kind, 'tiles'] += 1
            near_counts[kind, near] += 1
            source = frames_by_hash[rows[index]['image_sha256']]
            if file_sha256(source) != rows[index]['image_sha256']:
                raise SystemExit(f'Raw frame bytes changed: {record} f{index}')
            fresh = _tile(read_image(source), {'index': index, 'time_s': rows[index]['time_s']}, record, rows[index], 'game')
            same = bool(np.array_equal(fresh, image[y0:y0 + TILE_H, x0:x0 + TILE_W]))
            identical += same
            unsampled = _unsampled(round(point['x']), width, round(width * scale)) and                 _unsampled(round(point['y']), height, round(height * scale))
            erased += near == 0 and unsampled
            if not same or far or raw.any() or (near == 0 and not unsampled):
                failures.append({'sheet': sheet['path'], 'frame': index, 'feature': kind, 'near': near,
                                 'far': int(far), 'raw_green': int(raw.sum()), 'identical_render': same,
                                 'cross_lines_unsampled': unsampled})
    report = {'kind': 'V4FinalSheetMarkerCheck', 'schema_version': 1, 'review': str(review.resolve()),
              'review_result_sha256': file_sha256(review / 'result.json'),
              'contact_selection_sha256': file_sha256(review / 'contact-selection.json'),
              'producer_sha256': file_sha256(Path(__file__)), 'sheets': len(result['sheets']),
              'tiles': sum(v for (_, key), v in counts.items() if key == 'tiles'),
              'tiles_by_feature': {kind: v for (kind, key), v in counts.items() if key == 'tiles'},
              'near_marker_pixels_by_feature': {kind: {str(n): c for (k2, n), c in sorted(near_counts.items()) if k2 == kind}
                                                for kind in {k for k, _ in near_counts}},
              'tiles_identical_to_fresh_render': identical,
              'tiles_marker_erased_by_resize': erased,
              'red_pixels_on_non_wall_sheets': red_outside_walls,
              'failures': failures, 'status': 'pass' if not failures and not red_outside_walls else 'fail',
              'scope': 'Numeric marker presence only; visual review of the sheets and magnified pages is separate.'}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite: {args.output}')
    report = check(args.review)
    write_json(args.output, report)
    print(json.dumps({key: report[key] for key in ('status', 'sheets', 'tiles', 'tiles_by_feature',
                                                   'near_marker_pixels_by_feature', 'tiles_identical_to_fresh_render',
                                                   'tiles_marker_erased_by_resize')} | {'failures': len(report['failures']),
                                                   'non_wall_sheets_with_red': len(report['red_pixels_on_non_wall_sheets'])}))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
