"""Blind focus-ring qualification for the V5 ring-aware SVG endpoint rule (PHASE3D_V5_PLAN.md 4.3).

The V3 set (C:/kc3d/r/v3s-ring-score-01) failed and has been seen, so V5 uses a
new set. The rule and this criterion are fixed before any recording:

``record``
    Two reference-app recordings with new qualification-only drags of node b.
    Destinations (.20, .20) and (.80, .72) keep b away from a and c and make the
    a-b connector oblique; times are new. No suite recipe uses them.
``sample``
    Population: every visible end of a connection at node b after the actual
    drag start, in both recordings, defined by public input only. Frames with
    identical bytes count once. 32 ends are drawn with a new seed from strata of
    |production - independent| difference. Unmarked crops are written; the
    production end, its attachment decision and the helper's are sealed in a
    file the marker does not open.
``score``
    One scoring run. Both are required:

    - at least 30 of 32 production ends within 2.0 px of the blind marks;
    - at least 30 of 32 production attachment decisions equal to the marked
      decision. A mark is attached when it is "touching", or when its "gap" is
      within max(2 px, 1% of the box width).

No rule or threshold is tuned here; a failing result is preserved, and SVG
acceptance is then not attempted.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from kiroclops.visual.package import frame_paths, load_package, read_image  # noqa: E402
from scripts.phase3d_v3_svg_focus_ring import MARK_PROTOCOL, _labelled  # noqa: E402

CASES = {'realistic-svg-focus-ring-qualification-v5a-r1': {'viewport_x': .20, 'viewport_y': .20},
         'realistic-svg-focus-ring-qualification-v5b-r1': {'viewport_x': .80, 'viewport_y': .72}}
DRAG = {'start': .52, 'duration': .27, 'release': .91}
SEED = 20261013
QUOTA = {'gt2': 12, '1to2': 10, 'le1': 10}
SAMPLES = 32
REQUIRED_WITHIN, REQUIRED_DECISIONS, LIMIT_PX = 30, 30, 2.


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scenario(case_id):
    """A qualification-only drag of node b; times and destination are new."""
    from kiroclops.local_engine import suite_web as legacy, suite_web_v2
    from kiroclops.web_local.specs import validate_scenario
    base = deepcopy(suite_web_v2.scenarios('svg', 'calibration')[0])
    events = legacy._drag('svg', DRAG['start'], duration=DRAG['duration'], release=DRAG['release'])
    events[0]['destination'] = dict(CASES[case_id])
    count = base['initial_node_count']
    base.update(id=case_id, case_id=case_id, split='qualification', suite_split='qualification',
                recipe='focus-ring-drag-qualification-' + case_id.split('-')[-2], expected_final_node_count=count,
                expected_selected_id='b', events=events, blind_validation=False)
    validate_scenario(base)
    return base


def record(identifier, root=Path('C:/kc3d')):
    from scripts import phase3d_v2_prepare as v2p
    from kiroclops.local_engine.backend import _lock
    from kiroclops.web_local.specs import fixture_project
    from kiroclops.visual.schema import write_json
    output = v2p._new(root, identifier)
    cases = [{'case_id': case_id, 'role': 'calibration', 'stage': 'svg', 'purpose': 'measurement_qualification',
              'qualification_kind': 'focus_ring_endpoint_v5', 'behavior_fit_eligible': False,
              'scenario': scenario(case_id)} for case_id in CASES]
    write_json(output / 'cases.json', cases)
    cap = v2p.Capture('svg', output, root)
    records = {}
    try:
        with _lock(Path(cap.runtime['capture_lock']), timeout=60):
            for case in cases:
                destination = output / 'reference' / case['case_id']
                try:
                    reference = cap.run(fixture_project('svg', 'reference'), case, destination, 'reference')
                except BaseException as exc:
                    write_json(output / 'failure.json', {'code': getattr(exc, 'code', type(exc).__name__),
                                                         'message': str(exc), 'case_id': case['case_id'],
                                                         'model_started': False, 'previous_attempts_preserved': True})
                    raise
                records[case['case_id']] = {'record': str(destination / 'record.json'),
                                            'record_sha256': _sha(destination / 'record.json'),
                                            'measurement_status': reference['measurement_status']}
        result = {'status': 'recorded', 'records': records, 'model_started': False}
        write_json(output / 'result.json', result)
        return result
    finally:
        cap.close()


def _box(frame):
    return (frame['left'], frame['top'], frame['left'] + frame['width'], frame['top'] + frame['height'])


def box_distance(point, box):
    """The production attachment distance (geometry_acceptance._distance)."""
    x, y = point
    left, top, right, bottom = box
    if left <= x <= right and top <= y <= bottom:
        return min(x - left, right - x, y - top, bottom - y)
    return math.hypot(max(left - x, 0, x - right), max(top - y, 0, y - bottom))


def _limit(box):
    return max(LIMIT_PX, (box[2] - box[0]) * .01)


def population(record_paths):
    """Every visible b end after the actual drag start, once per distinct frame image."""
    from kiroclops.local_engine import suite_generic_raw_review as generic
    _, helper, _ = generic._svg_helpers()
    rows, seen = [], set()
    for record_path in record_paths:
        record = json.loads(Path(record_path).read_text(encoding='utf-8'))
        case_id = record['scenario']['case_id']
        if case_id not in CASES:
            raise SystemExit('Not a V5 focus-ring qualification recording: ' + case_id)
        observations = record['observations']
        starts = [e['time_s'] for e in observations['inputs']['events'] if e['action'] == 'drag_start']
        if len(starts) != 1:
            raise SystemExit('Expected exactly one actual drag start.')
        nodes = {o['id']: {f['index']: f for f in o['frames']} for o in observations['objects']}
        manifest = load_package(record['package'])
        paths = frame_paths(record['package'], manifest)
        for edge in observations.get('connections', []):
            if 'b' not in (edge['from_object'], edge['to_object']):
                continue
            end = 'end' if edge['to_object'] == 'b' else 'start'
            for frame in edge['frames']:
                if frame.get('valid') is not True or frame['time_s'] <= starts[0]:
                    continue
                key = (manifest['frames'][frame['index']]['sha256'], edge['id'], end)
                if key in seen:
                    continue
                seen.add(key)
                image = read_image(paths[frame['index']])
                if image.ndim == 3 and image.shape[2] == 4:
                    image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
                raw = helper.proposals(image)
                connection = raw['connections'].get(edge['from_object'] + '-' + edge['to_object'])
                reverse = connection is None
                connection = connection or raw['connections'].get(edge['to_object'] + '-' + edge['from_object'])
                if connection is None or 'b' not in raw['objects']:
                    continue
                raw_end_name = 'start' if (end == 'start') != reverse else 'end'
                node = nodes['b'].get(frame['index'])
                if not node or not node.get('valid'):
                    continue
                box = _box(node)
                produced = frame[end]
                produced_attached = bool(frame.get(end + '_occluded')) or box_distance(produced, box) <= _limit(box)
                raw_box = raw['objects']['b']['bounds']
                raw_end = connection[raw_end_name]
                raw_attached = (bool(connection.get(raw_end_name + '_occluded'))
                                or box_distance(raw_end, raw_box) <= _limit(raw_box))
                rows.append({'case_id': case_id, 'record': str(record_path), 'connection': edge['id'],
                             'index': frame['index'], 'time_s': frame['time_s'], 'end': end,
                             'frame_sha256': key[0], 'node_box': list(box), 'produced': produced,
                             'produced_occluded': bool(frame.get(end + '_occluded')),
                             'produced_attached': produced_attached, 'raw': raw_end,
                             'raw_attached': raw_attached,
                             'difference_px': math.hypot(produced[0] - raw_end[0], produced[1] - raw_end[1]),
                             'frame_path': str(paths[frame['index']])})
    return rows


def sample(record_paths, output):
    output = Path(output)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {output}')
    rows = population(record_paths)
    strata = {'gt2': [p for p in rows if p['difference_px'] > 2],
              '1to2': [p for p in rows if 1 < p['difference_px'] <= 2],
              'le1': [p for p in rows if p['difference_px'] <= 1]}
    rng = random.Random(SEED)
    chosen = []
    order = lambda p: (p['case_id'], p['index'], p['connection'], p['end'])  # noqa: E731
    for name in ('gt2', '1to2', 'le1'):
        pool = sorted(strata[name], key=order)
        rng.shuffle(pool)
        chosen += [{**item, 'stratum': name} for item in pool[:QUOTA[name]]]
    leftover = sorted((p for p in rows if all(p is not c for c in chosen)), key=order)
    rng.shuffle(leftover)
    chosen += [{**item, 'stratum': 'fill'} for item in leftover[:SAMPLES - len(chosen)]]
    rng.shuffle(chosen)
    if len(chosen) != SAMPLES:
        raise SystemExit(f'The population holds only {len(chosen)} distinct ends; {SAMPLES} are required.')
    output.mkdir(parents=True)
    (output / 'blind').mkdir()
    sealed = []
    for number, item in enumerate(chosen):
        image = read_image(item['frame_path'])
        if image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        (rx, ry), (px, py) = item['raw'], item['produced']
        cx, cy = (rx + px) / 2, (ry + py) / 2
        half_w, half_h = int(max(16, abs(rx - px) / 2 + 10)), int(max(12, abs(ry - py) / 2 + 9))
        x0, y0 = int(cx) - half_w, int(cy) - half_h
        fine = _labelled(image, x0, y0, x0 + 2 * half_w, y0 + 2 * half_h, 20)
        wide = _labelled(image, int(cx) - 70, int(cy) - 50, int(cx) + 70, int(cy) + 50, 3)
        sheet = np.full((max(fine.shape[0], wide.shape[0]), fine.shape[1] + wide.shape[1] + 20, 3), 255, np.uint8)
        sheet[:wide.shape[0], :wide.shape[1]] = wide
        sheet[:fine.shape[0], wide.shape[1] + 20:] = fine
        name = f'blind-{number:02d}.png'
        cv2.imencode('.png', sheet)[1].tofile(str(output / 'blind' / name))
        sealed.append({'sample': number, 'file': name, **item, 'zoom_origin': [x0, y0],
                       'zoom_size': [2 * half_w, 2 * half_h]})
    (output / 'sealed-method-coordinates.json').write_text(json.dumps(sealed, indent=1), encoding='utf-8')
    manifest = {
        'kind': 'FocusRingBlindSampleV5', 'schema_version': 1,
        'records': {str(p): _sha(p) for p in record_paths}, 'population': len(rows),
        'strata_sizes': {k: len(v) for k, v in strata.items()}, 'quota': QUOTA, 'seed': SEED,
        'samples': [{'sample': s['sample'], 'file': s['file'], 'sha256': _sha(output / 'blind' / s['file'])} for s in sealed],
        'sealed_file_sha256': _sha(output / 'sealed-method-coordinates.json'), 'mark_protocol': MARK_PROTOCOL,
        'producer_sha256': _sha(__file__)}
    (output / 'blind-manifest.json').write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    return {'population': len(rows), 'samples': len(sealed), 'strata': manifest['strata_sizes']}


def score(sample_dir, marks_path, output):
    sample_dir, output = Path(sample_dir), Path(output)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {output}')
    manifest = json.loads((sample_dir / 'blind-manifest.json').read_text(encoding='utf-8'))
    if _sha(sample_dir / 'sealed-method-coordinates.json') != manifest['sealed_file_sha256']:
        raise SystemExit('The sealed method coordinates changed.')
    marks = json.loads(Path(marks_path).read_text(encoding='utf-8'))
    if marks.get('kind') != 'BlindFocusRingEndpointMarks' or marks.get('protocol') != MARK_PROTOCOL:
        raise SystemExit('Marks must follow the frozen blind protocol.')
    sealed = json.loads((sample_dir / 'sealed-method-coordinates.json').read_text(encoding='utf-8'))
    rows = []
    for item in sealed:
        mark = marks['marks']['%02d' % item['sample']]
        manual = [mark['x'], mark['y']]
        box = item['node_box']
        manual_attached = mark['contact'] == 'touching' or box_distance(manual, box) <= _limit(box)
        rows.append({'sample': item['sample'], 'case_id': item['case_id'], 'stratum': item['stratum'],
                     'manual': manual, 'manual_contact': mark['contact'], 'manual_attached': manual_attached,
                     'produced': item['produced'], 'produced_occluded': item['produced_occluded'],
                     'produced_attached': item['produced_attached'], 'raw_attached': item['raw_attached'],
                     'production_error_px': math.dist(item['produced'], manual),
                     'independent_error_px': math.dist(item['raw'], manual),
                     'decision_agrees': item['produced_attached'] == manual_attached})
    within = sum(r['production_error_px'] <= LIMIT_PX + 1e-9 for r in rows)
    agreeing = sum(r['decision_agrees'] for r in rows)
    passed = len(rows) == SAMPLES and within >= REQUIRED_WITHIN and agreeing >= REQUIRED_DECISIONS
    result = {'kind': 'SvgFocusRingBlindQualificationV5', 'schema_version': 1, 'status': 'pass' if passed else 'fail',
              'criterion': {'within_px': LIMIT_PX, 'required_within': REQUIRED_WITHIN,
                            'required_decision_agreement': REQUIRED_DECISIONS, 'samples': SAMPLES},
              'production_within_2px': within, 'decision_agreement': agreeing,
              'independent_within_2px': sum(r['independent_error_px'] <= LIMIT_PX + 1e-9 for r in rows),
              'independent_decision_agreement': sum(r['raw_attached'] == r['manual_attached'] for r in rows),
              'manual_contact_counts': {c: sum(r['manual_contact'] == c for r in rows) for c in ('touching', 'gap')},
              'samples': rows, 'marks_sha256': _sha(marks_path), 'sample_manifest_sha256': _sha(sample_dir / 'blind-manifest.json'),
              'reviewer': marks.get('reviewer'), 'producer_sha256': _sha(__file__), 'single_scoring_run': True,
              'tuned_on_these_samples': False,
              'scope': 'Blind endpoint accuracy and attachment decisions at focus rings only; not SVG behavior acceptance.'}
    output.mkdir(parents=True)
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    rec = commands.add_parser('record')
    rec.add_argument('--id', required=True)
    rec.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    smp = commands.add_parser('sample')
    smp.add_argument('--record', type=Path, action='append', required=True)
    smp.add_argument('--output', type=Path, required=True)
    sco = commands.add_parser('score')
    sco.add_argument('--sample', type=Path, required=True)
    sco.add_argument('--marks', type=Path, required=True)
    sco.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'record':
        value = record(args.id, args.root)
    elif args.command == 'sample':
        value = sample(args.record, args.output)
    else:
        value = score(args.sample, args.marks, args.output)
    print(json.dumps({k: v for k, v in value.items() if k not in ('samples',)}, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
