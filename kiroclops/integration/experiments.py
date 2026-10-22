"""Immutable experiment contracts and explicit operator-only Phase 2B freezes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json

from kiroclops.common import ROOT
from .storage import IntegrationError, project_hashes, read_json, sha256, utc_now, write_json

LEGACY_ID = 'legacy-jump-v1'
GROUND_ID = 'ground-motion-v1'


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    schema_version: int
    calibration_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    repeats: tuple[int, ...] = (1, 2, 3)
    validation_offsets: tuple[int, ...] = (0, 24)
    fps: int = 60
    frame_count: int = 240
    warmup_ticks: int = 60
    max_candidates: int = 3
    max_seconds: int = 1800
    mechanic: str = 'jump'
    reference_package: str = 'artifacts/runs'
    parent_session_id: str | None = None
    parent_candidate_id: str | None = None
    acceptance_constants: tuple[tuple[str, float], ...] = ()

    def scenarios(self, split):
        if self.experiment_id != GROUND_ID or split not in {'calibration', 'validation'}:
            raise IntegrationError('invalid_experiment', 'Ground scenarios require a known ground split')
        recipes = {'r06': ((30, 'right', True), (36, 'right', False)),
                   'r48': ((30, 'right', True), (78, 'right', False)),
                   'r18': ((30, 'right', True), (48, 'right', False)),
                   'l30': ((30, 'left', True), (60, 'left', False)),
                   'reverse24': ((30, 'right', True), (54, 'right', False),
                                 (54, 'left', True), (78, 'left', False))}
        names = self.calibration_ids if split == 'calibration' else self.validation_ids
        offsets = (0,) if split == 'calibration' else self.validation_offsets
        return [{'id': name, 'split': split, 'repeat': repeat, 'spawn_offset': offset,
                 'events': [{'tick': t, 'action': a, 'pressed': p} for t, a, p in recipes[name]],
                 'frame_count': self.frame_count, 'warmup_ticks': self.warmup_ticks}
                for name in names for offset in offsets for repeat in self.repeats]

    def record(self):
        # JSON-shaped fresh copy; callers cannot mutate the dataclass contract.
        result = asdict(self)
        if self.experiment_id == LEGACY_ID:
            for key in ('mechanic', 'reference_package', 'parent_session_id', 'parent_candidate_id', 'acceptance_constants'):
                result.pop(key)
        else:
            values = dict(result.pop('acceptance_constants'))
            result['acceptance'] = {
                'ground': {**values, 'coverage': 'whole run and each compared phase',
                    'rmse': 'equal phase weight; native input ticks and standing-body scale',
                    'minimum_improvement_applies_to': 'validation only',
                    'speed_window': 'last eight native samples of an input segment lasting at least 18 ticks',
                    'stop_and_endpoint_tolerance_body': 'max(0.15, 1/reference_body_height_px + 1/target_body_height_px)',
                    'manual_error_tolerance_px': 'max(1, 0.05 * body_height_px)',
                    'manual_distinctness': 'native PNG SHA256', 'manual_categories': ['flip', 'brake', 'reverse'],
                    'required_validation_triplets': 18},
                'jump_regression': {'reference': 'artifacts/runs/tiny-reference-*',
                    'improvement_denominator': 'artifacts/runs/slay-before-*',
                    'parent_drift': 'diagnostic only; no second 50% improvement requirement against parent',
                    'required_validation_triplets': 12, 'minimum_valid_ratio': .95,
                    'maximum_curve_rmse_body': .15, 'minimum_improvement_fraction': .5,
                    'maximum_height_relative_error': .1,
                    'rise_fall_tolerance_s': 'max(2/60, 0.10 * reference_duration_s)',
                    'manual_points_per_game': 20, 'manual_error_tolerance_px': 'max(1, 0.05 * body_height_px)',
                    'manual_event_tolerance_frames': 2},
                'gameplay': {'required_probes': ['normal', 'ground', 'walk-double', 'wall'],
                    'wall_second_press_ticks': [55, 58], 'wall_launch': 'adjacent WallJump to Jump at absolute physics ticks 115..117'},
                'missing_independent_validation': 'pending; never a successful phase acceptance'}
            result['digest_bindings'] = {'tracking_lock': 'config/phase2b/tracking-lock.json',
                'raw_reference_lock': 'config/phase2b/frozen.json',
                'parent_source_hashes': 'tracking_lock.parent.source_hashes',
                'reference_source_hashes': 'tracking_lock.reference_source_hashes',
                'adapter_core_annotation_workflow_hashes': 'tracking_lock.files',
                'prepared_baseline_hashes': 'session.prepared_baseline_hashes'}
            result['definition_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return json.loads(json.dumps(result))


def get_experiment(experiment_id=LEGACY_ID):
    if experiment_id == LEGACY_ID:
        return ExperimentSpec(LEGACY_ID, 2, ('h03', 'h24'), ('h08', 'h14'))
    if experiment_id == GROUND_ID:
        return ExperimentSpec(GROUND_ID, 3, ('r06', 'r48'), ('r18', 'l30', 'reverse24'),
            mechanic='ground_horizontal', reference_package='artifacts/phase2b/reference',
            parent_session_id='ide-02', parent_candidate_id='candidate-01', acceptance_constants=(
                ('minimum_valid_ratio', .95), ('maximum_curve_rmse_body', .15),
                ('minimum_improvement_fraction', .5), ('maximum_speed_relative_error', .1),
                ('manual_points_per_game', 20), ('position_base_tolerance_body', .15),
                ('manual_minimum_tolerance_px', 1), ('manual_body_height_tolerance_fraction', .05)))
    raise IntegrationError('invalid_experiment', f'Unknown experiment: {experiment_id}')


def run_name(scenario):
    return f"{scenario['id']}-x{scenario['spawn_offset']}-r{scenario['repeat']}"


def _inside(root, value):
    root = Path(root).resolve()
    path = Path(value)
    path = root / path if not path.is_absolute() else path
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not path.is_file():
        raise IntegrationError('experiment_integrity', 'Locked evidence must be a regular workspace file')
    # Reject links at every path component, including Windows junctions.
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 0x400:
            raise IntegrationError('experiment_integrity', 'Linked experiment evidence is not allowed')
    return resolved


def ground_parent(root=ROOT):
    root = Path(root).resolve()
    directory = root / 'sessions/ide-02'
    state = read_json(directory / 'session.json')
    if state.get('status') != 'finished' or state.get('selected_candidate_id') != 'candidate-01':
        raise IntegrationError('parent_unverified', 'The approved IDE02 candidate must be finalized')
    final = state.get('final') or {}
    if final.get('numerical_status') != 'pass' or final.get('measurement_status') != 'pass':
        raise IntegrationError('parent_unverified', 'The parent needs numerical and independent visual validation')
    candidate_path = directory / 'candidates/candidate-01/candidate.json'
    candidate = read_json(candidate_path)
    project = candidate_path.parent / 'project'
    hashes = project_hashes(project)
    if not hashes or hashes != candidate.get('source_hashes'):
        raise IntegrationError('parent_unverified', 'The approved parent project changed')
    return {'session_id': 'ide-02', 'candidate_id': 'candidate-01',
            'project': project.relative_to(root).as_posix(), 'source_hashes': hashes,
            'candidate_record_sha256': sha256(candidate_path)}


def _tracking_files(root):
    names = ['kiroclops/integration/experiments.py', 'kiroclops/integration/ground_worker.py',
             'kiroclops/integration/ground_runner.py', 'kiroclops/integration/evaluation.py',
             'kiroclops/integration/runner.py', 'kiroclops/integration/storage.py',
             'kiroclops/integration/session.py', 'kiroclops/integration/worker.py',
             'kiroclops/integration/kiro.py', 'kiroclops/integration/mcp.py',
             'power/kiroclops/ground-motion.md',
             'config/phase2b/cohort.json']
    for directory, suffixes in [('kiroclops/ground', {'.py'}),
                                ('adapters/ground-godot3', {'.gd', '.tscn'}),
                                ('adapters/ground-godot4', {'.gd', '.tscn'})]:
        files = [p for p in (root / directory).rglob('*') if p.is_file() and p.suffix in suffixes]
        if not files:
            raise IntegrationError('missing_experiment_input', f'Ground implementation missing: {directory}')
        names.extend(p.relative_to(root).as_posix() for p in files)
    for game in ('tiny', 'slay'):
        name = f'config/phase2b/{game}-annotation.json'
        annotation = read_json(_inside(root, name))
        template = Path(annotation['template_run']) / 'frames' / f"{annotation['template_frame']:06d}.png"
        names.extend((name, _inside(root, template).relative_to(root).as_posix()))
    return {name: sha256(_inside(root, name)) for name in sorted(set(names))}


def _parent_jump_files(root):
    candidate = read_json(root / 'sessions/ide-02/candidates/candidate-01/candidate.json')
    result = {}
    for split, count in (('calibration', 6), ('validation', 12)):
        runs = candidate.get(f'{split}_runs', [])
        if len(runs) != count:
            raise IntegrationError('parent_unverified', 'The parent jump evidence matrix is incomplete')
        for value in runs:
            run = Path(value)
            for name in ('manifest.json', 'analysis.json'):
                path = _inside(root, run / name)
                result[path.relative_to(root).as_posix()] = sha256(path)
    return result


def freeze_ground_tracking(reference_project, *, root=ROOT):
    """Explicit preregistration before any held-out reference recording."""
    root = Path(root).resolve()
    destination = root / 'config/phase2b/tracking-lock.json'
    if destination.exists():
        raise IntegrationError('lock_exists', 'Tracking lock is immutable; do not overwrite it')
    for scenario in get_experiment(GROUND_ID).scenarios('validation'):
        run = root / 'artifacts/phase2b/reference' / run_name(scenario)
        if run.exists() and any(run.iterdir()):
            raise IntegrationError('holdout_before_freeze', 'Ground tracking must be locked before recording held-out reference footage')
    from scripts.run_suite import Suite
    phase1 = Suite(root).verify_frozen()
    project = Path(reference_project).resolve()
    if not project.is_relative_to(root):
        raise IntegrationError('invalid_project', 'Reference project must be inside this workspace')
    record = {'schema_version': 1, 'experiment': get_experiment(GROUND_ID).record(),
              'created_at': utc_now(), 'files': _tracking_files(root), 'parent': ground_parent(root),
              'parent_jump_files': _parent_jump_files(root),
              'phase1_frozen_files': phase1['files'],
              'reference_project': project.relative_to(root).as_posix(),
              'reference_source_hashes': project_hashes(project)}
    if not record['reference_source_hashes']:
        raise IntegrationError('invalid_project', 'Reference project cannot be empty')
    return write_json(destination, record)


def verify_ground_tracking(*, root=ROOT):
    root = Path(root).resolve()
    lock = read_json(root / 'config/phase2b/tracking-lock.json')
    if (lock.get('schema_version') != 1 or lock.get('experiment') != get_experiment(GROUND_ID).record()
            or lock.get('files') != _tracking_files(root) or lock.get('parent') != ground_parent(root)
            or lock.get('parent_jump_files') != _parent_jump_files(root)
            or project_hashes(root / lock['reference_project']) != lock.get('reference_source_hashes')):
        raise IntegrationError('experiment_integrity', 'Ground tracking, parent or reference sources changed after freeze')
    from scripts.run_suite import Suite
    if Suite(root).verify_frozen()['files'] != lock.get('phase1_frozen_files'):
        raise IntegrationError('experiment_integrity', 'Original jump experiment changed')
    return lock


def _reference_files(root, tracking):
    spec = get_experiment(GROUND_ID)
    files = {}
    for split in ('calibration', 'validation'):
        for scenario in spec.scenarios(split):
            run = root / 'artifacts/phase2b/reference' / run_name(scenario)
            manifest_path = _inside(root, run / 'manifest.json')
            manifest = read_json(manifest_path)
            provenance = manifest.get('provenance', {})
            if (manifest.get('game') != 'tiny' or manifest.get('variant') != 'reference'
                    or manifest.get('scenario') != scenario or manifest.get('fps') != 60
                    or manifest.get('frame_count') != 240 or manifest.get('status') != 'ok'
                    or provenance.get('full_source_hashes_before') != tracking['reference_source_hashes']
                    or provenance.get('full_source_hashes_after') != tracking['reference_source_hashes']):
                raise IntegrationError('reference_mismatch', f'Ground raw reference provenance mismatch: {run.name}')
            for path in [manifest_path, *[run / 'frames' / f'{i:06d}.png' for i in range(240)]]:
                path = _inside(root, path)
                files[path.relative_to(root).as_posix()] = sha256(path)
            if (run / 'video.mp4').is_file():
                path = _inside(root, run / 'video.mp4')
                files[path.relative_to(root).as_posix()] = sha256(path)
    return files


def freeze_ground_reference(*, root=ROOT):
    """Second freeze locks raw-only complete footage; it never analyzes held-out pixels."""
    root = Path(root).resolve()
    destination = root / 'config/phase2b/frozen.json'
    if destination.exists():
        raise IntegrationError('lock_exists', 'The completed reference lock is immutable')
    tracking = verify_ground_tracking(root=root)
    return write_json(destination, {'schema_version': 1, 'experiment_id': GROUND_ID,
                      'created_at': utc_now(), 'tracking_lock_sha256': sha256(root / 'config/phase2b/tracking-lock.json'),
                      'reference_files': _reference_files(root, tracking)})


def verify_ground_lock(*, root=ROOT):
    root = Path(root).resolve()
    tracking = verify_ground_tracking(root=root)
    frozen = read_json(root / 'config/phase2b/frozen.json')
    if (frozen.get('schema_version') != 1 or frozen.get('experiment_id') != GROUND_ID
            or frozen.get('tracking_lock_sha256') != sha256(root / 'config/phase2b/tracking-lock.json')
            or frozen.get('reference_files') != _reference_files(root, tracking)):
        raise IntegrationError('experiment_integrity', 'Ground reference pack changed after freeze')
    return {'tracking': tracking, 'reference': frozen,
            'tracking_lock_sha256': sha256(root / 'config/phase2b/tracking-lock.json'),
            'reference_lock_sha256': sha256(root / 'config/phase2b/frozen.json')}
