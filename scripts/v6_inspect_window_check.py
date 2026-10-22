"""Check inspect_window against the comparison's own window RMSE on a recorded evaluation.

Post-freeze fix of 22 October 2026: the frozen V6 module (code root v6b) placed
every window on the recording's first input and plotted absolute positions,
while the comparison aligns each window on its own input and scores some
windows relative to an anchor object. For every object window of one recorded
calibration evaluation, this script runs the frozen and the current module and
divides each inspection's rmse_px / scale_px by the comparison row's
position_rmse. The session folder is only read; inspection images go to a
temporary folder that is deleted afterwards.

    python scripts/v6_inspect_window_check.py --output artifacts/phase4-v6/post-freeze-inspect-window-01
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.local_engine.window_inspection import build_window_inspection  # noqa: E402
from kiroclops.visual.schema import file_sha256, write_json  # noqa: E402

MODULE = 'kiroclops/local_engine/window_inspection.py'
TOLERANCE = .15


def _frozen(code_root):
    spec = importlib.util.spec_from_file_location('frozen_window_inspection', code_root/MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_window_inspection


def _case(row):
    return row['scenario'].get('case_id', row['scenario'].get('id'))


def _totals(ratios):
    numeric = [r for r in ratios if isinstance(r, float)]
    return {'windows': len(ratios), 'errors': len(ratios)-len(numeric),
            'median_ratio': statistics.median(numeric) if numeric else None,
            'within_tolerance': sum(abs(r-1) <= TOLERANCE for r in numeric),
            'worst_ratio': max(numeric, key=lambda r: abs(r-1)) if numeric else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--session', type=Path, default=Path('C:/kc3d/s/v6p01'))
    parser.add_argument('--job', default='ef4d46e4d3c44a018197c738f83db961', help='a succeeded calibration evaluation')
    parser.add_argument('--frozen-root', type=Path, default=Path('C:/kc3d/code/v6b'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Keep prior evidence; choose a new folder: {args.output}')
    prepared = json.loads((args.session/'prepared-private.json').read_text(encoding='utf-8'))
    reference_path = Path(prepared['reference'])
    references = {_case(r): r for r in json.loads((reference_path if reference_path.is_absolute() else args.session/reference_path).read_text(encoding='utf-8'))}
    candidates = {_case(r): r for r in json.loads((args.session/'measurements'/args.job/'candidate.json').read_text(encoding='utf-8'))}
    result = json.loads((args.session/'jobs'/args.job/'result.json').read_text(encoding='utf-8'))
    engines = {'current': build_window_inspection, 'frozen_v6b': _frozen(args.frozen_root)}
    ratios = {name: [] for name in engines}
    detail = []
    scratch = Path(tempfile.mkdtemp(prefix='inspect-window-check-'))
    try:
        for comparison in result['comparisons']:
            case = comparison['case_id']
            seen = set()
            for window in comparison['pixel_comparison']['object_windows']:
                key = (window['reference_id'], window['window_id'])
                if key in seen or not isinstance(window.get('position_rmse'), (int, float)) or window['position_rmse'] <= 0:
                    continue
                seen.add(key)
                row = {'case_id': case, 'object_id': key[0], 'window_id': key[1],
                       'relative_to_reference': window.get('relative_to_reference'), 'position_rmse': window['position_rmse']}
                for name, build in engines.items():
                    try:
                        summary = build(references[case], candidates[case], comparison, *key, scratch/f'{name}-{len(detail)}', case_id=case)
                        row[name] = (summary['rmse_px']/window['scale_px'])/window['position_rmse']
                    except Exception as exc:  # noqa: BLE001 - every failure is reported as data
                        row[name] = f'{type(exc).__name__}: {exc}'[:160]
                    ratios[name].append(row[name])
                detail.append(row)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    args.output.mkdir(parents=True)
    write_json(args.output/'result.json', {
        'kind': 'InspectWindowPostFreezeCheck', 'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
        'session': str(args.session), 'job_id': args.job, 'tolerance': TOLERANCE,
        'ratio': 'inspection rmse_px / scale_px divided by the comparison window position_rmse (1.0 = identical)',
        'sources': {'current': file_sha256(ROOT/MODULE), 'frozen_v6b': file_sha256(args.frozen_root/MODULE),
                    'script': file_sha256(Path(__file__))},
        'relative_windows': sum(1 for d in detail if d['relative_to_reference'] is not None),
        'totals': {name: _totals(values) for name, values in ratios.items()}, 'windows': detail})
    print(json.dumps({name: _totals(values) for name, values in ratios.items()}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
