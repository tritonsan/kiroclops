"""Optional dual-jump bridge for frozen user clips and the Heart target.

This module is deliberately unrouted. It never reads Tiny reference recipes or
controller constants. Uploaded pixels retain their supplied input authority;
only target captures have native producer receipts. Native preservation and
independent raw review remain separate requirements, not numerical conclusions.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kiroclops.common import ROOT
from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from .session import _ordinary, source_hashes, copy_project


VERSION = 'user-game-dual-jump-v1'
BEHAVIORS = frozenset({'short_jump', 'long_jump'})
CONTRACT = {'min_valid_ratio': .95, 'require_static_camera': True, 'event_options': {'axis': 'y'}}


def _need(value, message, code='binding_changed'):
    if not value:
        raise VisualError(code, message)


def source_closure():
    """Full bytes of this optional adapter and its actual dependencies."""
    from kiroclops.cloud.identity import service_sources
    paths = ('kiroclops/local_engine/suite_game_intake_binding.py',
             'kiroclops/local_engine/game_media.py', 'kiroclops/local_engine/game.py',
             'kiroclops/local_engine/suite_intake.py', 'kiroclops/local_engine/suite_intake_binding.py',
             'kiroclops/local_engine/intake.py', 'kiroclops/local_engine/session.py',
             'kiroclops/local_engine/source_inventory.py', 'kiroclops/local_engine/evaluation.py',
             'kiroclops/local_engine/evidence.py', 'kiroclops/local_engine/backend.py',
             'kiroclops/cloud/execution.py', 'kiroclops/integration/storage.py',
             'kiroclops/integration/private_desktop_v1.py', 'kiroclops/portable_v2/runtime.py',
             'config/phase2c-v2/heart-source.json', 'config/phase2c-v2/heart-jump-annotation.json',
             'adapters/heart-godot4/Capture.tscn', 'adapters/heart-godot4/capture.gd')
    return {**service_sources(), **{p: file_sha256(ROOT / p) for p in paths}}


def _scenario(binding, case_id, role):
    from kiroclops.portable_v2 import runtime
    return runtime.canonical_scenario({**deepcopy(binding['scenario']), 'id': case_id, 'split': role})


def _selection(selection, manifest):
    _need(isinstance(selection, dict) and len(selection.get('objects', [])) == 1,
          'Select exactly one character.', 'invalid_binding')
    obj = selection['objects'][0]
    index = obj.get('frame_index', 0)
    _need(type(index) is int and 0 <= index < len(manifest['frames'])
          and obj.get('template_frame_sha256') == manifest['frames'][index]['sha256']
          and 'template_package' not in obj,
          'Character template must belong to an actual supplied frame.', 'selection_binding_mismatch')
    _need(not selection.get('connections') and not selection.get('relative_motion')
          and not obj.get('active_intervals_s'),
          'Dual-jump clips require one continuously visible character.', 'unsupported_game_media_recipe')
    _need(bool(selection.get('background_rois')),
          'Select a fixed textured background region to verify the static camera.', 'needs_camera_binding')
    return deepcopy(selection)


def _binding(project, manifest, selection, clip, options):
    from . import game_media
    _need(isinstance(options, dict) and not set(options) - {'body_height_px'},
          'Only measured reference body height may be supplied.', 'invalid_binding')
    _need(len(clip['behaviors']) == 1 and set(clip['behaviors']) <= BEHAVIORS,
          'Each clip must explicitly describe one short or long jump.', 'unsupported_game_media_recipe')
    _need(clip['role'] in ('calibration', 'validation'), 'Unsupported clip role.', 'invalid_binding')
    _selection(selection, manifest)
    binding = game_media.make_binding(project, manifest, selection, clip['object_map'], 'jump', options.get('body_height_px'))
    if binding['status'] != 'bound':
        return binding
    game_media.validate_binding(binding, project, manifest, selection)
    return binding


def _measurement(record, selection, output, check, progress, candidate_sha256):
    from .evaluation import _analysis
    code = source_closure()
    output = _ordinary(output)
    write_json(output / 'selection.json', selection)
    measured = _analysis(record['package'], selection, output, check, progress,
                         contract=deepcopy(CONTRACT), candidate_sha256=candidate_sha256)
    _need(source_closure() == code, 'Measurement code changed during analysis.', 'source_changed')
    row = {**record, 'selection': selection, 'analysis': str(output / 'analysis.json'),
           **{k: measured[k] for k in ('observations', 'events', 'measurement_status')},
           'measurement_contract': deepcopy(CONTRACT), 'measurement_source_hashes': code}
    _verify_measurement(row, candidate_sha256)
    return row


def _verify_measurement(record, candidate_sha256):
    """Replay saved local worker request/state/delivery; do not run tracking."""
    from kiroclops.cloud.identity import service_sources, engine_identity
    from kiroclops.cloud.results import request_binding, legacy_result
    from kiroclops.visual.events import extract_events
    from .backend import _validate_job_id
    from . import evidence
    _need(record.get('measurement_source_hashes') == source_closure()
          and record.get('measurement_contract') == CONTRACT,
          'Measurement code or contract differs.', 'measurement_source_changed')
    package = load_package(_ordinary(record['package']), verify='hashes')
    analysis = _ordinary(record['analysis'])
    _need(analysis.name == 'analysis.json', 'Unexpected worker result path.')
    directory = analysis.parent
    result = read_json(analysis)
    operation = read_json(_ordinary(directory / 'analysis-operation.json'))
    job_id = _validate_job_id(operation.get('job_id'))
    job = _ordinary(directory / 'jobs/jobs' / job_id)
    request = request_binding(package['package_sha256'], record['selection'], CONTRACT, candidate_sha256)
    state, delivery = read_json(job / 'state.json'), read_json(job / 'delivery-v2.json')
    _need(read_json(job / 'request.json') == request and operation.get('request_sha256') == digest(request)
          and operation.get('status') == state.get('status') == 'succeeded'
          and state.get('job_id') == job_id and state.get('request_sha256') == digest(request)
          and state.get('package_sha256') == package['package_sha256']
          and state.get('candidate_sha256') == candidate_sha256
          and state.get('result_sha256') == digest(result) and state.get('delivery_sha256') == digest(delivery),
          'Worker operation, request or completed state differs.', 'measurement_binding_changed')
    validated = legacy_result(delivery, request, logical_job_id=job_id, expected_sources=service_sources(),
                              expected_engine_sha256=engine_identity(threads=1)['engine_sha256'])
    _need(result == validated and result.get('observations') == record.get('observations')
          and result.get('events') == record.get('events')
          and result.get('measurement_status') == record.get('measurement_status') == 'ok'
          and result.get('events') == extract_events(record['observations'], record['selection'], CONTRACT),
          'Saved observations/events differ from the actual pixel worker.', 'measurement_binding_changed')
    camera = record['observations'].get('camera', [])
    _need(record['observations'].get('status') == 'ok' and len(camera) == len(package['frames'])
          and all(c.get('index') == f['index'] and c.get('time_s') == f['time_s'] and c.get('status') == 'pass'
                  for c, f in zip(camera, package['frames'])),
          'Every supplied frame needs verified pixel camera observations.', 'camera_unverified')
    evidence._record_observation(record, record['scenario'], 'media', measured=True,
        reference_package=package['package_sha256'], expected_source={'kind': 'uploaded'} if candidate_sha256 is None else None)
    return {'package_sha256': package['package_sha256'], 'observations_sha256': digest(record['observations']),
            'worker_job_id': job_id, 'candidate_sha256': candidate_sha256}


def verify_reference_record(record):
    """Uploaded references have pixel identity, never renderer-source identity."""
    from . import game_media, suite_intake
    manifest = load_package(_ordinary(record['package']), verify='hashes')
    identity, binding = record['intake_provenance'], record['intake_binding']
    selection = record['intake_source_selection']
    _selection(selection, manifest)
    game_media.validate_binding(binding, {}, manifest, selection)
    expected_source = digest({'kind': 'uploaded_pixels', 'package_sha256': manifest['package_sha256'],
                              'pixels_sha256': suite_intake._pixels(manifest)})
    _need(identity.get('version') == VERSION and identity.get('role') in ('calibration', 'validation')
          and len(identity.get('behaviors', [])) == 1 and set(identity['behaviors']) <= BEHAVIORS
          and identity.get('package_sha256') == manifest['package_sha256']
          and identity.get('pixels_sha256') == suite_intake._pixels(manifest)
          and identity.get('inputs_sha256') == digest(manifest['inputs'])
          and identity.get('selection_sha256') == digest(selection)
          and identity.get('object_map') == binding['object_map']
          and identity.get('input_provenance') == manifest['inputs']['provenance']
          and record.get('scenario') == _scenario(binding, identity['case_id'], identity['role'])
          and record.get('selection') == selection and record.get('body_height_px') == binding['body_height_px']
          and record.get('reference_kind') == 'uploaded_pixels' and record.get('source_sha256') == expected_source
          and record.get('blind_validation') is False and record.get('benchmark_qualified') is False
          and not any(k in record for k in ('raw_run', 'native_source_hashes', 'native_source_sha256', 'runtime_receipt')),
          'Uploaded clip identity, role, scope, selection or input authority differs.')
    result = _verify_measurement(record, None)
    start = binding['scenario']['events'][0]['tick']
    game_media._jump_metrics(game_media._series(record, binding['body_height_px'], start), start, binding['body_height_px'])
    return {**result, 'status': 'verified', 'claim': binding['claim'],
            'input_provenance': binding['input_provenance'], 'blind_validation': False, 'benchmark_qualified': False}


def bind_intake(intake_suite, output, *, original, authoring_parent, case_options=None,
                check=lambda: None, progress=lambda *args: None):
    """Copy source roles, measure frozen clips, and publish an unrouted plan.

    No engine, model, candidate patch or generated input occurs. Both jump
    examples must be calibration clips. Additional validation stays explicitly
    user-designated; observing it cannot establish a blind or benchmark claim.
    """
    from . import suite_intake, game_media
    from .suite_intake_binding import _load_intake
    from .intake import detect_project
    check()
    root, intake, coverage, clips = _load_intake(intake_suite)
    if intake is None:
        return {**coverage, 'status': 'needs_reference', 'execution_ready': False, 'model_started': False}
    _need(intake['domain'] == 'game' and set(intake['required_behaviors']) == BEHAVIORS,
          'This adapter supports an explicitly requested short/long jump pair only.', 'unsupported_media_adapter')
    output, original, authoring_parent = map(_ordinary, (output, original, authoring_parent))
    _need(not output.exists(), 'Use a fresh immutable adapter directory.', 'output_exists')
    for source in (root, original, authoring_parent):
        _need(not output.is_relative_to(source) and not source.is_relative_to(output),
              'Output must be separate from intake and both source roles.', 'unsafe_path')
    info = detect_project(authoring_parent)
    _need(suite_intake._target(info) == intake['target_identity'],
          'Authoring parent must be the unchanged intake target.', 'wrong_authoring_parent')
    original_info = detect_project(original)
    _need(original_info['domain'] == info['domain'] == 'game' and original_info['entry'] == info['entry'],
          'Original and parent must use the same real controller entry.', 'wrong_baseline')
    game_media.validate_project(original); game_media.validate_project(authoring_parent)
    options = deepcopy(case_options or {})
    ids = {c['scenario_id'] for c in clips}
    _need(isinstance(options, dict) and set(options) <= ids, 'Options must name frozen clips only.', 'invalid_binding')
    calibration = {b for c in clips if c['role'] == 'calibration' for b in c['behaviors']}
    missing = sorted(BEHAVIORS - calibration)
    if missing:
        return {'status': 'needs_reference', 'missing_calibration_behaviors': missing,
                'requests': [suite_intake.MISSING_REQUESTS[b] for b in missing], 'execution_ready': False,
                'model_started': False, 'behavior_accepted': False}
    planned, pixels = [], set()
    for clip in clips:
        check()
        name = clip['scenario_id']
        manifest = load_package(root / 'clips' / name / 'package', verify='hashes')
        selection = read_json(root / 'clips' / name / 'selection.json')
        _need(clip['object_map'] == intake['object_map'] and clip['target_identity'] == intake['target_identity'],
              'Clips must retain the common target and character map.')
        try:
            binding = _binding(info, manifest, selection, clip, options.get(name, {}))
        except VisualError as error:
            if error.code in ('needs_camera_binding', 'needs_input_binding'):
                return {'status': error.code, 'case_id': name, 'request': str(error), 'execution_ready': False,
                        'model_started': False, 'inferred_inputs': False, 'behavior_accepted': False}
            raise
        if binding['status'] != 'bound':
            return {**binding, 'case_id': name, 'execution_ready': False, 'inferred_inputs': False}
        _need(clip['pixels_sha256'] not in pixels, 'Jump examples require distinct supplied pixels.', 'duplicate_reference')
        pixels.add(clip['pixels_sha256'])
        planned.append((clip, manifest, selection, binding))
    holds = {b: [binding['scenario']['events'][1]['tick'] - binding['scenario']['events'][0]['tick']
                 for c, _, _, binding in planned if c['role'] == 'calibration' and c['behaviors'] == [b]] for b in BEHAVIORS}
    _need(max(holds['short_jump']) < min(holds['long_jump']),
          'Declared short calibration holds must be shorter than every long calibration hold.', 'invalid_input_binding')
    code = source_closure()
    source_maps = {'original': source_hashes(original), 'authoring_parent': source_hashes(authoring_parent)}
    for role, project in (('original', original), ('authoring_parent', authoring_parent)):
        copy_project(project, output / 'sources' / role)
        _need(source_hashes(output / 'sources' / role) == source_maps[role], 'Source copy differs.', 'source_changed')
    cases = []
    for clip, manifest, selection, binding in planned:
        check()
        name = clip['scenario_id']; directory = output / 'cases' / name
        copy_package(root / 'clips' / name / 'package', directory / 'package')
        identity = {'version': VERSION, 'intake_suite_sha256': digest(intake), 'clip_sha256': digest(clip),
            'case_id': name, 'role': clip['role'], 'behaviors': deepcopy(clip['behaviors']),
            'object_map': deepcopy(clip['object_map']), 'package_sha256': manifest['package_sha256'],
            'pixels_sha256': clip['pixels_sha256'], 'selection_sha256': digest(selection),
            'inputs_sha256': digest(manifest['inputs']), 'input_provenance': manifest['inputs']['provenance'],
            'target_identity': deepcopy(intake['target_identity']),
            'original_source_sha256': digest(source_maps['original']),
            'authoring_parent_source_sha256': digest(source_maps['authoring_parent'])}
        record = {'scenario': _scenario(binding, name, clip['role']), 'package': str(directory / 'package'),
            'body_height_px': binding['body_height_px'], 'reference_kind': 'uploaded_pixels',
            'source_sha256': digest({'kind': 'uploaded_pixels', 'package_sha256': manifest['package_sha256'],
                                    'pixels_sha256': clip['pixels_sha256']}),
            'functional': {'status': 'not_observed_reference_video'}, 'intake_binding': binding,
            'intake_provenance': identity, 'intake_source_selection': selection,
            'blind_validation': False, 'benchmark_qualified': False, 'model_started': False}
        record = _measurement(record, selection, directory / 'measurement', check, progress, None)
        verify_reference_record(record)
        write_json(directory / 'record.json', record)
        cases.append({'case_id': name, 'role': clip['role'], 'behaviors': clip['behaviors'],
                      'record': f'cases/{name}/record.json', 'record_sha256': digest(record)})
    _need(source_maps['original'] == source_hashes(original)
          and source_maps['authoring_parent'] == source_hashes(authoring_parent)
          and _load_intake(root)[1] == intake and source_closure() == code,
          'Sources, intake or adapter code changed during preparation.', 'source_changed')
    suite = {'kind': 'GameUserReferenceSuite', 'schema_version': 1, 'version': VERSION, 'domain': 'game',
        'stage': 'jump', 'goal': intake['goal'], 'intake_suite_sha256': digest(intake), 'cases': cases,
        'source_hashes': source_maps, 'source_roles': {k: digest(v) for k, v in source_maps.items()},
        'measurement_source_hashes': code, 'runtime_adapter_ready': True, 'execution_ready': False,
        'session_routing': 'not_integrated', 'model_started': False, 'behavior_accepted': False,
        'benchmark_qualified': False, 'blind_validation': False,
        'validation_basis': 'user_designated_distinct_pixels' if any(c['role'] == 'validation' for c in cases) else 'not_provided',
        'native_preservation': 'required_separately', 'independent_raw_review': 'required_separately'}
    write_json(output / 'suite.json', suite); write_json(output / 'suite-binding.json', {'suite_sha256': digest(suite)})
    load_suite(output)
    return {'status': 'suite_bound', 'output': str(output), 'suite_sha256': digest(suite), 'case_count': len(cases),
            **{k: suite[k] for k in ('runtime_adapter_ready', 'execution_ready', 'session_routing', 'model_started',
                                    'behavior_accepted', 'benchmark_qualified', 'blind_validation', 'validation_basis')}}


def load_suite(directory):
    """Verify this independent plan without adapting it into a Tiny benchmark."""
    from kiroclops.visual.schema import safe_path
    from . import game_media
    root = _ordinary(directory)
    suite = read_json(root / 'suite.json')
    _need(digest(suite) == read_json(root / 'suite-binding.json').get('suite_sha256')
          and suite.get('kind') == 'GameUserReferenceSuite' and suite.get('version') == VERSION
          and suite.get('domain') == 'game' and suite.get('stage') == 'jump'
          and suite.get('measurement_source_hashes') == source_closure()
          and suite.get('runtime_adapter_ready') is True and suite.get('execution_ready') is False
          and suite.get('session_routing') == 'not_integrated'
          and all(suite.get(k) is False for k in ('model_started', 'behavior_accepted', 'benchmark_qualified', 'blind_validation')),
          'Optional game suite identity, code or claim differs.')
    _need(set(suite.get('source_hashes', {})) == {'original', 'authoring_parent'}, 'Both explicit source roles are required.')
    for role, sources in suite['source_hashes'].items():
        project = _ordinary(root / 'sources' / role)
        _need(source_hashes(project) == sources and suite['source_roles'].get(role) == digest(sources),
              'Frozen source role changed.', 'source_changed')
        game_media.validate_project(project)
    cases = suite.get('cases', [])
    _need(cases and len({c['case_id'] for c in cases}) == len(cases), 'Game case matrix is missing or duplicated.')
    calibrated, pixels = {}, set()
    for case in cases:
        _need(case.get('record') == f"cases/{case['case_id']}/record.json", 'Case record path differs.', 'unsafe_path')
        record = read_json(safe_path(root, case['record']))
        _need(digest(record) == case['record_sha256'], 'Frozen reference record changed.')
        identity = record['intake_provenance']
        _need(identity['case_id'] == case['case_id'] and identity['role'] == case['role']
              and identity['behaviors'] == case['behaviors'] and identity['intake_suite_sha256'] == suite['intake_suite_sha256']
              and identity['original_source_sha256'] == suite['source_roles']['original']
              and identity['authoring_parent_source_sha256'] == suite['source_roles']['authoring_parent']
              and _ordinary(record['package']) == root / 'cases' / case['case_id'] / 'package'
              and _ordinary(record['analysis']) == root / 'cases' / case['case_id'] / 'measurement/analysis.json',
              'Reference belongs to another suite, case or source role.')
        verify_reference_record(record)
        _need(identity['pixels_sha256'] not in pixels, 'Distinct references reuse the same pixels.', 'duplicate_reference')
        pixels.add(identity['pixels_sha256'])
        if case['role'] == 'calibration':
            events = record['scenario']['events']
            calibrated.setdefault(case['behaviors'][0], []).append(events[1]['tick'] - events[0]['tick'])
    _need(set(calibrated) == BEHAVIORS and max(calibrated['short_jump']) < min(calibrated['long_jump']),
          'Both explicitly contrasted calibration inputs are required.', 'invalid_input_binding')
    expected_validation = 'user_designated_distinct_pixels' if any(c['role'] == 'validation' for c in cases) else 'not_provided'
    _need(suite.get('validation_basis') == expected_validation
          and suite.get('native_preservation') == suite.get('independent_raw_review') == 'required_separately',
          'User-validation or remaining-review scope changed.')
    return suite


def _verify_source_scope(record, expected_sources):
    """Bind the selected full source to its explicit disposable capture overlay."""
    from kiroclops.portable_v2 import runtime
    scope = record['capture_source_scope']
    selected, prepared, rendered = map(_ordinary, (scope['selected'], scope['prepared'], scope['rendered']))
    _need(source_hashes(selected) == expected_sources == scope.get('selected_sources')
          and scope.get('selected_sha256') == record.get('source_sha256') == digest(expected_sources),
          'Target is not the required immutable full source.', 'source_changed')
    _need(source_hashes(prepared) == scope.get('prepared_sources') and runtime.source_hashes(rendered) == scope.get('native_sources'),
          'Capture overlay/native projection changed.', 'source_changed')
    # Reconstruct exact settings text without writing or running any engine.
    config = (selected / 'project.godot').read_text(encoding='utf-8-sig')
    lines = config.splitlines()
    if runtime.CUSTOM_DIR not in lines or 'config/use_custom_user_dir=true' not in lines:
        lines = [v for v in lines if not v.startswith(('config/use_custom_user_dir=', 'config/custom_user_dir_name='))]
        index = lines.index('[application]') + 1
        lines[index:index] = ['config/use_custom_user_dir=true', runtime.CUSTOM_DIR]
        config = '\n'.join(lines) + '\n'
    _need((prepared / 'project.godot').read_text(encoding='utf-8-sig') == config,
          'Prepared settings are not the exact isolation overlay.', 'source_changed')
    runtime._assert_prepared(prepared); runtime._assert_prepared(rendered)
    prepared_sources = scope['prepared_sources']
    _need(all(prepared_sources.get(k) == v for k, v in expected_sources.items()
              if k != 'project.godot' and not k.startswith('kiroclops_heart/'))
          and all(k in expected_sources or k.startswith('kiroclops_heart/') for k in prepared_sources)
          and runtime.source_hashes(prepared) == scope['native_sources'],
          'Preparation altered application source.', 'source_changed')
    changed = {k: {'before': expected_sources.get(k), 'after': prepared_sources.get(k)}
               for k in set(expected_sources) | set(prepared_sources) if expected_sources.get(k) != prepared_sources.get(k)}
    preparation = read_json(prepared.parent / (prepared.name + '-preparation.json'))
    _need(preparation == scope.get('preparation')
          and preparation.get('kind') == 'HeartCapturePreparation'
          and preparation.get('original_source_sha256') == digest(expected_sources)
          and preparation.get('prepared_source_sha256') == digest(prepared_sources)
          and preparation.get('changes') == changed
          and preparation.get('original_project_unchanged') is True
          and preparation.get('controller_unchanged') is True
          and preparation.get('runtime') == runtime.protocol_record(),
          'Preparation receipt differs from its actual source overlay.', 'source_changed')
    full_rendered = source_hashes(rendered)
    _need(full_rendered == scope.get('rendered_sources')
          and all(full_rendered.get(k) == v for k, v in prepared_sources.items()
              if Path(k).suffix.lower() not in ('.uid', '.import'))
          and all(k in prepared_sources or Path(k).suffix.lower() in ('.uid', '.import') for k in full_rendered),
          'Import introduced or altered application source.', 'source_changed')
    return scope


def capture_record(project, output, scenario, check, progress, cancel, variant, *, reference,
                   source_role, expected_sources):
    """Fresh native target trial; caller must already own the common capture lease.

    Target preparation is separately receipted. Candidate identity is the actual
    selected full source, while native receipts retain the prepared projection.
    No output from another case or native state enters pixel measurements.
    """
    from . import game_media
    from .game import _annotation
    from kiroclops.portable_v2 import runtime
    from kiroclops.visual.compat import selection_from_legacy
    import math
    verify_reference_record(reference)
    _need(source_role in ('original', 'before', 'candidate', 'final') and variant in ('before', 'after'),
          'Name the explicit target source role.', 'wrong_role')
    _need(variant == ('before' if source_role in ('original', 'before') else 'after'),
          'Display variant differs from the source role.', 'wrong_role')
    _need(scenario == reference['scenario'], 'Use the exact supplied input recipe.', 'suite_recipe_mismatch')
    project, output = _ordinary(project), _ordinary(output)
    _need(not output.exists() and not output.is_relative_to(project) and not project.is_relative_to(output),
          'Target evidence must use a fresh separate directory.', 'output_exists')
    _need(source_hashes(project) == expected_sources, 'Wrong target source.', 'source_changed')
    game_media.validate_project(project)
    if source_role in ('original', 'before'):
        expected = reference['intake_provenance']['original_source_sha256' if source_role == 'original' else 'authoring_parent_source_sha256']
        _need(digest(expected_sources) == expected, 'Baseline role is not the explicitly bound source.', 'wrong_role')
    code = source_closure()
    prepared, preparation = game_media.prepare_baseline(project, output / 'prepared')
    native_binding = {**deepcopy(reference['intake_binding']), 'scenario': deepcopy(scenario)}
    row = game_media._target(prepared, output / 'native', native_binding, check, progress, cancel, analyze=False)
    # The existing native adapter labels its actual recording "after". Keep that
    # raw label unchanged; source_role records the independently verified role.
    rendered = output / 'native/project'
    scope = {'kind': 'UserGameCaptureSourceScope', 'selected': str(project), 'prepared': str(prepared),
        'rendered': str(rendered), 'selected_sources': deepcopy(expected_sources), 'selected_sha256': digest(expected_sources),
        'prepared_sources': source_hashes(prepared), 'native_sources': runtime.source_hashes(rendered),
        'rendered_sources': source_hashes(rendered),
        'preparation': preparation, 'native_variant': 'after', 'source_role': source_role}
    row.update(source_sha256=digest(expected_sources), capture_source_scope=scope, source_role=source_role,
               intake_binding=deepcopy(reference['intake_binding']), intake_provenance=deepcopy(reference['intake_provenance']))
    annotation = _annotation('jump', 'heart'); definition = read_json(annotation)
    selection = selection_from_legacy(row['raw_run'], annotation)
    selection['objects'][0]['id'] = next(iter(reference['intake_binding']['object_map']))
    selection['objects'][0]['search_radius'] = max(8, math.ceil(definition['body_height_px'] * .75))
    row['body_height_px'] = definition['body_height_px']
    row = _measurement(row, selection, output / 'measurement', check, progress, digest(expected_sources))
    _need(source_closure() == code, 'Target capture code changed.', 'source_changed')
    verify_target_record(row, reference=reference, expected_sources=expected_sources, source_role=source_role)
    return row


def verify_target_record(record, *, reference, expected_sources, source_role):
    from . import evidence, game_media
    from kiroclops.portable_v2 import runtime
    verify_reference_record(reference)
    _need(source_role in ('original', 'before', 'candidate', 'final') and record.get('source_role') == source_role
          and record.get('intake_binding') == reference['intake_binding']
          and record.get('intake_provenance') == reference['intake_provenance']
          and record.get('scenario') == reference['scenario'], 'Target belongs to another role or user case.', 'wrong_role')
    if source_role in ('original', 'before'):
        wanted = reference['intake_provenance']['original_source_sha256' if source_role == 'original' else 'authoring_parent_source_sha256']
        _need(digest(expected_sources) == wanted, 'Repair parent cannot replace the original denominator.', 'wrong_role')
    scope = _verify_source_scope(record, expected_sources)
    game_media.validate_project(scope['selected'])
    raw = _ordinary(record['raw_run']); native_root = _ordinary(scope['rendered']).parent
    _need(raw == native_root / 'raw' and _ordinary(record['package']) == native_root / 'package'
          and _ordinary(record['analysis']) == native_root.parent / 'measurement/analysis.json'
          and _ordinary(scope['prepared']) == native_root.parent / 'prepared'
          and scope.get('source_role') == source_role and scope.get('native_variant') == 'after',
          'Target evidence paths or raw role differ.', 'unsafe_path')
    manifest = read_json(raw / 'manifest.json')
    recipe = {**record['scenario'], 'variant': 'after'}
    _need(manifest.get('game') == 'heart' and manifest.get('variant') == 'after'
          and manifest.get('scenario') == recipe
          and manifest.get('viewport') == [320, 180] and manifest.get('frame_count') == 240
          and manifest.get('warmup_ticks') == 60 and manifest.get('normal_level') is False and manifest.get('settled') is True
          and manifest.get('provenance', {}).get('coordinates_from_engine') is False,
          'Actual native recipe or pixel-only measurement boundary differs.')
    config = read_json(raw / 'capture_config.json')
    _need(config == {**recipe, 'output_dir': str(raw), 'game': 'heart', 'normal_level': False,
                     'protocol': runtime.VERSION, 'viewport': [320, 180], 'fps': 60},
          'Native configuration differs from the exact supplied recipe.')
    recording = read_json(raw / 'recording.json')
    try:
        runtime.validate_recording(recording, recipe, False)
    except runtime.PortableRuntimeError as error:
        raise VisualError('capture_timing_failure', str(error)) from error
    _need(recording.get('frames') == manifest.get('frames'), 'Actual native frame/input receipts differ.')
    expected = {'kind': 'native', 'sources': scope['native_sources'], 'session_sources': expected_sources}
    package = load_package(record['package'], verify='hashes')
    clock = evidence._record_source(record, package, expected)
    _need(clock['frame_edges'] == record['scenario']['events'], 'Native actions differ from supplied inputs.', 'capture_timing_failure')
    capture_receipt = raw / ('engine.' + runtime.VERSION) / 'bridge.json'
    import_path = native_root / 'import/import-result.json'
    imported = read_json(import_path)
    imported_receipt = _ordinary(imported['receipt_path'])
    _need(imported_receipt in (native_root / 'import' / ('engine.' + runtime.VERSION) / 'bridge.json',
                              native_root / 'import/clean-retry' / ('engine.' + runtime.VERSION) / 'bridge.json'),
          'Import receipt escaped this target trial.', 'unsafe_path')
    for path in (capture_receipt, imported_receipt):
        proof = runtime.verify_runtime_receipt(path, project=scope['rendered'], expected_source_hashes=scope['native_sources'])
        _need(proof['status'] == 'pass', 'Native import or capture process proof failed.', 'capture_provenance_changed')
    _need(imported.get('status') == 'completed' and imported.get('kind') == 'import'
          and imported.get('project') == scope['rendered'], 'Actual target import is missing.')
    from .game import _annotation
    definition = read_json(_annotation('jump', 'heart'))
    _need(record.get('body_height_px') == definition['body_height_px']
          and {o['id'] for o in record['selection']['objects']} == set(reference['intake_binding']['object_map']),
          'Target body units or character identity differ.')
    from kiroclops.visual.compat import selection_from_legacy
    import math
    selection = selection_from_legacy(raw, _annotation('jump', 'heart'))
    selection['objects'][0]['id'] = next(iter(reference['intake_binding']['object_map']))
    selection['objects'][0]['search_radius'] = max(8, math.ceil(definition['body_height_px'] * .75))
    _need(record['selection'] == selection, 'Target selection differs from the actual supported character pixels.')
    _verify_measurement(record, digest(expected_sources))
    return {'status': 'verified', 'source_sha256': digest(expected_sources), 'source_role': source_role,
            'native_source_sha256': digest(scope['native_sources']), 'fresh_native_process': True}


def compare_records(reference, original, candidate, require_improvement=True, *,
                    expected_original_sources, expected_candidate_sources):
    """Absolute and gain gates use the original, independently for each clip."""
    from . import game_media
    verify_reference_record(reference)
    verify_target_record(original, reference=reference, expected_sources=expected_original_sources, source_role='original')
    verify_target_record(candidate, reference=reference, expected_sources=expected_candidate_sources,
                         source_role=candidate.get('source_role'))
    _need(candidate.get('source_role') in ('candidate', 'final'), 'Candidate source role required.', 'wrong_role')
    result = game_media.compare(reference, original, candidate, reference['intake_binding'], require_improvement=require_improvement)
    result.update(case_id=reference['intake_provenance']['case_id'], baseline_role='original',
        reference_role=reference['intake_provenance']['role'], blind_validation=False, benchmark_qualified=False,
        behavior_accepted=False, native_preservation='required_separately', independent_raw_review='required_separately',
        conditional_on_user_declared_inputs=reference['intake_binding']['input_provenance'] == 'user_declared')
    return result


def compare_suite(directory, originals, candidates, *, expected_candidate_sources, require_improvement=True):
    """No aggregate score can hide a failing, missing or foreign supplied case."""
    root = _ordinary(directory); suite = load_suite(root)
    ids = {c['case_id'] for c in suite['cases']}
    _need(isinstance(originals, dict) and isinstance(candidates, dict)
          and set(originals) == set(candidates) == ids, 'Every exact frozen case is required.', 'suite_recipe_mismatch')
    rows = [compare_records(read_json(root / c['record']), originals[c['case_id']], candidates[c['case_id']],
        require_improvement=require_improvement, expected_original_sources=suite['source_hashes']['original'],
        expected_candidate_sources=expected_candidate_sources) for c in suite['cases']]
    return {'numerical_pass': all(r['pass'] for r in rows), 'comparisons': rows,
            'behavior_accepted': False, 'benchmark_qualified': False, 'blind_validation': False,
            'validation_basis': suite['validation_basis'], 'native_preservation': 'required_separately',
            'independent_raw_review': 'required_separately', 'session_routing': 'not_integrated'}
