"""Error table of every V6 measurement method against independent reference positions (V6 plan WP5.2).

The numerical calibration reviews of the frozen V5 suites hold points whose ``raw``
position was extracted from the raw pixels independently of the tracker (painted
body contour; no production coordinates, events, DOM or smoothing):
``C:/kc3d/a/v5p02-cal-review`` (panel, 117 points) and ``C:/kc3d/a/v5s01-cal-review``
(SVG, 216 points). This script measures the same frames with each method and
reports, per domain and method, the error against those points: signed bias
(mean dx, dy), scatter around the bias, median, p95 and maximum distance, and how
many points fall within the review's pixel floor.

Methods:

* ``accepted``: the stored V5b observation (pixel anchor), the value the review saw;
* ``template_integer``: fixed-template tracking of the same selection without the
  pixel anchor (integer NCC peak plus the explicit anchor);
* ``template_parabolic_v1`` and ``template_upsampled_v1``: the same with the opt-in
  sub-pixel peak refinement;
* ``features_v1``: ALIKED + LightGlue location of the selected patch (OpenCV 5),
  as the tracker's cross-check computes it.

The blind focus-ring endpoint marks (32) are summarised from their frozen score;
V6 reproduces those observations bit for bit (``artifacts/phase4-v6/equivalence-01``).
The script reads frozen folders only and writes a new evidence folder.

    python scripts/v6_marks_error_table.py --output artifacts/phase4-v6/marks-error-01
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import statistics
import sys
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual import features5
from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.visual.tracking import analyze_package
from scripts.v6_equivalence import DEFAULT_CONTRACT, _load_record

DOMAINS = {
    'panel': {'review': 'C:/kc3d/a/v5p02-cal-review/result.json', 'records_root': 'C:/kc3d/p/v5p02-cal'},
    'svg': {'review': 'C:/kc3d/a/v5s01-cal-review/result.json', 'records_root': 'C:/kc3d/p/v5s01-cal'},
}
RING_SCORE = 'C:/kc3d/r/v5s-ring-score-01/result.json'
TEMPLATE_VARIANTS = (('template_integer', None), ('template_parabolic_v1', 'parabolic_v1'),
                     ('template_upsampled_v1', 'upsampled_v1'))
METHODS = ('accepted',)+tuple(name for name, _ in TEMPLATE_VARIANTS)+('features_v1',)


def summarise(offsets, floor_px):
    """Bias, scatter and distance statistics of (dx, dy) offsets; ``None`` marks an unmeasured point."""
    measured = [o for o in offsets if o is not None]
    row: dict = {'points': len(offsets), 'measured': len(measured)}
    if not measured:
        return row
    dx, dy = [o[0] for o in measured], [o[1] for o in measured]
    bias = (statistics.fmean(dx), statistics.fmean(dy))
    distances = sorted(math.hypot(a, b) for a, b in measured)
    scatter = math.sqrt(statistics.fmean((a-bias[0])**2+(b-bias[1])**2 for a, b in measured))
    row.update(bias_px=[bias[0], bias[1]], bias_length_px=math.hypot(*bias), scatter_px=scatter,
               median_px=statistics.median(distances), p95_px=distances[min(len(distances)-1, int(len(distances)*.95))],
               max_px=distances[-1], within_floor=sum(d <= floor_px for d in distances), floor_px=floor_px)
    return row


def _frame_xy(observations, object_id, index):
    obj = next((o for o in observations.get('objects', []) if o.get('id') == object_id), None)
    if obj is None or not 0 <= index < len(obj['frames']):
        return None, None
    frame = obj['frames'][index]
    return ((frame['x'], frame['y']) if frame.get('valid') else None), frame


def measure_domain(name, config, engine, limit_records=None):
    review = read_json(config['review'])
    if review.get('basis') != 'independent_raw_pixels':
        raise VisualError('invalid_review', f'{name}: review is not an independent raw-pixel review')
    records = {r['record_id']: r for r in review['records']}
    by_record = {}
    for point in review['points']:
        by_record.setdefault(point['record_id'], []).append(point)
    rows, started = [], time.perf_counter()
    for record_id, points in sorted(by_record.items())[:limit_records]:
        meta = records[record_id]
        path = Path(config['records_root'])/meta['role']/meta['case_id']/'record.json'
        record, stored, selection = _load_record(path)
        package = record['package']
        manifest = load_package(package)
        if manifest['package_sha256'] != meta['package_sha256']:
            raise VisualError('evidence_changed', f'{record_id}: package differs from the reviewed one')
        paths = frame_paths(package, manifest)
        variants = {}
        for method, refinement in TEMPLATE_VARIANTS:
            changed = deepcopy(selection)
            for spec in changed.get('objects', []):
                if spec.get('method', 'template') == 'template':
                    spec.pop('pixel_anchor', None)
                    if refinement:
                        spec['peak_refinement'] = refinement
            variants[method] = analyze_package(package, changed, dict(DEFAULT_CONTRACT))
        specs = {s['id']: s for s in selection.get('objects', [])}
        templates = {}
        for point in points:
            index, object_id = point['index'], point['object_id']
            if manifest['frames'][index]['sha256'] != point['frame_sha256']:
                raise VisualError('evidence_changed', f'{record_id}: frame {index} differs from the reviewed one')
            raw = point['raw']
            row = {'record_id': record_id, 'case_id': meta['case_id'], 'role': meta['role'], 'object_id': object_id,
                   'index': index, 'raw': raw, 'review_error_px': point['error_px']}
            stored_xy, stored_frame = _frame_xy(stored, object_id, index)
            row['accepted'] = [point['produced'][0]-raw[0], point['produced'][1]-raw[1]]
            for method, _ in TEMPLATE_VARIANTS:
                xy, _ = _frame_xy(variants[method], object_id, index)
                row[method] = None if xy is None else [xy[0]-raw[0], xy[1]-raw[1]]
            spec = specs[object_id]
            x, y, w, h = (int(v) for v in spec['roi'])
            template_index = spec.get('template_frame_index', spec.get('frame_index', 0))
            if template_index not in templates:
                templates[template_index] = read_image(paths[template_index])
            anchor = spec.get('anchor', [w/2, h/2])
            center = stored_xy or tuple(point['produced'])
            radius = (stored_frame or {}).get('search_radius_px') or spec.get('search_radius', max(16, .8*max(w, h)))
            located = features5.locate(engine, templates[template_index], (x, y, w, h), anchor, read_image(paths[index]),
                                       center=center, search_radius=radius)
            row['features_v1'] = [located['x']-raw[0], located['y']-raw[1]] if located['valid'] else None
            row['features_reason'] = located.get('reason')
            rows.append(row)
        print(json.dumps({'domain': name, 'record': record_id, 'points': len(points)}), flush=True)
    floors = review.get('floors_px') or {}
    floor = max(floors.values()) if floors else 2.
    summary = {method: summarise([r[method] for r in rows], floor) for method in METHODS}
    per_object = {object_id: {method: summarise([r[method] for r in rows if r['object_id'] == object_id], floors.get(object_id, floor))
                              for method in METHODS}
                  for object_id in sorted({r['object_id'] for r in rows})}
    reasons = {}
    for r in rows:
        if r['features_v1'] is None:
            reasons[r['features_reason']] = reasons.get(r['features_reason'], 0)+1
    return {'review': config['review'], 'review_sha256': file_sha256(config['review']), 'records_root': config['records_root'],
            'points': len(rows), 'floor_px': floor, 'seconds': time.perf_counter()-started,
            'summary': summary, 'per_object': per_object, 'features_not_located': reasons, 'rows': rows}


def code_epoch(snapshot=Path('C:/kc3d/code/v6/epoch.json')):
    """Whether the measuring ``kiroclops`` tree is byte-identical to the frozen V6 snapshot."""
    root = Path(__file__).resolve().parents[1]
    live = {p.relative_to(root).as_posix(): file_sha256(p) for p in sorted((root/'kiroclops').rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}
    if not snapshot.is_file():
        return {'snapshot': str(snapshot), 'present': False, 'live_kiroclops_sha256': digest(live)}
    epoch = read_json(snapshot)
    frozen = {k: v for k, v in epoch['source_files'].items() if k.startswith('kiroclops/')}
    return {'snapshot': str(snapshot), 'present': True, 'snapshot_tree_sha256': epoch['source_tree_sha256'],
            'live_kiroclops_matches_snapshot': live == frozen, 'live_kiroclops_sha256': digest(live)}


def ring_marks():
    score = read_json(RING_SCORE)
    return {'source': RING_SCORE, 'source_sha256': file_sha256(RING_SCORE), 'status': score.get('status'),
            'samples': len(score.get('samples', [])), 'criterion': score.get('criterion'),
            'production_within_2px': score.get('production_within_2px'), 'decision_agreement': score.get('decision_agreement'),
            'independent_within_2px': score.get('independent_within_2px'), 'reviewer': score.get('reviewer'),
            'v6_note': 'V6 reproduces these observations bit for bit (artifacts/phase4-v6/equivalence-01), so the score holds unchanged.'}


def _fmt(value, digits=2):
    return '-' if value is None else f'{value:.{digits}f}'


def _markdown(report):
    epoch = report['code_epoch']
    lines = ['# V6 methods against independent reference positions', '',
             'Reference: the `raw` position of each point of the frozen numerical calibration reviews, extracted from raw '
             'pixels without production coordinates, events, DOM or smoothing. Offsets are method minus reference, in pixels.',
             f"Measuring code: live `kiroclops` identical to the V6 snapshot `{epoch.get('snapshot_tree_sha256', '-')[:12]}…`: "
             f"{epoch.get('live_kiroclops_matches_snapshot')}.", '']
    for domain, value in report['domains'].items():
        lines += [f"## {domain} ({value['points']} points, floor {value['floor_px']:.1f} px)", '',
                  '| method | measured | bias x | bias y | scatter | median | p95 | max | within floor |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for method, row in value['summary'].items():
            bias = row.get('bias_px') or [None, None]
            lines.append(f"| {method} | {row['measured']}/{row['points']} | {_fmt(bias[0])} | {_fmt(bias[1])} | "
                         f"{_fmt(row.get('scatter_px'))} | {_fmt(row.get('median_px'))} | {_fmt(row.get('p95_px'))} | "
                         f"{_fmt(row.get('max_px'))} | {row.get('within_floor', '-')} |")
        if value['features_not_located']:
            lines += ['', f"features_v1 not located: {value['features_not_located']}"]
        lines.append('')
    ring = report['ring_marks']
    lines += ['## Blind focus-ring endpoint marks (SVG)', '',
              f"{ring['samples']} blind marks ({ring['reviewer']}): production within 2 px {ring['production_within_2px']}/{ring['samples']}, "
              f"attachment decision agreement {ring['decision_agreement']}/{ring['samples']}; status `{ring['status']}`. {ring['v6_note']}", '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--domains', nargs='+', choices=sorted(DOMAINS), default=sorted(DOMAINS))
    parser.add_argument('--limit-records', type=int, default=None, help='smoke runs only')
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {args.output}')
    epoch = code_epoch()
    engine = features5.FeatureEngine()
    try:
        domains = {name: measure_domain(name, DOMAINS[name], engine, args.limit_records) for name in args.domains}
    finally:
        engine.close()
    report = {'kind': 'V6IndependentReferenceErrorTable', 'schema_version': 1,
              'created_at': datetime.now(timezone.utc).isoformat(), 'code_epoch': epoch, 'methods': list(METHODS),
              'contract': DEFAULT_CONTRACT, 'domains': domains, 'ring_marks': ring_marks(),
              'scope': 'Measurement accuracy diagnostics on frozen calibration media; not behavior acceptance.'}
    report['report_sha256'] = digest(report)
    args.output.mkdir(parents=True)
    write_json(args.output/'report.json', report)
    (args.output/'REPORT.md').write_text(_markdown(report), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'domains': {k: v['points'] for k, v in domains.items()}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
