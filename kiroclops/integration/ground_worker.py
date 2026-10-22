"""Ground transfer jobs with original jump gates and immutable candidate sources."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import uuid
import math
import statistics

from kiroclops.common import ROOT
from .experiments import GROUND_ID, get_experiment, run_name, verify_ground_lock
from .storage import (IntegrationError, copy_project, identifier, project_hashes,
                      read_json, session_path, sha256, utc_now, write_json)


def provision_session(name=None, *, configure=True):
    """Copy only the approved game project; never carry old agent/reference data."""
    locked = verify_ground_lock(root=ROOT)
    name = identifier(name or f'ground-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}')
    directory = session_path(ROOT / 'sessions' / name)
    if directory.exists():
        raise IntegrationError('session_exists', 'A ground session must be fresh')
    spec = get_experiment(GROUND_ID)
    parent = locked['tracking']['parent']
    directory.mkdir(parents=True)
    state = {'schema_version': 3, 'experiment_id': GROUND_ID, 'experiment_spec': spec.record(),
             'session_id': name, 'mode': 'tools', 'status': 'provisioning', 'created_at': utc_now(),
             'started_at': None, 'deadline_at': None, 'target': str(directory / 'target'),
             'baseline': str(directory / 'baseline'), 'baseline_hashes': {},
             'source_commit': read_json(ROOT / 'config/sources.json')['slay']['commit'],
             'parent': parent, 'parent_source_hashes': parent['source_hashes'],
             'phase1_frozen_files': locked['tracking']['phase1_frozen_files'],
             'experiment_locks': {key: locked[key] for key in ('tracking_lock_sha256', 'reference_lock_sha256')},
             'candidates': [], 'reference': {}, 'baseline_runs': {}, 'selected_candidate_id': None,
             'active_job_id': None, 'final': None,
             'budget': {'max_candidates': spec.max_candidates, 'max_seconds': spec.max_seconds}}
    write_json(directory / 'session.json', state)
    try:
        from .ground_runner import prepare_project, import_project
        original = ROOT / parent['project']
        copy_project(original, directory / 'baseline')
        if project_hashes(original) != parent['source_hashes'] or project_hashes(directory / 'baseline') != parent['source_hashes']:
            raise IntegrationError('parent_unverified', 'Parent changed while preparing the ground baseline')
        prepare_project(directory / 'baseline', game='slay')
        import_project(directory / 'baseline', game='slay', runtime_dir=directory / 'runtime/setup')
        copy_project(directory / 'baseline', directory / 'target')
        state['baseline_hashes'] = project_hashes(directory / 'baseline')
        state.update(status='new', prepared_baseline_hashes=state['baseline_hashes'])
        write_json(directory / 'session.json', state)
        _export_raw_calibration(directory)
        if configure:
            from .kiro import configure_kiro
            configure_kiro(directory, mode='tools')
        return directory
    except Exception as exc:
        state.update(status='setup_failed', error=str(exc))
        write_json(directory / 'session.json', state)
        raise


def _export_raw_calibration(directory):
    from scripts.inspect_frames import contact_sheet
    output = Path(directory) / 'target/.kiroclops-reference'
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in get_experiment(GROUND_ID).scenarios('calibration'):
        if scenario['repeat'] != 1:
            continue
        name = run_name(scenario)
        source = ROOT / 'artifacts/phase2b/reference' / name
        manifest = read_json(source / 'manifest.json')
        target = output / name
        target.mkdir()
        if (source / 'video.mp4').is_file():
            shutil.copy2(source / 'video.mp4', target / 'video.mp4')
        width, height = manifest['viewport']
        contact_sheet(source, [0, 29, 30, 33, 36, 42, 54, 66, 78, 84, 96, 120],
                      [0, 0, width, height], target / 'frames.png', scale=2, columns=4)
        rows.append({'scenario': scenario, 'fps': 60, 'contact_sheet': f'{name}/frames.png',
                     'video': f'{name}/video.mp4' if (target / 'video.mp4').exists() else None})
    write_json(output / 'inputs.json', {'experiment_id': GROUND_ID, 'inputs': rows,
               'note': 'Calibration footage only; no held-out frames or prior fitted profile.'})


def verify_session(session):
    state = session.read()
    lock = verify_ground_lock(root=ROOT)
    if (state.get('schema_version') != 3 or state.get('experiment_id') != GROUND_ID
            or state.get('experiment_spec') != get_experiment(GROUND_ID).record()
            or state.get('mode') != 'tools' or state.get('parent') != lock['tracking']['parent']
            or state.get('experiment_locks') != {key: lock[key] for key in ('tracking_lock_sha256', 'reference_lock_sha256')}
            or state.get('prepared_baseline_hashes') != state.get('baseline_hashes')
            or project_hashes(state['baseline']) != state.get('baseline_hashes')):
        raise IntegrationError('experiment_integrity', 'Ground session contract or baseline differs from its immutable lock')
    return state


def _verify_source(project, expected):
    if project_hashes(project) != expected:
        raise IntegrationError('experiment_integrity', 'Game source changed outside its frozen snapshot')


def _copy_raw(original, copied):
    """No old fitted profile or analysis is promoted to a fresh measurement."""
    original, copied = Path(original), Path(copied)
    manifest = read_json(original / 'manifest.json')
    files = ['manifest.json', *[f'frames/{i:06d}.png' for i in range(manifest['frame_count'])]]
    if (original / 'video.mp4').is_file():
        files.append('video.mp4')
    hashes = {name: sha256(original / name) for name in files}
    if copied.exists():
        if not (copied / 'copy-origin.json').is_file() or read_json(copied / 'copy-origin.json') != {'source': str(original), 'hashes': hashes}:
            raise IntegrationError('experiment_integrity', 'Raw cache origin mismatch')
        if any(sha256(copied / name) != digest for name, digest in hashes.items()):
            raise IntegrationError('experiment_integrity', 'Raw cache pixels or manifest changed')
    else:
        copied.mkdir(parents=True)
        for name in files:
            target = copied / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original / name, target)
        write_json(copied / 'copy-origin.json', {'source': str(original), 'hashes': hashes})
    if any(sha256(original / name) != digest for name, digest in hashes.items()):
        raise IntegrationError('experiment_integrity', 'Raw source changed during copying')
    return copied


def _ground_annotation(session, game):
    annotation = read_json(ROOT / f'config/phase2b/{game}-annotation.json')
    # Independent controls are collected for these exact captured images later.
    # Do not transplant coordinates merely because game/scenario names match.
    annotation['control_points'], annotation['control_events'] = [], []
    path = session.directory / 'annotations/ground' / f'{game}-annotation.json'
    if path.exists():
        current = read_json(path)
        measured = lambda x: {k: v for k, v in x.items() if k not in {'control_points', 'control_events'}}
        if measured(current) != measured(annotation):
            raise IntegrationError('annotation_changed', 'Locked ground annotation settings changed')
    else:
        write_json(path, annotation)
    return path


def _tracking(report):
    if report.get('status') != 'ok' or report.get('valid_ratio', 0) < .95 or report.get('camera_valid') is not True:
        raise IntegrationError('tracking_failure', f"Invalid ground pixels: {report.get('errors', [])}")


def prepare_reference(session, split, check):
    from kiroclops.ground.analysis import analyze_run
    from kiroclops.ground.fitting import fit_profile
    state = session.read()
    if split == 'validation' and not (state.get('selected_candidate_id') and state.get('sealed_at')):
        raise IntegrationError('validation_sealed', 'Holdout pixels require a sealed candidate')
    runs = []
    for scenario in get_experiment(GROUND_ID).scenarios(split):
        check()
        name = run_name(scenario)
        run = _copy_raw(ROOT / 'artifacts/phase2b/reference' / name,
                        session.directory / 'reference' / split / name)
        _tracking(analyze_run(run, _ground_annotation(session, 'tiny')))
        runs.append(str(run))
    result = {'runs': runs, 'profile': None, 'profile_path': None}
    if split == 'calibration':
        path = session.directory / 'reference/measured-profile.json'
        result['profile'] = fit_profile([Path(r) / 'analysis.json' for r in runs], path)
        result['profile_path'] = str(path)
        if result['profile'].get('status') != 'ok':
            raise IntegrationError('model_mismatch', 'Reference motion does not support the preregistered ground model families')
    return result


def _ground_group(session, project, base, variant, split, expected, cancel, check, progress):
    from .ground_runner import capture_project
    from kiroclops.ground.analysis import analyze_run
    runs = []
    scenarios = get_experiment(GROUND_ID).scenarios(split)
    for index, scenario in enumerate(scenarios):
        check()
        _verify_source(project, expected)
        progress(f'Ground {variant}: {run_name(scenario)}', index, len(scenarios))
        run = Path(base) / run_name(scenario)
        capture_project(project, run, game='slay', scenario=scenario, variant=variant, cancel=cancel)
        _verify_source(project, expected)
        _tracking(analyze_run(run, _ground_annotation(session, 'slay')))
        runs.append(str(run))
    return runs


def _jump_before(session, split, check):
    """Original pre-transfer Slay is the unchanged 50% improvement denominator."""
    from scripts.run_suite import Suite
    from .evaluation import analyze_capture
    suite = Suite(ROOT)
    suite.verify_frozen()
    result = []
    for scenario in suite.scenarios(split, (('slay', 'before'),)):
        check()
        suite.verify_capture(scenario)
        source = suite.run_path(scenario)
        run = _copy_raw(source, session.directory / 'jump-regression/original-before' / split / source.name)
        analyze_capture(run, session.directory / 'annotations/jump')
        result.append(str(run))
    return result


def _jump_group(session, project, base, split, expected, cancel, check, progress):
    from .runner import capture_project
    from .evaluation import analyze_capture
    holds, offsets = ((3, 24), (0,)) if split == 'calibration' else ((8, 14), (0, 24))
    runs = []
    for hold in holds:
        for offset in offsets:
            for repeat in (1, 2, 3):
                check()
                _verify_source(project, expected)
                progress(f'Jump regression: h{hold:02d}-x{offset}-r{repeat}')
                run = Path(base) / f'slay-after-h{hold:02d}-x{offset}-r{repeat}'
                capture_project(project, run, hold_ticks=hold, variant='after', repeat=repeat,
                                spawn_offset=offset, cancel=cancel)
                _verify_source(project, expected)
                report = analyze_capture(run, session.directory / 'annotations/jump')
                if report.get('status') != 'ok':
                    raise IntegrationError('tracking_failure', 'Jump regression tracking failed')
                runs.append(str(run))
    return runs


def _jump_drift(runs, split):
    """Diagnostic only; original-before remains the acceptance denominator."""
    from .evaluation import _reports, _scenario_key
    from kiroclops.comparison import curve_rmse
    parent = read_json(ROOT / 'sessions/ide-02/candidates/candidate-01/candidate.json')
    old = {_scenario_key(r): r for r in _reports(parent.get(f'{split}_runs', []))}
    rows = []
    for current in _reports(runs):
        previous = old.get(_scenario_key(current))
        if previous is None:
            return {'status': 'unavailable', 'reason': 'Parent jump scenario is missing'}
        rows.append({'scenario': current['scenario'], 'rmse_from_parent_body': curve_rmse(previous, current),
                     'metric_deltas': {k: current['metrics'][k] - previous['metrics'][k]
                                       for k in ('height_body', 'rise_s', 'fall_s')}})
    return {'status': 'diagnostic', 'parent_session': 'ide-02', 'scenarios': rows,
            'note': 'No second 50% improvement requirement against the already successful parent.'}


def _paths(runs):
    return [Path(run) / 'analysis.json' for run in runs]


def _calibration_reports(session, runs):
    """Validate the complete public calibration matrix before exporting it."""
    required = {run_name(s): s for s in get_experiment(GROUND_ID).scenarios('calibration')}
    reports = {}
    for raw in runs:
        run = Path(raw).resolve()
        if not run.is_relative_to(session.directory.resolve()):
            raise IntegrationError('invalid_evidence', 'Calibration export must come from this session')
        manifest, report = read_json(run / 'manifest.json'), read_json(run / 'analysis.json')
        scenario = report.get('scenario', {})
        name = run_name(scenario)
        if (name not in required or scenario != required[name] or name in reports
                or manifest.get('scenario') != scenario or manifest.get('fps') != 60
                or report.get('fps') != 60 or manifest.get('frame_count') != 240
                or report.get('provenance', {}).get('manifest_sha256') != sha256(run / 'manifest.json')):
            raise IntegrationError('invalid_evidence', 'Only the exact six measured calibration runs can be exported')
        height = report.get('body_height_px')
        if type(height) not in (int, float) or not math.isfinite(height) or height <= 0:
            raise IntegrationError('invalid_evidence', 'Calibration has no observed standing body scale')
        frames = report.get('frames', [])
        if len(frames) != 240 or any(f.get('frame_index') != i or type(f.get('time_s')) not in (int, float)
                or not math.isfinite(f['time_s']) or abs(f['time_s'] - i / report['fps']) > 1e-5
                or type(f.get('valid')) is not bool or f['valid'] and
                   (type(f.get('x_body')) not in (int, float) or not math.isfinite(f['x_body']))
                for i, f in enumerate(frames)):
            raise IntegrationError('invalid_evidence', 'Calibration curves require complete native frame/time samples')
        _tracking(report)
        reports[name] = (run, manifest, report)
    if set(reports) != set(required) or len({v[2]['body_height_px'] for v in reports.values()}) != 1:
        raise IntegrationError('invalid_evidence', 'Calibration must contain all six runs with one consistent body scale')
    return reports


def export_calibration_evidence(session, label, runs, check=lambda: None):
    """Public measured curves and pixels, confined to the agent's workspace."""
    from scripts.inspect_frames import contact_sheet
    label = identifier(label)
    if label not in {'reference', 'baseline', 'candidate-01', 'candidate-02', 'candidate-03'}:
        raise IntegrationError('invalid_evidence', 'Unknown calibration export role')
    reports = _calibration_reports(session, runs)
    expected_game, expected_variant = ('tiny', 'reference') if label == 'reference' else ('slay', 'before' if label == 'baseline' else 'after')
    if any(report.get('game') != expected_game or report.get('variant') != expected_variant
           or manifest.get('game') != expected_game or manifest.get('variant') != expected_variant
           for _, manifest, report in reports.values()):
        raise IntegrationError('invalid_evidence', 'Calibration export game/role differs from its label')
    output = session.directory / 'target/.kiroclops-evidence' / label
    if output.exists() and any(output.iterdir()):
        raise IntegrationError('evidence_exists', 'Existing calibration exports are never overwritten')
    summaries = []
    fields = ('frame_index', 'time_s', 'physics_tick', 'actions', 'x_body', 'valid', 'score', 'margin',
              'camera_valid', 'camera_shift_px', 'mirrored')
    for name, (run, manifest, report) in reports.items():
        check()
        out = output / name
        out.mkdir(parents=True)
        valid = [f for f in report['frames'] if f['valid']]
        def summary(field, operation):
            values = [f[field] for f in valid if type(f.get(field)) in (int, float) and math.isfinite(f[field])]
            return operation(values) if values else None
        confidence = {'valid_ratio': report['valid_ratio'], 'camera_valid': report['camera_valid'],
                      'minimum_valid_score': summary('score', min), 'median_valid_score': summary('score', statistics.median),
                      'minimum_valid_margin': summary('margin', min), 'maximum_camera_shift_px': summary('camera_shift_px', max),
                      'manual_validation_status': report.get('annotation_validation', {}).get('status', 'pending')}
        row = {key: report.get(key) for key in ('game', 'variant', 'scenario', 'fps', 'body_height_px', 'metrics', 'events', 'phases')}
        row.update(confidence=confidence, measurement_path=str(out / 'measurements.json'))
        evidence = {'analysis_sha256': sha256(run / 'analysis.json'), 'manifest_sha256': sha256(run / 'manifest.json')}
        measurement = {**row, 'schema_version': 1, 'experiment_id': GROUND_ID,
                       'frames': [{field: f.get(field) for field in fields} for f in report['frames']], 'origin_evidence': evidence}
        write_json(out / 'measurements.json', measurement)
        row['measurement_sha256'] = sha256(out / 'measurements.json')
        if report['scenario']['repeat'] == 1:
            if (run / 'video.mp4').is_file():
                shutil.copy2(run / 'video.mp4', out / 'video.mp4')
                row.update(video_path=str(out / 'video.mp4'), video_sha256=sha256(out / 'video.mp4'))
            width, height = manifest['viewport']
            contact_sheet(run, [0, 29, 30, 33, 36, 42, 54, 66, 78, 84, 96, 120],
                          [0, 0, width, height], out / 'frames.png', scale=2, columns=4)
            row.update(contact_sheet_path=str(out / 'frames.png'), contact_sheet_sha256=sha256(out / 'frames.png'))
        summaries.append(row)
    first = next(iter(reports.values()))[2]
    result = {'schema_version': 1, 'experiment_id': GROUND_ID, 'label': label,
              'body_height_px': first['body_height_px'], 'fps': first['fps'], 'dt_s': 1 / first['fps'],
              'runs': summaries, 'index_path': str(output / 'index.json'),
              'note': 'Calibration only. Native input-aligned curves and raw pixels; manual validation remains a separate status.'}
    write_json(output / 'index.json', result)
    return result


def _prepare_feedback(session, reference, before, check):
    from kiroclops.ground.comparison import curve_rmse
    refs = _calibration_reports(session, reference['runs'])
    originals = _calibration_reports(session, before)
    public_reference = export_calibration_evidence(session, 'reference', reference['runs'], check)
    public_baseline = export_calibration_evidence(session, 'baseline', before, check)
    rows = []
    for name, (_, _, target) in originals.items():
        source = refs[name][2]
        rows.append({'scenario': target['scenario'], 'rmse_body': curve_rmse(source, target),
                     'reference_metrics': source['metrics'], 'baseline_metrics': target['metrics']})
    return {'reference_body_height_px': public_reference['body_height_px'],
            'target_body_height_px': public_baseline['body_height_px'], 'fps': public_reference['fps'], 'dt_s': public_reference['dt_s'],
            'units': {'x_body': 'signed displacement in standing body heights; positive is right',
                      'max_speed_body_s': 'standing body heights per second',
                      'accel_body_s2': 'standing body heights per second squared',
                      'brake_body_s2': 'standing body heights per second squared',
                      'retention_per_tick': 'dimensionless velocity multiplier for one native physics tick',
                      'rendered_pixel_conversion': 'Multiply body speed/acceleration by measured target_body_height_px; inspect target code for world-to-render scale',
                      'origin': 'median native x over frames 20..29; no time stretching or maximum-distance normalization',
                      'update_order': reference['profile'].get('fit_diagnostics', {}).get('integration')},
            'confidence': {'fit': reference['profile'].get('fit_diagnostics', {}),
                           'reference_tracking': [r['confidence'] for r in public_reference['runs']],
                           'baseline_tracking': [r['confidence'] for r in public_baseline['runs']]},
            'before_calibration': {'diagnostic_only': True, 'mean_rmse_body': statistics.mean(row['rmse_body'] for row in rows), 'runs': rows},
            'calibration_evidence': {'reference': public_reference, 'baseline': public_baseline}}


def combine_comparisons(ground, jump, parent_drift):
    """Shared final schema; manual review can change proof, never numeric gates."""
    numeric = 'pass' if ground.get('numerical_status') == jump.get('numerical_status') == 'pass' else 'fail'
    manuals = [ground.get('manual_validation_status', 'pending'), jump.get('manual_validation_status', 'pending')]
    manual = 'fail' if 'fail' in manuals else ('pass' if all(v == 'pass' for v in manuals) else 'pending')
    status = 'fail' if numeric == 'fail' or manual == 'fail' else ('pass' if manual == 'pass' else 'pending')
    return {'schema_version': 3, 'experiment_id': GROUND_ID, 'status': status, 'success': status == 'pass',
            'numerical_status': numeric, 'manual_validation_status': manual, 'measurement_status': manual,
            'ground': ground, 'jump_regression': jump, 'parent_jump_drift': parent_drift,
            'validation_scenarios': ground.get('validation_scenarios', []),
            'coverage': ground.get('coverage', {}), 'mean_rmse_body': ground.get('mean_rmse_body')}


def run_action(session, job, *, cancel, check, progress):
    """Executed inside the legacy worker's cancellation/ownership envelope."""
    from .session import create_candidate_snapshot
    from . import evaluation
    from .ground_runner import import_project, smoke_project
    from kiroclops.ground import comparison
    check()
    state = verify_session(session)
    check()
    action = job['action']
    if action == 'prepare':
        progress('Re-analyzing frozen ground calibration and fitting a fresh profile')
        reference = prepare_reference(session, 'calibration', check)
        before = _ground_group(session, Path(state['baseline']), session.directory / 'baseline-runs/calibration',
                               'before', 'calibration', state['baseline_hashes'], cancel, check, progress)
        jump_ref = evaluation.prepare_reference(session.directory / 'jump-regression/reference', split='calibration')
        jump_before = _jump_before(session, 'calibration', check)
        public = _prepare_feedback(session, reference, before, check)
        session.update(lambda s: s.update(status='ready', reference={
            'calibration': reference['runs'], 'profile': reference['profile'], 'profile_path': reference['profile_path'],
            'jump': {'calibration': jump_ref['runs']}}, baseline_runs={
            'calibration': before, 'jump': {'calibration': jump_before}}, preparation_feedback=public))
        result = {'status': 'ready', 'experiment_id': GROUND_ID, 'reference_profile': reference['profile'],
                  'reference_runs': len(reference['runs']), 'baseline_runs': len(before),
                  **public,
                  'integration': 'Edit ground acceleration, speed cap and release behavior; preserve the inherited jump and state interfaces.'}
    elif action == 'evaluate':
        cid = job['candidate_id']
        root = create_candidate_snapshot(session, cid)
        import_project(root / 'project', game='slay', runtime_dir=session.directory / 'runtime' / cid / 'import', cancel=cancel)
        candidate = read_json(root / 'candidate.json')
        expected = candidate['source_hashes']
        runs = _ground_group(session, root / 'project', root / 'runs/calibration', 'after', 'calibration',
                             expected, cancel, check, progress)
        jump_runs = _jump_group(session, root / 'project', root / 'jump-runs/calibration', 'calibration',
                                expected, cancel, check, progress)
        feedback = comparison.calibration_feedback(_paths(state['reference']['calibration']),
                   _paths(state['baseline_runs']['calibration']), _paths(runs))
        jump_feedback = evaluation.calibration_feedback(state['reference']['jump']['calibration'],
                        state['baseline_runs']['jump']['calibration'], jump_runs)
        check()
        smoke = smoke_project(root / 'project', root / 'smoke', cancel=cancel)
        _verify_source(root / 'project', expected)
        error = feedback.get('mean_rmse_body')
        feedback['eligible'] = (feedback.get('eligible') is True and feedback.get('diagnostic_gates_pass') is True
                                and feedback.get('status') == 'ok' and isinstance(error, (int, float))
                                and math.isfinite(error) and error >= 0
                                and jump_feedback.get('diagnostic_gates_pass') is True
                                and smoke.get('status') in {'pass', 'passed'})
        feedback['jump_regression'] = jump_feedback
        feedback['parent_jump_drift'] = _jump_drift(jump_runs, 'calibration')
        public = export_calibration_evidence(session, cid, runs, check)
        candidate.update(status='evaluated', calibration_runs=runs, jump_calibration_runs=jump_runs,
                         feedback=feedback, smoke=smoke, calibration_evidence=public, finished_at=utc_now())
        write_json(root / 'candidate.json', candidate)
        session.update(lambda s: (s['candidates'].__setitem__(int(cid[-2:]) - 1, candidate), s.update(status='ready')))
        gameplay = {key: smoke[key] for key in ('status', 'checks', 'error', 'cancelled') if key in smoke}
        gameplay['runs'] = {name: {'path': item.get('path'), 'checks': item.get('checks')}
                            for name, item in smoke.get('runs', {}).items()}
        gameplay['receipt_path'] = str(root / 'smoke/smoke.json')
        result = {'candidate_id': cid, 'status': 'evaluated', 'calibration': feedback, 'gameplay': gameplay,
                  'calibration_evidence': public}
    elif action == 'finalize':
        cid = job['candidate_id']
        if state.get('selected_candidate_id') != cid or not state.get('sealed_at'):
            raise IntegrationError('validation_sealed', 'Validation requires the selected immutable candidate')
        root = session.directory / 'candidates' / cid
        candidate = read_json(root / 'candidate.json')
        _verify_source(root / 'project', candidate['source_hashes'])
        reference = prepare_reference(session, 'validation', check)
        before = _ground_group(session, Path(state['baseline']), session.directory / 'baseline-runs/validation',
                               'before', 'validation', state['baseline_hashes'], cancel, check, progress)
        after = _ground_group(session, root / 'project', root / 'runs/validation', 'after', 'validation',
                              candidate['source_hashes'], cancel, check, progress)
        jump_ref = evaluation.prepare_reference(session.directory / 'jump-regression/reference', split='validation')
        jump_before = _jump_before(session, 'validation', check)
        jump_after = _jump_group(session, root / 'project', root / 'jump-runs/validation', 'validation',
                                 candidate['source_hashes'], cancel, check, progress)
        ground = comparison.compare_runs(_paths(state['reference']['calibration'] + reference['runs']),
                    _paths(state['baseline_runs']['calibration'] + before),
                    _paths(candidate['calibration_runs'] + after), split='validation')
        jump = evaluation.final_comparison(state['reference']['jump']['calibration'] + jump_ref['runs'],
                    state['baseline_runs']['jump']['calibration'] + jump_before,
                    candidate['jump_calibration_runs'] + jump_after)
        combined = combine_comparisons(ground, jump, _jump_drift(jump_after, 'validation'))
        numeric, manual, status = (combined[key] for key in ('numerical_status', 'manual_validation_status', 'status'))
        path = write_json(session.directory / 'final/comparison.json', combined)
        candidate.update(validation_runs=after, jump_validation_runs=jump_after, status='finalized')
        write_json(root / 'candidate.json', candidate)
        final = {'comparison_path': str(path), 'numerical_status': numeric, 'measurement_status': manual,
                 'manual_validation_status': manual, 'overall_status': status, 'candidate_id': cid,
                 'smoke_status': candidate['smoke']['status'], 'ground_status': ground.get('numerical_status'),
                 'jump_status': jump.get('numerical_status')}
        def publish(s):
            s['reference']['validation'] = reference['runs']
            s['reference']['jump']['validation'] = jump_ref['runs']
            s['baseline_runs']['validation'] = before
            s['baseline_runs']['jump']['validation'] = jump_before
            s['candidates'][int(cid[-2:]) - 1] = candidate
            s.update(status='finished', final=final, finished_at=utc_now())
        session.update(publish)
        result = {'status': 'finished', **final, 'validation': combined}
    elif action == 'report':
        from .reporting import generate_session_report
        for candidate in state['candidates']:
            if candidate.get('source_hashes'):
                _verify_source(session.directory / 'candidates' / candidate['candidate_id'] / 'project', candidate['source_hashes'])
        result = {'status': 'ready', 'report_path': str(generate_session_report(session.directory))}
    else:
        raise IntegrationError('unknown_action', 'Unsupported ground action')
    check()
    verify_session(session)
    return result
