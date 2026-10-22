"""Magnified 1:1 mirrors of the contact sheets of a canonical ground normal raw review.

The normal review (``scripts/phase3d_v2_ground_normal_raw.py``) has no contact
selection file, so ``phase3d_v3_raw_review_zoom.py`` cannot read it. Its tiles
are the unique (image, point) pairs of each route's selected frames, in tile
order, 12 per sheet. This tool mirrors each sheet tile by tile with the same
cells: a native ±18 px crop around the independent point at 7x, next to the same
crop with a high-contrast cross. A frame where the reader found no single point
gets a "NO INDEPENDENT POINT" cell.

Post-hoc inspection aid (10 October 2026). It reads the review and the raw
frames only, re-hashing every frame against the review's own frame hashes, and
writes a new folder. It changes no review file, point, sheet or verdict.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.package import read_image  # noqa: E402
from scripts.phase3d_v3_game_review_zoom import WINDOW, ZOOM  # noqa: E402
from scripts.phase3d_v3_raw_review_zoom import _cell, _sha  # noqa: E402


def tiles(record):
    """The normal review's rendering plan: the first selected row of each tile, in tile order."""
    first = {}
    for row in record['selected_images']:
        first.setdefault(row['tile_index'], row)
    if sorted(first) != list(range(len(first))) or len(first) != record['unique_tile_count']:
        raise SystemExit(f"Tile plan is not contiguous: {record['route']}")
    return [first[index] for index in range(len(first))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {args.output}')
    result = json.loads((args.review / 'result.json').read_text(encoding='utf-8'))
    if result.get('kind') != 'UnsignedCanonicalGroundNormalRawAid':
        raise SystemExit('Not a canonical ground normal raw review.')
    plan = [(record, page, rows[page * 12:page * 12 + 12])
            for record in result['records'] for rows in [tiles(record)] for page in range((len(rows) + 11) // 12)]
    sheets = {sheet['path']: sheet['sha256'] for sheet in result['sheets']}
    if [f"sheets/{record['route']}-{page:02}.png" for record, page, _ in plan] != list(sheets):
        raise SystemExit('The sheets do not match the normal rendering plan.')
    args.output.mkdir(parents=True)
    pages, inputs = [], {}
    for record, page, rows in plan:
        cells = []
        for row in rows:
            path = Path(record['raw_run']) / 'frames' / f"{row['index']:06d}.png"
            if _sha(path) != row['image_sha256'] or record['frames'][row['index']]['image_sha256'] != row['image_sha256']:
                raise SystemExit(f"Raw frame bytes missing or changed: {record['route']} f{row['index']}")
            inputs[str(path)] = row['image_sha256']
            cells.append(_cell(read_image(path), row['index'], row['point'], []))
        cells.extend(np.full_like(cells[0], 24) for _ in range((-len(cells)) % 3))
        image = np.vstack([np.hstack(cells[i:i + 3]) for i in range(0, len(cells), 3)])
        sheet = f"sheets/{record['route']}-{page:02}.png"
        if _sha(args.review / sheet) != sheets[sheet]:
            raise SystemExit(f'Sheet bytes changed: {sheet}')
        relative = f"{record['route']}-{page:02}-zoom.png"
        cv2.imencode('.png', image)[1].tofile(str(args.output / relative))
        pages.append({'path': relative, 'mirrors_sheet': sheet, 'mirrored_sheet_sha256': sheets[sheet],
                      'frames': [row['index'] for row in rows], 'sha256': _sha(args.output / relative)})
    manifest = {'kind': 'MagnifiedRawReviewPages', 'schema_version': 1, 'review': str(args.review),
                'review_result_sha256': _sha(args.review / 'result.json'),
                'evidence_receipt_sha256': _sha(args.review / 'evidence-receipt.json'),
                'producer_sha256': _sha(Path(__file__)), 'window_native_px': WINDOW, 'zoom': ZOOM,
                'pages': pages, 'inputs_sha256': inputs,
                'scope': 'Inspection aid only; it does not change the review, its points or its verdict.'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'pages': len(pages), 'frames': sum(len(p['frames']) for p in pages), 'sheets': len(sheets)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
