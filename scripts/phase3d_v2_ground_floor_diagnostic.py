"""Post-hoc pixel-grid feasibility of the frozen ground improvement gate (diagnostic only).

Reads the frozen v2g01 suite records and the two real Kiro candidate analyses.
It never captures, edits a target, changes a threshold or starts a model.

1. Pixel-grid floor: the lowest RMSE any per-frame Heart trajectory on its own
   16 px body grid can reach against each reference clip.
2. Physics families: exhaustive grids of simple velocity models (constant or
   exponential acceleration; constant, proportional or combined braking; 0/1
   tick input delay) on the Heart grid, scored with the frozen suite_game
   formulas. The model is first required to reproduce the recorded original and
   candidate-02 calibration traces.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import suite_game
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json

DT = 1 / 60
HEART_PX = 16.0
SHORT, LONG = 'ground-calibration-r06-x0-r1', 'ground-calibration-r48-x0-r1'
FAMILIES = {
    'P': 'constant acceleration; proportional brake v*=r, v=0 below s (candidate-02 family)',
    'F': 'constant acceleration; constant friction (original family)',
    'E': 'exponential approach v+=(V-v)g while held; proportional brake with stop speed',
    'C': 'constant acceleration; combined brake |v|=max(0,|v|r-F dt)',
}


def _need(value, message):
    if not value:
        raise VisualError('diagnostic_failed', message)


class Inputs:
    def __init__(self, suite, session):
        self.suite, self.session = Path(suite), Path(session)
        self.files = {}

    def load(self, path):
        path = Path(path)
        self.files[str(path)] = file_sha256(path)
        return read_json(path)

    def reference(self, case):
        return self.load(self.suite / 'clips' / case / 'measurement/analysis.json')

    def original(self, case, role):
        folder = 'initial/original' if role == 'calibration' else 'fixed-baselines/original'
        return self.load(self.suite / folder / case / 'measurement/analysis.json')

    def candidate(self, job, case):
        return self.load(self.session / 'measurements' / job / 'candidate' / case / 'analysis.json')


def pixel_floor(reference, target_px=HEART_PX):
    mse = []
    for phase in suite_game.phases(reference['scenario']):
        errors = [(f['x_body'] - round(f['x_body'] * target_px) / target_px) ** 2
                  for f in reference['frames'][phase['start']:phase['end']] if f['valid']]
        mse.append(sum(errors) / len(errors))
    return math.sqrt(sum(mse) / len(mse))


def _axis(scenario, ticks, delay):
    axis = np.zeros(ticks)
    for event in sorted(scenario['events'], key=lambda e: (e['tick'], e['pressed'])):
        sign = 1 if event['action'] == 'right' else -1
        axis[event['tick'] + delay:] = sign if event['pressed'] else 0
    return axis


def simulate(reference, family, accel, speed, brake, delay, keep=False):
    """Frozen ground metrics of simulated Heart trajectories; parameters broadcast together."""
    plan = suite_game.phases(reference['scenario'])
    last = plan[-1]['end']
    arrays = np.broadcast_arrays(*(np.asarray(v, float) for v in (accel, speed, *brake)))
    accel, speed, brakes = arrays[0], arrays[1], arrays[2:]
    v, x = np.zeros(accel.shape), np.zeros(accel.shape)
    ref = [f['x_body'] if f['valid'] else math.nan for f in reference['frames']]
    sums = [np.zeros(accel.shape) for _ in plan]
    held = plan[-2]
    window = list(range(held['end'] - 8, held['end'])) if held['end'] - held['start'] >= 18 else []
    captured, trace = {}, []
    axis = _axis(reference['scenario'], last, delay)
    for t in range(last):
        a = axis[t]
        if a != 0 and family == 'E':
            v = v + (speed * a - v) * (accel / 1000.0)
        elif a != 0:
            target, step = speed * a, accel * DT
            v = np.where(np.abs(target - v) <= step, target, v + np.sign(target - v) * step)
        elif family in ('P', 'E'):
            v = v * brakes[0]
            v = np.where(np.abs(v) < brakes[1], 0.0, v)
        elif family == 'C':
            v = np.sign(v) * np.maximum(0.0, np.abs(v) * brakes[0] - brakes[1] * DT)
        else:
            step = brakes[0] * DT
            v = np.where(np.abs(v) <= step, 0.0, v - np.sign(v) * step)
        x = x + v * DT
        body = np.floor(x + 0.5) / HEART_PX
        if keep:
            trace.append(body.copy())
        for index, phase in enumerate(plan):
            if phase['start'] <= t < phase['end']:
                sums[index] += (ref[t] - body) ** 2
        if t in window or t in (plan[-1]['start'] - 1, last - 1):
            captured[t] = body
    rmse = np.sqrt(np.mean([s / (p['end'] - p['start']) for s, p in zip(sums, plan)], axis=0))
    end = captured[last - 1]
    result = {'rmse': rmse, 'end': end, 'stop': end - captured[plan[-1]['start'] - 1], 'speed': None, 'trace': trace}
    if window:
        times = np.array(window) / 60.0
        centered = times - times.mean()
        values = np.stack([captured[t] for t in window])
        result['speed'] = np.tensordot(centered, values - values.mean(axis=0), axes=(0, 0)) / (centered * centered).sum()
    return result


def gates(reference, original_rmse, result):
    """The frozen _ground_row gates, vectorized."""
    metrics = reference['metrics']
    tolerance = max(.15, 1 / reference['body_height_px'] + 1 / HEART_PX)
    ok = (result['rmse'] <= .15) & (1 - result['rmse'] / original_rmse >= .5)
    ok &= np.abs(result['end'] - metrics['end_position_body']) <= tolerance
    ok &= np.abs(result['stop'] - metrics['stop_distance_body']) <= tolerance
    if result['speed'] is not None:
        ok &= np.abs(result['speed'] - metrics['speed_body_s']) / abs(metrics['speed_body_s']) <= .1
    return ok


def _grids(family):
    speeds = np.arange(60, 201, 1.0)[:, None, None]
    if family == 'E':
        return np.arange(20, 1001, 10.0), speeds, (np.arange(0.50, 0.981, 0.01)[None, :, None], np.array([0.0, 1.0, 2.0])[None, None, :])
    accels = np.arange(200, 4001, 10.0)
    if family == 'P':
        return accels, speeds, (np.arange(0.50, 0.981, 0.005)[None, :, None], np.array([0.0, 0.5, 1.0, 2.0, 4.0])[None, None, :])
    if family == 'F':
        return accels, speeds, (np.arange(100, 6001, 20.0)[None, :, None],)
    return accels, speeds, (np.arange(0.60, 0.981, 0.02)[None, :, None], np.arange(0, 2001, 100.0)[None, None, :])


def _grid_description(family):
    first, speeds, brakes = _grids(family)
    names = {'P': ('retention', 'stop_speed'), 'F': ('friction',), 'E': ('retention', 'stop_speed'), 'C': ('retention', 'friction')}[family]
    def span(values):
        values = np.unique(np.asarray(values).ravel())
        return {'min': float(values.min()), 'max': float(values.max()), 'count': int(values.size)}
    return {'acceleration' if family != 'E' else 'gain_per_tick_x1000': span(first), 'run_speed_px_s': span(speeds),
            **{name: span(value) for name, value in zip(names, brakes)}}


def search(references, originals):
    rows = []
    for family in FAMILIES:
        first, speeds, brakes = _grids(family)
        for delay in (0, 1):
            total = passing = 0
            short_min, short_min_long_pass = math.inf, math.inf
            for value in first:
                short = simulate(references[SHORT], family, value, speeds, brakes, delay)
                long = simulate(references[LONG], family, value, speeds, brakes, delay)
                short_ok = gates(references[SHORT], originals[SHORT], short)
                long_ok = gates(references[LONG], originals[LONG], long)
                total += short['rmse'].size
                passing += int((short_ok & long_ok).sum())
                short_min = min(short_min, float(short['rmse'].min()))
                if long_ok.any():
                    short_min_long_pass = min(short_min_long_pass, float(np.where(long_ok, short['rmse'], np.inf).min()))
            rows.append({'family': family, 'description': FAMILIES[family], 'input_delay_ticks': delay,
                         'grid': _grid_description(family), 'combinations': total,
                         'pass_both_calibrations': passing, 'short_min_rmse_body': short_min,
                         'short_min_rmse_body_when_long_passes': short_min_long_pass})
    return rows


def validate_model(references, inputs, candidate_job):
    """Reproduce recorded traces before trusting the grid."""
    checks = [('original', lambda case: inputs.original(case, 'calibration'), 'F', 1000.0, 100.0, (1000.0,)),
              ('candidate-02', lambda case: inputs.candidate(candidate_job, case), 'P', 960.0, 120.5, (0.78, 1.0))]
    rows = []
    for name, loader, family, accel, speed, brake in checks:
        for case, reference in references.items():
            recorded = loader(case)
            result = simulate(reference, family, accel, speed, brake, 0, keep=True)
            predicted = np.array([float(v) for v in result['trace']])
            actual = np.array([f['x_body'] for f in recorded['frames'][:len(predicted)]])
            mismatch = [int(i) for i in np.flatnonzero(np.abs(predicted - actual) > 1e-9)]
            rows.append({'trace': name, 'case': case, 'frames': len(predicted), 'mismatched_frames': mismatch,
                         'model_rmse_body': float(result['rmse']),
                         'recorded_rmse_body': suite_game._ground_rmse(reference, recorded)})
    return rows


def run(suite, session, output):
    output = Path(output)
    _need(not output.exists(), 'Preserve every earlier diagnostic.')
    inputs = Inputs(suite, session)
    frozen = inputs.load(Path(suite) / 'suite.json')
    state = inputs.load(Path(session) / 'session.json')
    jobs = {c['candidate_id']: c['job_id'] for c in state['candidates']}
    table, signatures = [], {}
    for case in frozen['cases']:
        if case['scenario']['mechanic'] != 'ground':
            continue
        reference, original = inputs.reference(case['case_id']), inputs.original(case['case_id'], case['role'])
        before = suite_game._ground_rmse(reference, original)
        row = {'case_id': case['case_id'], 'role': case['role'],
               'events': [[e['tick'], e['action'], e['pressed']] for e in case['scenario']['events']],
               'original_rmse_body': before, 'required_rmse_body': .5 * before,
               'pixel_grid_floor_body': pixel_floor(reference),
               'reference_body_px': reference['body_height_px'], 'target_body_px': original['body_height_px']}
        row['floor_over_required'] = row['pixel_grid_floor_body'] / row['required_rmse_body']
        if case['role'] == 'calibration':
            row['candidates'] = {name: suite_game._ground_rmse(reference, inputs.candidate(job, case['case_id']))
                                 for name, job in sorted(jobs.items())}
        table.append(row)
        recipe = case['case_id'].rsplit('-r', 1)[0]
        signature = (round(before, 12), round(row['pixel_grid_floor_body'], 12))
        _need(signatures.setdefault(recipe, signature) == signature, 'Deterministic repeats differ: ' + case['case_id'])
    references = {case: inputs.reference(case) for case in (SHORT, LONG)}
    originals = {case: suite_game._ground_rmse(references[case], inputs.original(case, 'calibration')) for case in (SHORT, LONG)}
    validation = validate_model(references, inputs, jobs['candidate-02'])
    _need(all(not row['mismatched_frames'] for row in validation if row['case'] == SHORT),
          'The model does not reproduce the recorded short-run traces.')
    families = search(references, originals)
    result = {'kind': 'GroundShortRunPixelFeasibilityDiagnostic', 'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'suite': str(Path(suite)), 'session': str(Path(session)), 'producer_sha256': file_sha256(__file__),
              'suite_game_sha256': file_sha256(suite_game.__file__), 'input_files': inputs.files,
              'input_files_sha256': digest(inputs.files), 'short_case': SHORT, 'long_case': LONG,
              'short_required_rmse_body': .5 * originals[SHORT], 'cases': table, 'model_validation': validation,
              'physics_families': families,
              'any_family_passes_both_calibrations': any(row['pass_both_calibrations'] for row in families),
              'diagnostic_only': True, 'thresholds_changed': False, 'new_model_generation': False,
              'new_capture': False, 'historical_results_changed': False,
              'scope': ('Simple velocity families on the Heart pixel grid only. A non-physical per-frame trajectory can '
                        'still reach the pixel-grid floor, so this is not a proof that every program fails; it shows '
                        'where the frozen gate sits relative to the pixel quantization.')}
    output.mkdir(parents=True)
    with (output / 'result.json').open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')
    (output / 'producer.py').write_bytes(Path(__file__).read_bytes())
    return result


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('--suite', type=Path, default=Path('C:/kc3d/p/v2g01/suite'))
    parser.add_argument('--session', type=Path, default=Path('C:/kc3d/s/v2g01'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.suite, args.session, args.output)
    print(json.dumps({'status': 'pass', 'short_required_rmse_body': result['short_required_rmse_body'],
                      'any_family_passes_both_calibrations': result['any_family_passes_both_calibrations'],
                      'families': [{k: r[k] for k in ('family', 'input_delay_ticks', 'combinations', 'pass_both_calibrations',
                                                       'short_min_rmse_body', 'short_min_rmse_body_when_long_passes')}
                                   for r in result['physics_families']]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
