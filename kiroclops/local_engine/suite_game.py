"""Prospective 3D game scenarios and pixel measurements.

The existing portable protocol remains frozen. This module adds new input
recipes, labels previously observed recipes as regressions, and uses the same
60 Hz, 240-frame metric thresholds. Native state is used only by the separate
preservation suite. Callers hold the session's global capture lease.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import math
from pathlib import Path
import shutil
import struct
import subprocess
import uuid

import numpy as np

from kiroclops.common import ROOT
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.visual.compat import import_legacy_run, selection_from_legacy
from kiroclops.cloud.execution import analyze_package
from kiroclops.integration.storage import copy_project
from kiroclops.integration import ground_runner, private_desktop_v1 as private
from kiroclops.portable_v2 import runtime, measurement
from . import game, reliability

VERSION = 'phase3d-game-suite-v1'
TINY_COMMIT = 'c9b835c46ac5a98cf2aaaa0ee824378af14ff003'
TINY_ENGINE = '.tools/godot3/Godot_v3.6-stable_win64.exe'
TINY_ENGINE_SHA256 = 'aa19acae5bbc67bd7b79e20328ae2d7e2c2bb8589a2934c43c9126d742f60c97'
TINY_CONTROLLERS = {
    'src/actor/Actor.gd': '73995d3c383e218888b674328e3e0523fa2405ec89228077efe8cef22727c2f2',
    'src/actor/Player.gd': '837177de0fac49daeb7b350376fb0ed4605eac2235a45f5f0ff3ee2ba77d1b81',
}
FPS, FRAME_COUNT, WARMUP = 60, 240, 60
SPLITS = ('calibration', 'regression', 'validation')
_GROUND = {
    'r06': ((30, 'right', True), (36, 'right', False)),
    'r48': ((30, 'right', True), (78, 'right', False)),
    'r18': ((30, 'right', True), (48, 'right', False)),
    'l30': ((30, 'left', True), (60, 'left', False)),
    'reverse24': ((30, 'right', True), (54, 'right', False), (54, 'left', True), (78, 'left', False)),
    'r21': ((30, 'right', True), (51, 'right', False)),
    'l33': ((30, 'left', True), (63, 'left', False)),
    'reverse27': ((30, 'right', True), (57, 'right', False), (57, 'left', True), (78, 'left', False)),
    # V4 held-out recipes (PHASE3D_V4_PLAN.md): hold lengths never used before.
    'r36': ((30, 'right', True), (66, 'right', False)),
    'l42': ((30, 'left', True), (72, 'left', False)),
    'reverse30': ((30, 'right', True), (60, 'right', False), (60, 'left', True), (90, 'left', False)),
}
_IDS = {
    'jump': {'calibration': ('h03', 'h24'), 'regression': ('h08', 'h14'), 'validation': ('h10', 'h18')},
    'ground': {'calibration': ('r06', 'r48'), 'regression': ('r18', 'l30', 'reverse24'),
               'validation': ('r21', 'l33', 'reverse27')},
}
# V4: the V3 held-out ground recipes become known regression and three unseen
# recipes are added. V4 ground scenarios carry suite_registry='v4' so that
# canonical identity always resolves against the registry they were made from.
_IDS_V4 = {
    'jump': _IDS['jump'],
    'ground': {'calibration': ('r06', 'r48'), 'regression': ('r18', 'l30', 'reverse24', 'r21', 'l33', 'reverse27'),
               'validation': ('r36', 'l42', 'reverse30')},
}
REGISTRIES = {'v1': _IDS, 'v4': _IDS_V4}


def _need(condition, code, message):
    if not condition:
        raise VisualError(code, message)


def run_name(scenario):
    return f"{scenario['id']}-x{scenario['spawn_offset']}-r{scenario['repeat']}"


def scenarios(stage, split, registry='v1'):
    """Return independent copies of the registered prospective case recipes."""
    _need(stage in _IDS and split in SPLITS and registry in REGISTRIES, 'invalid_config',
          'Expected jump/ground, a registered suite split and a known recipe registry')
    result = []
    for sid in REGISTRIES[registry][stage][split]:
        for offset in ((0,) if split == 'calibration' else (0, 24)):
            for repeat in (1, 2, 3):
                if stage == 'jump':
                    hold = int(sid[1:])
                    events = ((30, 'jump', True), (30 + hold, 'jump', False))
                else:
                    events = _GROUND[sid]
                scenario = {'id': sid, 'mechanic': stage, 'split': split, 'repeat': repeat,
                    'spawn_offset': offset, 'fps': FPS, 'warmup_ticks': WARMUP, 'frame_count': FRAME_COUNT,
                    'events': [{'tick': t, 'action': a, 'pressed': p} for t, a, p in events]}
                if stage == 'jump':
                    scenario.update(start_tick=30, hold_ticks=hold)
                if registry != 'v1':
                    scenario['suite_registry'] = registry
                scenario['case_id'] = f"{stage}-{split}-{run_name(scenario)}"
                result.append(scenario)
    return result


def canonical_scenario(stage, scenario):
    """Identity includes its exact recipe and split, never its display name alone."""
    _need(isinstance(scenario, dict), 'invalid_config', 'A registered game scenario is required')
    registry = scenario.get('suite_registry', 'v1')
    _need(registry in REGISTRIES, 'scenario_mismatch', 'Unknown game recipe registry')
    expected = next((s for s in scenarios(stage, scenario.get('split'), registry)
                     if s['case_id'] == scenario.get('case_id')), None)
    _need(expected is not None and digest(expected) == digest(scenario),
          'scenario_mismatch', 'Scenario differs from its registered suite recipe')
    return deepcopy(expected)


def native_scenario(stage, scenario, *, variant=None):
    value = canonical_scenario(stage, scenario)
    value = {k: v for k, v in value.items() if k not in ('case_id', 'mechanic', 'fps', 'suite_registry')}
    if variant is not None:
        value['variant'] = variant
    return runtime.canonical_scenario(value)


def phases(scenario):
    """Use the frozen phase arithmetic with the new registered input durations."""
    value = canonical_scenario('ground', scenario)
    events = value['events']
    held = [e for e in events if e['pressed']]
    result = []
    for index, press in enumerate(held):
        release = next(e['tick'] for e in events
                       if e['action'] == press['action'] and not e['pressed'] and e['tick'] >= press['tick'])
        result.append({'name': 'held' if index == 0 else 'reversal', 'start': press['tick'],
                       'end': release, 'direction': 1 if press['action'] == 'right' else -1})
    release = events[-1]['tick']
    return result + [{'name': 'coast', 'start': release, 'end': release + 31, 'direction': 0}]


def _measure_ground(frames, scenario, body):
    """Frozen ground metric formulas applied to registered, arbitrary durations."""
    plan, errors = phases(scenario), []
    origin_frames = frames[20:30]
    if len(origin_frames) != 10 or not all(f['valid'] for f in origin_frames):
        return plan, {}, ['origin frames 20..29 must all be valid']
    origin = float(np.median([f['x'] for f in origin_frames]))
    if np.ptp([f['x'] for f in origin_frames]) > max(1., .05 * body):
        errors.append('pre-input horizontal origin is not stationary')
    for frame in frames:
        frame['x_body'] = (frame['x'] - origin) / body if frame['valid'] else None
    for phase in plan:
        selected = frames[phase['start']:phase['end']]
        phase['valid_ratio'] = sum(f['valid'] for f in selected) / len(selected)
        if phase['valid_ratio'] < .95:
            errors.append(f"valid frame ratio below 0.95 in {phase['name']} phase")
    held = plan[-2]
    window = list(range(held['end'] - 8, held['end'])) if held['end'] - held['start'] >= 18 else []
    speed = None
    if window:
        if not all(frames[t]['valid'] for t in window):
            errors.append('last-eight-frame speed window is incomplete')
        else:
            times = np.array(window, dtype=float) / FPS
            values = np.array([frames[t]['x_body'] for t in window])
            speed = float(np.dot(times - times.mean(), values - values.mean()) /
                          np.dot(times - times.mean(), times - times.mean()))
    release, endpoint = plan[-1]['start'], plan[-1]['end'] - 1
    end, at_release = frames[endpoint]['x_body'], frames[release - 1]['x_body']
    if end is None or at_release is None:
        errors.append('release or terminal position is not observable')
    metrics = {'speed_body_s': speed, 'speed_window': window, 'speed_direction': held['direction'],
        'speed_eligible': bool(window), 'stop_distance_body': end - at_release if end is not None and at_release is not None else None,
        'end_position_body': end, 'origin_x_px': origin, 'window_start': 30, 'window_end': endpoint}
    # Calibration IDs are the frozen IDs, so existing fitter diagnostics remain exact.
    if scenario['split'] == 'calibration':
        from kiroclops.ground.analysis import ground_diagnostics
        metrics['diagnostics'] = ground_diagnostics(frames, scenario, body)
    return plan, metrics, errors


def observation_to_motion(observations, manifest, annotation, stage, scenario):
    """Project common pixel coordinates. Engine positions are never accepted."""
    scenario = canonical_scenario(stage, scenario)
    # The jump projection already accepts all durations; its strict native lattice
    # checks are also reused to construct ground frames before domain extraction.
    if stage == 'jump':
        report = game.observation_to_motion(observations, manifest, annotation, 'jump')
        report['scenario'] = scenario
        return report
    body = annotation.get('body_height_px')
    _need(type(body) in (int, float) and math.isfinite(body) and body > 0,
          'invalid_selection', 'A fixed visually measured body height is required')
    _need(manifest.get('fps') == FPS and len(manifest.get('frames', [])) == FRAME_COUNT
          and len(observations.get('objects', [])) == 1, 'capture_timing_failure', 'Expected one object and 240 native frames')
    obj = observations['objects'][0]
    _need(len(obj.get('frames', [])) == len(observations.get('camera', [])) == FRAME_COUNT,
          'capture_timing_failure', 'Common observations are incomplete')
    frames = []
    for i, (native, observed, camera) in enumerate(zip(manifest['frames'], obj['frames'], observations['camera'])):
        _need(native.get('frame_index') == i and native.get('physics_tick') == WARMUP + i
              and observed.get('index') == i and abs(observed['time_s'] - i / FPS) <= 1e-5
              and abs(native['time_s'] - observed['time_s']) <= 1e-8,
              'capture_timing_failure', 'Native and common frame timing differs')
        frames.append({'frame_index': i, 'physics_tick': native['physics_tick'], 'time_s': observed['time_s'],
            'actions': deepcopy(native['actions']), **{k: observed.get(k) for k in ('x', 'y', 'score', 'margin', 'valid')},
            'mirrored': bool(observed.get('mirrored', False)), 'template_rotation': observed.get('rotation_deg', 0),
            'camera_valid': camera['status'] == 'pass', 'camera_shift_px': camera.get('max_shift_px'),
            'height_body': None, 'x_body': None})
    plan, metrics, errors = _measure_ground(frames, scenario, body)
    errors += [str(e.get('code', 'tracking_failure')) for e in observations.get('errors', [])]
    ratio, camera_valid = sum(bool(f['valid']) for f in frames) / FRAME_COUNT, all(f['camera_valid'] for f in frames)
    if ratio < .95: errors.append('valid frame ratio below 0.95')
    if not camera_valid: errors.append('fixed-camera condition failed')
    return {'schema_version': 1, 'status': 'rejected' if errors else 'ok', 'game': manifest['game'],
        'variant': manifest['variant'], 'scenario': scenario, 'mechanic': 'ground', 'fps': FPS,
        'body_height_px': float(body), 'valid_ratio': ratio, 'camera_valid': camera_valid,
        'events': {'first_input_tick': 30, 'final_release_tick': plan[-1]['start'],
                   'reversal_ticks': [p['start'] for p in plan if p['name'] == 'reversal']},
        'metrics': metrics, 'phases': plan, 'frames': frames, 'errors': errors,
        'annotation_validation': {'status': 'pending', 'reason': 'Independent raw-pixel control review required'},
        'provenance': {'measurement': 'common visual core; rendered pixels only',
            'package_sha256': observations['package_sha256'], 'observation_sha256': digest(observations),
            'opencv_version': observations.get('opencv_version'), 'domain_math_reuse_only': True}}


def _frame_hashes(run, viewport):
    directory = runtime._plain(Path(run) / 'frames')
    names = [f'{i:06d}.png' for i in range(FRAME_COUNT)]
    _need(directory.is_dir() and sorted(p.name for p in directory.iterdir()) == names,
          'capture_timing_failure', 'Exactly 240 native PNG files are required')
    hashes = {}
    for name in names:
        path = runtime._plain(directory / name)
        with path.open('rb') as stream:
            header = stream.read(24)
        _need(len(header) == 24 and header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR'
              and list(struct.unpack('>II', header[16:24])) == viewport,
              'capture_timing_failure', 'PNG dimensions differ from the native viewport')
        hashes[name] = file_sha256(path)
    return hashes


def _bind_manifest(manifest, stage, scenario, *, legacy=False):
    """Allow only the documented historical validation -> regression relabeling."""
    expected = native_scenario(stage, scenario)
    raw = manifest.get('scenario', {})
    _need(isinstance(raw, dict), 'scenario_mismatch', 'Native scenario must be an object')
    _need(manifest.get('status') == 'ok' and manifest.get('fps') == FPS
          and manifest.get('frame_count') == FRAME_COUNT and manifest.get('warmup_ticks', WARMUP) == WARMUP
          and manifest.get('settled') is True and manifest.get('normal_level') is False,
          'capture_timing_failure', 'Source recording is not a complete settled fixture run')
    split = 'validation' if legacy and scenario['split'] == 'regression' else scenario['split']
    for key in ('split', 'repeat', 'spawn_offset'):
        wanted = split if key == 'split' else expected[key]
        _need(raw.get(key) == wanted, 'scenario_mismatch', 'Native scenario differs: ' + key)
    for key in ('id', 'warmup_ticks', 'frame_count', 'events', 'start_tick', 'hold_ticks'):
        _need((legacy and key not in raw and key not in ('start_tick', 'hold_ticks') or
               key not in expected or raw.get(key) == expected[key]),
              'scenario_mismatch', 'Native recipe differs: ' + key)
    _need(manifest.get('viewport') == ([228, 128] if manifest.get('game') == 'tiny' else [320, 180]),
          'capture_timing_failure', 'Native viewport differs from the registered game')
    frames = manifest.get('frames')
    _need(isinstance(frames, list) and len(frames) == FRAME_COUNT,
          'capture_timing_failure', 'Exactly 240 native frame receipts are required')
    active = set()
    for i, frame in enumerate(frames):
        for event in expected['events']:
            if event['tick'] == i:
                (active.add if event['pressed'] else active.remove)(event['action'])
        _need(isinstance(frame, dict) and set(frame) == {'frame_index', 'physics_tick', 'time_s', 'actions'}
              and type(frame.get('frame_index')) is int and frame['frame_index'] == i
              and type(frame.get('physics_tick')) is int and frame['physics_tick'] == WARMUP + i
              and type(frame.get('time_s')) in (int, float) and math.isfinite(frame['time_s'])
              and abs(frame['time_s'] - i / FPS) <= 1e-5 and frame['actions'] == sorted(active),
              'capture_timing_failure', 'Native frame timing or input schedule differs')
    if manifest['game'] == 'tiny':
        _need(manifest.get('variant') == 'reference', 'wrong_role', 'Tiny recording must be a reference')
        provenance = manifest.get('provenance', {})
        _need(provenance.get('source_commit') == TINY_COMMIT,
              'source_changed', 'Tiny reference is not bound to the pinned commit')
        hashes = provenance.get('controller_hashes', {})
        _need(all(hashes.get(k) == v for k, v in TINY_CONTROLLERS.items()),
              'source_changed', 'Tiny controller differs from the pinned reference')
    else:
        _need(manifest['game'] == 'heart' and manifest.get('variant') in ('before', 'after'),
              'wrong_role', 'Target must be a before/after Heart recording')


def analyze_record(run, stage, scenario, output, *, check=lambda: None, progress=lambda *args: None,
                   candidate_sha256=None, legacy=False):
    scenario = canonical_scenario(stage, scenario)
    run, output = runtime._plain(run), runtime._plain(output)
    _need(not output.exists() and output != run and not output.is_relative_to(run),
          'unsafe_path', 'Measurement requires a new directory outside raw evidence')
    check(); manifest_path = run / 'manifest.json'; manifest_hash = file_sha256(manifest_path)
    manifest = read_json(manifest_path); _bind_manifest(manifest, stage, scenario, legacy=legacy)
    raw_hashes = _frame_hashes(run, manifest['viewport'])
    _need('raw_frame_hashes' not in manifest or raw_hashes == manifest['raw_frame_hashes'],
          'source_changed', 'Raw PNGs differ from the recorded frame hashes')
    annotation_path = game._annotation(stage, manifest['game']); annotation = read_json(annotation_path)
    output.mkdir(parents=True); import_legacy_run(run, output / 'package')
    selection = selection_from_legacy(run, annotation_path)
    selection['objects'][0]['search_radius'] = max(8, math.ceil(annotation['body_height_px'] * .75))
    observations = analyze_package(output / 'package', selection,
        {'min_valid_ratio': .95, 'require_static_camera': True,
         'camera_max_shift_px': annotation.get('camera_max_shift_px', 1.)}, check, progress,
        output=output / 'common-analysis', candidate_sha256=candidate_sha256)
    report = observation_to_motion(observations, manifest, annotation, stage, scenario)
    report['candidate_sha256'] = candidate_sha256
    from kiroclops.visual.events import extract_events
    event_set = extract_events(observations, selection)
    report['run_id'] = str(run)
    report['provenance'].update(manifest_sha256=manifest_hash, annotation_sha256=file_sha256(annotation_path),
        suite=VERSION, raw_frames_sha256=digest(raw_hashes), native_scenario=deepcopy(manifest['scenario']),
        legacy_regression_relabel=bool(legacy and scenario['split'] == 'regression'),
        historical_pixel_seal_present='raw_frame_hashes' in manifest)
    check()
    _need(file_sha256(manifest_path) == manifest_hash and _frame_hashes(run, manifest['viewport']) == raw_hashes,
          'source_changed', 'Source recording changed during pixel measurement')
    for name, value in (('selection', selection), ('observations', observations), ('events', event_set), ('analysis', report)):
        write_json(output / f'{name}.json', value)
    return {'scenario': scenario, 'case_id': scenario['case_id'], 'package': str(output / 'package'),
        'analysis': str(output / 'analysis.json'), 'observations': observations,
        'observations_path': str(output / 'observations.json'),
        'events': event_set, 'motion_events': report['events'], 'selection': selection,
        'raw_run': str(run), 'measurement_status': report['status'],
        'body_height_px': report['body_height_px'], 'source_manifest_sha256': manifest_hash,
        'raw_frames_sha256': digest(raw_hashes)}


def _short_runtime(runtime_root=None):
    base = runtime._plain(runtime_root or reliability.DEFAULT_RUNTIME_ROOT / 'runtime/game')
    directory = base / uuid.uuid4().hex[:12]
    reliability.check_path_budget([
        directory / 'engine/environment/tmp', directory / 'engine/environment/localappdata/Godot/app_userdata/KiroClops/phase3d-tiny/tmp',
        directory / 'import' / ('engine.' + runtime.VERSION) / 'environment/tmp',
        directory / 'import/clean-retry' / ('engine.' + runtime.VERSION) / 'environment/tmp',
        directory / 'raw' / ('engine.' + runtime.VERSION) / 'environment/tmp',
        directory / 'raw' / ('engine.' + runtime.VERSION) / 'environment/localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp',
    ])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _qualify_tiny():
    vendor, work = ROOT / 'vendor/tiny', ROOT / 'work/tiny'
    source = read_json(ROOT / 'config/sources.json')['tiny']
    _need(source['commit'] == TINY_COMMIT, 'source_changed', 'Tiny source registration changed')
    # Per-command safe.directory is read-only and never alters global Git config.
    args = ['git', '-c', f'safe.directory={vendor.as_posix()}', '-C', str(vendor)]
    head = subprocess.run(args + ['rev-parse', 'HEAD'], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    _need(head == TINY_COMMIT, 'source_changed', 'Vendor checkout is not the pinned Tiny commit')
    dirty = subprocess.run(args + ['status', '--porcelain', '--untracked-files=all'],
                           capture_output=True, text=True, timeout=15, check=True).stdout
    _need(not dirty.strip(), 'source_changed', 'Pinned Tiny vendor sources contain local changes')
    for name, expected in TINY_CONTROLLERS.items():
        _need(file_sha256(vendor / name) == file_sha256(work / name) == expected,
              'source_changed', 'Tiny motion source differs from the qualified controller')
    return vendor, work, runtime.source_hashes(vendor)


def _prepare_tiny(project, work):
    """Copy only generated imports from the qualified working source, then fixture."""
    # Import products/sidecars are caches; all gameplay assets come from vendor.
    cache = work / '.import'
    _need(cache.is_dir(), 'import_error', 'Tiny working import cache is unavailable')
    for path in sorted(cache.rglob('*')):
        runtime._plain(path)
        if path.is_file():
            target = project / '.import' / path.relative_to(cache)
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
    for path in work.rglob('*.import'):
        if '.import' in path.relative_to(work).parts: continue
        runtime._plain(path)
        target = project / path.relative_to(work)
        source_asset = Path(str(path)[:-7]); target_asset = Path(str(target)[:-7])
        if target_asset.is_file():
            _need(file_sha256(source_asset) == file_sha256(target_asset), 'source_changed', 'Cached Tiny asset differs from vendor')
            shutil.copy2(path, target)
    adapter = project / 'kiroclops_phase3d'; adapter.mkdir()
    for name in ('Capture.tscn', 'capture.gd'):
        shutil.copy2(ROOT / 'adapters/phase3d-tiny' / name, adapter / name)
    config = project / 'project.godot'; lines = config.read_text(encoding='utf-8-sig').splitlines()
    _need('[application]' in lines, 'invalid_config', 'Tiny application section is missing')
    lines = [line for line in lines if not line.startswith(('config/use_custom_user_dir=', 'config/custom_user_dir_name='))]
    index = next((i + 1 for i, line in enumerate(lines) if line.startswith('run/main_scene=')), lines.index('[application]') + 1)
    lines[index:index] = ['config/use_custom_user_dir=true', 'config/custom_user_dir_name="KiroClops/phase3d-tiny"']
    config.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _verify_private(proof, directory, argv, project, env=None, *, expected_environment_sha256=None):
    """Bind the existing private-desktop receipt to the pinned Godot 3 binary."""
    _need(isinstance(proof, dict) and proof.get('runner') == private.VERSION
          and proof.get('status') == 'completed' and proof.get('exit_code') == 0
          and proof.get('owned_process_termination_confirmed') is True and proof.get('cleanup_errors') == []
          and proof.get('policy') == runtime.PROCESS_POLICY and proof.get('argv') == argv
          and proof.get('cwd') == str(project) and proof.get('executable_sha256') == TINY_ENGINE_SHA256,
          'isolation_error', 'Tiny private process completion, command or cleanup differs')
    _need(read_json(directory / 'process.json') == proof, 'isolation_error', 'Saved and returned private receipts differ')
    for name in ('process-start.json', 'process-identity.json', 'stdout.log', 'stderr.log'):
        path, entry = directory / name, proof.get('evidence', {}).get(name, {})
        _need(path.is_file() and entry.get('sha256') == file_sha256(path) and entry.get('bytes') == path.stat().st_size,
              'isolation_error', 'Private process evidence differs: ' + name)
    start, identity = read_json(directory / 'process-start.json'), read_json(directory / 'process-identity.json')
    _need(start.get('runner') == identity.get('runner') == private.VERSION
          and start.get('status') == 'starting' and identity.get('status') == 'suspended_in_job'
          and identity.get('start_sha256') == file_sha256(directory / 'process-start.json'),
          'isolation_error', 'Private suspended process identity is missing')
    for key in ('argv', 'cwd', 'executable_sha256', 'policy', 'desktop_name', 'started_at',
                'timeout_seconds', 'environment_keys', 'environment_sha256'):
        _need(key in start and start[key] == proof.get(key), 'isolation_error', 'Process-start binding differs: ' + key)
    pid = proof.get('process_identity', {})
    _need(all(type(pid.get(k)) is int and pid[k] > 0 for k in ('pid', 'primary_thread_id', 'creation_filetime_100ns'))
          and identity.get('process_identity') == pid, 'isolation_error', 'Private process identity differs')
    desktop = proof.get('desktop_name'); desktop_path = proof.get('desktop_path')
    _need(isinstance(desktop, str) and desktop and isinstance(desktop_path, str)
          and desktop_path.endswith('\\' + desktop) and identity.get('desktop_path') == desktop_path,
          'isolation_error', 'Private desktop identity differs')
    samples = proof.get('desktop_samples')
    _need(isinstance(samples, list) and samples and all(isinstance(s, dict) and s.get('name') == desktop
          and s.get('receives_input') is False for s in samples) and proof.get('saw_owned_window') is True
          and any(type(s.get('owned_window_count')) is int and s['owned_window_count'] > 0 for s in samples),
          'isolation_error', 'Owned non-input desktop window proof is missing')
    try:
        times = [datetime.fromisoformat(t) for t in (proof['started_at'], identity['recorded_at'], proof['resumed_at'], proof['finished_at'])]
        _need(all(t.utcoffset() is not None for t in times) and times == sorted(times), 'isolation_error', 'Process timings differ')
    except (ValueError, KeyError, TypeError) as exc:
        raise VisualError('isolation_error', 'Private process timing evidence is invalid') from exc
    environment_hash = (hashlib.sha256(private.environment_block(env).encode('utf-16-le')).hexdigest()
                        if env is not None else expected_environment_sha256)
    _need(isinstance(environment_hash, str) and len(environment_hash) == 64
          and all(c in '0123456789abcdef' for c in environment_hash),
          'isolation_error', 'A bound private environment digest is required')
    _need(proof.get('environment_sha256') == environment_hash, 'isolation_error', 'Private environment binding differs')
    log = (directory / 'stdout.log').read_bytes() + (directory / 'stderr.log').read_bytes()
    _need(not runtime._SCRIPT_ERROR.search(log.decode('utf-8', 'replace')),
          'runtime_error', 'Tiny capture contains script/resource errors')


def _capture_tiny(scenario, directory, *, check, cancel, timeout=120):
    vendor, work, vendor_hashes = _qualify_tiny(); check()
    project, raw = directory / 'project', directory / 'raw'
    copy_project(vendor, project)
    _need(runtime.source_hashes(project) == vendor_hashes, 'source_changed', 'Tiny disposable copy differs from pinned vendor')
    _prepare_tiny(project, work); before = runtime.source_hashes(project)
    raw.mkdir(); config = native_scenario(scenario['mechanic'], scenario)
    config.update(output_dir=str(raw), normal_level=False)
    write_json(raw / 'capture_config.json', config)
    engine = runtime._plain(ROOT / TINY_ENGINE)
    _need(engine.is_file() and file_sha256(engine) == TINY_ENGINE_SHA256
          and file_sha256(Path(private.__file__)) == runtime.PRIVATE_SHA256,
          'engine_changed', 'Pinned Godot 3 executable/private transport differs')
    argv = [str(engine), '--path', str(project), '--fixed-fps', '60', '--resolution', '228x128',
            '--windowed', 'res://kiroclops_phase3d/Capture.tscn', '--video-driver', 'GLES2']
    engine_output = directory / 'engine'; engine_output.mkdir()
    env = runtime._environment(engine_output / 'environment', raw / 'capture_config.json')
    reliability.check_path_budget([Path(env['TMP']), Path(env['TEMP']),
        Path(env['LOCALAPPDATA']) / 'Godot/app_userdata/KiroClops/phase3d-tiny/tmp'])
    receipt = {'schema_version': 1, 'suite': VERSION, 'source_commit': TINY_COMMIT,
               'vendor_source_hashes': vendor_hashes, 'source_hashes_before': before,
               'argv': argv, 'capture_config_sha256': file_sha256(raw / 'capture_config.json'),
               'environment_sha256': hashlib.sha256(private.environment_block(env).encode('utf-16-le')).hexdigest()}
    try:
        with runtime._engine_lock(cancel, timeout) as remaining:
            proof = private.run_private_desktop(argv, cwd=project, env=env, output_dir=engine_output / 'process',
                                                timeout=remaining, cancel=cancel)
        _verify_private(proof, engine_output / 'process', argv, project, env); check()
        recording = read_json(raw / 'recording.json')
        ground_runner.validate_recording(recording, config, 'tiny')
        hashes = _frame_hashes(raw, [228, 128])
        manifest = {'schema_version': 1, 'run_id': scenario['case_id'], 'status': 'ok', 'game': 'tiny',
            'variant': 'reference', 'fps': FPS, 'viewport': [228, 128], 'frame_count': FRAME_COUNT,
            'warmup_ticks': WARMUP, 'normal_level': False, 'settled': True,
            'scenario': native_scenario(scenario['mechanic'], scenario), 'frames': recording['frames'],
            'raw_frame_hashes': hashes, 'raw_frames_sha256': digest(hashes),
            'provenance': {'source_commit': TINY_COMMIT, 'engine_sha256': TINY_ENGINE_SHA256,
                'controller_hashes': {k: file_sha256(project / k) for k in TINY_CONTROLLERS},
                'full_source_hashes_before': before, 'full_source_hashes_after': runtime.source_hashes(project),
                'coordinates_from_engine': False, 'capture_script_sha256': file_sha256(project / 'kiroclops_phase3d/capture.gd'),
                'runtime_receipt': str(engine_output / 'receipt.json'), 'process_receipt': str(engine_output / 'process/process.json'),
                'process_receipt_sha256': file_sha256(engine_output / 'process/process.json'),
                'capture_config_sha256': receipt['capture_config_sha256'], 'recording_sha256': file_sha256(raw / 'recording.json')}}
        receipt['status'] = 'completed'
    except BaseException as exc:
        receipt.update(status='error', code=getattr(exc, 'code', 'capture_error'), message=str(exc))
        raise
    finally:
        receipt['source_hashes_after'] = runtime.source_hashes(project)
        receipt['vendor_source_hashes_after'] = runtime.source_hashes(vendor)
        write_json(engine_output / 'receipt.json', receipt)
        _need(receipt['source_hashes_after'] == before and receipt['vendor_source_hashes_after'] == vendor_hashes,
              'source_changed', 'Tiny source changed during reference capture')
    manifest['provenance']['runtime_receipt_sha256'] = file_sha256(engine_output / 'receipt.json')
    write_json(raw / 'manifest.json', manifest)
    return raw


def build_reference(stage, scenario, output, *, check=lambda: None, progress=lambda *args: None,
                    cancel=None, runtime_root=None, reuse_known=True):
    """Reuse immutable known pixels or record fresh pixels from pinned Tiny."""
    scenario = canonical_scenario(stage, scenario); output = runtime._plain(output)
    _need(not output.exists(), 'output_exists', 'Reference output must be fresh')
    bridge = game._Cancellation(cancel, check); check(); runtime._cancel(bridge)
    name = run_name(scenario)
    known = ROOT / (f'artifacts/runs/tiny-reference-{name}' if stage == 'jump' else f'artifacts/phase2b/reference/{name}')
    legacy = reuse_known and scenario['split'] in ('calibration', 'regression') and (known / 'manifest.json').is_file()
    raw = known if legacy else _capture_tiny(scenario, _short_runtime(runtime_root), check=check, cancel=bridge)
    result = analyze_record(raw, stage, scenario, output, check=check, progress=progress, legacy=legacy)
    result['reference_kind'] = 'known_pixels' if legacy else 'fresh_pinned_capture'
    result['source_sha256'] = digest({'source_commit': TINY_COMMIT, 'controller_hashes': TINY_CONTROLLERS})
    return result


def verify_reference_record(record, stage):
    """Read-only Tiny source/PNG/native receipt validation for a frozen suite.

    Historical pixels are bound to their suite-creation snapshot, not represented
    as having a historical pixel seal or the newer private-desktop transport.
    """
    _need(isinstance(record, dict) and isinstance(record.get('raw_run'), str),
          'incomplete_review', 'The reference must retain its original native run identity')
    scenario = canonical_scenario(stage, record.get('scenario'))
    kind = record.get('reference_kind')
    _need(kind in ('known_pixels', 'fresh_pinned_capture'), 'wrong_role', 'Reference recording origin is missing')
    raw = runtime._plain(record['raw_run']); manifest_path = raw / 'manifest.json'
    manifest_sha = file_sha256(manifest_path); manifest = read_json(manifest_path)
    _need(record.get('source_manifest_sha256') == manifest_sha,
          'source_changed', 'Reference manifest differs from its measured snapshot')
    _bind_manifest(manifest, stage, scenario, legacy=kind == 'known_pixels')
    _need(manifest['game'] == 'tiny', 'wrong_role', 'Only pinned Tiny reference recordings are accepted')
    hashes = _frame_hashes(raw, [228, 128]); raw_sha = digest(hashes)
    _need(record.get('raw_frames_sha256') == raw_sha, 'source_changed', 'Reference PNG snapshot changed')
    _need('raw_frame_hashes' not in manifest or manifest['raw_frame_hashes'] == hashes,
          'source_changed', 'Native recorded PNG hashes changed')
    identity = digest({'source_commit': TINY_COMMIT, 'controller_hashes': TINY_CONTROLLERS})
    _need(record.get('source_sha256') == identity, 'source_changed', 'Pinned reference source identity changed')
    result = {'status': 'pass', 'reference_kind': kind, 'source_manifest_sha256': manifest_sha,
        'raw_frames_sha256': raw_sha, 'source_sha256': identity,
        'historical_pixel_seal_present': 'raw_frame_hashes' in manifest,
        'private_transport_verified': False}
    if kind == 'known_pixels':
        _need(scenario['split'] in ('calibration', 'regression'),
              'wrong_split', 'Previously observed pixels cannot become fresh validation')
        return result
    provenance = manifest['provenance']
    receipt_path = runtime._plain(provenance.get('runtime_receipt', ''))
    _need(receipt_path == raw.parent / 'engine/receipt.json'
          and provenance.get('runtime_receipt_sha256') == file_sha256(receipt_path),
          'isolation_error', 'Tiny reference runtime receipt changed')
    receipt = read_json(receipt_path); project = raw.parent / 'project'
    source = receipt.get('source_hashes_before')
    vendor = receipt.get('vendor_source_hashes')
    _need(receipt.get('status') == 'completed' and receipt.get('suite') == VERSION
          and receipt.get('source_commit') == TINY_COMMIT and isinstance(source, dict) and isinstance(vendor, dict)
          and receipt.get('source_hashes_after') == source and receipt.get('vendor_source_hashes_after') == vendor
          and provenance.get('full_source_hashes_before') == provenance.get('full_source_hashes_after') == source
          and runtime.source_hashes(project) == source,
          'source_changed', 'Tiny reference sources differ before/after or from the native copy')
    protected = {k: v for k, v in vendor.items() if k != 'project.godot'}
    _need(all(source.get(k) == v for k, v in protected.items())
          and set(source) == set(vendor) | {'kiroclops_phase3d/Capture.tscn', 'kiroclops_phase3d/capture.gd'}
          and all(source.get(k) == v for k, v in TINY_CONTROLLERS.items()),
          'source_changed', 'Tiny gameplay sources changed under the recorder overlay')
    for name in ('Capture.tscn', 'capture.gd'):
        _need(source.get('kiroclops_phase3d/' + name) == file_sha256(ROOT / 'adapters/phase3d-tiny' / name),
              'source_changed', 'Prospective Tiny recording adapter changed')
    engine = runtime._plain(ROOT / TINY_ENGINE)
    argv = [str(engine), '--path', str(project), '--fixed-fps', '60', '--resolution', '228x128',
            '--windowed', 'res://kiroclops_phase3d/Capture.tscn', '--video-driver', 'GLES2']
    _need(receipt.get('argv') == argv and file_sha256(engine) == TINY_ENGINE_SHA256
          and file_sha256(Path(private.__file__)) == runtime.PRIVATE_SHA256
          and provenance.get('coordinates_from_engine') is False,
          'isolation_error', 'Tiny reference command, engine or pixel-only recording differs')
    config_path, recording_path = raw / 'capture_config.json', raw / 'recording.json'
    config = read_json(config_path)
    _need(config == dict(native_scenario(stage, scenario), output_dir=str(raw), normal_level=False)
          and receipt.get('capture_config_sha256') == provenance.get('capture_config_sha256') == file_sha256(config_path)
          and provenance.get('recording_sha256') == file_sha256(recording_path),
          'scenario_mismatch', 'Tiny native input configuration or recording changed')
    recording = read_json(recording_path); ground_runner.validate_recording(recording, config, 'tiny')
    _need(recording['frames'] == manifest['frames'], 'capture_timing_failure', 'Manifest and recording frame receipts differ')
    process_path = runtime._plain(provenance.get('process_receipt', ''))
    _need(process_path == raw.parent / 'engine/process/process.json'
          and provenance.get('process_receipt_sha256') == file_sha256(process_path),
          'isolation_error', 'Tiny private process receipt changed')
    proof = read_json(process_path)
    _verify_private(proof, process_path.parent, argv, project,
                    expected_environment_sha256=receipt.get('environment_sha256'))
    result.update(private_transport_verified=True, runtime_receipt_sha256=file_sha256(receipt_path),
                  process_receipt_sha256=file_sha256(process_path), native_source_sha256=digest(source))
    return result


def capture_target(project, scenario, output, check=lambda: None, progress=lambda *args: None, cancel=None,
                   *, stage=None, variant='after', runtime_root=None, prepared=None,
                   prepared_job_id=None, prepared_role=None):
    stage = stage or scenario.get('mechanic'); scenario = canonical_scenario(stage, scenario)
    _need(variant in ('before', 'after', 'candidate', 'final'), 'wrong_role', 'Unknown target capture role')
    native_variant = 'before' if variant == 'before' else 'after'
    project, output = runtime._plain(project), runtime._plain(output)
    _need(not output.exists() and not output.is_relative_to(project), 'output_exists', 'Target measurement output must be fresh and outside source')
    bridge = game._Cancellation(cancel, check); check(); runtime._cancel(bridge)
    from .session import source_hashes as session_source_hashes
    session_before = session_source_hashes(project)
    before = runtime.source_hashes(project)
    if prepared is None:
        directory = _short_runtime(runtime_root)
        rendered = directory / 'project'; copy_project(project, rendered)
        _need(runtime.source_hashes(rendered) == before, 'source_changed', 'Target disposable copy differs')
        reliability.check_path_budget([
            directory / 'import' / ('engine.' + runtime.VERSION) / 'environment/tmp',
            directory / 'import/clean-retry' / ('engine.' + runtime.VERSION) / 'environment/tmp',
            directory / 'import' / ('engine.' + runtime.VERSION) / 'environment/localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp',
            directory / 'import/clean-retry' / ('engine.' + runtime.VERSION) / 'environment/localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp',
        ])
        runtime.import_project(rendered, directory / 'import', cancel=bridge)
        raw = directory / 'raw'
        reliability.check_path_budget([
            raw / ('engine.' + runtime.VERSION) / 'environment/tmp',
            raw / ('engine.' + runtime.VERSION) / 'environment/localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp'])
        runtime.capture_project(rendered, raw, native_scenario(stage, scenario, variant=native_variant), cancel=bridge)
    else:
        from . import suite_prepared_target
        _need(prepared_job_id is not None and prepared_role is not None,
              'prepared_target_changed', 'Prepared capture needs its trusted running job and source role')
        _need((prepared_role in ('original', 'before')) is (variant == 'before'),
              'wrong_role', 'Prepared source role differs from target capture role')
        raw = suite_prepared_target.capture_case(prepared, project,
            native_scenario(stage, scenario, variant=native_variant), job_id=prepared_job_id,
            role=prepared_role, expected_full_sources=session_before, check=check, cancel=bridge)
        rendered = raw.parent / 'project'
    result = analyze_record(raw, stage, scenario, output, check=check, progress=progress, candidate_sha256=digest(session_before))
    check()
    _need(runtime.source_hashes(project) == runtime.source_hashes(rendered) == before,
          'source_changed', 'Target source changed during capture or analysis')
    _need(session_source_hashes(project) == session_before,
          'source_changed', 'Immutable session source changed during capture or analysis')
    result['candidate_source_sha256'] = digest(session_before)
    result['source_sha256'] = digest(session_before)
    result['native_renderer_source_sha256'] = digest(before)
    result['capture_role'] = variant
    if prepared is not None:
        result = suite_prepared_target.attach_record(result, prepared, project,
            job_id=prepared_job_id, role=prepared_role, expected_full_sources=session_before)
    return result



def _report(record):
    if isinstance(record, dict) and 'frames' in record:
        return deepcopy(record)
    return read_json(record['analysis'] if isinstance(record, dict) else record)


def _reliable(report, stage):
    measurement._reliable(report)
    canonical_scenario(stage, report['scenario'])
    _need(type(report.get('body_height_px')) in (int, float) and math.isfinite(report['body_height_px'])
          and report['body_height_px'] > 0 and sum(f.get('valid') is True for f in report['frames']) / FRAME_COUNT >= .95,
          'tracking_failure', 'Actual frame coverage or body scale is unreliable')
    _need(all(type(f.get('valid')) is bool for f in report['frames']),
          'tracking_failure', 'Native frame validity must be explicitly boolean')
    if stage == 'ground':
        _need(all(not f['valid'] or type(f.get('x_body')) in (int, float) and math.isfinite(f['x_body'])
                  for f in report['frames']), 'tracking_failure', 'Valid ground frames need finite measured positions')
        for phase in phases(report['scenario']):
            selected = report['frames'][phase['start']:phase['end']]
            _need(sum(f.get('valid') is True and type(f.get('x_body')) in (int, float)
                      and math.isfinite(f['x_body']) for f in selected) / len(selected) >= .95,
                  'tracking_failure', 'Ground phase has less than 95% finite native coverage')


def _ground_rmse(reference, target):
    mse = []
    for phase in phases(reference['scenario']):
        pairs = [(reference['frames'][i]['x_body'], target['frames'][i]['x_body'])
                 for i in range(phase['start'], phase['end'])
                 if reference['frames'][i]['valid'] and target['frames'][i]['valid']]
        _need(len(pairs) / (phase['end'] - phase['start']) >= .95,
              'tracking_failure', 'Common native ground coverage is below 95%')
        mse.append(float(np.mean([(a - b) ** 2 for a, b in pairs])))
    return float(np.sqrt(np.mean(mse)))


def _ground_row(reference, original, candidate, require_improvement):
    old, new = _ground_rmse(reference, original), _ground_rmse(reference, candidate)
    improvement = 1 - new / old if old > 1e-12 else None
    ref, after = reference['metrics'], candidate['metrics']
    for field in ('stop_distance_body', 'end_position_body'):
        _need(all(type(m.get(field)) in (int, float) and math.isfinite(m[field]) for m in (ref, after)),
              'tracking_failure', 'Ground endpoint/coast metric is unobservable')
    plan = phases(reference['scenario']); eligible = plan[-2]['end'] - plan[-2]['start'] >= 18
    speed_error = None
    if eligible:
        _need(all(type(m.get('speed_body_s')) in (int, float) and math.isfinite(m['speed_body_s']) for m in (ref, after))
              and abs(ref['speed_body_s']) > 1e-12, 'tracking_failure', 'Eligible speed is unobservable')
        speed_error = abs(after['speed_body_s'] - ref['speed_body_s']) / abs(ref['speed_body_s'])
    tolerance = max(.15, 1 / reference['body_height_px'] + 1 / candidate['body_height_px'])
    stop, endpoint = abs(after['stop_distance_body'] - ref['stop_distance_body']), abs(after['end_position_body'] - ref['end_position_body'])
    gates = {'curve_rmse': new <= .15, 'improvement': not require_improvement or improvement is not None and improvement >= .5,
             'speed': not eligible or speed_error <= .1, 'stop_distance': stop <= tolerance, 'end_position': endpoint <= tolerance}
    return {'scenario': reference['scenario'], 'before_rmse_body': old, 'after_rmse_body': new,
        'candidate_rmse_body': new, 'improvement_fraction': improvement, 'improvement_required': require_improvement,
        'speed_relative_error': speed_error, 'speed_eligible': eligible, 'stop_distance_error_body': stop,
        'end_position_error_body': endpoint, 'position_tolerance_body': tolerance, 'gates': gates, 'pass': all(gates.values())}


def compare_records(reference, original, candidate, stage, *, require_improvement=True):
    """Require >=50% improvement against original clean baseline, never repair parent.

    Use require_improvement=False when independently checking whether a previously
    passing parent case still passes the unchanged absolute metric thresholds.
    """
    refs = [_report(v) for v in (reference, original, candidate)]
    for report in refs: _reliable(report, stage)
    scenario = refs[0]['scenario']
    _need(all(digest(r['scenario']) == digest(scenario) for r in refs),
          'scenario_mismatch', 'Comparison requires identical suite case recipes')
    reference_role = refs[0].get('game') == 'tiny' and refs[0].get('variant') == 'reference'
    parent_role = not require_improvement and refs[0].get('game') == 'heart' and refs[0].get('variant') == 'before'
    _need((reference_role or parent_role) and all(r.get('game') == 'heart' for r in refs[1:])
          and refs[1].get('variant') == 'before' and refs[2].get('variant') == 'after',
          'wrong_role', 'Comparison roles differ from reference/original/candidate or authoring-parent preservation')
    row = measurement._jump_row(*refs, require_improvement) if stage == 'jump' else _ground_row(*refs, require_improvement)
    row.update(case_id=scenario['case_id'], stage=stage, baseline_role='authoring_parent' if parent_role else 'original_clean', suite=VERSION,
               numerical_status='pass' if row['pass'] else 'fail', independent_pixel_review='pending',
               score=row['after_rmse_body'])
    return row


def preservation(project, baseline, output, stage, check=lambda: None, cancel=None):
    """Keep native ground/double/wall/coyote checks separate from pixel acceptance."""
    output = runtime._plain(output)
    _need(not output.exists(), 'output_exists', 'Preservation summary output must be fresh')
    native = _short_runtime() / 'preservation'
    planned = []
    for parent in (native, native / 'wall-coyote'):
        for role in ('baseline', 'candidate'):
            for operation in ('import', 'import/clean-retry', 'capture'):
                environment = parent / role / operation / ('engine.' + runtime.VERSION) / 'environment'
                planned.extend((environment / 'tmp', environment / 'temp',
                    environment / 'localappdata/Godot/app_userdata/KiroClops/portable-heart/tmp'))
    path_budget = reliability.check_path_budget(planned)
    from .session import source_hashes as session_source_hashes
    before = {'candidate': session_source_hashes(project), 'baseline': session_source_hashes(baseline)}
    result = game._preservation(project, baseline, native, stage, check, game._Cancellation(cancel, check))
    _need(session_source_hashes(project) == before['candidate'] and session_source_hashes(baseline) == before['baseline'],
          'source_changed', 'Preservation source changed during native checks')
    # Native receipts already bind PNGs, process proofs, source copies and the
    # combined wall/coyote comparison. Bind every stable evidence byte once more
    # to the small session-owned summary, without copying long runtime trees.
    evidence_hashes = {p.relative_to(native).as_posix(): file_sha256(runtime._plain(p))
                       for p in sorted(native.rglob('*')) if p.is_file()}
    output.mkdir(parents=True)
    index_path = output / 'native-evidence-index.json'
    write_json(index_path, {'kind': 'NativePreservationEvidenceIndex', 'suite': VERSION,
        'root': str(native), 'file_hashes': evidence_hashes, 'file_hashes_sha256': digest(evidence_hashes)})
    result.update(native_evidence_root=str(native), native_evidence_sha256=digest(evidence_hashes),
        native_evidence_index={'path': str(index_path), 'sha256': file_sha256(index_path), 'file_count': len(evidence_hashes)},
        source_sha256=digest(before['candidate']),
        baseline_source_sha256=digest(before['baseline']), path_budget=path_budget)
    write_json(output / 'comparison.json', result)
    return result


def profile(records, stage, output=None):
    """Fit only the six registered calibration clips; return measurements, no patch."""
    records = list(records)
    reports = [_report(record) for record in records]
    expected = {s['case_id'] for s in scenarios(stage, 'calibration')}
    actual = [r.get('scenario', {}).get('case_id') for r in reports]
    _need(len(actual) == len(set(actual)) and set(actual) == expected,
          'wrong_split', 'Profile requires the complete calibration matrix and no other clips')
    for report in reports:
        _reliable(report, stage)
        _need(report.get('game') == 'tiny' and report.get('variant') == 'reference',
              'wrong_role', 'Profile accepts pinned Tiny reference calibration pixels only')
    if output is None:
        # Existing fitters write their diagnostic JSON. Use the supplied evidence
        # directory by default; it stays outside reference source and raw pixels.
        first = records[0]
        _need(isinstance(first, dict) and first.get('analysis'), 'invalid_config',
              'An output path is required when fitting in-memory reports')
        output = Path(first['analysis']).parent.parent / f'{stage}-profile.json'
    output = runtime._plain(output)
    _need(not output.exists(), 'output_exists', 'Profile output must be fresh')
    analysis_values = []
    for index, (record, report) in enumerate(zip(records, reports)):
        if isinstance(record, dict) and record.get('analysis'):
            path = Path(record['analysis'])
        elif isinstance(record, (str, Path)):
            path = Path(record)
        else:
            path = output.parent / (output.stem + '-inputs') / f'{index:02}.json'
            _need(not path.exists(), 'output_exists', 'Profile input snapshot must be fresh')
            write_json(path, report)
        _need(read_json(path) == report, 'source_changed', 'Profile input changed after qualification')
        analysis_values.append(str(path))
    value = measurement.fit_reference(stage, analysis_values, output)
    value['suite'] = VERSION
    value['units'] = {'height': 'body heights', 'time': 'seconds', 'speed': 'body heights/second',
                      'acceleration': 'body heights/second squared', 'retention': 'per 60 Hz physics tick'}
    diagnostics = value.get('fit_diagnostics', {})
    value['confidence'] = {'model_mismatch': bool(diagnostics.get('model_mismatch')),
        'ambiguous_family': bool(diagnostics.get('ambiguous')), 'independent_pixel_review': 'pending',
        'scope': 'Calibration pixels and supported motion families only; no held-out results or reference code'}
    write_json(output, value)
    return value
