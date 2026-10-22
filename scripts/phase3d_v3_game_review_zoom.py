"""Magnified inspection pages for a generic game raw-review aid.

The contact sheets of ``suite_generic_raw_review`` draw a one-pixel cross at
native resolution and then shrink each frame with nearest-neighbour sampling.
For the 320x180 Heart frames that can drop the marker rows or columns, so a
reviewer cannot always see the independent point on the sheet. This aid
re-renders every selected sheet frame from its hash-verified raw PNG: a native
crop around the independent point, magnified, next to the same crop with a
high-contrast cross at the exact sub-pixel point.

It writes only to a new sibling folder. The review folder, its sheets, points
and verdicts are read, never changed. The pages mirror the sheet pagination
(same records, same frames, twelve per page).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.package import frame_paths, load_package, read_image  # noqa: E402

WINDOW = 18          # native pixels on each side of the point
ZOOM = 7
CELL = (2 * WINDOW + 1) * ZOOM


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _crop(image, x, y):
    height, width = image.shape[:2]
    cx, cy = int(round(x)), int(round(y))
    canvas = np.zeros((2 * WINDOW + 1, 2 * WINDOW + 1, 3), np.uint8)
    x0, y0 = cx - WINDOW, cy - WINDOW
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(width, cx + WINDOW + 1), min(height, cy + WINDOW + 1)
    if sx1 > sx0 and sy1 > sy0:
        canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = image[sy0:sy1, sx0:sx1]
    return cv2.resize(canvas, (CELL, CELL), interpolation=cv2.INTER_NEAREST), (x0, y0)


def _cell(image, frame_index, point):
    if point is None or 'x' not in point:
        blank = np.full((CELL + 26, 2 * CELL + 30, 3), 24, np.uint8)
        cv2.putText(blank, f'f{frame_index} NO INDEPENDENT POINT', (8, 18), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 255), 1)
        return blank
    raw, (x0, y0) = _crop(image, point['x'], point['y'])
    marked = raw.copy()
    px = int(round((point['x'] - x0 + .5) * ZOOM))
    py = int(round((point['y'] - y0 + .5) * ZOOM))
    for color, thickness in (((0, 0, 0), 5), ((255, 0, 255), 2)):
        cv2.line(marked, (px - 22, py), (px + 22, py), color, thickness)
        cv2.line(marked, (px, py - 22), (px, py + 22), color, thickness)
    cell = np.full((CELL + 26, 2 * CELL + 30, 3), 24, np.uint8)
    cv2.putText(cell, f"f{frame_index}  point ({point['x']:.1f}, {point['y']:.1f})  {point.get('feature', '')}",
                (8, 18), cv2.FONT_HERSHEY_SIMPLEX, .45, (240, 240, 240), 1)
    cell[26:26 + CELL, 10:10 + CELL] = raw
    cell[26:26 + CELL, 20 + CELL:20 + 2 * CELL] = marked
    return cell


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
    ordered = {}
    for item in selection['selected_images']:
        ordered.setdefault(item['record_id'], []).append(item)
    args.output.mkdir(parents=True)
    pages, inputs = [], {}
    for record_id, items in ordered.items():
        package = packages[record_id]
        manifest = load_package(package)
        paths = frame_paths(package, manifest)
        points_path = args.review / 'raw' / record_id / 'points.json'
        rows = json.loads(points_path.read_text(encoding='utf-8'))['rows']
        inputs[str(points_path)] = _sha(points_path)
        cells = []
        for item in sorted(items, key=lambda value: value['index']):
            index = item['index']
            path = paths[index]
            digest = _sha(path)
            if digest != item['image_sha256'] or digest != manifest['frames'][index]['sha256']:
                raise SystemExit(f'Raw frame bytes changed: {record_id} f{index}')
            inputs[str(path)] = digest
            point = next(iter(rows[index]['points'].values()), None)
            cells.append(_cell(read_image(path), index, point))
        for page, start in enumerate(range(0, len(cells), 12)):
            batch = cells[start:start + 12]
            batch.extend(np.full_like(cells[0], 24) for _ in range((-len(batch)) % 3))
            sheet = np.vstack([np.hstack(batch[i:i + 3]) for i in range(0, len(batch), 3)])
            relative = f'{record_id}-{page:03}-zoom.png'
            cv2.imencode('.png', sheet)[1].tofile(str(args.output / relative))
            pages.append({'path': relative, 'mirrors_sheet': f'sheets/{record_id}-{page:03}.png',
                          'frames': [item['index'] for item in sorted(items, key=lambda v: v['index'])][start:start + 12],
                          'sha256': _sha(args.output / relative)})
    manifest = {'kind': 'MagnifiedGameReviewPages', 'schema_version': 1, 'review': str(args.review),
                'review_result_sha256': _sha(args.review / 'result.json'),
                'contact_selection_sha256': _sha(args.review / 'contact-selection.json'),
                'producer_sha256': _sha(Path(__file__)), 'window_native_px': WINDOW, 'zoom': ZOOM,
                'pages': pages, 'inputs_sha256': inputs,
                'scope': 'Inspection aid only; it does not change the review, its points or its verdict.'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'pages': len(pages), 'frames': sum(len(p['frames']) for p in pages)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
