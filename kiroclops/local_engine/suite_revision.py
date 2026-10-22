"""Prospective revisions of saved pixels, never historical code rebranding.

A suite-wide orchestration hash is not a receipt for earlier measurements.
Records without a verified per-record measurement stamp are reanalyzed from
their immutable pixels. Saved inputs, source identities and recipes stay fixed.
No function here starts an application, captures frames or launches a model.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import math
from pathlib import Path
import time

from kiroclops.cloud.identity import engine_identity, service_sources
from kiroclops.cloud.results import legacy_result, make_measurement, request_binding, validate_delivery
from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from .session import _ordinary, source_hashes

ROOT = Path(__file__).resolve().parents[2]
STAGES = ('panel', 'svg', 'jump', 'ground')
# Only comparison and a separate application oracle may change without changing
# the web pixel-measurement identity. Imports, globals, recipes, event_contract
# and capture_record remain in the semantic stamp.
WEB_NON_MEASUREMENT = frozenset({'_input_identity', '_required_inputs', 'compare_records',
                               '_functional_browser_checks', 'functional_checks'})


def _need(condition, message, code='measurement_provenance_changed'):
    if not condition:
        raise VisualError(code, message)


def _web_measurement_hash(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 or node.name not in WEB_NON_MEASUREMENT]
    return digest(ast.dump(tree, include_attributes=False))


def measurement_source_hashes(stage, *, root=None):
    """Exact measurement closure; shared event options are never file-exempt.

    media_suite orchestration is deliberately absent. suite_web has an explicit
    semantic slice; all other measurement dependencies are byte-exact. Native
    annotation/template identities are additionally bound by the saved report.
    """
    _need(stage in STAGES, 'Unknown measurement stage.', 'invalid_suite')
    root = Path(root or ROOT)
    paths = set((root / 'kiroclops/visual').glob('*.py'))
    paths.update(root / path for path in (
        'kiroclops/__init__.py', 'kiroclops/common.py',
        'kiroclops/cloud/execution.py', 'kiroclops/cloud/identity.py', 'kiroclops/cloud/results.py',
        'kiroclops/local_engine/backend.py', 'kiroclops/local_engine/worker.py',
        'kiroclops/local_engine/evaluation.py', 'kiroclops/local_engine/suite_revision.py',
        'kiroclops/local_engine/source_inventory.py', 'kiroclops/local_engine/session.py',
        'kiroclops/local_engine/stage_closure.py'))
    if stage in ('panel', 'svg'):
        paths.update((root / 'kiroclops/web_local').glob('*.py'))
        # New captures preserve the actual replay wrapper and frozen policy;
        # old stamps remain readable with their historical source inventory.
        for name in ('suite_web_v2','realistic_policy','execution_policy','suite_web_v3','realistic_policy_v3','suite_web_v4',
                     'suite_web_v5'):
            path=root / ('kiroclops/local_engine/'+name+'.py')
            if path.is_file(): paths.add(path)
        paths.update(root / ('kiroclops/local_engine/' + name + '.py') for name in
                     ('suite_intake', 'suite_intake_binding', 'media_session', 'capture_modes'))
    else:
        paths.update(root / path for path in (
            'kiroclops/local_engine/game.py', 'kiroclops/local_engine/suite_game.py',
            'kiroclops/local_engine/suite_prepared_target.py',
            'kiroclops/integration/storage.py', 'kiroclops/integration/private_desktop_v1.py',
            'config/tiny-annotation.json', 'config/phase2b/tiny-annotation.json',
            'config/phase2c-v2/heart-jump-annotation.json', 'config/phase2c-v2/heart-ground-annotation.json'))
        for directory in ('kiroclops/portable_v2', 'kiroclops/ground'):
            paths.update((root / directory).rglob('*.py'))
        paths.update((root / 'kiroclops/integration').rglob('*.py'))
    _need(bool(paths) and all(path.is_file() for path in paths),
          'Measurement dependencies are missing.', 'missing_analysis_sources')
    result = {path.relative_to(root).as_posix(): file_sha256(path) for path in sorted(paths)}
    if stage in ('panel', 'svg'):
        result['kiroclops/local_engine/suite_web.py#measurement_ast'] = _web_measurement_hash(
            root / 'kiroclops/local_engine/suite_web.py')
    else:
        for relative in ('config/tiny-annotation.json', 'config/phase2b/tiny-annotation.json',
                         'config/phase2c-v2/heart-jump-annotation.json', 'config/phase2c-v2/heart-ground-annotation.json'):
            annotation = read_json(root / relative)
            template = Path(annotation['template_run']) / 'frames' / f"{annotation.get('template_frame', 0):06d}.png"
            result[relative + '#template_frame'] = file_sha256(template)
    result['@stage'] = digest(stage)
    return result


def _values(record):
    from .evidence import observations, event_set
    observed, events = observations(record), event_set(record)
    selection = record.get('selection')
    if isinstance(selection, (str, Path)):
        selection = read_json(selection)
    return observed, events, selection


def _common_result(record, stage):
    path = Path(record['analysis'])
    if stage in ('jump', 'ground'):
        path = path.parent / 'common-analysis/analysis.json'
    return read_json(path), path.parent


def _analysis_candidate(record, stage, manifest):
    if record.get('reference_kind') == 'uploaded_pixels' or stage in ('jump', 'ground') and manifest['metadata'].get('game') == 'tiny':
        return None
    return record.get('source_sha256')


def _worker_proof(record, stage, contract):
    """Preserve the actual worker's source and binary receipt before cloning."""
    result, directory = _common_result(record, stage)
    operation = read_json(directory / 'analysis-operation.json')
    job_id = operation.get('job_id')
    _need(isinstance(job_id, str) and job_id.isalnum(), 'Analysis worker identity is missing.')
    job = directory / 'jobs/jobs' / job_id
    state, request, delivery = (read_json(job / name) for name in ('state.json', 'request.json', 'delivery-v2.json'))
    observed, _, selection = _values(record)
    package = load_package(record['package'], verify='hashes')
    expected = request_binding(package['package_sha256'], selection, contract, _analysis_candidate(record, stage, package))
    # Native reference source identity describes the pinned renderer, while its
    # common worker request intentionally has no candidate identity.
    _need(request == expected and operation.get('request_sha256') == digest(request)
          and operation.get('status') == state.get('status') == 'succeeded'
          and state.get('request_sha256') == digest(request)
          and state.get('result_sha256') == digest(result)
          and state.get('delivery_sha256') == digest(delivery), 'Analysis execution receipt differs.')
    validated = legacy_result(delivery, request, logical_job_id=job_id, expected_sources=service_sources())
    _need(validated == result and observed == result.get('observations'), 'Worker pixels or observations differ.')
    return {'request': request, 'receipt': delivery['receipt'], 'receipt_sha256': delivery['receipt_sha256'],
            'worker_result_sha256': digest(result),
            'execution_interval_s': [state.get('started_at'), state.get('finished_at')]}


def _measurement_contract(record, stage):
    if stage in ('panel', 'svg'):
        from .suite_web import event_contract
        return event_contract(stage)
    from . import game
    native = read_json(_ordinary(record['raw_run']) / 'manifest.json')
    annotation = read_json(game._annotation(stage, native['game']))
    return {'min_valid_ratio': .95, 'require_static_camera': True,
            'camera_max_shift_px': annotation.get('camera_max_shift_px', 1.)}


def stamp_new_measurement(record, stage, *, expected_sources, capture_started_at):
    """Stamp a fresh capture using its caller's explicit pre-capture snapshot.

    The caller records capture_started_at immediately after taking the snapshot,
    before starting the actual capture. An already completed worker cannot pass
    this chronological check. Historical records use revise_record instead.
    """
    _need(stage in STAGES, 'Unknown measurement stage.', 'invalid_suite')
    _need(isinstance(expected_sources, dict) and expected_sources == measurement_source_hashes(stage),
          'Fresh capture used a different measurement pipeline.', 'analysis_source_changed')
    _need(type(capture_started_at) in (int, float) and math.isfinite(capture_started_at)
          and 0 < capture_started_at <= time.time(), 'A pre-capture timestamp is required.', 'fresh_capture_provenance_missing')
    _need(not any(record.get(key) is not None for key in ('measurement_source_hashes', 'measurement_provenance', 'measurement_revision')),
          'Do not restamp a historical or already stamped measurement.')
    _need(record.get('measurement_status') == 'ok', 'Fresh measurement does not qualify.', 'tracking_failure')
    contract = _measurement_contract(record, stage)
    worker = _worker_proof(record, stage, contract)
    started, finished = worker['execution_interval_s']
    _need(all(type(value) in (int, float) and math.isfinite(value) for value in (started, finished))
          and capture_started_at <= started <= finished <= time.time(),
          'Worker execution predates the declared fresh capture.', 'fresh_capture_provenance_missing')
    _need(expected_sources == measurement_source_hashes(stage),
          'Measurement pipeline changed during capture.', 'analysis_source_changed')
    value = deepcopy(record)
    value['measurement_source_hashes'] = deepcopy(expected_sources)
    value['measurement_provenance'] = {'kind': 'SuitePixelMeasurement', 'schema_version': 1,
        'measurement_source_hashes': deepcopy(expected_sources), 'contract_sha256': digest(contract),
        'binding': _record_binding(value, stage), 'worker': worker,
        'fresh_capture_started_at': capture_started_at, 'historical_restamp': False}
    verify_measurement_record(value, stage, expected_sources=expected_sources,
                              expected_engine_sha256=engine_identity()['engine_sha256'])
    return value


def _record_binding(record, stage):
    observed, events, selection = _values(record)
    package = load_package(record['package'], verify='hashes')
    _need(isinstance(observed, dict) and isinstance(events, dict) and isinstance(selection, dict),
          'Pixel measurement, events or selection is missing.')
    _need(observed.get('package_sha256') == package['package_sha256'], 'Observation belongs to different pixels.')
    common, _ = _common_result(record, stage)
    _need(common.get('observations') == observed, 'Saved common analysis and observations differ.')
    value = {'stage': stage, 'package_sha256': package['package_sha256'],
             'observations_sha256': digest(observed), 'events_sha256': digest(events),
             'selection_sha256': digest(selection), 'scenario_sha256': digest(record['scenario']),
             'source_sha256': record.get('source_sha256'), 'worker_result_sha256': digest(common)}
    if record.get('execution_scenario') is not None:
        from .suite_web_v2 import _binding as verify_replay_binding
        verify_replay_binding(record)
        value.update(execution_scenario_sha256=digest(record['execution_scenario']),
            input_replay_sha256=digest(record.get('input_replay')))
    if stage in ('jump', 'ground'):
        projection = read_json(record['analysis'])
        expected_candidate = _analysis_candidate(record, stage, package)
        _need(projection.get('candidate_sha256') == expected_candidate
              and projection.get('provenance', {}).get('package_sha256') == package['package_sha256']
              and projection.get('provenance', {}).get('observation_sha256') == digest(observed),
              'Native metric projection differs from its pixels or full source identity.')
        value['projection_sha256'] = digest(projection)
    return value


def verify_measurement_record(record, stage, *, expected_sources=None, expected_engine_sha256=None):
    """Read-only exact identity check for a stamped saved measurement."""
    sources, proof = record.get('measurement_source_hashes'), record.get('measurement_provenance')
    _need(isinstance(sources, dict) and bool(sources) and isinstance(proof, dict),
          'Per-record measurement provenance is missing.', 'measurement_provenance_missing')
    _need(proof.get('schema_version') == 1 and proof.get('kind') == 'SuitePixelMeasurement'
          and proof.get('measurement_source_hashes') == sources
          and proof.get('binding') == _record_binding(record, stage), 'Measurement stamp or output binding changed.')
    worker = proof.get('worker', {})
    common, _ = _common_result(record, stage)
    request, receipt = worker.get('request'), worker.get('receipt')
    _need(isinstance(request, dict) and isinstance(receipt, dict), 'Worker code and engine receipt is missing.')
    measurement = make_measurement(common['observations'], common['events'])
    delivery = {'schema_version': 2, 'kind': 'AnalysisDelivery', 'measurement': measurement,
                'measurement_sha256': digest(measurement), 'receipt': receipt,
                'receipt_sha256': worker.get('receipt_sha256')}
    validate_delivery(delivery, request, logical_job_id=receipt.get('logical_job_id'))
    _need(worker.get('worker_result_sha256') == digest(common)
          and legacy_result(delivery, request, logical_job_id=receipt['logical_job_id']) == common,
          'Saved worker result differs from its receipt.')
    _need(all(sources.get(path) == sha for path, sha in receipt['analysis_sources'].items()),
          'Worker sources differ from the wider measurement closure.')
    _need(request.get('package_sha256') == proof['binding']['package_sha256']
          and digest(request.get('selection')) == proof['binding']['selection_sha256']
          and digest(request.get('contract')) == proof.get('contract_sha256'), 'Worker request or contract changed.')
    expected_candidate = _analysis_candidate(record, stage, load_package(record['package'], verify='hashes'))
    _need(request.get('candidate_sha256') == expected_candidate, 'Worker belongs to another source identity.')
    if stage in ('panel', 'svg'):
        _need(record.get('suite_event_contract') == record.get('independent_review_contract') == request['contract']
              and record.get('event_contract_sha256') == digest(request['contract']), 'Production/review contract differs.')
        if record.get('reference_kind') == 'uploaded_pixels':
            from .suite_intake_binding import verify_reference_record
            verify_reference_record(record)
    if expected_sources is not None:
        _need(sources == expected_sources, 'Saved measurement uses a different pipeline.', 'analysis_source_changed')
    if expected_engine_sha256 is not None:
        _need(receipt['engine']['engine_sha256'] == expected_engine_sha256,
              'Saved measurement uses a different engine.', 'analysis_engine_changed')
    return proof


def _previous_identity(record):
    observed, events, selection = _values(record)
    return {'record_sha256': digest(record), 'package_sha256': load_package(record['package'], verify='hashes')['package_sha256'],
            'observations_sha256': digest(observed), 'events_sha256': digest(events), 'selection_sha256': digest(selection),
            'analysis_sha256': file_sha256(record['analysis']) if record.get('analysis') else None,
            'measurement_source_hashes': deepcopy(record.get('measurement_source_hashes')),
            'measurement_provenance': deepcopy(record.get('measurement_provenance')),
            'historical_code_identity': 'recorded' if record.get('measurement_source_hashes') else 'unknown'}


def _reanalyze_web(record, output, stage, reference_selection, reference_record, check, progress):
    from . import evaluation
    from .suite_web import event_contract
    from kiroclops.web_local.selection import select_package_from_pixels
    package = output / 'package'
    copy_package(record['package'], package)
    manifest = load_package(package, verify='hashes')
    imported_reference = record.get('reference_kind') == 'uploaded_pixels'
    imported_target = bool(record.get('intake_binding')) and not imported_reference
    if imported_reference:
        from .suite_intake_binding import verify_reference_record
        verify_reference_record(record)
        selection = deepcopy(record['selection'])
    else:
        try:
            selection = select_package_from_pixels(package, stage)
        except ValueError as exc:
            raise VisualError('tracking_failure', str(exc)) from exc
    if imported_target:
        from . import media_session
        from .suite_intake_binding import verify_reference_record
        _need(isinstance(reference_record, dict), 'Imported target needs its exact frozen reference.', 'needs_reference')
        verify_reference_record(reference_record)
        binding = record['intake_binding']
        _need(binding == reference_record['intake_binding'], 'Imported target mapping or recipe changed.')
        lookup = {obj['id']: obj for obj in selection['objects']}
        source_objects = {obj['id']: obj for obj in reference_record['selection']['objects']}
        objects = []
        for ref_id, target_id in binding['object_map'].items():
            _need(target_id in lookup, 'Explicitly mapped target object is missing.', 'tracking_failure')
            obj = media_session._preserve_point_semantics(source_objects[ref_id], lookup[target_id], manifest)
            obj['id'] = ref_id
            objects.append(obj)
        selection.update(objects=objects, connections=deepcopy(binding.get('connections', [])),
                         relative_motion=deepcopy(binding['relative_motion']))
        reference_selection = reference_record['selection']
    if imported_reference:
        bindings = selection['lifecycle_bindings']
    elif reference_selection is not None:
        bindings = reference_selection['lifecycle_bindings']
        intervals = evaluation._map_lifecycle(bindings, manifest)
        lookup = {obj['id']: obj for obj in selection['objects']}
        for expected in reference_selection['objects']:
            _need(expected['id'] in lookup, 'Required visible object is missing.', 'tracking_failure')
            if record['scenario'].get('analysis_policy') != 'observed_response_v2':
                lookup[expected['id']]['active_intervals_s'] = intervals[expected['id']]
        for key in ('connections', 'relative_motion'):
            if reference_selection.get(key):
                selection[key] = deepcopy(reference_selection[key])
    else:
        bindings = evaluation._lifecycle_bindings(selection, manifest)
    selection['lifecycle_bindings'] = deepcopy(bindings)
    selection['input_origin_s'] = manifest['inputs']['events'][0]['time_s'] if manifest['inputs']['events'] else 0.
    destination = output / 'measurement'
    write_json(destination / 'selection.json', selection)
    contract = event_contract(stage)
    result = evaluation._analysis(package, selection, destination, check, progress,
                                  contract=contract, candidate_sha256=_analysis_candidate(record, stage, manifest))
    value = {**deepcopy(record), 'package': str(package), 'analysis': str(destination / 'analysis.json'),
             'selection': selection, 'observations': result['observations'], 'events': result['events'],
             'measurement_status': result.get('measurement_status'), 'suite_event_contract': deepcopy(contract),
             'independent_review_contract': deepcopy(contract), 'event_contract_sha256': digest(contract)}
    if imported_reference:
        value['intake_observations_sha256'] = digest(value['observations'])
    for key in ('observations', 'events'):
        write_json(destination / (key + '.json'), value[key])
    return value, contract


def _reference_selection_matches(record, reference_selection):
    if reference_selection is None:
        return True
    from . import evaluation
    _, _, selection = _values(record)
    if not isinstance(selection, dict) or selection.get('lifecycle_bindings') != reference_selection['lifecycle_bindings']:
        return False
    intervals = evaluation._map_lifecycle(reference_selection['lifecycle_bindings'], load_package(record['package'], verify='hashes'))
    objects = {obj['id']: obj for obj in selection['objects']}
    for expected in reference_selection['objects']:
        if expected['id'] not in objects:
            return False
        if record['scenario'].get('analysis_policy') != 'observed_response_v2' and objects[expected['id']].get('active_intervals_s') != intervals[expected['id']]:
            return False
    return all(not reference_selection.get(key) or selection.get(key) == reference_selection[key]
               for key in ('connections', 'relative_motion'))


def revise_record(record, output, stage, *, reference_selection=None, reference_record=None, check=lambda: None,
                  progress=lambda *args: None, expected_sources=None, current_engine=None):
    """Copy a verified current record, otherwise remeasure its existing pixels.

    Invalid existing stamps fail closed. A valid older stamp or an unstamped
    record can be remeasured; its earlier identity remains in revision lineage.
    """
    _need(stage in STAGES, 'Unknown measurement stage.', 'invalid_suite')
    output = _ordinary(output)
    _need(not output.exists() and not output.is_relative_to(_ordinary(record['package']).parent),
          'Revision output must be new and outside earlier evidence.', 'output_exists')
    _need(not record.get('raw_run') or not output.is_relative_to(_ordinary(record['raw_run'])),
          'Revision output must be outside native evidence.', 'unsafe_path')
    check()
    sources = expected_sources or measurement_source_hashes(stage)
    _need(sources == measurement_source_hashes(stage), 'Measurement pipeline changed before revision.', 'analysis_source_changed')
    engine = current_engine or engine_identity()
    previous = _previous_identity(record)
    proof = None
    if record.get('measurement_source_hashes') is not None or record.get('measurement_provenance') is not None:
        proof = verify_measurement_record(record, stage)
    can_copy = (proof is not None and record['measurement_source_hashes'] == sources
                and proof['worker']['receipt']['engine']['engine_sha256'] == engine['engine_sha256']
                and (stage not in ('panel', 'svg') or _reference_selection_matches(record, reference_selection)))
    if can_copy:
        from .media_suite import clone_record
        value = clone_record(record, output)
        action = 'copied_current_measurement'
    else:
        if stage in ('panel', 'svg'):
            value, contract = _reanalyze_web(record, output, stage, reference_selection, reference_record, check, progress)
        else:
            from . import suite_game, game
            raw = _ordinary(record['raw_run'])
            native = read_json(raw / 'manifest.json')
            if native.get('game') == 'tiny':
                suite_game.verify_reference_record(record, stage)
            result = suite_game.analyze_record(raw, stage, record['scenario'], output / 'measurement',
                check=check, progress=progress, candidate_sha256=None if native.get('game') == 'tiny' else record.get('source_sha256'),
                legacy=record.get('reference_kind') == 'known_pixels')
            value = {**deepcopy(record), **result}
            annotation = read_json(game._annotation(stage, native['game']))
            contract = {'min_valid_ratio': .95, 'require_static_camera': True,
                        'camera_max_shift_px': annotation.get('camera_max_shift_px', 1.)}
        _need(value.get('measurement_status') == 'ok', 'Remeasured pixels do not qualify.', 'tracking_failure')
        _need(load_package(value['package'], verify='hashes')['package_sha256'] == previous['package_sha256'],
              'Revision changed raw pixels, timing or producer metadata.')
        worker = _worker_proof(value, stage, contract)
        value['measurement_source_hashes'] = deepcopy(sources)
        value['measurement_provenance'] = {'kind': 'SuitePixelMeasurement', 'schema_version': 1,
            'measurement_source_hashes': deepcopy(sources), 'contract_sha256': digest(contract),
            'binding': _record_binding(value, stage), 'worker': worker}
        action = 'reanalyzed_existing_pixels'
    check()
    _need(sources == measurement_source_hashes(stage), 'Measurement pipeline changed during revision.', 'analysis_source_changed')
    verify_measurement_record(value, stage, expected_sources=sources, expected_engine_sha256=engine['engine_sha256'])
    value['measurement_revision'] = {'schema_version': 1, 'action': action, 'previous': previous,
                                     'pixels_preserved': True, 'native_capture_started': False, 'model_started': False}
    write_json(output / 'record.json', value)
    return value


def revise_suite(previous_suite, output, authoring_parent, *, check=lambda: None, progress=lambda *args: None):
    """Create a new bound suite from old pixels; every review becomes pending."""
    from . import media_suite
    from .suite_evidence import _source_expectation
    from . import evidence
    previous_suite, output, authoring_parent = map(_ordinary, (previous_suite, output, authoring_parent))
    _need(not output.exists() and not output.is_relative_to(previous_suite)
          and not output.is_relative_to(authoring_parent), 'Use a fresh revision output outside source/evidence.', 'output_exists')
    suite = media_suite.load_suite(previous_suite, verify_analysis_code=False)
    _need(source_hashes(authoring_parent) == suite['authoring_parent_source_hashes'], 'Authoring parent changed.', 'wrong_authoring_parent')
    if suite['stage'] in ('panel', 'svg'):
        from .suite_web import event_contract
        _need(suite.get('event_contract') == event_contract(suite['stage']),
              'A revision must preserve the approved event contract.', 'revision_contract_changed')
    stages = {case['stage'] for case in suite['cases']}
    stamps = {stage: measurement_source_hashes(stage) for stage in stages}
    engine = engine_identity()
    executable_sources = media_suite.analysis_source_hashes()
    old_sha = digest(suite)
    records, revised_by_id, initial, fixed = [], {}, [], []
    output.mkdir(parents=True)
    write_json(output / 'revision-plan.json', {'previous_suite': str(previous_suite), 'previous_suite_sha256': old_sha,
        'reference_cases': len(suite['cases']), 'initial_cases': len(suite.get('initial_records', [])),
        'private_baseline_records': len(suite.get('baseline_records', [])),
        'native_capture_started': False, 'model_started': False, 'status': 'analysis_pending'})
    for index, case in enumerate(suite['cases']):
        progress('reference ' + case['case_id'], index, len(suite['cases']))
        record = media_suite.record_from_suite(previous_suite, case)
        revised = revise_record(record, output / 'measurements/reference' / case['case_id'], case['stage'],
            check=check, progress=progress, expected_sources=stamps[case['stage']], current_engine=engine)
        records.append({**deepcopy(case), 'record': revised})
        revised_by_id[case['case_id']] = revised
    cached_items = [('initial', item) for item in suite.get('initial_records', [])]
    cached_items += [('fixed', item) for item in suite.get('baseline_records', [])]
    seen = set()
    for index, (group, item) in enumerate(cached_items):
        _need((group, item['case_id'], item['role']) not in seen, 'Baseline cache contains duplicate cases.', 'wrong_baseline')
        seen.add((group, item['case_id'], item['role']))
        case = next(c for c in suite['cases'] if c['case_id'] == item['case_id'])
        _need(item['role'] in ('original', 'before') and
              (case['role'] == 'calibration' if group == 'initial' else case['role'] in ('regression', 'validation')),
              'Baseline cache role or split differs.', 'wrong_baseline')
        record = media_suite.record_from_suite(previous_suite, item)
        source = previous_suite / suite['original'] if item['role'] == 'original' else authoring_parent
        full_hashes = suite['original_source_hashes'] if item['role'] == 'original' else suite['authoring_parent_source_hashes']
        expected = _source_expectation(source, full_hashes, suite['domain'])
        evidence._record_source(record, load_package(record['package'], verify='hashes'), expected)
        progress(group + ' ' + item['role'] + ' ' + item['case_id'], index, len(cached_items))
        revised = revise_record(record, output / 'measurements' / group / item['role'] / item['case_id'], case['stage'],
            reference_selection=revised_by_id[item['case_id']]['selection'] if case['stage'] in ('panel', 'svg') else None,
            reference_record=revised_by_id[item['case_id']],
            check=check, progress=progress, expected_sources=stamps[case['stage']], current_engine=engine)
        (initial if group == 'initial' else fixed).append({**deepcopy(item), 'record': revised})
    check()
    _need(stamps == {stage: measurement_source_hashes(stage) for stage in stages}
          and executable_sources == media_suite.analysis_source_hashes(),
          'Measurement or orchestration changed during revision.', 'analysis_source_changed')
    _need(digest(media_suite.load_suite(previous_suite, verify_analysis_code=False)) == old_sha,
          'Previous suite changed during revision.')
    new = media_suite.create_suite(output / 'suite', domain=suite['domain'], stage=suite['stage'], records=records,
        required_cases=suite['required_cases'], original=previous_suite / suite['original'],
        authoring_parent=authoring_parent, goal=suite['goal'], event_contract=suite['event_contract'],
        preservation=suite['preservation_contract'], initial_records=initial, baseline_records=fixed)
    all_records = [item['record'] for item in records + initial + fixed]
    result = {'status': 'ready_for_raw_review', 'suite': str(output / 'suite'), 'suite_sha256': digest(new),
        'previous_suite': str(previous_suite), 'previous_suite_sha256': old_sha,
        'reference_cases': len(records), 'initial_cases': len(initial),
        'private_baseline_records': len(fixed), 'measurement_revision_required': False,
        'reanalyzed_records': sum(r['measurement_revision']['action'] == 'reanalyzed_existing_pixels' for r in all_records),
        'copied_records': sum(r['measurement_revision']['action'] == 'copied_current_measurement' for r in all_records),
        'reference_pool_changed': False, 'pixels_preserved': True, 'native_capture_started': False, 'model_started': False,
        'reference_raw_review_status': 'pending', 'initial_raw_review_status': 'pending'}
    write_json(output / 'revision.json', result)
    return result
