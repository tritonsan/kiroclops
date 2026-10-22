"""Magnified 1:1 mirrors of every contact sheet in any game raw-review folder.

This generalizes ``phase3d_v3_game_review_zoom.py`` (calibration only) to final
and normal game reviews, where packages live in session job folders.

- Raw frames are located by their exact byte hash in the review's own
  ``evidence-receipt.json`` source inventory, and re-hashed on read.
- Each sheet page is mirrored with the same records, frames and order.
- Each cell shows a native ±18 px crop around the independent point at 7x,
  next to the same crop with a high-contrast cross at the exact sub-pixel point.
- An independent wall edge is drawn as a cyan line where it crosses the crop,
  and its endpoints as cyan rings.

It reads the review and the raw frames only and writes a new sibling folder. It
changes no review file, point, sheet or verdict.
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

from kiroclops.visual.package import read_image  # noqa: E402
from scripts.phase3d_v3_game_review_zoom import CELL, WINDOW, ZOOM, _crop  # noqa: E402


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _to_cell(value, x0, y0):
    return int(round((value[0] - x0 + .5) * ZOOM)), int(round((value[1] - y0 + .5) * ZOOM))


def _cell(image, frame_index, point, edges):
    cell = np.full((CELL + 26, 2 * CELL + 30, 3), 24, np.uint8)
    if point is None or 'x' not in point:
        cv2.putText(cell, f'f{frame_index} NO INDEPENDENT POINT', (8, 18), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 255), 1)
        return cell
    raw, (x0, y0) = _crop(image, point['x'], point['y'])
    marked = raw.copy()
    for edge in edges:
        (sx, sy), (ex, ey) = _to_cell(edge['start'], x0, y0), _to_cell(edge['end'], x0, y0)
        cv2.line(marked, (sx, sy), (ex, ey), (255, 255, 0), 1)
        for end in ((sx, sy), (ex, ey)):
            if 0 <= end[0] < CELL and 0 <= end[1] < CELL:
                cv2.circle(marked, end, 6, (255, 255, 0), 2)
    px, py = _to_cell((point['x'], point['y']), x0, y0)
    for color, thickness in (((0, 0, 0), 5), ((255, 0, 255), 2)):
        cv2.line(marked, (px - 22, py), (px + 22, py), color, thickness)
        cv2.line(marked, (px, py - 22), (px, py + 22), color, thickness)
    label = f"f{frame_index}  point ({point['x']:.1f}, {point['y']:.1f})  {point.get('feature', '')}"
    if edges:
        label += '  wall-edge'
    cv2.putText(cell, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, .45, (240, 240, 240), 1)
    cell[26:26 + CELL, 10:10 + CELL] = raw
    cell[26:26 + CELL, 20 + CELL:20 + 2 * CELL] = marked
    return cell


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {args.output}')
    result = json.loads((args.review / 'result.json').read_text(encoding='utf-8'))
    selection = json.loads((args.review / 'contact-selection.json').read_text(encoding='utf-8'))
    receipt = json.loads((args.review / 'evidence-receipt.json').read_text(encoding='utf-8'))
    by_hash = {}
    for path, digest in receipt['source_files'].items():
        if path.lower().endswith('.png'):
            by_hash.setdefault(digest, path)
    frames = {}
    for item in selection['selected_images']:
        frames.setdefault(item['record_id'], []).append(item)
    args.output.mkdir(parents=True)
    pages, inputs = [], {}
    rows_cache = {}
    for sheet in result['sheets']:
        record_id, page = Path(sheet['path']).stem.rsplit('-', 1)
        items = sorted(frames[record_id], key=lambda value: value['index'])[int(page) * 12:int(page) * 12 + 12]
        if record_id not in rows_cache:
            points_path = args.review / 'raw' / record_id / 'points.json'
            rows_cache[record_id] = json.loads(points_path.read_text(encoding='utf-8'))['rows']
            inputs[str(points_path)] = _sha(points_path)
        rows = rows_cache[record_id]
        cells = []
        for item in items:
            path = by_hash.get(item['image_sha256'])
            if path is None or _sha(path) != item['image_sha256']:
                raise SystemExit(f"Raw frame bytes missing or changed: {record_id} f{item['index']}")
            inputs[path] = item['image_sha256']
            row = rows[item['index']]
            point = next(iter(row['points'].values()), None)
            edges = [edge for edge in row.get('connections', {}).values() if 'start' in edge and 'end' in edge]
            cells.append(_cell(read_image(path), item['index'], point, edges))
        if not cells:
            raise SystemExit(f"Sheet without frames: {sheet['path']}")
        cells.extend(np.full_like(cells[0], 24) for _ in range((-len(cells)) % 3))
        image = np.vstack([np.hstack(cells[i:i + 3]) for i in range(0, len(cells), 3)])
        relative = f'{record_id}-{page}-zoom.png'
        cv2.imencode('.png', image)[1].tofile(str(args.output / relative))
        pages.append({'path': relative, 'mirrors_sheet': sheet['path'], 'mirrored_sheet_sha256': sheet['sha256'],
                      'frames': [item['index'] for item in items], 'sha256': _sha(args.output / relative)})
    manifest = {'kind': 'MagnifiedRawReviewPages', 'schema_version': 1, 'review': str(args.review),
                'review_result_sha256': _sha(args.review / 'result.json'),
                'contact_selection_sha256': _sha(args.review / 'contact-selection.json'),
                'evidence_receipt_sha256': _sha(args.review / 'evidence-receipt.json'),
                'producer_sha256': _sha(Path(__file__)), 'window_native_px': WINDOW, 'zoom': ZOOM,
                'pages': pages, 'inputs_sha256': inputs,
                'scope': 'Inspection aid only; it does not change the review, its points or its verdict.'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'pages': len(pages), 'frames': sum(len(p['frames']) for p in pages),
                      'sheets': len(result['sheets'])}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
