"""Synthetic sub-pixel accuracy benchmark for the V6 peak refinement (PHASE4_V6_PLAN.md WP5.3).

A textured patch is placed at an integer position, then shifted by known
fractional offsets with bilinear resampling (the same model a renderer uses for
a translated raster), optionally with Gaussian noise and a gamma change. Each
pair is measured by ``analyze_package`` three ways: integer NCC peak, the
three-point parabola (``parabolic_v1``) and the symmetric upsampling
(``upsampled_v1``). The report tabulates bias and RMSE against the known shift
per condition. It states what the estimators do on this synthetic model; it is
not a claim about rendered frames, which ``scripts/v6_equivalence.py``
measures on real recordings.

    python scripts/v6_subpixel_benchmark.py --output artifacts/phase4-v6/subpixel-benchmark-01
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.package import create_package
from kiroclops.visual.schema import digest, write_json
from kiroclops.visual.tracking import analyze_package

SHIFTS = [(.0, .0), (.1, .0), (.2, .0), (.3, .0), (.4, .0), (.5, .0), (.25, -.25), (-.35, .15), (.45, .45), (-.1, -.4)]
NOISE_SIGMAS = [0., 2., 5.]
GAMMAS = [1., 1.15]
TEXTURES = ('smooth_random', 'card_with_text')
METHODS = (None, 'parabolic_v1', 'upsampled_v1')


def _patch(kind, rng):
    if kind == 'smooth_random':
        return cv2.GaussianBlur(rng.integers(0, 255, (14, 16, 3), dtype=np.uint8), (3, 3), 0)
    card = np.full((40, 64, 3), (236, 226, 214), np.uint8)
    cv2.rectangle(card, (2, 2), (61, 37), (120, 80, 40), 1, cv2.LINE_AA)
    cv2.putText(card, 'Ab7', (8, 27), cv2.FONT_HERSHEY_SIMPLEX, .7, (30, 30, 30), 1, cv2.LINE_AA)
    cv2.circle(card, (50, 20), 6, (60, 140, 220), -1, cv2.LINE_AA)
    return card


def _frames(patch, shift, sigma, gamma, rng, background):
    h, w = patch.shape[:2]
    base = background.copy()
    base[40:40+h, 30:30+w] = patch
    matrix = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
    moved = cv2.warpAffine(base, matrix, (base.shape[1], base.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    if gamma != 1.:
        table = np.array([255.*(i/255.)**gamma for i in range(256)]).astype(np.uint8)
        moved = cv2.LUT(moved, table)
    if sigma > 0:
        moved = np.clip(moved.astype(np.float32)+rng.normal(0, sigma, moved.shape), 0, 255).astype(np.uint8)
    return base, moved


def measure(root, frames, roi, method):
    directory = root/f'{abs(hash((str(roi), method, frames[1].tobytes()[:64]))):x}'
    directory.mkdir()
    paths = []
    for i, image in enumerate(frames):
        path = directory/f'{i}.png'
        path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
        paths.append(path)
    package = directory/'package'
    create_package(package, paths, [0., .04])
    obj = {'id': 'p', 'roi': list(roi), 'anchor': [0, 0], 'method': 'template', 'min_score': .2, 'min_margin': 0., 'search_radius': 8}
    if method:
        obj['peak_refinement'] = method
    result = analyze_package(package, {'schema_version': 1, 'objects': [obj]}, {'min_valid_ratio': 0.})
    frame = result['objects'][0]['frames'][1]
    return frame


def run(output, *, repeats=3):
    output = Path(output)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {output}')
    output.mkdir(parents=True)
    rng = np.random.default_rng(20261015)
    rows = []
    with tempfile.TemporaryDirectory(prefix='subpixel ') as temp:
        root = Path(temp)
        for texture in TEXTURES:
            patch = _patch(texture, rng)
            h, w = patch.shape[:2]
            roi = (30, 40, w, h)
            for sigma in NOISE_SIGMAS:
                for gamma in GAMMAS:
                    for shift in SHIFTS:
                        for repeat in range(repeats):
                            background = cv2.GaussianBlur(rng.integers(60, 200, (120, 160, 3), dtype=np.uint8), (7, 7), 0)
                            base, moved = _frames(patch, shift, sigma, gamma, rng, background)
                            for method in METHODS:
                                frame = measure(root, [base, moved], roi, method)
                                if not frame['valid']:
                                    rows.append({'texture': texture, 'sigma': sigma, 'gamma': gamma, 'shift': shift, 'method': method or 'integer',
                                                 'valid': False})
                                    continue
                                rows.append({'texture': texture, 'sigma': sigma, 'gamma': gamma, 'shift': shift, 'method': method or 'integer',
                                             'valid': True, 'error_x': frame['x']-(30+shift[0]), 'error_y': frame['y']-(40+shift[1]),
                                             'score': frame['score']})
    table = {}
    for row in rows:
        key = (row['texture'], row['sigma'], row['gamma'], row['method'])
        table.setdefault(key, []).append(row)
    summary = []
    for key, items in sorted(table.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], str(kv[0][3]))):
        valid = [r for r in items if r['valid']]
        errors = [math.hypot(r['error_x'], r['error_y']) for r in valid]
        summary.append({'texture': key[0], 'sigma': key[1], 'gamma': key[2], 'method': key[3], 'trials': len(items),
                        'valid': len(valid), 'rmse_px': math.sqrt(statistics.mean(e*e for e in errors)) if errors else None,
                        'max_px': max(errors) if errors else None,
                        'bias_x_px': statistics.mean(r['error_x'] for r in valid) if valid else None,
                        'bias_y_px': statistics.mean(r['error_y'] for r in valid) if valid else None})
    report = {'kind': 'V6SubpixelBenchmark', 'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'opencv_version': cv2.__version__, 'shifts': SHIFTS, 'noise_sigmas': NOISE_SIGMAS, 'gammas': GAMMAS,
              'textures': list(TEXTURES), 'repeats': repeats, 'summary': summary, 'rows': rows}
    report['report_sha256'] = digest({k: v for k, v in report.items()})
    write_json(output/'report.json', report)
    lines = ['# V6 sub-pixel benchmark (synthetic bilinear shifts)', '',
             f'OpenCV {cv2.__version__}; shifts {SHIFTS}; repeats {repeats}.', '',
             '| texture | noise σ | gamma | method | valid/trials | RMSE px | max px | bias x | bias y |', '|---|---:|---:|---|---:|---:|---:|---:|---:|']
    for s in summary:
        lines.append(f"| {s['texture']} | {s['sigma']} | {s['gamma']} | {s['method']} | {s['valid']}/{s['trials']} | "
                     f"{s['rmse_px'] if s['rmse_px'] is None else round(s['rmse_px'], 3)} | {s['max_px'] if s['max_px'] is None else round(s['max_px'], 3)} | "
                     f"{s['bias_x_px'] if s['bias_x_px'] is None else round(s['bias_x_px'], 3)} | {s['bias_y_px'] if s['bias_y_px'] is None else round(s['bias_y_px'], 3)} |")
    lines += ['', 'The integer method cannot do better than half a pixel by construction. Bias is mean signed error against the known shift.']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args(argv)
    report = run(args.output, repeats=args.repeats)
    print(json.dumps([{k: s[k] for k in ('texture', 'sigma', 'gamma', 'method', 'rmse_px', 'max_px')} for s in report['summary']], indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
