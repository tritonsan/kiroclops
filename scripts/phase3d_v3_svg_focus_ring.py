"""Blind focus-ring endpoint qualification for the V3 SVG endpoint rule.

The V2 SVG calibration recipes contain no drag, so they show no focus ring. The
32-sample blind requalification had none either. The V3 plan therefore needs a
separate blind set from a new calibration drag recording. Steps:

``record``
    One reference-app recording with a new qualification-only drag of node
    ``b``. It never uses a held-out recipe; the times and destination are new.
``sample``
    Population: every visible end of a connection at the dragged node ``b``,
    from the first frame after the actual drag start onward. It is defined by
    public input only, never by a measurement tool. 32 ends are drawn from
    strata of |production - independent| difference, as in V2. The tool writes
    unmarked crops and keeps the method coordinates in a sealed file the
    marker does not open.
``score``
    Scores the production ends once against the blind marks. Preregistered
    criterion (PHASE3D_V3_PLAN.md): at least 30 of 32 within 2 px. The agreement
    of the attached/gap decision is reported alongside.

No rule or threshold is tuned here; a failing result is preserved.
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

# Attempt 1 (C:/kc3d/p/v3s-ring-01, recipe 'focus-ring-drag-qualification') dropped node b onto
# node a at viewport (.38, .60): a was occluded for 15 frames and the recording did not qualify.
# It is preserved. Attempt 2 drags b up and to the right, away from a and c, to a destination
# that no suite recipe uses (the suite's only destination is .64, .32).
CASE_ID = 'realistic-svg-focus-ring-qualification-2-r1'
DESTINATION = {'viewport_x': .84, 'viewport_y': .26}
SEED = 20261009
QUOTA = {'gt2': 12, '1to2': 10, 'le1': 10}
REQUIRED_WITHIN, LIMIT_PX = 30, 2.
MARK_PROTOCOL = ('Mark from the unmarked crops before any method coordinate is read. Pixel-index coordinates. '
                 'Endpoint = centre of the last visible stroke cross-section, where stroke pixels are still '
                 'distinguishable from the node border or a painted occluder (focus ring). Also record "touching" '
                 'when no background pixel separates that end from the node border or occluder, else "gap".')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scenario():
    """A qualification-only drag of node b; times and destination are new."""
    from kiroclops.local_engine import suite_web as legacy, suite_web_v2
    from kiroclops.web_local.specs import validate_scenario
    base = deepcopy(suite_web_v2.scenarios('svg', 'calibration')[0])
    events = legacy._drag('svg', .47, duration=.27, release=.86)
    events[0]['destination'] = dict(DESTINATION)
    count = base['initial_node_count']
    base.update(id=CASE_ID, case_id=CASE_ID, split='qualification', suite_split='qualification',
                recipe='focus-ring-drag-qualification-2', expected_final_node_count=count,
                expected_selected_id='b', events=events, blind_validation=False)
    validate_scenario(base)
    return base


def record(identifier, root=Path('C:/kc3d')):
    from scripts import phase3d_v2_prepare as v2p
    from kiroclops.local_engine.backend import _lock
    from kiroclops.web_local.specs import fixture_project
    from kiroclops.visual.schema import write_json
    output = v2p._new(root, identifier)
    case = {'case_id': CASE_ID, 'role': 'calibration', 'stage': 'svg', 'purpose': 'measurement_qualification',
            'qualification_kind': 'focus_ring_endpoint', 'behavior_fit_eligible': False, 'scenario': scenario()}
    write_json(output / 'case.json', case)
    cap = v2p.Capture('svg', output, root)
    try:
        with _lock(Path(cap.runtime['capture_lock']), timeout=60):
            try:
                reference = cap.run(fixture_project('svg', 'reference'), case, output / 'reference' / CASE_ID, 'reference')
            except BaseException as exc:
                write_json(output / 'failure.json', {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc),
                                                     'model_started': False, 'previous_attempts_preserved': True})
                raise
        result = {'status': 'recorded', 'case': case, 'record': str(output / 'reference' / CASE_ID / 'record.json'),
                  'record_sha256': _sha(output / 'reference' / CASE_ID / 'record.json'),
                  'measurement_status': reference['measurement_status'], 'model_started': False}
        write_json(output / 'result.json', result)
        return result
    finally:
        cap.close()


def _labelled(image, x0, y0, x1, y1, scale):
    """Unmarked crop; every column/row labelled by its pixel index (full value every 10)."""
    canvas_crop = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(image.shape[1], x1), min(image.shape[0], y1)
    canvas_crop[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = image[sy0:sy1, sx0:sx1]
    big = cv2.resize(canvas_crop, ((x1 - x0) * scale, (y1 - y0) * scale), interpolation=cv2.INTER_NEAREST)
    if scale >= 8:
        big[:, ::scale] = (big[:, ::scale] * .75).astype(np.uint8)
        big[::scale, :] = (big[::scale, :] * .75).astype(np.uint8)
    top = left = 34
    canvas = np.full((big.shape[0] + top, big.shape[1] + left, 3), 255, np.uint8)
    canvas[top:, left:] = big
    step = 1 if scale >= 8 else 10
    for i in range(0, x1 - x0, step):
        value = x0 + i
        text = str(value) if value % 10 == 0 else f'{value % 100:02d}'
        cv2.putText(canvas, text, (left + i * scale + 2, 12 if value % 10 == 0 else 28), cv2.FONT_HERSHEY_SIMPLEX, .3,
                    (0, 0, 200) if value % 10 == 0 else (0, 0, 0), 1)
    for j in range(0, y1 - y0, step):
        value = y0 + j
        text = str(value) if value % 10 == 0 else f'{value % 100:02d}'
        cv2.putText(canvas, text, (1 if value % 10 == 0 else 14, top + j * scale + 13), cv2.FONT_HERSHEY_SIMPLEX, .3,
                    (0, 0, 200) if value % 10 == 0 else (0, 0, 0), 1)
    return canvas


def sample(record_path, output):
    from kiroclops.local_engine import suite_generic_raw_review as generic
    record_path, output = Path(record_path), Path(output)
    if output.exists():
        raise SystemExit(f'Refusing to overwrite an existing folder: {output}')
    record = json.loads(record_path.read_text(encoding='utf-8'))
    if record['scenario']['case_id'] != CASE_ID:
        raise SystemExit('Not the focus-ring qualification recording.')
    observations = record['observations']
    starts = [e['time_s'] for e in observations['inputs']['events'] if e['action'] == 'drag_start']
    if len(starts) != 1:
        raise SystemExit('Expected exactly one actual drag start.')
    _, helper, _ = generic._svg_helpers()
    manifest = load_package(record['package'])
    paths = frame_paths(record['package'], manifest)
    population = []
    for edge in observations.get('connections', []):
        if 'b' not in (edge['from_object'], edge['to_object']):
            continue
        end = 'end' if edge['to_object'] == 'b' else 'start'
        for frame in edge['frames']:
            if frame.get('valid') is not True or frame['time_s'] <= starts[0]:
                continue
            image = read_image(paths[frame['index']])
            if image.ndim == 3 and image.shape[2] == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
            raw = helper.proposals(image)['connections']
            key = edge['from_object'] + '-' + edge['to_object']
            other = raw.get(key)
            reverse = other is None
            other = other or raw.get(edge['to_object'] + '-' + edge['from_object'])
            if other is None:
                continue
            raw_end = other['start' if (end == 'start') != reverse else 'end']
            produced = frame[end]
            difference = math.hypot(produced[0] - raw_end[0], produced[1] - raw_end[1])
            population.append({'connection': edge['id'], 'index': frame['index'], 'time_s': frame['time_s'], 'end': end,
                               'produced': produced, 'raw': raw_end, 'difference_px': difference,
                               'raw_occluded': other.get(('start' if (end == 'start') != reverse else 'end') + '_occluded'),
                               'frame_sha256': manifest['frames'][frame['index']]['sha256']})
    strata = {'gt2': [p for p in population if p['difference_px'] > 2],
              '1to2': [p for p in population if 1 < p['difference_px'] <= 2],
              'le1': [p for p in population if p['difference_px'] <= 1]}
    rng = random.Random(SEED)
    chosen = []
    for name in ('gt2', '1to2', 'le1'):
        pool = sorted(strata[name], key=lambda p: (p['index'], p['connection']))
        rng.shuffle(pool)
        chosen += [{**item, 'stratum': name} for item in pool[:QUOTA[name]]]
    leftover = sorted((p for p in population if all(p is not c for c in chosen)), key=lambda p: (p['index'], p['connection']))
    rng.shuffle(leftover)
    chosen += [{**item, 'stratum': 'fill'} for item in leftover[:32 - len(chosen)]]
    rng.shuffle(chosen)
    output.mkdir(parents=True)
    (output / 'blind').mkdir()
    sealed = []
    for number, item in enumerate(chosen):
        image = read_image(paths[item['index']])
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
    (output / 'blind-manifest.json').write_text(json.dumps({
        'kind': 'FocusRingBlindSample', 'schema_version': 1, 'record': str(record_path), 'record_sha256': _sha(record_path),
        'population': len(population), 'strata_sizes': {k: len(v) for k, v in strata.items()}, 'quota': QUOTA,
        'seed': SEED, 'samples': [{'sample': s['sample'], 'file': s['file'], 'sha256': _sha(output / 'blind' / s['file'])}
                                 for s in sealed],
        'sealed_file_sha256': _sha(output / 'sealed-method-coordinates.json'), 'mark_protocol': MARK_PROTOCOL,
        'producer_sha256': _sha(__file__)}, indent=1), encoding='utf-8')
    return {'population': len(population), 'samples': len(sealed), 'strata': {k: len(v) for k, v in strata.items()}}


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
        error = math.hypot(item['produced'][0] - mark['x'], item['produced'][1] - mark['y'])
        rows.append({'sample': item['sample'], 'stratum': item['stratum'], 'manual': [mark['x'], mark['y']],
                     'manual_contact': mark['contact'], 'produced': item['produced'], 'production_error_px': error,
                     'independent_error_px': math.hypot(item['raw'][0] - mark['x'], item['raw'][1] - mark['y']),
                     'raw_occluded': item.get('raw_occluded')})
    within = sum(r['production_error_px'] <= LIMIT_PX + 1e-9 for r in rows)
    result = {'kind': 'SvgFocusRingBlindQualification', 'schema_version': 1,
              'status': 'pass' if len(rows) == 32 and within >= REQUIRED_WITHIN else 'fail',
              'criterion': {'within_px': LIMIT_PX, 'required': REQUIRED_WITHIN, 'samples': 32},
              'production_within_2px': within, 'independent_within_2px':
                  sum(r['independent_error_px'] <= LIMIT_PX + 1e-9 for r in rows),
              'manual_contact_counts': {c: sum(r['manual_contact'] == c for r in rows) for c in ('touching', 'gap')},
              'samples': rows, 'marks_sha256': _sha(marks_path), 'sample_manifest_sha256': _sha(sample_dir / 'blind-manifest.json'),
              'reviewer': marks.get('reviewer'), 'producer_sha256': _sha(__file__), 'single_scoring_run': True,
              'tuned_on_these_samples': False,
              'scope': 'Blind endpoint accuracy at focus rings only; not SVG behavior acceptance.'}
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
    smp.add_argument('--record', type=Path, required=True)
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
    print(json.dumps({k: v for k, v in value.items() if k not in ('samples', 'case')}, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
