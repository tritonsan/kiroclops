"""Immutable multi-clip reference evidence and prospective editing stages.

This module measures and runs applications. It never creates a behavior patch.
Original comparison sources and authoring parents are deliberately distinct.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import statistics

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from kiroclops.visual.package import copy_package, load_package
from .session import Session, _ordinary, copy_project, create_session, source_hashes

VERSION = 1
ROLES = ('calibration', 'regression', 'validation')
PROVENANCE = ('recorder_verified', 'user_declared', 'unknown')
LIMITS = {'minimum_valid_ratio': .95, 'required_improvement_fraction': .5,
          'game_curve_error_bodies': .15, 'game_height_difference_fraction': .10,
          'web_curve_error_fraction': .03, 'raw_control_points_per_object': 20}
ROOT=Path(__file__).resolve().parents[2]
LEGACY_GAME_PROFILE = 'legacy-game-v1'
# V3: the 50% relative gain gate is applied only where the measurement can
# resolve it. Both pixel grids round one continuous motion; their irreducible
# RMS disagreement is the floor below. Absolute game gates never change.
GROUND_FLOOR_PROFILE = 'ground-floor-aware-v1'
GROUND_FLOOR_RULE = {
    'id': GROUND_FLOOR_PROFILE, 'schema_version': 1, 'stage': 'ground',
    'required_improvement_fraction': .5, 'resolvability_factor': 1.5,
    'floor_body': 'sqrt(((1/reference_body_px)^2 + (1/candidate_body_px)^2) / 12)',
    'gain_gate_applies_when': 'required_improvement_fraction * original_rmse_body >= resolvability_factor * floor_body',
    'otherwise_preservation': 'candidate_rmse_body <= max(original_rmse_body, resolvability_factor * floor_body)',
    'absolute_gates_unchanged': True,
}
# V4 (PHASE3D_V4_PLAN.md): the speed gate reads a slope the 7 px reference can
# resolve. Speed is the least-squares slope of the last 16 held frames, the gate
# applies only when that phase lasts at least 24 frames, and its tolerance never
# falls below the slope's own one-pixel stall bound. The V3 gain rule is kept.
GROUND_SPEED_PROFILE = 'ground-floor-speed-aware-v1'
SPEED_WINDOW_FRAMES = 16
SPEED_MIN_HELD_FRAMES = 24
GROUND_SPEED_RULE = {
    'id': GROUND_SPEED_PROFILE, 'schema_version': 1, 'stage': 'ground',
    'improvement_rule': deepcopy(GROUND_FLOOR_RULE),
    'speed_eligible_when': 'last held phase lasts at least 24 frames',
    'speed_window': 'last 16 frames of the last held phase; least-squares slope of x_body',
    'stall_bound_px_per_frame': '3N / (2(N^2 - 1)) with N = 16',
    'speed_tolerance': 'max(0.10, stall_bound * fps * (1/reference_body_px + 1/candidate_body_px) / |reference_speed|)',
    'other_absolute_gates_unchanged': True,
}
GAME_PROFILES = (LEGACY_GAME_PROFILE, GROUND_FLOOR_PROFILE, GROUND_SPEED_PROFILE)
GROUND_RULES = {GROUND_FLOOR_PROFILE: GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE: GROUND_SPEED_RULE}


def ground_measurement_floor(reference_body_px, candidate_body_px):
    """Irreducible RMS (body units) of one continuous motion drawn on two pixel grids."""
    heights = (reference_body_px, candidate_body_px)
    if any(type(h) not in (int, float) or not math.isfinite(h) or h <= 0 for h in heights):
        raise VisualError('invalid_suite', 'The ground floor needs finite positive body heights.')
    return math.sqrt(((1 / reference_body_px) ** 2 + (1 / candidate_body_px) ** 2) / 12)


def _ground_floor_gate(row, reference, original, candidate):
    """Apply the frozen floor-aware gain rule to one primary ground comparison."""
    from . import suite_game
    reports = [suite_game._report(record) for record in (reference, original, candidate)]
    heights = [report.get('body_height_px') for report in reports]
    if heights[1] != heights[2]:
        raise VisualError('invalid_suite', 'Original and candidate must share one fixed target body height.')
    floor = ground_measurement_floor(heights[0], heights[2])
    old, new, gain = row.get('before_rmse_body'), row.get('after_rmse_body'), row.get('improvement_fraction')
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (old, new)):
        raise VisualError('tracking_failure', 'Ground RMSE is unobservable.')
    rule = GROUND_FLOOR_RULE
    applies = rule['required_improvement_fraction'] * old >= rule['resolvability_factor'] * floor
    bound = max(old, rule['resolvability_factor'] * floor)
    if applies:
        gate = gain is not None and gain >= rule['required_improvement_fraction']
    else:
        gate = new <= bound + 1e-12
    gates = {**row['gates'], 'improvement': bool(gate)}
    passed = all(gates.values())
    return {**row, 'gates': gates, 'pass': passed, 'numerical_status': 'pass' if passed else 'fail',
            'acceptance_profile': GROUND_FLOOR_PROFILE, 'improvement_rule': deepcopy(rule),
            'measurement_floor_body': floor, 'reference_body_px': heights[0], 'candidate_body_px': heights[2],
            'improvement_gate_mode': 'relative_gain' if applies else 'floor_preservation',
            'floor_waiver_bound_body': None if applies else bound}


def speed_stall_bound(frames=SPEED_WINDOW_FRAMES):
    """Largest least-squares slope change (px/frame) one pixel stall can cause in a window."""
    return 3 * frames / (2 * (frames ** 2 - 1))


def _slope(frames, window, fps):
    times = [index / fps for index in window]
    values = [frames[index]['x_body'] for index in window]
    mean_t, mean_v = sum(times) / len(times), sum(values) / len(values)
    return (sum((t - mean_t) * (v - mean_v) for t, v in zip(times, values))
            / sum((t - mean_t) ** 2 for t in times))


def _ground_speed_gate(row, reference, candidate):
    """Apply the frozen V4 speed rule to one ground comparison; other gates are unchanged."""
    from . import suite_game
    ref, new = suite_game._report(reference), suite_game._report(candidate)
    held = suite_game.phases(ref['scenario'])[-2]
    length = held['end'] - held['start']
    fields = {'speed_rule': GROUND_SPEED_PROFILE, 'speed_held_frames': length,
              'speed_eligible_v4': length >= SPEED_MIN_HELD_FRAMES}
    gate = True
    if fields['speed_eligible_v4']:
        window = list(range(held['end'] - SPEED_WINDOW_FRAMES, held['end']))
        for report in (ref, new):
            frames = report['frames']
            if not all(frames[t].get('valid') and type(frames[t].get('x_body')) in (int, float)
                       and math.isfinite(frames[t]['x_body']) for t in window):
                raise VisualError('tracking_failure', 'The 16-frame speed window is incomplete.')
        v_ref, v_new = _slope(ref['frames'], window, suite_game.FPS), _slope(new['frames'], window, suite_game.FPS)
        if abs(v_ref) <= 1e-12:
            raise VisualError('tracking_failure', 'The reference does not move in the speed window.')
        error = abs(v_new - v_ref) / abs(v_ref)
        quantum = speed_stall_bound() * suite_game.FPS * (1 / ref['body_height_px'] + 1 / new['body_height_px'])
        tolerance = max(.10, quantum / abs(v_ref))
        gate = error <= tolerance + 1e-12
        fields.update(speed_window_v4=[window[0], window[-1]], reference_speed_v4=v_ref, candidate_speed_v4=v_new,
                      speed_relative_error_v4=error, speed_tolerance_v4=tolerance, speed_quantization_body_s=quantum)
    gates = {**row['gates'], 'speed': bool(gate)}
    passed = all(gates.values())
    return {**row, **fields, 'gates': gates, 'pass': passed, 'numerical_status': 'pass' if passed else 'fail',
            'acceptance_profile': GROUND_SPEED_PROFILE, 'speed_rule_document': deepcopy(GROUND_SPEED_RULE)}


def analysis_source_hashes(version=VERSION):
    paths=set((ROOT/'kiroclops').glob('*.py'))
    for directory in ('kiroclops/visual','kiroclops/web_local','kiroclops/portable_v2','kiroclops/ground',
                      'kiroclops/cloud','adapters/phase3d-tiny'):
        base=ROOT/directory
        if base.is_dir():paths.update(p for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for name in ('media_suite','suite_game','suite_web','evaluation','game','feedback','geometry_acceptance',
                 'suite_intake_binding','media_session','capture_modes','suite_boundary','visual_boundary_v4',
                 'backend','worker','suite_revision','source_inventory','session','suite_game_demo','suite_prepared_target'):
        path=Path(__file__).with_name(name+'.py')
        if path.is_file():paths.add(path)
    if version == 2:
        for name in ('realistic_policy','suite_web_v2','execution_policy','realistic_review','stage_closure',
                     'realistic_policy_v3','suite_web_v3','suite_web_v4','suite_web_v5'):
            path=Path(__file__).with_name(name+'.py')
            if not path.is_file():
                raise VisualError('analysis_source_changed','V2 analysis implementation is incomplete: '+name)
            paths.add(path)
    return {p.relative_to(ROOT).as_posix():file_sha256(p) for p in sorted(paths)}


def validate_web_policy(policy, stage):
    """Frozen web acceptance: realistic-behavior-v1, or V3 with its embedded V1 base."""
    from . import realistic_policy, realistic_policy_v3
    if realistic_policy_v3.is_v3(policy):
        return realistic_policy_v3.validate_policy(policy, domain=stage)
    return realistic_policy.validate_policy(policy, domain=stage)


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise VisualError('invalid_suite', 'Case identifiers must be unique simple path components.')
    return value


def coverage(cases, required_cases):
    """Report missing evidence without fabricating an interaction rule."""
    actual = {case['case_id']: case for case in cases}
    missing = [case for case in required_cases if case['case_id'] not in actual or actual[case['case_id']]['role']!=case['role']]
    unknown = [case['case_id'] for case in cases if case.get('input_provenance') == 'unknown']
    return {'status': 'needs_reference' if missing or unknown else 'ready',
            'missing_references': [{'case_id': c['case_id'], 'role': c['role'],
                'request': c.get('request', 'Add an extra recording that shows this behavior and its input.')} for c in missing],
            'unknown_inputs': unknown, 'inferred_inputs': False,
            'calibration_count': sum(c['role'] == 'calibration' for c in cases),
            'validation_count': sum(c['role'] == 'validation' for c in cases)}


def clone_record(record, destination):
    """Copy only pixel/measurement evidence, never reference source code."""
    from .evidence import observations, event_set
    destination = _ordinary(destination)
    if destination.exists():
        raise VisualError('output_exists', 'Keep earlier evidence unchanged.')
    value = deepcopy(record)
    copy_package(record['package'], destination / 'package')
    value['package'] = str(destination / 'package')
    # Preserve the pure game projection and common pixel measurements together.
    observed = observations(record)
    events = event_set(record)
    if observed is not None:
        value['observations'] = observed
        write_json(destination / 'measurement/observations.json', observed)
    if events is not None:
        value['events'] = events
        write_json(destination / 'measurement/events.json', events)
    if record.get('analysis'):
        value['analysis'] = str(destination / 'measurement/analysis.json')
        write_json(value['analysis'], read_json(record['analysis']))
        common=Path(record['analysis']).parent/'common-analysis'
        if common.is_dir():
            for path in sorted(common.rglob('*.json')):
                write_json(destination/'measurement/common-analysis'/path.relative_to(common),read_json(path))
    selection = record.get('selection')
    if isinstance(selection, (str, Path)):
        selection = read_json(selection)
    if selection is not None:
        value['selection'] = deepcopy(selection)
        write_json(destination / 'measurement/selection.json', selection)
    # Native runs remain bound by package manifest / source receipt, not a
    # mutable pointer to a previous output directory.
    # raw_run, when present, is private provenance. Native source review binds
    # its owned process receipt and frame hashes; it is never exported to Kiro.
    return value


def _relative_record(record, root):
    value = deepcopy(record)
    for key in ('package', 'analysis'):
        if value.get(key):
            value[key] = Path(value[key]).resolve().relative_to(root).as_posix()
    for key in ('observations','events','selection'):
        if isinstance(value.get(key),dict):
            path=Path(record['package']).parent/'measurement'/ (key+'.json')
            if path.is_file():value[key]=path.resolve().relative_to(root).as_posix()
    return value


def record_from_suite(root, case):
    root = _ordinary(root)
    value = deepcopy(case['record'])
    for key in ('package', 'analysis'):
        if value.get(key):
            value[key] = str(safe_path(root, value[key], must_exist=key != 'package'))
    for key in ('observations','events','selection'):
        if isinstance(value.get(key),str):value[key]=read_json(safe_path(root,value[key]))
    return value


def inspect_suite(directory, expected_sha256):
    """Read declared coverage cheaply; execution verifies every bound pixel."""
    directory=_ordinary(directory);value=read_json(directory/'suite.json')
    if (value.get('kind')!='ReferenceMediaSuite' or digest(value)!=expected_sha256
            or read_json(directory/'suite-binding.json').get('suite_sha256')!=expected_sha256):
        raise VisualError('suite_changed','Declared reference coverage changed.')
    available=[]
    for case in value['cases']:
        if safe_path(directory,case['record']['package'],must_exist=False).joinpath('manifest.json').is_file():
            available.append(case)
    return coverage(available,value['required_cases'])


def create_suite(directory, *, domain, stage, records, required_cases, original,
                 authoring_parent, goal, event_contract=None, preservation=None, initial_records=None, baseline_records=None,
                 acceptance_policy=None, execution_policy=None, game_acceptance_profile=LEGACY_GAME_PROFILE):
    """Freeze a complete pool before a model starts; no historical rewrite."""
    directory = _ordinary(directory)
    if directory.exists():
        raise VisualError('output_exists', 'Reference suites are immutable revisions.')
    if domain not in ('game', 'web_panel', 'svg') or stage not in ('jump', 'ground', 'panel', 'svg'):
        raise VisualError('invalid_suite', 'Unsupported local behavior stage.')
    if not goal or not isinstance(goal, str):
        raise VisualError('invalid_suite', 'A natural-language user goal is required.')
    version = 2 if acceptance_policy is not None or execution_policy is not None else VERSION
    limits = dict(LIMITS)
    if version == 2:
        from .execution_policy import validate_policy as validate_execution_policy
        validate_execution_policy(execution_policy,stage=stage)
        if stage in ('panel','svg'):
            validate_web_policy(acceptance_policy, stage)
            limits.update(required_improvement_fraction=None,web_curve_error_fraction=.05)
        elif acceptance_policy is not None:
            raise VisualError('invalid_suite','Game thresholds keep the legacy game acceptance policy.')
    if game_acceptance_profile not in GAME_PROFILES:
        raise VisualError('invalid_suite', 'Unknown game acceptance profile.')
    if game_acceptance_profile in GROUND_RULES and (version != 2 or stage != 'ground'):
        raise VisualError('invalid_suite', 'The floor-aware ground rules are only defined for revised ground suites.')
    original, authoring_parent = _ordinary(original), _ordinary(authoring_parent)
    if directory.is_relative_to(original) or directory.is_relative_to(authoring_parent):
        raise VisualError('unsafe_path', 'Reference media must be outside editable sources.')
    cases = []
    ids = set()
    for item in records:
        case_id = _name(item['case_id'])
        if case_id in ids or item['role'] not in ROLES:
            raise VisualError('invalid_suite', 'Duplicate case or invalid evidence role.')
        ids.add(case_id)
        record = clone_record(item['record'], directory / 'clips' / case_id)
        manifest = load_package(record['package'])
        provenance = manifest['inputs']['provenance']
        if provenance not in PROVENANCE:
            raise VisualError('invalid_suite', 'Unsupported input provenance.')
        if record['scenario'].get('case_id', record['scenario'].get('id')) != case_id:
            raise VisualError('invalid_suite', 'Pixels must keep their own immutable case identity.')
        if record.get('measurement_status') != 'ok':
            raise VisualError('tracking_failure', 'Unqualified references cannot start a model stage.')
        case = {'case_id': case_id, 'role': item['role'], 'stage': item.get('stage', stage),
                'purpose': item.get('purpose', 'primary'), 'scenario': deepcopy(record['scenario']),
                'input_provenance': provenance, 'package_sha256': manifest['package_sha256'],
                'timeline_sha256': digest(manifest['inputs']),
                'selection_sha256': digest(record.get('selection')),
                'object_map': deepcopy(item.get('object_map', {})),
                'source_sha256': record.get('source_sha256'),
                'record': _relative_record(record, directory)}
        cases.append(case)
    required = deepcopy(required_cases)
    if len({c['case_id'] for c in required}) != len(required):
        raise VisualError('invalid_suite', 'Required scenario identities repeat.')
    status = coverage(cases, required)
    if status['status'] != 'ready':
        write_json(directory / 'incomplete.json', status)
        raise VisualError('needs_reference', str(status))
    if not any(c['role'] == 'calibration' for c in cases):
        raise VisualError('needs_reference', 'At least one calibration example is required.')
    original_hashes = copy_project(original, directory / 'comparison-original')
    parent_hashes = source_hashes(authoring_parent)
    initial=[]
    for item in initial_records or []:
        name=_name(item['case_id']);case=next((c for c in cases if c['case_id']==name),None)
        if case is None or case['role']!='calibration' or item['role'] not in ('original','before'):
            raise VisualError('invalid_suite','Initial evidence must belong to a fixed calibration case and baseline role.')
        expected=original_hashes if item['role']=='original' else parent_hashes
        if (item['record'].get('source_sha256')!=digest(expected)
                or digest(item['record']['scenario'])!=digest(case['scenario'])
                or item['record'].get('measurement_status')!='ok'):
            raise VisualError('wrong_baseline','Initial measurement is not bound to its exact baseline source and recipe.')
        record=clone_record(item['record'],directory/'initial'/item['role']/name)
        initial.append({'case_id':name,'role':item['role'],'record':_relative_record(record,directory)})
    if initial and len({(c['case_id'],c['role']) for c in initial})!=len(initial):
        raise VisualError('invalid_suite','Initial case evidence repeats.')
    baseline=[]
    for item in baseline_records or []:
        name=_name(item['case_id']);case=next((c for c in cases if c['case_id']==name),None)
        if case is None or case['role']=='calibration' or item['role'] not in ('original','before'):
            raise VisualError('invalid_suite','Private fixed-baseline cache must bind a regression/validation case.')
        expected=original_hashes if item['role']=='original' else parent_hashes
        if (item['record'].get('source_sha256')!=digest(expected)
                or digest(item['record']['scenario'])!=digest(case['scenario'])
                or item['record'].get('measurement_status')!='ok'):
            raise VisualError('wrong_baseline','Private baseline cache uses a different source or recipe.')
        record=clone_record(item['record'],directory/'fixed-baselines'/item['role']/name)
        baseline.append({'case_id':name,'role':item['role'],'record':_relative_record(record,directory)})
    if len({(c['role'],c['case_id']) for c in baseline})!=len(baseline):
        raise VisualError('invalid_suite','Private fixed-baseline case repeats.')
    manifest = {'kind': 'ReferenceMediaSuite', 'schema_version': version,
                'domain': domain, 'stage': stage, 'goal': goal, 'cases': cases,
                'required_cases': required, 'coverage': status, 'limits': limits,
                'event_contract': deepcopy(event_contract),
                'preservation_contract': deepcopy(preservation or {}),
                'original': 'comparison-original', 'original_source_hashes': original_hashes,
                'authoring_parent_source_hashes': parent_hashes,
                'initial_records':initial,
                'baseline_records':baseline,
                'analysis_source_hashes':analysis_source_hashes(version),
                'heldout_status': 'provided' if any(c['role'] == 'validation' for c in cases) else 'not_provided',
                'index_similarity': False, 'calibration_frozen': True,
                'file_hashes': source_hashes(directory)}
    if version == 2:
        manifest.update(acceptance_policy=deepcopy(acceptance_policy), execution_policy=deepcopy(execution_policy),
            acceptance_profile=acceptance_policy['profile_id'] if acceptance_policy and 'profile_id' in acceptance_policy
                else acceptance_policy['id'] if acceptance_policy else game_acceptance_profile,
            source_epoch=digest(manifest['analysis_source_hashes']))
        if game_acceptance_profile in GROUND_RULES:
            manifest['game_acceptance_rule'] = deepcopy(GROUND_RULES[game_acceptance_profile])
    write_json(directory / 'suite.json', manifest)
    write_json(directory / 'suite-binding.json', {'suite_sha256': digest(manifest)})
    load_suite(directory)
    return manifest


def load_suite(directory, *, verify_analysis_code=True):
    directory = _ordinary(directory)
    value = read_json(directory / 'suite.json')
    version=value.get('schema_version')
    expected_limits=dict(LIMITS)
    if version == 2:
        from .execution_policy import validate_policy as validate_execution_policy
        validate_execution_policy(value.get('execution_policy'),stage=value.get('stage'))
        if value.get('stage') in ('panel','svg'):
            validate_web_policy(value.get('acceptance_policy'), value['stage'])
            expected_limits.update(required_improvement_fraction=None,web_curve_error_fraction=.05)
        elif value.get('acceptance_policy') is not None:
            raise VisualError('suite_changed','Game acceptance thresholds changed.')
        if value.get('stage') in ('jump', 'ground'):
            profile = value.get('acceptance_profile')
            if profile not in GAME_PROFILES:
                raise VisualError('suite_changed', 'Unknown game acceptance profile.')
            expected_rule = GROUND_RULES.get(profile)
            if ((profile in GROUND_RULES and value.get('stage') != 'ground')
                    or value.get('game_acceptance_rule') != expected_rule):
                raise VisualError('suite_changed', 'Game acceptance rule changed.')
        if value.get('source_epoch') != digest(value.get('analysis_source_hashes')):
            raise VisualError('suite_changed','V2 source epoch changed.')
    if (value.get('kind') != 'ReferenceMediaSuite' or version not in (VERSION,2)
            or read_json(directory / 'suite-binding.json').get('suite_sha256') != digest(value)
            or value.get('limits') != expected_limits or value.get('calibration_frozen') is not True):
        raise VisualError('suite_changed', 'Reference suite identity or acceptance changed.')
    if verify_analysis_code and value.get('analysis_source_hashes')!=analysis_source_hashes(version):
        raise VisualError('analysis_source_changed','Frozen measurement, event, capture or comparison sources changed.')
    files = source_hashes(directory)
    expected = value.get('file_hashes', {})
    if set(files) != set(expected) | {'suite.json', 'suite-binding.json'}:
        raise VisualError('suite_changed', 'Reference suite contains missing or unexpected evidence.')
    if any(files[k] != sha for k, sha in expected.items()):
        raise VisualError('suite_changed', 'Reference pixels, measurements or original baseline changed.')
    if source_hashes(safe_path(directory, value['original'], must_exist=False)) != value['original_source_hashes']:
        raise VisualError('suite_changed', 'The original comparison target changed.')
    if coverage(value['cases'], value['required_cases']) != value['coverage'] or value['coverage']['status'] != 'ready':
        raise VisualError('needs_reference', 'Reference coverage is not complete.')
    ids = set()
    for case in value['cases']:
        if _name(case['case_id']) in ids or case['role'] not in ROLES:
            raise VisualError('invalid_suite', 'Duplicate or invalid case identity.')
        ids.add(case['case_id'])
        record = record_from_suite(directory, case)
        package = load_package(record['package'],verify='hashes')
        if (package['package_sha256'] != case['package_sha256']
                or digest(package['inputs']) != case['timeline_sha256']
                or package['inputs']['provenance'] != case['input_provenance']
                or digest(record.get('selection')) != case['selection_sha256']
                or digest(record['scenario']) != digest(case['scenario'])):
            raise VisualError('suite_changed', 'Clip, selection, input or recipe binding changed.')
    for initial in value.get('initial_records',[]):
        record=record_from_suite(directory,initial)
        case=next((c for c in value['cases'] if c['case_id']==initial['case_id']),None)
        expected=value['original_source_hashes'] if initial['role']=='original' else value['authoring_parent_source_hashes']
        if (case is None or case['role']!='calibration' or initial['role'] not in ('original','before')
                or record.get('source_sha256')!=digest(expected) or digest(record['scenario'])!=digest(case['scenario'])):
            raise VisualError('wrong_baseline','Initial cache belongs to a different source or case.')
        load_package(record['package'],verify='hashes')
    for cached in value.get('baseline_records',[]):
        record=record_from_suite(directory,cached)
        case=next((c for c in value['cases'] if c['case_id']==cached['case_id']),None)
        expected=value['original_source_hashes'] if cached['role']=='original' else value['authoring_parent_source_hashes']
        if (case is None or case['role']=='calibration' or cached['role'] not in ('original','before')
                or record.get('source_sha256')!=digest(expected) or digest(record['scenario'])!=digest(case['scenario'])):
            raise VisualError('wrong_baseline','Private baseline cache belongs to another source or recipe.')
        load_package(record['package'],verify='hashes')
    return value


def create_suite_session(directory, suite_directory, authoring_parent, *, runtime):
    suite = load_suite(suite_directory)
    if source_hashes(authoring_parent) != suite['authoring_parent_source_hashes']:
        raise VisualError('wrong_authoring_parent', 'Use the independently preserved successful candidate.')
    from .reliability import check_path_budget, planned_tmp_paths
    budget = check_path_budget(planned_tmp_paths(directory))
    experiment = {'experiment_id': 'phase3d-' + suite['stage'] + '-v'+str(suite['schema_version']),
        'reference_mode': 'media_suite', 'suite_sha256': digest(suite),
        'mechanic': suite['stage'], 'stage': suite['stage'],
        'calibration': [deepcopy(c['scenario']) for c in suite['cases'] if c['role'] == 'calibration'],
        'description': 'Multi-reference behavior repair; improvement uses the original target; parent behavior is preserved separately.',
        'heldout_status': suite['heldout_status'], 'benchmark_qualified': True,
        'input_provenance': sorted({c['input_provenance'] for c in suite['cases']}),
        'capture_lock': runtime['capture_lock'], 'runtime_environment': runtime['environment'],
        'runtime_driver': runtime['driver'], 'path_budget': budget}
    if suite['schema_version'] == 2:
        experiment.update(execution_policy=deepcopy(suite['execution_policy']),
            acceptance_profile=suite['acceptance_profile'],
            acceptance_policy_sha256=(suite.get('acceptance_policy') or {}).get('policy_sha256'),
            source_epoch=suite['source_epoch'])
    create_session(directory, suite['domain'], authoring_parent, suite_directory, suite['goal'], experiment=experiment,
        **({'execution_policy':suite['execution_policy']} if suite['schema_version']==2 else {}))
    from .launcher import configure
    configure(directory)
    return Session(directory).inspect()


def runtime_environment(experiment):
    """Verify the copied driver every time; settings stay in child processes."""
    driver=experiment.get('runtime_driver',{})
    if (not driver.get('sha256') or any(file_sha256(driver[k])!=driver['sha256'] for k in ('source','path'))):
        raise VisualError('environment_not_ready','Installed and short-path Playwright drivers differ.')
    environment=experiment.get('runtime_environment',{})
    if (set(environment)-{'PLAYWRIGHT_NODEJS_PATH','PLAYWRIGHT_BROWSERS_PATH'}
            or environment.get('PLAYWRIGHT_NODEJS_PATH')!=driver['path']):
        raise VisualError('environment_not_ready','Unexpected runtime environment or driver path.')
    return dict(environment)


def accepted_parent(directory,stage):
    """Carry a stage forward only with numerical, raw, source and native proof."""
    directory=_ordinary(directory);session=Session(directory);state=session._load()
    final=state.get('final') or {}
    if (state.get('experiment',{}).get('reference_mode')!='media_suite'
            or state['experiment'].get('stage')!=stage or not state.get('sealed_at')
            or final.get('status')!='succeeded' or final.get('pass') is not True):
        raise VisualError('parent_not_accepted','The preceding behavior stage has no complete sealed numerical acceptance.')
    selected=state.get('selected_candidate_id')
    candidate=next((c for c in state['candidates'] if c['candidate_id']==selected),None)
    if not candidate:raise VisualError('parent_not_accepted','Selected parent is missing.')
    source=directory/'candidates'/_name(selected)/'source'
    if digest(source_hashes(source))!=candidate['source_sha256']:
        raise VisualError('source_changed','Accepted parent source changed.')
    from .review import validate_independent_review,validate_source_review,audit_native_run
    from .evidence import required_observation_bindings
    reviewed=validate_independent_review(directory/'independent_review.json',candidate_sha256=candidate['source_sha256'])
    validate_source_review(directory/'source_review.json',directory=directory,candidate_sha256=candidate['source_sha256'])
    audit_native_run(directory)
    if {tuple(v) for v in reviewed.get('required_observations',[])}!=required_observation_bindings(directory,state):
        raise VisualError('parent_not_accepted','Independent review does not cover the complete preceding stage.')
    return source


def _capture(project, case, destination, check, progress, cancel, variant, reference,
             *, prepared_targets=None, job_id=None, acceptance_policy=None):
    """Record one suite case; a host timing stall re-records it (V6 plan WP2.1).

    The retry rule is the one ``evaluation._capture_web_retrying`` applies to
    intake sessions: only ``capture_timing_failure``/``machine_not_quiet`` are
    retried, the failed attempt's folder is kept beside the canonical one, and
    every attempt is listed on the returned record.
    """
    import time
    from .evaluation import CAPTURE_ATTEMPTS, RETRYABLE_CAPTURE_CODES, RETRY_PAUSE_S
    destination = Path(destination)
    history = []
    for attempt in range(1, CAPTURE_ATTEMPTS+1):
        try:
            record = _capture_once(project, case, destination, check, progress, cancel, variant, reference,
                                   prepared_targets=prepared_targets, job_id=job_id, acceptance_policy=acceptance_policy)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code not in RETRYABLE_CAPTURE_CODES or attempt >= CAPTURE_ATTEMPTS:
                if history:
                    raise VisualError(code or 'capture_failed',
                                      f'{exc} (attempt {attempt} of {CAPTURE_ATTEMPTS}; earlier attempts: '
                                      + '; '.join(f"{h['attempt']}={h['code']}" for h in history) + ')') from exc
                raise
            preserved = destination.with_name(f'{destination.name}~failed-attempt{attempt}')
            if destination.exists():
                destination.rename(preserved)
            history.append({'attempt': attempt, 'code': code, 'message': str(exc),
                            'evidence': str(preserved) if preserved.exists() else None})
            progress(f"{variant} {case['case_id']}: re-recording after {code} (attempt {attempt+1} of {CAPTURE_ATTEMPTS})")
            check()
            time.sleep(RETRY_PAUSE_S)
            continue
        if isinstance(record, dict):
            record['recording_retries'] = history+[{'attempt': attempt, 'status': 'ok'}]
        return record
    raise VisualError('capture_failed', 'Unreachable capture retry state')


def _capture_once(project, case, destination, check, progress, cancel, variant, reference,
                  *, prepared_targets=None, job_id=None, acceptance_policy=None):
    if case['stage'] in ('jump', 'ground'):
        from . import suite_game, suite_prepared_target
        prepared = None
        role = destination.parent.name if variant == 'before' else variant
        if prepared_targets is not None:
            _validate_job_id = __import__('kiroclops.local_engine.backend', fromlist=['_validate_job_id'])._validate_job_id
            _validate_job_id(job_id)
            full = source_hashes(project)
            key = (role, digest(full), str(Path(project).resolve()))
            if key not in prepared_targets:
                prepared_targets[key] = suite_prepared_target.prepare_target(project,
                    suite_game._short_runtime() / 'p', job_id=job_id, role=role,
                    expected_full_sources=full, check=check, progress=progress,
                    cancel=suite_game.game._Cancellation(cancel, check))
            prepared = prepared_targets[key]
        return suite_game.capture_target(project, case['scenario'], destination, check, progress, cancel,
            variant=variant, prepared=prepared, prepared_job_id=job_id, prepared_role=role)
    if acceptance_policy is not None:
        from .realistic_policy_v3 import is_v3
        if is_v3(acceptance_policy):
            from .suite_web_v3 import capture_record
        else:
            from .suite_web_v2 import capture_record
        return capture_record(project,destination,case['scenario'],check,progress,cancel,variant,
            reference_selection=reference.get('selection'),reference_record=reference,acceptance_policy=acceptance_policy)
    if reference.get('intake_binding'):
        from .suite_intake_binding import capture_record
        return capture_record(project,destination,case['scenario'],check,progress,cancel,variant,reference=reference)
    from .suite_web import capture_record
    return capture_record(project, destination, case['scenario'], check, progress, cancel, variant,
                          reference_selection=reference.get('selection'))



def _compare(reference, original, candidate, stage, *, require_improvement, acceptance_policy=None,
             acceptance_profile=None):
    if stage in ('jump', 'ground'):
        from .suite_game import compare_records
        if acceptance_profile not in (None,) + GAME_PROFILES:
            raise VisualError('invalid_suite', 'Unknown game acceptance profile.')
        row = compare_records(reference, original, candidate, stage, require_improvement=require_improvement)
        if stage == 'ground' and require_improvement and acceptance_profile in GROUND_RULES:
            row = _ground_floor_gate(row, reference, original, candidate)
        if stage == 'ground' and acceptance_profile == GROUND_SPEED_PROFILE:
            row = _ground_speed_gate(row, reference, candidate)
        return row
    if acceptance_policy is not None:
        from .realistic_policy_v3 import is_v3
        if is_v3(acceptance_policy):
            from .suite_web_v3 import compare_records
        else:
            from .suite_web_v2 import compare_records
        return compare_records(reference,original,candidate,require_improvement=require_improvement,
            acceptance_policy=acceptance_policy)
    if reference.get('intake_binding'):
        from .suite_intake_binding import compare_records
        return compare_records(reference,original,candidate,require_improvement=require_improvement)
    from .suite_web import compare_records
    return compare_records(reference, original, candidate, require_improvement=require_improvement)


def _checks(source, parent, stage, output, check, cancel):
    if stage in ('jump', 'ground'):
        from .suite_game import preservation
        return preservation(source, parent, output, stage, check, cancel)
    from .suite_web import functional_checks
    return functional_checks(source, domain=stage, output=output, check=check, cancel=cancel)


def _parent_acceptance(reference,original,parent,stage,acceptance_policy=None):
    if stage not in ('jump','ground'):
        return _compare(reference,original,parent,stage,require_improvement=False,acceptance_policy=acceptance_policy)
    # A parent remains a 'before' capture. Check its measured error without
    # relabelling any saved evidence as a new candidate.
    from . import suite_game
    reports=[suite_game._report(record) for record in (reference,original,parent)]
    for report in reports:suite_game._reliable(report,stage)
    if (reports[0].get('game')!='tiny' or any(r.get('game')!='heart' or r.get('variant')!='before' for r in reports[1:])
            or len({digest(r['scenario']) for r in reports})!=1):
        raise VisualError('wrong_baseline','Parent acceptance requires pinned reference and two baseline captures of the same recipe.')
    row=(suite_game.measurement._jump_row(*reports,False) if stage=='jump'
         else suite_game._ground_row(*reports,False))
    return {**row,'baseline_role':'authoring_parent','improvement_required':False}


def _export(directory, records, role):
    """Case identity includes position/repeat; a short recipe ID does not."""
    root=Path(directory)/'target/.kiroclops-evidence'/role
    rows=[]
    for record in records:
        name=record['scenario'].get('case_id',record['scenario']['id'])
        destination=root/_name(name)
        if destination.exists():
            if load_package(destination)['package_sha256']!=load_package(record['package'])['package_sha256']:
                raise VisualError('evidence_conflict','Existing case media belongs to another recording.')
        else:copy_package(record['package'],destination)
        rows.append({'scenario_id':name,'package':str(destination)})
    return rows


def run_suite_job(directory, job, state, check, progress, cancel):
    from .prepared_evidence import store_prepared, load_prepared
    from .evaluation import _profile
    directory = Path(directory)
    suite = load_suite(state['reference'])
    if digest(suite) != state['experiment']['suite_sha256']:
        raise VisualError('suite_changed', 'Session points to a different reference revision.')
    acceptance_policy=suite.get('acceptance_policy')
    if suite['schema_version']==2:
        if (state['experiment'].get('execution_policy')!=suite['execution_policy']
                or state['experiment'].get('acceptance_policy_sha256')!=(acceptance_policy or {}).get('policy_sha256')):
            raise VisualError('suite_changed','Session execution or acceptance policy differs from its frozen suite.')
    output = directory / 'measurements' / job['job_id']
    operation = job['operation']
    prepared_targets = {}  # One immutable import per job/source; every recording remains new.
    if operation == 'report':
        from .reporting import build_report
        return build_report(directory)
    if operation != 'prepare':
        prepared = load_prepared(directory, state)
    roles = ('calibration',) if operation in ('prepare', 'evaluate') else ('regression', 'validation')
    if operation == 'finalize' and state.get('sealed_at') is None:
        raise VisualError('not_sealed', 'Validation opens only after immutable candidate selection.')
    cases = [c for c in suite['cases'] if c['role'] in roles]
    requested = job.get('case_ids') if operation == 'evaluate' else None
    if requested is not None:
        # V6 plan WP3.2: a partial probe measures only the named calibration cases.
        unknown = [c for c in requested if c not in {case['case_id'] for case in cases}]
        if unknown:
            raise VisualError('invalid_request', 'Unknown calibration cases: ' + ', '.join(unknown))
        cases = [c for c in cases if c['case_id'] in requested]
    refs, originals, parents, candidates, comparisons, preservation_rows = [], [], [], [], [], []
    if operation == 'evaluate':
        refs, originals, parents = (read_json(prepared[k]) for k in ('reference', 'original', 'before'))
    lookup = {r['scenario'].get('case_id', r['scenario']['id']): r for r in refs}
    old = {r['scenario'].get('case_id', r['scenario']['id']): r for r in originals}
    parent = {r['scenario'].get('case_id', r['scenario']['id']): r for r in parents}
    initial={(r['role'],r['case_id']):r for r in suite.get('initial_records',[])}
    fixed={(r['role'],r['case_id']):r for r in suite.get('baseline_records',[])}
    for index, case in enumerate(cases):
        check(); progress('suite ' + operation + ': ' + case['case_id'], index, len(cases))
        name = case['case_id']
        if operation != 'evaluate':
            ref = clone_record(record_from_suite(state['reference'], case), output / 'reference' / name)
            if operation=='prepare' and ('original',name) in initial:
                original=clone_record(record_from_suite(state['reference'],initial['original',name]),output/'original'/name)
            elif operation=='finalize' and ('original',name) in fixed:
                original=clone_record(record_from_suite(state['reference'],fixed['original',name]),output/'original'/name)
            else:
                original = _capture(safe_path(state['reference'], suite['original'], must_exist=False), case, output / 'original' / name,
                                    check, progress, cancel, 'before', ref,
                                    prepared_targets=prepared_targets, job_id=job['job_id'],acceptance_policy=acceptance_policy)
            if operation=='prepare' and ('before',name) in initial:
                prior=clone_record(record_from_suite(state['reference'],initial['before',name]),output/'before'/name)
            elif operation=='finalize' and ('before',name) in fixed:
                prior=clone_record(record_from_suite(state['reference'],fixed['before',name]),output/'before'/name)
            else:
                prior = _capture(state['baseline'], case, output / 'before' / name, check, progress, cancel, 'before', ref,
                                 prepared_targets=prepared_targets, job_id=job['job_id'],acceptance_policy=acceptance_policy)
            refs.append(ref); originals.append(original); parents.append(prior)
        else:
            ref, original, prior = lookup[name], old[name], parent[name]
        if operation != 'prepare':
            candidate = _capture(job['source_path'], case, output / 'candidate' / name,
                                 check, progress, cancel, 'final' if operation == 'finalize' else 'candidate', ref,
                                 prepared_targets=prepared_targets, job_id=job['job_id'],acceptance_policy=acceptance_policy)
            candidates.append(candidate)
            result = _compare(ref, original, candidate, case['stage'], require_improvement=case['purpose'] == 'primary',
                acceptance_policy=acceptance_policy,
                acceptance_profile=suite.get('acceptance_profile') if suite['domain'] == 'game' else None)
            result.update(case_id=name, scenario_id=name, role=case['role'], purpose=case['purpose'])
            comparisons.append(result)
            # Preservation uses parent pixels as reference, never as the 50% baseline.
            if acceptance_policy is None:
                preserved = _compare(prior, prior, candidate, case['stage'], require_improvement=False)
            else:
                # Preserve accepted reference behavior, not a replay receipt
                # anchored to the parent's slightly different delivered clock.
                preserved = deepcopy(result)
                preserved['preservation_reference_basis']='primary_reference_quality'
            preserved.update(case_id=name, role=case['role'], purpose='authoring_parent_preservation')
            prior_acceptance=_parent_acceptance(ref,original,prior,case['stage'],acceptance_policy=acceptance_policy)
            preserved['parent_reference_acceptance']=prior_acceptance
            preserved['previously_accepted_case']=prior_acceptance.get('pass') is True
            # Passing reference gates are retained without requiring the new
            # code to duplicate the parent's particular residual pixel error.
            preserved['retained_reference_behavior']=not preserved['previously_accepted_case'] or result.get('pass') is True
            preservation_rows.append(preserved)
    indexes = {}
    for key, rows in (('reference', refs), ('original', originals), ('before', parents),
                      ('final' if operation == 'finalize' else 'candidate', candidates)):
        if rows:
            # Evaluation must not rewrite the preparation's copied measurements.
            if operation == 'evaluate' and key != 'candidate':
                continue
            write_json(output / (key + '.json'), rows); indexes[key] = output / (key + '.json')
    if operation == 'prepare':
        if any(r.get('measurement_status') != 'ok' for r in refs + originals + parents):
            raise VisualError('tracking_failure', 'Every reference and both baseline roles must qualify before Kiro runs.')
        profile = _profile(refs, full_output=directory / 'target/.kiroclops-evidence/calibration/full-measurements.json')
        if suite['stage'] in ('jump','ground'):
            from .suite_game import profile as game_profile
            primary=[r for r,c in zip(refs,cases) if c['purpose']=='primary']
            profile={'common_pixel_feedback':profile,'motion_profile':game_profile(primary,suite['stage'],output/'motion-profile.json')}
        write_json(output / 'profile.json', profile)
        private = {k: str(v) for k, v in indexes.items()}
        receipt = store_prepared(directory, job, state, private, indexes, {'profile': output / 'profile.json'})
        return {'status': 'ready', 'coverage': suite['coverage'], 'suite_sha256': digest(suite),
                **({'acceptance_profile':suite['acceptance_profile'], 'acceptance_policy_sha256':(acceptance_policy or {}).get('policy_sha256'),
                    'execution_policy':suite['execution_policy']} if suite['schema_version']==2 else {}),
                'reference_media': _export(directory, refs, 'calibration/reference'),
                'before_media': _export(directory, parents, 'calibration/before'), 'profile': profile,
                'prepared_evidence_sha256': receipt, 'baseline_roles': {'improvement': 'original', 'preservation': 'authoring_parent'}}
    functional = _checks(job['source_path'], state['baseline'], suite['stage'], output / 'functional', check, cancel)
    visual_wall=None
    normal_level=None
    if operation=='finalize' and suite['domain']=='game':
        from .suite_boundary import capture as boundary_capture
        visual_wall=boundary_capture(job['source_path'],output/'visual-boundary.json',job['candidate_id'],check,progress,cancel)
        from .suite_game_demo import capture as normal_capture
        normal_level=normal_capture(job['source_path'],output/'normal-level',check,cancel)
    write_json(output / 'comparison.json', comparisons)
    write_json(output / 'parent-preservation.json', preservation_rows)
    write_json(output / 'functional.json', functional)
    passed = all(c.get('pass') is True for c in comparisons)
    # A repair may intentionally change an incorrect parent curve. Preserve
    # prior accepted behaviors, not the parent's known failed primary behavior.
    required_parent = [r for r, c in zip(preservation_rows, cases) if c['purpose'] == 'preservation']
    intact = (functional.get('status') == 'pass' and (visual_wall is None or visual_wall.get('status')=='pass')
              and (normal_level is None or normal_level.get('status')=='pass')
              and all(r.get('retained_reference_behavior') is True for r in preservation_rows)
              and all(r.get('pass') is True for r in required_parent))
    scores = [c.get('score') for c in comparisons if c.get('score') is not None]
    result = {'eligible': passed and intact, 'score': statistics.mean(scores) if len(scores) == len(comparisons) else None,
              'comparisons': comparisons, 'preservation': functional, 'parent_preservation': preservation_rows,
              'suite_sha256': digest(suite), 'independent_review_status': 'pending', 'source_review_status': 'pending',
              'numerical_acceptance_only': True, 'baseline_roles': {'improvement': 'original', 'preservation': 'authoring_parent'}}
    if suite['schema_version']==2:
        result.update(acceptance_profile=suite['acceptance_profile'],acceptance_policy_sha256=(acceptance_policy or {}).get('policy_sha256'),
            execution_policy=suite['execution_policy'], source_epoch=suite['source_epoch'],
            quality_pass=passed, preservation_pass=intact,
            measurement_pass=all(c.get('measurement_pass',c.get('pass')) is True for c in comparisons))
    if visual_wall is not None:result.update(visual_wall=visual_wall,visual_wall_status=visual_wall['status'])
    if normal_level is not None:result.update(normal_level=normal_level,normal_level_status=normal_level['status'])
    if operation == 'evaluate':
        role = ('probes/' if requested is not None else 'candidates/')+job['candidate_id']
        result['candidate_media'] = _export(directory, candidates, role)
        if requested is not None:
            result.update(eligible=False, partial=True, partial_cases=list(requested),
                          message='Partial probe: only the named calibration cases were measured; it cannot be finalized.')
    else:
        groups = {role: {'passed': sum(c.get('pass') is True for c in comparisons if c['role'] == role),
                         'required': sum(c['role'] == role for c in cases)} for role in roles}
        result.update(pass_=passed and intact and suite['heldout_status'] == 'provided', groups=groups,
                      heldout_status=suite['heldout_status'])
        fresh=[c for c in comparisons if c['role']=='validation' and c['purpose']=='primary']
        result.update(required=len(fresh),passed=sum(c.get('pass') is True for c in fresh))
        result['pass'] = result.pop('pass_')
    check(); load_suite(state['reference'])
    return result
