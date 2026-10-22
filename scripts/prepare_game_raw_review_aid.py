"""Unsigned raw-pixel review aid for the two inspected game illustrations.

Reads review identities, original PNGs, and the declared physical anchor only.
Never reads tracked positions or issues acceptance. A reviewer must view the
sheets, resolve ambiguity, and separately check events and relationships.
"""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest, safe_path
from kiroclops.visual.package import read_image


def anchor(image):
    height, width = image.shape[:2]
    found = []
    if (width, height) == (228, 128):
        # Visually inspected pink hat: five top pixels, six on next row.
        pink = np.all(image == np.array([168, 119, 255], dtype=np.uint8), axis=2)
        for y in range(height - 1):
            xs = np.flatnonzero(pink[y])
            for x in xs:
                if x > 0 and pink[y, x - 1]:
                    continue
                if (x + 6 < width and pink[y, x:x+5].all() and not pink[y, x+5]
                        and pink[y+1, x:x+6].all()):
                    found.append({'x': float(x+1), 'y': float(y+2),
                                  'feature': 'pink_hat_top_left', 'feature_xy': [int(x), y]})
    elif (width, height) == (320, 180):
        # Visually inspected skeleton head: 8 white top pixels, 9 or 10 below
        # depending on the walk pose. Exclude long, visibly solid wall columns
        # and the floor using original pixels, never measured boundary values.
        white = np.all(image == 255, axis=2)
        background = image[0, 0]
        occupied = np.any(image != background, axis=2)
        # Include the textured border column even when small gray holes break
        # its run. A 16px character cannot alone occupy 60 rows above the floor.
        wall_columns = occupied[:160].sum(axis=0) >= 60
        white[:, wall_columns] = False
        white[160:] = False
        for y in range(height - 1):
            xs = np.flatnonzero(white[y])
            for x in xs:
                if x == 0 or x+8 >= width or white[y, x-1]:
                    continue
                if (white[y, x:x+8].all() and not white[y, x+8]
                        and (y == 0 or not white[y-1, x:x+8].any())
                        and white[y+1, x:x+8].all()
                        and np.count_nonzero(white[y+1, x-1:x+9]) >= 8):
                    found.append({'x': float(x+3.5), 'y': float(y+3),
                                  'feature': 'white_head_top_left', 'feature_xy': [int(x), y]})
    return found


def run(review, output):
    review, output = Path(review).resolve(), Path(output).resolve()
    assert not output.exists(), 'Preserve previous review aids'
    request = read_json(review/'request.json')
    output.mkdir(parents=True)
    (output/'helper-source.py').write_bytes(Path(__file__).read_bytes())
    unique, points = {}, []
    for task in request['tasks']:
        raw = safe_path(review, task['image'])
        assert file_sha256(raw) == task['image_sha256']
        key = task['image_sha256']
        if key not in unique:
            image = read_image(raw)
            found = anchor(image)
            unique[key] = {'index': len(unique), 'image': str(raw),
                           'image_sha256': key, 'anchors': found,
                           'status': 'proposed' if len(found) == 1 else 'ambiguous',
                           'tasks': []}
        item = unique[key]
        identity = {k: task[k] for k in ('record_id', 'role', 'package_sha256',
                                       'object_id', 'index', 'image_sha256')}
        item['tasks'].append(identity)
        points.append({**identity, 'raw_proposal_index': item['index'],
                       'status': item['status'],
                       **(item['anchors'][0] if len(item['anchors']) == 1 else {})})
    rows = list(unique.values())
    sheets = []
    for offset in range(0, len(rows), 20):
        batch = rows[offset:offset+20]
        board = np.full((5*226, 4*250, 3), 28, np.uint8)
        bindings = []
        for j, row in enumerate(batch):
            raw = read_image(row['image'])
            bx, by = j % 4 * 250, j // 4 * 226
            if len(row['anchors']) == 1:
                a = row['anchors'][0]
                left, top = max(0, int(a['x'])-12), max(0, int(a['y'])-10)
                crop = raw[top:min(top+23, raw.shape[0]), left:min(left+25, raw.shape[1])]
                enlarged = cv2.resize(crop, None, fx=7, fy=7, interpolation=cv2.INTER_NEAREST)
                # Cross is raw-pixel aid output, never a production tracking value.
                cx, cy = round((a['x']-left+.5)*7), round((a['y']-top+.5)*7)
                cv2.drawMarker(enlarged, (cx, cy), (80, 200, 60), cv2.MARKER_CROSS, 11, 1)
                text = f"{a['x']:g}, {a['y']:g}"
            else:
                left = top = 0
                enlarged = cv2.resize(raw, (220, 140), interpolation=cv2.INTER_NEAREST)
                text = f"AMBIGUOUS {len(row['anchors'])}"
            h, w = enlarged.shape[:2]
            board[by+39:by+39+h, bx+8:bx+8+w] = enlarged
            for line, value in enumerate((f"raw#{row['index']} ({len(row['tasks'])} uses)", text)):
                cv2.putText(board, value, (bx+8, by+15+17*line),
                            cv2.FONT_HERSHEY_SIMPLEX, .41, (235,235,235), 1)
            bindings.append({'raw_proposal_index': row['index'],
                             'image_sha256': row['image_sha256'],
                             'sheet_region': [bx+8, by+39, w, h],
                             'native_crop_origin': [left, top],
                             'tasks': row['tasks']})
        name = f'raw-points-{len(sheets):03}.png'
        cv2.imencode('.png', board)[1].tofile(output/name)
        sheets.append({'file': name, 'sha256': file_sha256(output/name), 'bindings': bindings})
    result = {'kind': 'UnsignedIndependentGamePixelAid', 'status': 'unreviewed',
        'request_sha256': digest(request), 'helper_sha256': file_sha256(__file__),
        'method': 'Raw inspected hat/head glyph geometry, not tracked positions.',
        'truth_source': 'native PNG bytes only; no code/engine positions',
        'points': points, 'unique_images': rows, 'sheets': sheets,
        'ambiguous_points': sum(p['status'] != 'proposed' for p in points),
        'independent_review_signed': False}
    write_json(output/'proposals.json', result)
    print({'points': len(points), 'unique_images': len(rows),
           'sheets': len(sheets), 'ambiguous': result['ambiguous_points']}, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('review', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.review, args.output)
