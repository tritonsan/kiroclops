"""One-shot blind requalification of the V3 SVG connection endpoint rule.

Scores the frozen V3 rule (``axis_band_v2``) once against the 32 blind manual
endpoint marks of artifacts/phase3d-v2/svg-endpoint-blind-calibration-01. Every
sample must first reproduce its historical production endpoint exactly with the
unchanged V1 rule (``extreme_v1``) from the same inputs; this proves the frame,
object boxes and connection spec were rebuilt correctly.

Preregistered pass criterion (PHASE3D_V3_PLAN.md): at least 30 of 32 samples
within 2 px of the manual mark. No threshold or rule is tuned on these samples;
a failing result is preserved as a failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from kiroclops.visual import tracking  # noqa: E402
from kiroclops.visual.package import read_image  # noqa: E402

CALIBRATION = Path('D:/Amazon Developer Hackathon/artifacts/phase3d-v2/svg-endpoint-blind-calibration-01')
REQUIRED_WITHIN = 30
LIMIT_PX = 2.


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(path, inventory):
    inventory[str(path)] = _sha(path)
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _analysis_path(frame_png):
    # .../<case>[/attempt-NN]/package/frames/NNNNNN.png -> .../measurement/analysis.json
    return Path(frame_png).parents[2] / 'measurement' / 'analysis.json'


def _summary(rows, key):
    errors = [row[key] for row in rows]
    return {'count': len(errors), 'within_2px': sum(e <= LIMIT_PX + 1e-9 for e in errors),
            'median_px': statistics.median(errors), 'max_px': max(errors)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {args.output}')
    inventory = {}
    calibration = _load(CALIBRATION / 'result.json', inventory)
    marks = _load(CALIBRATION / 'manual-marks.json', inventory)['marks']
    analyses = {}
    rows = []
    for sample in calibration['samples']:
        png = Path(sample['frame_png'])
        if _sha(png) != sample['frame_png_sha256']:
            raise SystemExit(f'Frame bytes changed for sample {sample["sample"]}')
        inventory[str(png)] = sample['frame_png_sha256']
        analysis_path = _analysis_path(png)
        if str(analysis_path) not in analyses:
            analyses[str(analysis_path)] = _load(analysis_path, inventory)
        observations = analyses[str(analysis_path)]['observations']
        index = sample['index']
        frames = {}
        for obj in observations['objects']:
            frame = obj['frames'][index]
            if frame.get('index') != index:
                raise SystemExit(f'Frame index mismatch for sample {sample["sample"]}')
            frames[obj['id']] = frame
        spec = next(c for c in observations['selection']['connections'] if c['id'] == sample['connection'])
        gray = cv2.cvtColor(read_image(png), cv2.COLOR_BGR2GRAY)
        old = tracking._line_connection(gray, spec, frames, endpoint_method='extreme_v1')
        new = tracking._line_connection(gray, spec, frames, endpoint_method='axis_band_v2')
        old_point = old.get(sample['end'])
        if old_point is None or [float(v) for v in old_point] != [float(v) for v in sample['produced']]:
            raise SystemExit(f'V1 identity check failed for sample {sample["sample"]}: {old_point} != {sample["produced"]}')
        mark_key = '%02d' % sample['sample']
        manual = marks[mark_key]
        if [float(v) for v in manual] != [float(v) for v in sample['manual']]:
            raise SystemExit(f'Manual mark differs from the calibration record for sample {mark_key}')
        new_point = new.get(sample['end'])
        error = math.hypot(new_point[0] - manual[0], new_point[1] - manual[1]) if new_point else math.inf
        rows.append({'sample': sample['sample'], 'stratum': sample['stratum'], 'role': sample['role'],
                     'case_id': sample['case_id'], 'connection': sample['connection'], 'index': index,
                     'end': sample['end'], 'manual': manual, 'v1_produced': old_point,
                     'v1_identity': True, 'v2_valid': new.get('valid'), 'v2_reason': new.get('reason'),
                     'v2_point': new_point, 'v2_error_px': error,
                     'v1_error_px': sample['production_error_px'],
                     'independent_error_px': sample['independent_error_px']})
    for path, digest in inventory.items():
        if _sha(path) != digest:
            raise SystemExit(f'Input changed during scoring: {path}')
    overall = _summary(rows, 'v2_error_px')
    strata = sorted({row['stratum'] for row in rows})
    result = {
        'kind': 'SvgEndpointBlindRequalification', 'schema_version': 1,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'rule': 'axis_band_v2', 'rule_method': tracking.LINE_ENDPOINT_METHODS['axis_band_v2'],
        'preregistered_criterion': {'within_px': LIMIT_PX, 'required': REQUIRED_WITHIN, 'samples': len(rows)},
        'status': 'pass' if overall['within_2px'] >= REQUIRED_WITHIN else 'fail',
        'v2': overall, 'v1_historical': _summary(rows, 'v1_error_px'),
        'independent_helper': _summary(rows, 'independent_error_px'),
        'by_stratum': {s: {'v2': _summary([r for r in rows if r['stratum'] == s], 'v2_error_px'),
                           'v1': _summary([r for r in rows if r['stratum'] == s], 'v1_error_px')}
                       for s in strata},
        'v1_identity_reproduced': len(rows),
        'samples': rows,
        'code_sha256': {'tracking.py': _sha(ROOT / 'kiroclops/visual/tracking.py'), 'producer': _sha(Path(__file__))},
        'code_root': str(ROOT),
        'runtime': {'python': platform.python_version(), 'opencv': cv2.__version__, 'numpy': np.__version__},
        'inputs_sha256': inventory, 'inputs_rehashed_after_scoring': True,
        'tuned_on_these_samples': False, 'single_scoring_run': True,
        'scope': 'Blind endpoint accuracy of the measurement rule only; not SVG behavior acceptance.',
    }
    args.output.mkdir(parents=True)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'v2', 'v1_historical', 'independent_helper', 'by_stratum')},
                     indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
