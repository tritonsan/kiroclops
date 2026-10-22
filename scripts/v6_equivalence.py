"""Re-measure frozen recordings with the V6 tracker and compare against their stored observations.

V6 (``PHASE4_V6_PLAN.md`` WP5.2) changed ``kiroclops.visual.tracking``: an OpenCV 5
runtime gate, a symmetric mirror/rotation rule for the default anchor, the
opt-in sub-pixel ``peak_refinement`` and a runtime record in every
ObservationSet. None of these may move a number that an accepted closure relied
on. This script replays ``analyze_package`` over frozen records (calibration
preparations such as ``C:/kc3d/p/v5s01-cal`` or session measurement rows) with
V6 defaults and reports every per-frame difference against the observations
stored with the record. It reads the frozen folders only; results go to a new
evidence folder.

With ``--refinement upsampled_v1`` it additionally measures the same records
with the opt-in sub-pixel refinement and reports how far the refined
coordinates move from the integer ones (a diagnostic, not a gate).

    python scripts/v6_equivalence.py --records-root C:/kc3d/p/v5s01-cal --roles reference before \
        --output artifacts/phase4-v6/equivalence-01/svg-cal --refinement upsampled_v1
    python scripts/v6_equivalence.py --rows-json C:/kc3d/s/v4g01/measurements/<job>/candidate.json \
        --output artifacts/phase4-v6/equivalence-01/game-candidate
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from kiroclops.visual.tracking import analyze_package

DEFAULT_CONTRACT = {'min_valid_ratio': .95, 'require_static_camera': True}
FRAME_KEYS = ('x', 'y', 'left', 'top', 'width', 'height')


def _records_from_root(root, roles, limit=None):
    root = Path(root)
    found = []
    for role in roles:
        base = root/role
        if not base.is_dir():
            continue
        for case_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            record = case_dir/'record.json'
            if record.is_file():
                found.append((role, case_dir.name, record))
    return found[:limit] if limit else found


def _load_record(path):
    record = read_json(path)
    observations = record.get('observations')
    if observations is None and record.get('analysis'):
        observations = read_json(Path(record['analysis'])).get('observations')
    if observations is None:
        raise VisualError('invalid_record', f'{path}: no stored observations')
    selection = record.get('selection') or observations.get('selection')
    if selection is None:
        raise VisualError('invalid_record', f'{path}: no selection')
    return record, observations, selection


def _frame_delta(old, new, keys=FRAME_KEYS):
    """Largest absolute coordinate difference over frames valid in both; counts validity mismatches."""
    worst, mismatches, compared = 0., 0, 0
    for a, b in zip(old, new):
        if bool(a.get('valid')) != bool(b.get('valid')):
            mismatches += 1
            continue
        if not a.get('valid'):
            continue
        compared += 1
        for key in keys:
            va, vb = a.get(key), b.get(key)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                worst = max(worst, abs(float(va)-float(vb)))
            elif va != vb:
                worst = max(worst, float('inf'))
    return worst, mismatches, compared


def _point_delta(old, new):
    worst, mismatches, compared = 0., 0, 0
    for a, b in zip(old, new):
        if bool(a.get('valid')) != bool(b.get('valid')):
            mismatches += 1
            continue
        if not a.get('valid'):
            continue
        compared += 1
        for key in ('start', 'end'):
            pa, pb = a.get(key), b.get(key)
            if isinstance(pa, (list, tuple)) and isinstance(pb, (list, tuple)) and len(pa) == len(pb) == 2:
                worst = max(worst, math.hypot(pa[0]-pb[0], pa[1]-pb[1]))
            elif pa != pb:
                worst = max(worst, float('inf'))
        for key in ('start_occluded', 'end_occluded'):
            if a.get(key) != b.get(key):
                mismatches += 1
    return worst, mismatches, compared


def compare_observation_sets(old, new):
    report = {'status': [old.get('status'), new.get('status')], 'objects': {}, 'connections': {},
              'camera_max_shift_delta_px': 0., 'identical': True}
    new_objects = {o['id']: o for o in new.get('objects', [])}
    for obj in old.get('objects', []):
        other = new_objects.get(obj['id'])
        if other is None or len(other['frames']) != len(obj['frames']):
            report['objects'][obj['id']] = {'missing_or_length_mismatch': True}
            report['identical'] = False
            continue
        worst, mismatches, compared = _frame_delta(obj['frames'], other['frames'])
        report['objects'][obj['id']] = {'max_abs_delta_px': worst, 'validity_mismatches': mismatches,
                                        'frames_compared': compared, 'valid_ratio': [obj.get('valid_ratio'), other.get('valid_ratio')]}
        if worst > 0 or mismatches or obj.get('valid_ratio') != other.get('valid_ratio'):
            report['identical'] = False
    new_connections = {c['id']: c for c in new.get('connections', [])}
    for conn in old.get('connections', []):
        other = new_connections.get(conn['id'])
        if other is None or len(other['frames']) != len(conn['frames']):
            report['connections'][conn['id']] = {'missing_or_length_mismatch': True}
            report['identical'] = False
            continue
        worst, mismatches, compared = _point_delta(conn['frames'], other['frames'])
        report['connections'][conn['id']] = {'max_endpoint_delta_px': worst, 'mismatches': mismatches, 'frames_compared': compared}
        if worst > 0 or mismatches:
            report['identical'] = False
    for a, b in zip(old.get('camera', []), new.get('camera', [])):
        if a.get('status') != b.get('status'):
            report['identical'] = False
        sa, sb = a.get('max_shift_px'), b.get('max_shift_px')
        if isinstance(sa, (int, float)) and isinstance(sb, (int, float)):
            report['camera_max_shift_delta_px'] = max(report['camera_max_shift_delta_px'], abs(sa-sb))
    if old.get('status') != new.get('status'):
        report['identical'] = False
    return report


def refinement_offsets(default, refined):
    """How far refined coordinates move from the integer ones, per object."""
    rows = {}
    refined_objects = {o['id']: o for o in refined.get('objects', [])}
    for obj in default.get('objects', []):
        other = refined_objects.get(obj['id'])
        if other is None:
            continue
        offsets, statuses = [], {}
        for a, b in zip(obj['frames'], other['frames']):
            info = b.get('peak_refinement')
            if a.get('valid') and b.get('valid') and info:
                offsets.append((b['x']-a['x'], b['y']-a['y']))
                statuses[info.get('status')] = statuses.get(info.get('status'), 0)+1
        if offsets:
            magnitudes = [math.hypot(dx, dy) for dx, dy in offsets]
            rows[obj['id']] = {'frames': len(offsets), 'mean_abs_offset_px': statistics.mean(magnitudes),
                               'max_offset_px': max(magnitudes), 'median_abs_dx': statistics.median(abs(d[0]) for d in offsets),
                               'median_abs_dy': statistics.median(abs(d[1]) for d in offsets),
                               'fraction_over_quarter_px': sum(m > .25 for m in magnitudes)/len(magnitudes),
                               'statuses': statuses}
    return rows


def cross_check_summary(checked):
    """Per object: how often the learned-feature location agreed with the template tracker."""
    rows = {}
    for obj in checked.get('objects', []):
        checks = [f['features_cross_check'] for f in obj['frames'] if f.get('valid') and f.get('features_cross_check')]
        if not checks:
            continue
        valid = [c for c in checks if c.get('valid')]
        distances = sorted(c['consistency_px'] for c in valid if isinstance(c.get('consistency_px'), (int, float)))
        reasons = {}
        for c in checks:
            if not c.get('valid'):
                reasons[c.get('reason')] = reasons.get(c.get('reason'), 0)+1
        rows[obj['id']] = {'frames_checked': len(checks), 'located': len(valid), 'located_fraction': len(valid)/len(checks),
                           'median_consistency_px': statistics.median(distances) if distances else None,
                           'p95_consistency_px': distances[min(len(distances)-1, int(len(distances)*.95))] if distances else None,
                           'max_consistency_px': distances[-1] if distances else None,
                           'median_scale': statistics.median(c['scale'] for c in valid) if valid else None,
                           'median_inlier_ratio': statistics.median(c['inlier_ratio'] for c in valid if isinstance(c.get('inlier_ratio'), (int, float))) if valid else None,
                           'median_matches': statistics.median(c['matches'] for c in valid) if valid else None,
                           'not_located_reasons': reasons}
    return rows


def run(records, output, *, refinement=None, contract=None, cross_check=None):
    output = Path(output)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {output}')
    output.mkdir(parents=True)
    contract = dict(contract or DEFAULT_CONTRACT)
    results = []
    for role, case, path in records:
        record, stored, selection = _load_record(path)
        started = time.perf_counter()
        fresh = analyze_package(record['package'], deepcopy(selection), contract)
        elapsed = time.perf_counter()-started
        entry = {'role': role, 'case': case, 'record': str(path), 'package': record['package'],
                 'stored_opencv': stored.get('opencv_version'), 'fresh_opencv': fresh.get('opencv_version'),
                 'fresh_runtime': fresh.get('opencv_runtime'), 'seconds': elapsed,
                 'comparison': compare_observation_sets(stored, fresh)}
        if cross_check:
            checked_selection = deepcopy(selection)
            for obj in checked_selection.get('objects', []):
                if obj.get('method', 'template') == 'template':
                    obj['cross_check'] = cross_check
            started = time.perf_counter()
            checked = analyze_package(record['package'], checked_selection, contract)
            entry['cross_check'] = {'method': cross_check, 'seconds': time.perf_counter()-started,
                                    'status': [fresh.get('status'), checked.get('status')],
                                    'objects': cross_check_summary(checked)}
        if refinement:
            refined_selection = deepcopy(selection)
            for obj in refined_selection.get('objects', []):
                if obj.get('method', 'template') == 'template':
                    obj['peak_refinement'] = refinement
            started = time.perf_counter()
            refined = analyze_package(record['package'], refined_selection, contract)
            entry['refinement'] = {'method': refinement, 'seconds': time.perf_counter()-started,
                                   'status': [fresh.get('status'), refined.get('status')],
                                   'offsets': refinement_offsets(fresh, refined)}
        results.append(entry)
        print(json.dumps({'role': role, 'case': case, 'identical': entry['comparison']['identical'], 'seconds': round(elapsed, 2)}))
    summary = {'kind': 'V6EquivalenceReplay', 'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
               'contract': contract, 'records': len(results), 'identical': sum(r['comparison']['identical'] for r in results),
               'refinement': refinement, 'cross_check': cross_check, 'results': results}
    summary['summary_sha256'] = digest({k: v for k, v in summary.items()})
    write_json(output/'report.json', summary)
    (output/'REPORT.md').write_text(_markdown(summary), encoding='utf-8')
    return summary


def _markdown(summary):
    lines = ['# V6 equivalence replay', '',
             f"Records: {summary['records']}, identical to stored observations: {summary['identical']}.",
             f"Contract: `{json.dumps(summary['contract'])}`. Refinement: `{summary['refinement']}`.", '',
             '| role | case | identical | max object delta px | validity mismatches | max connection delta px | seconds |',
             '|---|---|---|---:|---:|---:|---:|']
    for r in summary['results']:
        c = r['comparison']
        obj_delta = max([v.get('max_abs_delta_px', float('inf')) for v in c['objects'].values()] or [0.])
        mism = sum(v.get('validity_mismatches', 0) for v in c['objects'].values())
        conn_delta = max([v.get('max_endpoint_delta_px', float('inf')) for v in c['connections'].values()] or [0.])
        lines.append(f"| {r['role']} | {r['case']} | {'yes' if c['identical'] else 'NO'} | {obj_delta:.4g} | {mism} | {conn_delta:.4g} | {r['seconds']:.1f} |")
    if summary['refinement']:
        lines += ['', f"## Opt-in refinement `{summary['refinement']}`: offset from the integer peak", '',
                  '| role | case | object | frames | mean |offset| px | max px | fraction > 0.25 px | statuses | seconds |',
                  '|---|---|---|---:|---:|---:|---:|---|---:|']
        for r in summary['results']:
            ref = r.get('refinement') or {}
            for obj, row in (ref.get('offsets') or {}).items():
                lines.append(f"| {r['role']} | {r['case']} | {obj} | {row['frames']} | {row['mean_abs_offset_px']:.3f} | "
                             f"{row['max_offset_px']:.3f} | {row['fraction_over_quarter_px']:.2f} | {row['statuses']} | {ref['seconds']:.1f} |")
    if summary.get('cross_check'):
        lines += ['', f"## Learned-feature cross-check `{summary['cross_check']}` (ALIKED + LightGlue, OpenCV 5 features module)", '',
                  '| role | case | object | frames | located | median px | p95 px | max px | median scale | median inlier ratio | median matches | not located | seconds |',
                  '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|']
        for r in summary['results']:
            cc = r.get('cross_check') or {}
            for obj, row in (cc.get('objects') or {}).items():
                fmt = lambda v: '-' if v is None else (f'{v:.2f}' if isinstance(v, float) else str(v))
                lines.append(f"| {r['role']} | {r['case']} | {obj} | {row['frames_checked']} | {row['located']} | {fmt(row['median_consistency_px'])} | "
                             f"{fmt(row['p95_consistency_px'])} | {fmt(row['max_consistency_px'])} | {fmt(row['median_scale'])} | "
                             f"{fmt(row['median_inlier_ratio'])} | {fmt(row['median_matches'])} | {row['not_located_reasons'] or '-'} | {cc['seconds']:.1f} |")
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--records-root', type=Path, help='calibration preparation folder holding <role>/<case>/record.json')
    source.add_argument('--rows-json', type=Path, help='JSON list of measurement rows (package + observations + selection)')
    parser.add_argument('--roles', nargs='+', default=['reference', 'before', 'original', 'parent'])
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--refinement', choices=['upsampled_v1', 'parabolic_v1'], default=None)
    parser.add_argument('--cross-check', choices=['features_v1'], default=None,
                        help='also measure with the learned-feature cross-check on template objects (needs the fetched models)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.records_root:
        records = _records_from_root(args.records_root, args.roles, args.limit)
    else:
        rows = read_json(args.rows_json)
        staging = args.output.parent/(args.output.name+'-rows')
        staging.mkdir(parents=True, exist_ok=True)
        records = []
        for index, row in enumerate(rows[:args.limit] if args.limit else rows):
            path = staging/f'row-{index:03d}.json'
            write_json(path, row)
            records.append((row.get('variant', 'row'), row.get('scenario', {}).get('case_id', row.get('scenario', {}).get('id', str(index))), path))
    if not records:
        raise SystemExit('No records found')
    summary = run(records, args.output, refinement=args.refinement, cross_check=args.cross_check)
    print(json.dumps({'records': summary['records'], 'identical': summary['identical'], 'output': str(args.output)}))
    return 0 if summary['identical'] == summary['records'] else 1


if __name__ == '__main__':
    sys.exit(main())
