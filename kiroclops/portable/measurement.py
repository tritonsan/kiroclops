"""Pixel-only Heart measurements sharing the validated jump/ground mathematics."""
from __future__ import annotations
from copy import deepcopy
import math
from pathlib import Path
import numpy as np
from kiroclops.common import ROOT
from kiroclops.integration.storage import read_json, write_json, sha256, IntegrationError
from .specs import CONFIG, scenarios, run_name


def analyze(run, stage, output=None, annotation=None):
    if stage == 'jump':
        from kiroclops.analysis import analyze_run
    elif stage == 'ground':
        from kiroclops.ground.analysis import analyze_run
    else:
        raise ValueError('Unknown motion stage')
    annotation = Path(annotation or CONFIG/f'heart-{stage}-annotation.json')
    result = analyze_run(run, annotation, output)
    if result['status'] != 'ok':
        raise IntegrationError('tracking_failure', '; '.join(result['errors']))
    return result


def reference_runs(stage, split, output, check=lambda: None):
    """Reanalyze original immutable pixels, writing only new result files."""
    output = Path(output)
    annotation = ROOT / ('config/tiny-annotation.json' if stage == 'jump' else 'config/phase2b/tiny-annotation.json')
    paths = []
    for scenario in scenarios(stage, split):
        check()
        name = run_name(scenario)
        source = ROOT / (f'artifacts/runs/tiny-reference-{name}' if stage == 'jump'
                         else f'artifacts/phase2b/reference/{name}')
        path = output / f'{name}.json'
        analyze(source, stage, path, annotation)
        paths.append(str(path))
    return paths


def fit_reference(stage, paths, output):
    if stage == 'jump':
        from kiroclops.fitting import fit_profile
    else:
        from kiroclops.ground.fitting import fit_profile
    profile = fit_profile(paths, output)
    if profile.get('status') == 'model_mismatch' or profile.get('fit_diagnostics', {}).get('model_mismatch'):
        raise IntegrationError('model_mismatch', 'The registered motion families do not fit calibration pixels')
    return profile


def load_reports(values):
    return [deepcopy(v) if isinstance(v, dict) else read_json(Path(v)/'analysis.json' if Path(v).is_dir() else v)
            for v in values]


def _key(report):
    s = report['scenario']
    return (s.get('id') or f"h{s['hold_ticks']:02}", s.get('spawn_offset', 0), s['repeat'])


def _reliable(report):
    ratio = report.get('valid_ratio')
    if (report.get('status') != 'ok' or report.get('camera_valid') is not True
            or type(ratio) not in (int, float) or not math.isfinite(ratio) or not .95 <= ratio <= 1):
        raise IntegrationError('tracking_failure', 'Comparison input is not a reliable pixel measurement')
    frames = report.get('frames', [])
    if report.get('fps') != 60 or len(frames) != 240 or any(
            f.get('frame_index') != i or type(f.get('time_s')) not in (int, float)
            or not math.isfinite(f['time_s']) or abs(f['time_s']-i/60) > 1e-5
            or f.get('physics_tick') != 60+i for i,f in enumerate(frames)):
        raise IntegrationError('capture_timing_failure', 'Comparison requires exactly 240 ordered native frames')


def _jump_row(reference, before, after, require_improvement):
    from kiroclops.comparison import curve_rmse
    old, new = curve_rmse(reference, before), curve_rmse(reference, after)
    improvement = 1-new/old if old > 1e-12 else None
    rm, nm = reference['metrics'], after['metrics']
    for field in ('height_body', 'rise_s', 'fall_s'):
        if not all(type(m.get(field)) in (int, float) and math.isfinite(m[field]) and m[field] > 0 for m in (rm,nm)):
            raise IntegrationError('tracking_failure', 'Missing observable jump metric')
    height = abs(nm['height_body']-rm['height_body'])/rm['height_body']
    rise, fall = abs(nm['rise_s']-rm['rise_s']), abs(nm['fall_s']-rm['fall_s'])
    gates = {'curve_rmse': new <= .15,
             'improvement': not require_improvement or improvement is not None and improvement >= .5,
             'height': height <= .1, 'rise': rise <= max(2/60, .1*rm['rise_s']),
             'fall': fall <= max(2/60, .1*rm['fall_s'])}
    return {'scenario': reference['scenario'], 'before_rmse_body': old, 'after_rmse_body': new,
            'improvement_fraction': improvement, 'height_relative_error': height,
            'rise_error_s': rise, 'fall_error_s': fall, 'gates': gates, 'pass': all(gates.values())}


def compare_motion(stage, reference, before, after, split, *, baseline_diagnostic=False):
    if baseline_diagnostic and split != 'calibration':
        raise IntegrationError('wrong_split', 'Baseline diagnostics cannot open validation')
    groups = [load_reports(v) for v in (reference, before, after)]
    planned = {(s['id'], s['spawn_offset'], s['repeat']) for s in scenarios(stage, split)}
    mappings = []
    for role, group in zip(('reference', 'before', 'after'), groups):
        mapping = {}
        for report in group:
            _reliable(report)
            expected_game = 'tiny' if role == 'reference' else 'heart'
            expected_variant = 'reference' if role == 'reference' else 'before' if role == 'before' or baseline_diagnostic else 'after'
            if report.get('game') != expected_game or report.get('variant') != expected_variant:
                raise IntegrationError('wrong_role', 'Comparison game/variant does not match its role')
            if report['scenario']['split'] != split:
                raise IntegrationError('wrong_split', 'Comparison may not mix calibration and held-out observations')
            key = _key(report)
            intended = next((s for s in scenarios(stage,split) if (s['id'],s['spawn_offset'],s['repeat']) == key), None)
            if intended is None:
                raise IntegrationError('incomplete_matrix', 'Unregistered scenario')
            if stage == 'jump':
                if report['scenario'].get('hold_ticks') != intended['hold_ticks'] or report['scenario'].get('start_tick',30) != 30:
                    raise IntegrationError('wrong_scenario', 'Jump input duration/timing differs')
                if 'events' in report['scenario'] and report['scenario']['events'] != intended['events']:
                    raise IntegrationError('wrong_scenario', 'Explicit jump action schedule differs')
            elif report['scenario'].get('events') != intended['events']:
                raise IntegrationError('wrong_scenario', 'Ground input schedule differs')
            if key in mapping:
                raise IntegrationError('duplicate_scenario', 'A scenario cannot supply two results')
            mapping[key] = report
        if set(mapping) != planned:
            raise IntegrationError('incomplete_matrix', f'{role} comparison matrix differs from the registered scenarios')
        mappings.append(mapping)
    rows = []
    for key in sorted(planned):
        ref, old, new = (m[key] for m in mappings)
        if stage == 'jump':
            row = _jump_row(ref, old, new, split == 'validation')
        else:
            from kiroclops.ground.comparison import _triplet
            from kiroclops.integration.ground_comparison_v2 import speed_triplet
            row = speed_triplet(_triplet, ref, old, new, require_improvement=split == 'validation')
        row['reference_metrics'] = ref['metrics']
        row['target_metrics'] = new['metrics']
        rows.append(row)
    unresolved = any(r.get('speed_measurement_status') in ('indeterminate', 'invalid', 'model_mismatch') for r in rows)
    passed = all(row['pass'] for row in rows)
    return {'stage': stage, 'split': split, 'numerical_status': 'pass' if passed else 'pending' if unresolved else 'fail',
            'pass': passed, 'passed': sum(r['pass'] for r in rows), 'required': len(planned),
            'mean_rmse_body': float(np.mean([r['after_rmse_body'] for r in rows])),
            'scenario_checks': rows, 'measurement_status': 'pending',
            'measurement_limitation': 'Independent visual and source review is a separate acceptance gate'}


def public_comparison(result):
    """Return calibration detail without raw engine traces or backend evidence locations."""
    return {key: deepcopy(result[key]) for key in ('stage', 'split', 'numerical_status', 'pass',
        'passed', 'required', 'mean_rmse_body', 'scenario_checks', 'measurement_status')}


def diagnostics(stage, reference, target, profile):
    if stage == 'ground':
        from kiroclops.integration.calibration_motion_feedback_v3 import calibration_diagnostics
        return calibration_diagnostics(load_reports(reference), load_reports(target), profile)
    ref, new = load_reports(reference), load_reports(target)
    by_key = {_key(r): r for r in ref}
    return {'scope': 'calibration pixels only; no acceptance time shifting', 'samples': [
        {'scenario': r['scenario'], 'reference_events': by_key[_key(r)]['events'], 'target_events': r['events'],
         'reference_input_latency_s': by_key[_key(r)]['metrics']['input_latency_s'],
         'target_input_latency_s': r['metrics']['input_latency_s']}
        for r in new]}
