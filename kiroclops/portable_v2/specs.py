"""Immutable public target/experiment definitions for the second-target protocol."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from kiroclops.common import ROOT
from kiroclops.integration.storage import read_json, sha256, IntegrationError

VERSION = 'heart-transfer-chain-v2'
HEART_COMMIT = 'cfcc75db398bf429eece175b4181ab468e716d2c'
ENGINE_SHA256 = 'a829242096d640007de9fa93ea923d0f666d13d6a3b472a0231c820be6dc7627'
CONFIG = ROOT / 'config/phase2c-v2'
ARTIFACTS = ROOT / 'artifacts/phase2c-v2'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class TargetSpec:
    target_id: str = 'heart'
    source_commit: str = HEART_COMMIT
    source_url: str = 'https://github.com/uheartbeast/Heart-Platformer-Godot-4'
    engine_version: str = '4.5.1.stable.official.f62fdbde1'
    engine_sha256: str = ENGINE_SHA256
    character_scene: str = 'res://player.tscn'
    normal_scene: str = 'res://level_one.tscn'
    viewport: tuple[int, int] = (320, 180)
    fps: int = 60
    editable_files: tuple[str, ...] = ('player.gd',)
    editable_globs: tuple[str, ...] = ('motion/*.gd',)
    protected_resources: tuple[str, ...] = ('PlayerMovementData.gd', 'DefaultMovementData.tres',
                                           'player.tscn', 'project.godot')

    def record(self):
        value = json.loads(json.dumps(asdict(self)))
        value['actions'] = {'left': 'move_left', 'right': 'move_right', 'jump': 'jump'}
        lock = CONFIG / 'heart-source.json'
        if lock.is_file():
            value['source_lock_sha256'] = sha256(lock)
        value['definition_sha256'] = digest(value)
        return value


def get_target(target_id='heart'):
    if target_id != 'heart':
        raise IntegrationError('unsupported_target', f'No qualified portable target: {target_id}')
    return TargetSpec()


def scenarios(stage, split):
    if stage not in ('jump', 'ground') or split not in ('calibration', 'validation'):
        raise IntegrationError('invalid_experiment', 'Expected jump/ground and calibration/validation')
    values = []
    if stage == 'jump':
        recipes = [(f'h{hold:02}', [(30, 'jump', True), (30+hold, 'jump', False)], hold)
                   for hold in ((3, 24) if split == 'calibration' else (8, 14))]
    else:
        from kiroclops.ground.analysis import SCENARIOS
        names = ('r06', 'r48') if split == 'calibration' else ('r18', 'l30', 'reverse24')
        recipes = [(name, SCENARIOS[name], None) for name in names]
    for name, events, hold in recipes:
        for offset in ((0,) if split == 'calibration' else (0, 24)):
            for repeat in (1, 2, 3):
                value = {'id': name, 'mechanic': stage, 'split': split, 'repeat': repeat,
                         'spawn_offset': offset, 'fps': 60, 'warmup_ticks': 60, 'frame_count': 240,
                         'events': [{'tick': t, 'action': a, 'pressed': p} for t, a, p in events]}
                if hold is not None:
                    value.update(hold_ticks=hold, start_tick=30)
                values.append(value)
    return values


def run_name(scenario):
    return f"{scenario['id']}-x{scenario['spawn_offset']}-r{scenario['repeat']}"


def experiment(stage):
    from kiroclops.integration.ground_speed_v2 import LONG_HOLD_POLICY
    value = {'experiment_id': f'heart-{stage}-v2', 'schema_version': 5,
             'target_id': 'heart', 'stage': stage, 'mechanic': stage,
             'calibration': scenarios(stage, 'calibration'),
             'validation': scenarios(stage, 'validation'),
             'max_candidates': 3, 'max_seconds': 1800,
             'acceptance': {'minimum_valid_ratio': .95, 'maximum_curve_rmse_body': .15,
                 'minimum_improvement_fraction': .5, 'maximum_height_relative_error': .1,
                 'duration_tolerance': 'max(2/60,0.1*reference_duration_s)',
                 'position_tolerance': 'max(0.15,1/Hreference+1/Htarget)',
                 'speed_policy': LONG_HOLD_POLICY.record(), 'manual_distinct_points': 20,
                 'manual_position_tolerance': 'max(1px,0.05*H)', 'manual_event_tolerance_frames': 2},
             'reference_source': 'calibration rendered pixels only',
             'scope': ('normal/coyote jump; preserve ground/air-horizontal/double/wall/rights/hazard'
                       if stage == 'jump' else 'ground horizontal only; preserve jump parent and air/rights/hazard')}
    value['definition_sha256'] = digest(value)
    return value


def verify_source():
    record = read_json(CONFIG / 'heart-source.json')
    if record['commit'] != HEART_COMMIT or any(sha256(ROOT/'vendor/heart'/p) != h for p,h in record['files'].items()):
        raise IntegrationError('source_changed', 'Pinned upstream Heart source differs')
    if sha256(ROOT/'.tools/godot4/Godot_v4.5.1-stable_win64.exe') != ENGINE_SHA256:
        raise IntegrationError('engine_changed', 'Expected the qualified portable Godot 4.5.1 binary')
    return record


def verify_legacy():
    registry = read_json(ROOT/'artifacts/model-trials/autonomous-v4-cohort.json')
    # Resolve by actual registry structure, never silently skip the frozen map.
    maps = [registry.get(k) for k in ('source_hashes', 'protocol_source_hashes', 'sources')]
    maps.extend([v.get('source_hashes') for v in registry.values() if isinstance(v, dict)])
    files = next((m for m in maps if isinstance(m, dict) and len(m) == 61
                  and all(isinstance(v, str) and len(v) == 64 for v in m.values())), None)
    if files is None:
        raise IntegrationError('legacy_registry_invalid', 'Expected the original 61 frozen v4 sources')
    changed = [p for p,h in files.items() if not (ROOT/p).is_file() or sha256(ROOT/p) != h]
    if changed:
        raise IntegrationError('legacy_changed', f'Frozen v4 files changed: {changed}')
    old_registry = ROOT/'config/phase2c/cohorts/heart-02.json'
    if sha256(old_registry) != '59ff1fd9787597a98b36ae48791271e412676272438cc3e4fdbbfa9a5e22300d':
        raise IntegrationError('legacy_registry_changed', 'Original heart-02 registry changed')
    old = read_json(old_registry).get('protocol_sources', {})
    if len(old) != 20 or any(not (ROOT/p).is_file() or sha256(ROOT/p) != h for p,h in old.items()):
        raise IntegrationError('legacy_changed', 'Original 20 Heart protocol sources changed')
    return {**files, **old}


def protocol_sources():
    paths = [*sorted((ROOT/'kiroclops/portable_v2').glob('*.py')),
             *sorted((ROOT/'adapters/heart-godot4').glob('*')),
             *sorted((ROOT/'adapters/heart-integrity-godot4').glob('*')),
             *sorted((ROOT/'adapters/heart-wall-coyote-godot4-v2').glob('*'))]
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in paths if p.is_file()}
