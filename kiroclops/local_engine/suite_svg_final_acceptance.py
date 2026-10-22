"""Standalone unchanged-SVG closure; no capture, model, or attestation writes.

All evidence remains in its original epoch. A named human must inspect source
and historical generation evidence separately from the final image inspector.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import html
import json
import math
from pathlib import Path
import re
import time

from kiroclops.cloud.identity import engine_identity
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from . import evidence, media_suite, review, suite_initial_acceptance as initial, suite_revision, suite_svg_functional
from .session import Session, _ordinary, source_hashes

VERSION = 1
ROLES = ('reference', 'original', 'before', 'unchanged')
SOURCE_CHECKS = frozenset(('historical_authorship_inspected', 'no_coordinate_replay',
    'no_private_reference_access', 'no_operator_coaching', 'protected_behavior_preserved'))
FUNCTIONAL_CHECKS = frozenset(('selection_add_remove', 'resize_structure', 'pointer_cancel', 'rapid_mode_changes', 'reset'))


def _need(value, message, code='incomplete_svg_acceptance'):
    if not value:
        raise VisualError(code, message)


def _file(path, inventory):
    path = _ordinary(path)
    _need(path.is_file(), 'A required evidence file is missing: ' + str(path))
    inventory[str(path)] = file_sha256(path)
    return read_json(path)


def _code_sources():
    paths = [Path(__file__).resolve(), media_suite.ROOT / 'scripts/phase3d_svg_final_acceptance.py',
             Path(suite_svg_functional.__file__).resolve(), media_suite.ROOT / 'scripts/phase3d_svg_functional.py']
    return {str(p): file_sha256(p) for p in paths}


def _historical_proof(value, verification):
    """Use the existing version dispatcher; unsupported evidence stays blocked."""
    generation = verification['historical_generation']
    root = _ordinary(generation['historical_session'])
    _need(value.get('kind') == 'HistoricalSVGGenerationEvidenceBindings' and value.get('schema_version') == 1
          and _ordinary(value.get('historical_session', '')) == root
          and value.get('historical_candidate_id') == generation['historical_candidate_id']
          and value.get('new_model_generation') is False and isinstance(value.get('files'), dict) and value['files'],
          'Historical evidence must name the actual existing candidate and native files.')
    # audit_native_run dispatches supported historical CLI/KAS/launcher tuples.
    # A missing/unsupported tuple raises; no current-schema substitution occurs.
    actual = review.audit_native_run(root)
    _need(actual.get('status') == 'pass', 'Historical native generation cannot be replayed.')
    launch = read_json(root / 'kiro/launch.json')
    identity = {'model': actual['model'], 'effort': actual['effort'], 'native_session_id': actual['native_session_id'],
                'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version')}
    _need(value.get('recorded_model_identity') == identity,
          'Declared historical model identity differs from actual recorded transport.')
    files = {}
    for name, sha in value['files'].items():
        path = _ordinary(safe_path(root, name))
        _need(file_sha256(path) == sha, 'Historical generation evidence changed.')
        files[str(path)] = sha
    _need(all(value['files'].get(name) == sha for name, sha in actual['evidence_hashes'].items()),
          'Every replayed historical native file must remain in the source-inspection request.')
    source_path = _ordinary(value['source_review'])
    _need(file_sha256(source_path) == value.get('source_review_sha256'), 'Historical source-inspection receipt changed.')
    source_review = review.validate_source_review(source_path, root, candidate_sha256=generation['source_sha256'])
    _need(source_review.get('historical_only') is True and source_review.get('full_behavior_acceptance') is False,
          'Historical source inspection must remain explicitly historical and separate from behavior acceptance.')
    files[str(source_path)] = file_sha256(source_path)
    return {'status': 'pass', 'historical_only': True, 'model_identity': identity,
            'native_validation': actual, 'source_validation': source_review, 'files': files, 'new_model_generation': False}


def _context(directory):
    directory = _ordinary(directory)
    plan, suite = initial.load_plan(directory)
    result = initial.load_result(directory)
    _need(result.get('complete') is True and result.get('measurement_qualified') is True,
          'Only the complete qualified unchanged matrix can be closed.')
    _need(result.get('model_started') is False and result.get('new_model_generation') is False
          and result.get('source_unchanged') is True and result.get('candidate_count') == 0,
          'The unchanged workflow cannot assert a new model generation.')
    cases = initial._matrix(suite)
    _need(len(cases) == 28, 'All 28 SVG cases remain mandatory.')
    rows = {}
    for role in ROLES:
        values = read_json(directory / 'indexes' / (role + '.json'))
        for record in values:
            key = role, record['case_id']
            _need(key not in rows, 'Duplicate source-role/case recording.')
            rows[key] = record
    expected = {(role, c['case_id']) for role in ROLES for c in cases}
    _need(set(rows) == expected and len(rows) == 112, 'The complete 112-record matrix is required.')
    return directory, plan, suite, result, cases, rows


def _origins(plan, suite, cases, inventory):
    """Only named producer paths in the authenticated preparation chain."""
    head = _ordinary(plan['suite']).parent
    _need((head / 'session.json').is_file(), 'Canonical suite must retain its preparation Session.')
    allowed = {(role, c['case_id']): {'capture': set(), 'worker': set()} for role in ROLES for c in cases}
    seen = set()
    current = head
    while current is not None:
        _need(current not in seen and len(seen) < 20, 'Preparation lineage is cyclic or unexpectedly deep.')
        seen.add(current)
        state = Session(current)._load()
        for name in ('session.json', 'binding.json', 'environment-readiness.json'):
            _file(current / name, inventory)
        ready = read_json(current / 'environment-readiness.json')
        _need(state.get('started_at') is None and not state.get('candidates'),
              'An unchanged preparation lineage cannot contain model authoring.')
        _need(state.get('experiment', {}).get('reference_mode') == 'media_suite'
              and _ordinary(state['reference']) == current / 'reference', 'Unexpected preparation layout.')
        predecessor = media_suite.load_suite(state['reference'], verify_analysis_code=False)
        _need(digest(predecessor) == state['experiment']['suite_sha256']
              and predecessor['original_source_hashes'] == suite['original_source_hashes']
              and predecessor['authoring_parent_source_hashes'] == suite['authoring_parent_source_hashes']
              and [(c['case_id'], c['role'], c['scenario']) for c in predecessor['cases']]
              == [(c['case_id'], c['role'], c['scenario']) for c in suite['cases']],
              'Preparation lineage changes source roles or cases.')
        runtime = ready.get('runtime', {})
        root = _ordinary(runtime['root'])
        _need(current == root / 's' / current.name, 'Preparation belongs to another runtime root.')
        producer = root / 'p' / current.name
        prepared = _file(producer / 'result.json', inventory)
        _need(_ordinary(prepared['session']) == current and prepared.get('model_started') is False,
              'Preparation result does not bind its actual Session.')
        revision_path, cache_path = producer / 'revision.json', producer / 'cache.json'
        if revision_path.is_file():
            revision = _file(revision_path, inventory)
            _need(revision.get('pixels_preserved') is True and revision.get('native_capture_started') is False,
                  'A measurement revision cannot replace native captures.')
            for c in cases:
                cid = c['case_id']
                allowed['reference', cid]['worker'].add(producer / 'measurements/reference' / cid / 'measurement')
                group = 'initial' if c['role'] == 'calibration' else 'fixed'
                for role in ('original', 'before'):
                    allowed[role, cid]['worker'].add(producer / 'measurements' / group / role / cid / 'measurement')
        elif cache_path.is_file():
            cache = _file(cache_path, inventory)
            start = _file(producer / 'start-binding.json', inventory)
            _need(start.get('kind') == 'PrivateFixedBaselinePreparation'
                  and start.get('original_source_sha256') == digest(suite['original_source_hashes'])
                  and start.get('authoring_parent_source_sha256') == digest(suite['authoring_parent_source_hashes'])
                  and cache.get('private_baseline_cache_complete') is True,
                  'Fixed baseline preparation has another source or scope.')
            for c in cases:
                if c['role'] != 'calibration':
                    for role in ('original', 'before'):
                        path = producer / 'captures' / role / c['case_id']
                        allowed[role, c['case_id']]['capture'].add(path)
                        allowed[role, c['case_id']]['worker'].add(path / 'measurement')
        elif prepared.get('initial_cases') == 8 and prepared.get('reference_pool_changed') is False:
            for c in cases:
                if c['role'] == 'calibration':
                    for role in ('original', 'before'):
                        path = producer / role / c['case_id']
                        allowed[role, c['case_id']]['capture'].add(path)
                        allowed[role, c['case_id']]['worker'].add(path / 'measurement')
        else:
            start = _file(producer / 'start-binding.json', inventory)
            _need(start.get('stage') == 'svg' and start.get('model_started') is False
                  and start.get('original_sha256') == digest(suite['original_source_hashes'])
                  and start.get('authoring_parent_sha256') == digest(suite['authoring_parent_source_hashes']),
                  'Reference preparation is not this SVG/source epoch.')
            for c in cases:
                path = producer / 'references' / c['case_id']
                allowed['reference', c['case_id']]['capture'].add(path)
                allowed['reference', c['case_id']]['worker'].add(path / 'measurement')
        previous = ready.get('reference_revision')
        current = _ordinary(previous) if previous is not None else None
    return allowed


def _bindings(value, allowed, directory, cases):
    _need(value.get('kind') == 'UnchangedSVGCaptureOrigins' and value.get('schema_version') == VERSION
          and _ordinary(value.get('initial_acceptance_directory', '')) == directory,
          'Native/worker origins belong to another unchanged run.')
    found = {}
    for row in value.get('records', []):
        _need(isinstance(row, dict), 'An origin row is malformed.')
        key = row.get('role'), row.get('case_id')
        _need(key in allowed and key not in found, 'Foreign or duplicate origin role/case.')
        capture, worker = _ordinary(row['capture_directory']), _ordinary(row['worker_directory'])
        if key[0] == 'unchanged':
            expected = directory / 'records/unchanged' / key[1]
            _need(capture == expected and worker == expected / 'measurement',
                  'Unchanged evidence must be freshly produced inside this exact initial run.')
        else:
            _need(capture in allowed[key]['capture'] and worker in allowed[key]['worker'],
                  'A producer is outside the authenticated role/case preparation chain.')
        found[key] = capture, worker
    _need(set(found) == set(allowed) and len(found) == 112, 'Every capture and direct worker origin is mandatory.')
    return found


def _worker(record, worker, inventory, sources, engine):
    """Replay real direct receipts; a copied inline stamp is insufficient."""
    operation = _file(worker / 'analysis-operation.json', inventory)
    job = operation.get('job_id')
    _need(isinstance(job, str) and re.fullmatch(r'[A-Za-z0-9]{1,100}', job), 'Unsafe worker identity.')
    _file(worker / 'analysis.json', inventory)
    for name in ('request.json', 'state.json', 'delivery-v2.json'):
        _file(worker / 'jobs/jobs' / job / name, inventory)
    direct = {**record, 'analysis': str(worker / 'analysis.json')}
    proof = suite_revision._worker_proof(direct, 'svg', suite_revision._measurement_contract(record, 'svg'))
    stamped = suite_revision.verify_measurement_record(record, 'svg', expected_sources=sources,
                                                       expected_engine_sha256=engine)
    _need(proof == stamped['worker'] and read_json(record['analysis']) == read_json(direct['analysis']),
          'Direct worker differs from the stamped/copied measurement.')
    return digest(proof)


def _capture_impl(record, capture, expected, variant, inventory):
    """Replay original native transport/visible clock/trusted inputs, no browser."""
    from kiroclops.web_local import capture as native
    from kiroclops.web_local.oracle import validate_oracle
    from kiroclops.web_local.transport import normalize_compositor_delivery
    from kiroclops.web_local.visual_clock import bind_frame_times

    start = _file(capture / 'capture-start.json', inventory)
    result = _file(capture / 'capture-result.json', inventory)
    saved = _file(capture / 'capture-record.json', inventory)
    _need(not (capture / 'capture-error.json').exists() and result.get('status') == 'ok'
          and start.get('variant') == variant and start.get('scenario') == saved.get('scenario') == record['scenario']
          and start.get('source_hashes') == result.get('source_hashes') == expected
          and start.get('adapter_hashes') == result.get('adapter_hashes') == native.adapter_hashes(),
          'Native capture was failed, stale, foreign, or produced under another role/source.')
    package = load_package(record['package'], verify='hashes')
    origin = load_package(capture / 'package', verify='hashes')
    _need(_ordinary(result['package_dir']) == capture / 'package' and origin == package
          and package['metadata'].get('source_hashes') == expected
          and package['metadata'].get('adapter_hashes') == native.adapter_hashes()
          and package['metadata'].get('variant') == variant, 'Capture and copied package identities differ.')
    _file(capture / 'package/manifest.json', inventory)
    received = _file(capture / 'private/received-evidence.json', inventory)
    context = _file(capture / 'private/transport-context.json', inventory)
    order = _file(capture / 'private/transport-order.json', inventory)
    prior_frames = _file(capture / 'private/frame-receipts.json', inventory)
    clock = _file(capture / 'private/clock.json', inventory)
    oracle = _file(capture / 'private/oracle.json', inventory)
    runtime = _file(capture / 'private/clock-runtime.json', inventory)
    visible = _file(capture / 'private/visual-clock.json', inventory)
    _need(received.get('kind') == 'RawCaptureDiagnosticEvidence' and received.get('recording_complete') is True
          and received.get('error') is None and runtime.get('kind') == 'CaptureClockRuntime'
          and runtime.get('status') == 'completed' and runtime.get('loop_closed') is True
          and runtime.get('timer_period_released') is True
          and not any(k in runtime for k in ('error', 'timer_release_error', 'power_throttling_restore_error')),
          'Native capture did not close its owned clock/runtime successfully.')
    if runtime.get('timer_period_requested_ms') is not None:
        _need(runtime.get('timer_period_requested_ms') == 1 and runtime.get('timer_begin_result') == 0
              and runtime.get('timer_end_result') == 0, 'Native timer receipt uses another policy.')
    _need(oracle.get('scenario') == record['scenario'] and oracle.get('domain') == 'svg'
          and result['functional'] == oracle.get('functional') == validate_oracle(oracle, 'svg')
          and oracle['functional'].get('status') == 'pass', 'Native functional/relationship oracle differs or failed.')
    _need(isinstance(received.get('private_oracle'), dict)
          and all(oracle.get(k) == v for k, v in received['private_oracle'].items()),
          'Final private oracle differs from the actual captured diagnostic receipt.')
    frames = []
    for row in received['frames']:
        path = _ordinary(safe_path(capture, row['path']))
        _need(file_sha256(path) == row.get('png_sha256'), 'Received PNG changed.')
        inventory[str(path)] = file_sha256(path)
        frame = {k: v for k, v in row.items() if k not in ('path', 'png_sha256')}
        frame['data'] = base64.b64encode(path.read_bytes()).decode('ascii')
        frames.append(frame)
    observed_start, observed_end = context['browser_observation_interval_epoch_s']
    delivery_start, delivery_end = context['actual_delivery_interval_epoch_s']
    _need(delivery_start == observed_start and context['browser_version'] == saved['browser_version']
          == package['metadata']['browser_version'], 'Native browser/segment identity differs.')
    frames, replay_order = normalize_compositor_delivery(frames, segment_start_s=observed_start,
        segment_end_s=observed_end, delivery_end_s=delivery_end, browser_version=context['browser_version'])
    replay_order['raw_received_evidence_sha256'] = file_sha256(capture / 'private/received-evidence.json')
    _need(replay_order == order, 'Recorded native transport order cannot be replayed.')
    frames, excluded = native.exclude_redundant_checkpoints(frames)
    _need(excluded == prior_frames['excluded_redundant_checkpoints'], 'Native checkpoint exclusions changed.')
    commands = received['commands']
    _need(commands == clock['commands'] and [c['planned'] for c in commands] == record['scenario']['events'],
          'Native commands do not match the exact case recipe.')
    inputs = []
    expected_type = {'click': 'click', 'drag_start': 'pointerdown', 'drag_move': 'pointermove', 'drag_end': 'pointerup'}
    origin_time = oracle['start_epoch_s']
    for index, command in enumerate(commands):
        _need(command['index'] == index, 'Native command IDs differ.')
        event = next((e for e in oracle['events'] if e['command'] == index
            and e['event'] == expected_type[command['planned']['action']] and e['trusted'] is True), None)
        _need(event is not None, 'A required trusted native input receipt is absent.')
        native.require_input_deadline(command['planned']['time_s'], event['epoch_s'] - origin_time)
        inputs.append({**command['planned'], 'planned_time_s': command['planned']['time_s'],
            'time_s': event['epoch_s'] - origin_time, 'receipt_event': event['event'],
            'client_x': event['client_x'], 'client_y': event['client_y'], 'pointer_id': event['pointer_id'],
            'command_id': index, 'clock_uncertainty_s': max(clock['start']['uncertainty_s'], clock['end']['uncertainty_s'])})
    epochs = [e['epoch_s'] for e in oracle['events'] if e.get('trusted') is True]
    frames, excluded = native.exclude_input_ambiguous_frames(frames, epochs)
    _need(excluded == prior_frames['excluded_input_ambiguous_frames'], 'Native input-side exclusions changed.')
    frames = native.merge_frame_receipts(frames)
    frames, replay_visible = bind_frame_times(frames, oracle['visual_clock'],
        clock_tolerance_s=max(clock['start']['uncertainty_s'], clock['end']['uncertainty_s']))
    frames, excluded = native.exclude_input_ambiguous_frames(frames, epochs)
    replay_visible['input_ambiguous_images'] = excluded
    _need(replay_visible == visible, 'Actual visible SVG clock cannot be replayed.')
    frames = [f for f in frames if f['epoch_s'] >= origin_time]
    _need(clock.get('frames') == [{k: v for k, v in f.items() if k != 'data'} for f in frames],
          'Final native clock rows differ from exact transport/visible-clock replay.')
    times = [f['epoch_s'] - origin_time for f in frames]
    metadata = [{'timestamp_source': f['timestamp_source'],
        'uncertainty_s': (f['interval_epoch_s'][1] - f['interval_epoch_s'][0]) / 2,
        'time_interval_s': [t - origin_time for t in f['interval_epoch_s']]} for f in frames]
    _need(saved['timestamps'] == times and saved['frame_metadata'] == metadata
          and saved['inputs'] == package['inputs'] == {'provenance': 'recorder_verified', 'events': inputs}
          and len(frames) == len(package['frames']), 'Native frames/timing/trusted inputs differ from measured pixels.')
    for index, (frame, published) in enumerate(zip(frames, package['frames'])):
        path = _ordinary(safe_path(capture, saved['frames'][index]))
        inventory[str(path)] = file_sha256(path)
        _need(path == capture / 'raw-frames' / f'{index:06d}.png'
              and path.read_bytes() == base64.b64decode(frame['data'], validate=True)
              and file_sha256(path) == published['sha256'] and published['time_s'] == times[index]
              and all(published[k] == v for k, v in metadata[index].items()), 'A measured PNG/native frame clock differs.')
    timing = native.timing_summary(times, [clock['start'], clock['end']], inputs,
                                   record['scenario']['timing_limits'], metadata)
    timing['frame_clock'] = 'Visible SVG counter bound to real rendering-batch interval; CDP transport times retained privately'
    timing['source_clock_semantics'] = {**timing['source_clock_semantics'], 'measurement_clock': 'svg_visible_raf_batch',
        'physical_display_presentation_claimed': False, 'source_geometry_used': False}
    _need(timing['status'] == 'pass' and timing == saved['timing'] == result['timing'] == record['timing']
          == package['metadata']['timing'], 'Native timing policy or its result changed.')
    return {'package_sha256': package['package_sha256'], 'capture_directory': str(capture),
            'capture_result_sha256': digest(result), 'browser_version': saved['browser_version'],
            'native_capture_replay': 'pass', 'physical_display_presentation_claimed': False}


def _capture(record, capture, expected, variant, inventory):
    try:
        return _capture_impl(record, capture, expected, variant, inventory)
    except VisualError:
        raise
    except (RuntimeError, KeyError, TypeError, ValueError) as error:
        raise VisualError('native_capture_replay_failed', str(error)) from error


def _numerics(directory, plan, result, cases, rows):
    comparisons, preserved = [], []
    for case in cases:
        cid = case['case_id']
        ref, original, parent, unchanged = (rows[role, cid] for role in ROLES)
        value = media_suite._compare(ref, original, unchanged, 'svg', require_improvement=True)
        prior = media_suite._parent_acceptance(ref, original, parent, 'svg')
        keep = media_suite._compare(parent, parent, unchanged, 'svg', require_improvement=False)
        comparisons.append({**value, 'case_id': cid, 'role': case['role']})
        preserved.append({**keep, 'case_id': cid, 'role': case['role'], 'accepted_parent_reference_gates': prior['pass'],
            'parent_reference_result': prior, 'preservation_required': True})
    _need(comparisons == result['comparisons'] == read_json(directory / 'comparisons.json')
          and preserved == result['parent_preservation'] == read_json(directory / 'preservation.json'),
          'Stored numerical/relationship results differ from current exact replay.')
    functional = read_json(directory / 'functional.json')
    checks = functional.get('checks', [])
    _need(functional == result['functional'] and functional.get('kind') == 'ReferenceSuiteFunctionalChecks'
          and functional.get('schema_version') == 1 and functional.get('domain') == 'svg'
          and functional.get('checks_source') == 'separate_private_browser_oracle'
          and functional.get('source_sha256') == plan['unchanged_source_sha256']
          and len(checks) == len(FUNCTIONAL_CHECKS) and {c.get('id') for c in checks} == FUNCTIONAL_CHECKS,
          'Required separate SVG functional checks are absent, foreign, or incomplete.')
    passed = all(initial._measurement_ok(r) and r.get('pass') is True for r in comparisons + preserved)
    passed = passed and functional.get('status') == 'pass' and not functional.get('page_errors') \
        and all(c.get('pass') is True for c in checks)
    _need(result.get('numerical_pass') is bool(passed), 'Saved numerical verdict contradicts actual replay.')
    _need(passed, 'The genuine unchanged numerical/functional behavior result failed.', 'svg_numerical_failure')
    return {'status': 'pass', 'comparison_count': 28, 'preservation_count': 28,
            'functional_sha256': digest(functional), 'thresholds_changed': False}


def _visual(path, directory, plan, rows):
    value = review.validate_independent_review(path, candidate_sha256=plan['unchanged_source_sha256'],
                                              initial_acceptance_directory=directory)
    wanted = {(load_package(r['package'], verify='hashes')['package_sha256'], digest(evidence.observations(r)))
              for r in rows.values()}
    _need(value.get('kind') == 'IndependentFinalRawVisualReview' and value.get('status') == 'pass'
          and value.get('record_count') == 112 and value.get('selected_candidate_id') is None
          and value.get('seal_kind') == 'UnchangedHistoricalSVGFinalRawMatrix'
          and value.get('session_directory') is None and _ordinary(value['initial_acceptance_directory']) == directory
          and set(map(tuple, value.get('required_observations', []))) == wanted
          and value.get('verified_scopes') == {scope: 'pass' for scope in ('position', 'events', 'relationships')},
          'Final raw review omits a role/case/scope or belongs to another seal.')
    return value


def _verification(directory, visual_receipt, origins_file, *, functional_execution=None, functional_witness=None):
    _need(functional_execution is not None and functional_witness is not None,
          'A genuine owned functional rerun and independent witness are mandatory.', 'incomplete_svg_functional')
    directory, plan, suite, result, cases, rows = _context(directory)
    inventory = {}
    for name in ('initial-plan.json', 'initial-plan-binding.json', 'result.json', 'result-binding.json',
                 'functional.json', 'comparisons.json', 'preservation.json'):
        _file(directory / name, inventory)
    for role in ROLES:
        _file(directory / 'indexes' / (role + '.json'), inventory)
    origins_value = _file(origins_file, inventory)
    allowed = _origins(plan, suite, cases, inventory)
    origins = _bindings(origins_value, allowed, directory, cases)
    from kiroclops.web_local.capture import project_hashes
    from kiroclops.web_local.specs import fixture_project
    projects = {'reference': fixture_project('svg', 'reference'), 'original': Path(plan['suite']) / suite['original'],
                'before': Path(plan['authoring_parent']), 'unchanged': Path(plan['unchanged_source'])}
    hashes = {role: project_hashes(p) for role, p in projects.items()}
    _need(hashes['original'] == plan['original_source_hashes']
          and hashes['before'] == hashes['unchanged'] == plan['authoring_parent_source_hashes'],
          'Actual full original/parent/unchanged bytes changed.')
    for role, project in projects.items():
        for relative, sha in hashes[role].items():
            path = _ordinary(safe_path(project, relative))
            _need(file_sha256(path) == sha, 'Producer source changed.')
            inventory[str(path)] = sha
    sources, engine = suite_revision.measurement_source_hashes('svg'), engine_identity()['engine_sha256']
    records = []
    case_map = {c['case_id']: c for c in cases}
    for key in sorted(rows):
        role, cid = key
        record = rows[key]
        expected = {'kind': 'web', 'sources': hashes[role]}
        initial._qualified(record, case_map[cid], suite, expected, sources=sources)
        capture, worker = origins[key]
        proof = _capture(record, capture, hashes[role], 'reference' if role == 'reference' else 'candidate' if role == 'unchanged' else 'before', inventory)
        records.append({'role': role, 'case_id': cid, 'source_sha256': digest(hashes[role]),
            'observations_sha256': digest(evidence.observations(record)), 'events_sha256': digest(evidence.event_set(record)),
            'worker_proof_sha256': _worker(record, worker, inventory, sources, engine), **proof})
    numeric = _numerics(directory, plan, result, cases, rows)
    functional = suite_svg_functional.validate_review(functional_execution, functional_witness,
        directory=directory, source_sha256=plan['unchanged_source_sha256'], initial_result_sha256=digest(result),
        browser_versions=[record['browser_version'] for record in records])
    inventory.update(functional['input_files'])
    visual = _visual(visual_receipt, directory, plan, rows)
    _file(visual_receipt, inventory)
    for path, sha in plan['historical_generation']['files'].items():
        _need(file_sha256(_ordinary(path)) == sha, 'Historical generation binding changed.')
        inventory[str(_ordinary(path))] = sha
    _need(sources == suite_revision.measurement_source_hashes('svg') and engine == engine_identity()['engine_sha256'],
          'Measurement code or engine changed during closure replay.')
    _need(all(file_sha256(_ordinary(p)) == sha for p, sha in inventory.items()),
          'An evidence file changed during closure replay.')
    return {'kind': 'UnchangedSVGAcceptanceVerification', 'schema_version': VERSION,
        'initial_acceptance_directory': str(directory), 'initial_result_sha256': digest(result),
        'plan_sha256': digest(plan), 'suite_sha256': digest(suite), 'unchanged_source_sha256': plan['unchanged_source_sha256'],
        'original_source_hashes': plan['original_source_hashes'], 'authoring_parent_source_hashes': plan['authoring_parent_source_hashes'],
        'historical_generation': deepcopy(plan['historical_generation']), 'numeric': numeric, 'functional': functional,
        'visual': visual, 'records': records, 'record_count': 112, 'measurement_source_hashes': sources,
        'source_projects': {role: str(_ordinary(project)) for role, project in projects.items()},
        'engine_sha256': engine, 'input_files': inventory, 'closure_sources': _code_sources(),
        'new_model_generation': False, 'model_started': False, 'thresholds_changed': False}


def build_origin_bindings(directory, output):
    """Resolve original receipts only within named, bound preparation epochs."""
    output = _ordinary(output)
    _need(not output.exists(), 'Keep earlier origin bindings unchanged.', 'output_exists')
    directory, plan, suite, result, cases, rows = _context(directory)
    inventory = {}
    allowed = _origins(plan, suite, cases, inventory)
    sources, engine = suite_revision.measurement_source_hashes('svg'), engine_identity()['engine_sha256']
    bindings = []
    for key in sorted(rows):
        role, cid = key
        record = rows[key]
        capture_choices = allowed[key]['capture']
        worker_choices = allowed[key]['worker']
        if role == 'unchanged':
            capture_choices = {directory / 'records/unchanged' / cid}
            worker_choices = {directory / 'records/unchanged' / cid / 'measurement'}
        package_sha = load_package(record['package'], verify='hashes')['package_sha256']
        captures = [p for p in sorted(capture_choices) if (p / 'package/manifest.json').is_file()
                    and load_package(p / 'package', verify='hashes')['package_sha256'] == package_sha]
        workers = []
        for path in sorted(worker_choices):
            if not (path / 'analysis-operation.json').is_file():
                continue
            try:
                _worker(record, path, {}, sources, engine)
            except (VisualError, OSError, ValueError, KeyError, TypeError):
                continue
            workers.append(path)
        _need(len(captures) == len(workers) == 1,
              'A role/case needs one exact retained native producer and direct worker: ' + '/'.join(key))
        bindings.append({'role': role, 'case_id': cid, 'capture_directory': str(captures[0]), 'worker_directory': str(workers[0])})
    value = {'kind': 'UnchangedSVGCaptureOrigins', 'schema_version': VERSION,
             'initial_acceptance_directory': str(directory), 'records': bindings,
             'source_inspection_inferred': False, 'full_behavior_acceptance': False}
    _bindings(value, allowed, directory, cases)
    protected = [directory, _ordinary(plan['suite']), _ordinary(plan['authoring_parent'])]
    _need(not any(output.is_relative_to(p) for p in protected),
          'Origin bindings must stay outside immutable source/initial/suite trees.')
    write_json(output, value)
    return value


def build_historical_bindings(directory, output, *, source_review):
    """Record replayable old evidence identities, never a source attestation."""
    output = _ordinary(output)
    _need(not output.exists(), 'Preserve earlier historical evidence declarations.', 'output_exists')
    directory, plan, suite, result, cases, rows = _context(directory)
    generation = plan['historical_generation']
    root = _ordinary(generation['historical_session'])
    actual = review.audit_native_run(root)
    launch = read_json(root / 'kiro/launch.json')
    value = {'kind': 'HistoricalSVGGenerationEvidenceBindings', 'schema_version': VERSION,
        'historical_session': str(root), 'historical_candidate_id': generation['historical_candidate_id'],
        'files': actual['evidence_hashes'], 'recorded_model_identity': {'model': actual['model'], 'effort': actual['effort'],
            'native_session_id': actual['native_session_id'], 'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version')},
        'source_review': str(_ordinary(source_review)), 'source_review_sha256': file_sha256(source_review),
        'new_model_generation': False, 'source_inspection_inferred': False, 'full_behavior_acceptance': False}
    _historical_proof(value, {'historical_generation': generation})
    _need(not output.is_relative_to(directory) and not output.is_relative_to(root),
          'Historical declaration must remain outside original evidence.')
    write_json(output, value)
    return value


def _axes(inspected):
    axes = {scope: {'status': 'pass'} for scope in ('numerical_behavior', 'functional_integrity', 'relationships',
        'final_raw_visual', 'native_capture_replay', 'direct_worker_replay', 'unchanged_source', 'independent_source_inspection')}
    axes['historical_model_generation'] = {'status': 'pass', 'scope': inspected['historical_generation_review']['validation_scope'],
        'model_identity': deepcopy(inspected['historical_generation_review']['model_identity']), 'historical_only': True}
    axes['fresh_model_generation'] = {'status': 'not_applicable', 'new_model_generation': False}
    return axes


def prepare_source_review(directory, visual_receipt, origins_file, output, *, historical_evidence,
                          functional_execution, functional_witness):
    """Write an unsigned request only. Never infer a human inspection."""
    output = _ordinary(output)
    _need(not output.exists(), 'Preserve earlier source-review requests.', 'output_exists')
    verification = _verification(directory, visual_receipt, origins_file,
        functional_execution=functional_execution, functional_witness=functional_witness)
    historical = read_json(historical_evidence)
    historical_proof = _historical_proof(historical, verification)
    protected = [_ordinary(directory), _ordinary(verification['visual']['raw_review_directory'])]
    protected += [_ordinary(p) for p in verification['source_projects'].values()]
    _need(not any(output.is_relative_to(p) for p in protected), 'Source-review request must stay outside immutable source/evidence.')
    request = {'kind': 'UnsignedUnchangedSVGSourceInspectionRequest', 'schema_version': VERSION,
        'status': 'pending_independent_inspection', 'created_at': time.time(),
        'verification': verification, 'verification_sha256': digest(verification),
        'historical_evidence': historical, 'historical_evidence_sha256': digest(historical),
        'historical_evidence_files': historical_proof['files'], 'historical_native_validation': historical_proof,
        'required_checks': sorted(SOURCE_CHECKS),
        'source_inspection_inferred': False, 'new_model_generation': False, 'full_behavior_acceptance': False}
    write_json(output, request)
    return request


def _source_receipt(path, request_path, verification):
    request = read_json(request_path)
    _need(request.get('kind') == 'UnsignedUnchangedSVGSourceInspectionRequest' and request.get('schema_version') == VERSION
          and request.get('status') == 'pending_independent_inspection'
          and request.get('verification') == verification and request.get('verification_sha256') == digest(verification)
          and request.get('source_inspection_inferred') is False and request.get('new_model_generation') is False
          and request.get('full_behavior_acceptance') is False and request.get('required_checks') == sorted(SOURCE_CHECKS),
          'Source-review request is stale, foreign, or claims its own inspection.')
    historical = request['historical_evidence']
    _need(request.get('historical_evidence_sha256') == digest(historical), 'Historical evidence declaration changed.')
    historical_proof = _historical_proof(historical, verification)
    _need(request.get('historical_native_validation') == historical_proof
          and request.get('historical_evidence_files') == historical_proof['files'],
          'Historical native proof is stale or substituted.')
    for name, sha in request['historical_evidence_files'].items():
        _need(file_sha256(_ordinary(name)) == sha, 'Inspected historical generation bytes changed.')
    receipt = read_json(path)
    checks = receipt.get('checks', [])
    _need(receipt.get('kind') == 'IndependentUnchangedSVGSourceReview' and receipt.get('schema_version') == VERSION
          and receipt.get('status') == 'pass' and receipt.get('method') == 'independent_source_and_historical_native_inspection'
          and isinstance(receipt.get('reviewer'), str) and bool(receipt['reviewer'].strip())
          and receipt.get('reviewer_role') == 'independent_source_reviewer' and receipt.get('authored_source') is False
          and receipt.get('inspection_performed') is True and receipt.get('accepted') is True
          and receipt.get('request_sha256') == digest(request)
          and receipt.get('verification_sha256') == digest(verification)
          and receipt.get('unchanged_source_sha256') == verification['unchanged_source_sha256']
          and receipt.get('initial_acceptance_directory') == verification['initial_acceptance_directory']
          and receipt.get('historical_evidence_sha256') == digest(historical)
          and receipt.get('new_model_generation') is False and receipt.get('historical_generation_only') is True,
          'An actual named independent source inspection is absent, stale, or foreign.')
    _need(type(receipt.get('inspected_at')) in (int, float) and math.isfinite(receipt['inspected_at'])
          and request['created_at'] <= receipt['inspected_at'] <= time.time(), 'Source-inspection chronology is missing or invalid.')
    _need(len(checks) == len(SOURCE_CHECKS) and {c.get('id') for c in checks} == SOURCE_CHECKS
          and all(c.get('verdict') == 'pass' and isinstance(c.get('evidence'), str) and c['evidence'].strip() for c in checks),
          'All source/authorship/protection inspection checks must pass with evidence.')
    inspected = receipt.get('inspected_files', {})
    required = verification['input_files'] | request['historical_evidence_files']
    _need(all(file_sha256(_ordinary(p)) == sha for p, sha in required.items()),
          'Source/native/worker evidence changed after independent inspection.')
    _need(isinstance(inspected, dict) and all(inspected.get(p) == sha for p, sha in required.items()),
          'Source reviewer did not bind every required source/native/worker/historical evidence file.')
    historical_review = receipt.get('historical_generation_review', {})
    _need(historical_review.get('status') == 'pass' and historical_review.get('generation_recorded') is True
          and historical_review.get('model_identity') == historical['recorded_model_identity']
          and isinstance(historical_review.get('validation_scope'), str) and historical_review['validation_scope'].strip()
          and isinstance(historical_review.get('evidence'), str) and historical_review['evidence'].strip()
          and historical_review.get('new_model_generation') is False,
          'Historical authorship/model evidence remains missing or unverified.')
    return receipt


def close_acceptance(directory, visual_receipt, origins_file, source_request, source_receipt, output):
    """Join mandatory evidence in a new standalone receipt; never edit inputs."""
    output = _ordinary(output)
    _need(not output.exists(), 'Keep earlier acceptance closures unchanged.', 'output_exists')
    request = read_json(source_request)
    functional = request.get('verification', {}).get('functional', {})
    verification = _verification(directory, visual_receipt, origins_file,
        functional_execution=functional.get('execution_directory'), functional_witness=functional.get('witness'))
    inspected = _source_receipt(source_receipt, source_request, verification)
    protected = [_ordinary(directory), _ordinary(verification['visual']['raw_review_directory']),
                 _ordinary(initial.load_plan(directory)[0]['suite']),
                 _ordinary(initial.load_plan(directory)[0]['authoring_parent'])]
    protected += [_ordinary(p) for p in verification['source_projects'].values()]
    _need(not any(output.is_relative_to(p) or p.is_relative_to(output) for p in protected),
          'Closure output must remain outside immutable source/evidence trees.', 'unsafe_path')
    axes = _axes(inspected)
    receipt = {'kind': 'UnchangedSVGFinalAcceptance', 'schema_version': VERSION, 'status': 'pass',
        'created_at': time.time(), 'initial_acceptance_directory': verification['initial_acceptance_directory'],
        'verification': verification, 'verification_sha256': digest(verification), 'axes': axes,
        'source_request': str(_ordinary(source_request)), 'source_request_sha256': file_sha256(source_request),
        'source_receipt': str(_ordinary(source_receipt)), 'source_receipt_sha256': file_sha256(source_receipt),
        'visual_receipt': str(_ordinary(visual_receipt)), 'origins_file': str(_ordinary(origins_file)),
        'record_count': 112, 'full_behavior_acceptance': True, 'new_model_generation': False,
        'opus_run_claimed': False, 'candidate_count': 0, 'thresholds_changed': False,
        'scope': 'Unchanged historical SVG source on all 28 registered cases; historical model identity remains historical.'}
    output.mkdir(parents=True)
    write_json(output / 'acceptance.json', receipt)
    document = '<!doctype html><meta charset="utf-8"><title>Unchanged SVG acceptance</title>' \
        '<h1>Unchanged SVG acceptance</h1><p>All 28 registered cases / 112 source-role records passed the mandatory closure.</p>' \
        '<p>No new model generation or Opus run is claimed.</p><pre>' + html.escape(json.dumps(axes, indent=2)) + '</pre>'
    (output / 'report.html').write_text(document, encoding='utf-8')
    write_json(output / 'acceptance-binding.json', {'kind': 'UnchangedSVGFinalAcceptanceBinding', 'schema_version': VERSION,
        'acceptance_sha256': digest(receipt), 'files': source_hashes(output)})
    return receipt


def validate_acceptance(output):
    """Replay all original gates; a stored success is never sufficient."""
    output = _ordinary(output)
    value, binding = read_json(output / 'acceptance.json'), read_json(output / 'acceptance-binding.json')
    files = source_hashes(output)
    files.pop('acceptance-binding.json', None)
    _need(binding.get('kind') == 'UnchangedSVGFinalAcceptanceBinding' and binding.get('schema_version') == VERSION
          and binding.get('acceptance_sha256') == digest(value) and binding.get('files') == files
          and value.get('kind') == 'UnchangedSVGFinalAcceptance' and value.get('schema_version') == VERSION
          and value.get('status') == 'pass' and value.get('full_behavior_acceptance') is True
          and value.get('new_model_generation') is False and value.get('opus_run_claimed') is False
          and value.get('candidate_count') == 0 and value.get('record_count') == 112,
          'Acceptance closure or its scoped verdict changed.')
    _need(file_sha256(value['source_request']) == value['source_request_sha256']
          and file_sha256(value['source_receipt']) == value['source_receipt_sha256'], 'Source inspection changed.')
    functional = value.get('verification', {}).get('functional', {})
    current = _verification(value['initial_acceptance_directory'], value['visual_receipt'], value['origins_file'],
        functional_execution=functional.get('execution_directory'), functional_witness=functional.get('witness'))
    _need(current == value['verification'] and digest(current) == value['verification_sha256'], 'Accepted evidence is stale.')
    inspected = _source_receipt(value['source_receipt'], value['source_request'], current)
    _need(value.get('axes') == _axes(inspected) and value.get('thresholds_changed') is False,
          'Scoped acceptance axes differ from actual inspection/replay.')
    return value
