"""Final-only independent game/SVG raw measurement review.

Final selected sources and unchanged historical SVG sources have different
seal types. This aid checks measured pixels; it neither launches a model nor
grants behavior/source-code acceptance. An actual sheet inspection remains a
separate, explicit attestation. Premodel admission is untouched.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
from pathlib import Path

import numpy as np

from kiroclops.visual.package import frame_paths, load_package, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from . import evidence, media_suite, suite_evidence, suite_generic_raw_review as raw
from .session import Session, _ordinary, source_hashes

VERSION = 'final-game-svg-raw-review-v1'
FINAL_ROLES = ('reference', 'original', 'before', 'selected')
UNCHANGED_ROLES = ('reference', 'original', 'before', 'unchanged')


def _need(value, message, code='incomplete_review'):
    if not value:
        raise VisualError(code, message)


def _code_sources():
    # Bind the complete local review/orchestration closure, including helpers
    # used to check the seal. No source-code inspection is inferred from this.
    return raw._code_sources() | {str(path): file_sha256(path)
        for path in Path(__file__).parent.glob('*.py')}


def _file(path, inventory):
    path = _ordinary(path)
    inventory[str(path)] = file_sha256(path)


def _source(project, hashes, inventory):
    project = _ordinary(project)
    _need(source_hashes(project) == hashes, 'Immutable producer source changed.', 'source_changed')
    for name, expected in hashes.items():
        path = safe_path(project, name)
        _need(file_sha256(path) == expected, 'Producer bytes changed during review.', 'source_changed')
        inventory[str(path)] = expected


def _label(role, case_id, index):
    return f'{role}-{index:03}-{digest(case_id)[:10]}'


def _row(record, case, role, index, *, source=None, project=None, capture_role=None):
    value = {'id': _label(role, case['case_id'], index), 'case_id': case['case_id'],
        'stage': case['stage'], 'split_role': case['role'], 'purpose': case.get('purpose', 'primary'),
        'domain': 'game' if case['stage'] in ('jump', 'ground') else 'svg', 'role': role,
        'capture_role': capture_role or role, 'reference': role == 'reference',
        'object_map': deepcopy(case.get('object_map') or record.get('intake_binding', {}).get('object_map') or {}),
        'record': deepcopy(record)}
    if source is not None:
        value.update(expected_source=deepcopy(source), source_project=str(_ordinary(project)))
    return value


def _context_session(directory):
    directory = _ordinary(directory)
    saved = Session(directory)
    state = saved._load()
    _need(state.get('domain') in ('game', 'svg') and state.get('experiment', {}).get('reference_mode') == 'media_suite',
          'Final raw review supports a reference-suite game or SVG Session.')
    suite_directory = _ordinary(state['reference'])
    suite = media_suite.load_suite(suite_directory)
    selected, layout = suite_evidence._matrix(suite, state)
    _need(selected is not None and selected.get('eligible') is True and selected.get('status') == 'succeeded'
          and state.get('sealed_at') is not None and (state.get('final') or {}).get('status') == 'succeeded',
          'A completed immutable selected source and sealed final measurement are required.', 'source_not_sealed')
    sealed = directory / 'candidates' / media_suite._name(selected['candidate_id']) / 'source'
    hashes = source_hashes(sealed)
    _need(digest(hashes) == selected['source_sha256'] and digest(suite) == state['experiment']['suite_sha256'],
          'Selected source or canonical suite changed.', 'source_changed')
    original = suite_directory / suite['original']
    sources = {'original': suite_evidence._source_expectation(original, suite['original_source_hashes'], suite['domain']),
        'before': suite_evidence._source_expectation(state['baseline'], suite['authoring_parent_source_hashes'], suite['domain']),
        'selected': suite_evidence._source_expectation(sealed, hashes, suite['domain'])}
    projects = {'original': original, 'before': _ordinary(state['baseline']), 'selected': sealed}
    rows = []
    for job_id, capture_role, cases in layout:
        role = 'selected' if capture_role in ('candidate', 'final') else capture_role
        for record, case in suite_evidence._records(directory / 'measurements' / job_id / (capture_role + '.json'), cases, complete=True):
            rows.append(_row(record, case, role, len(rows), source=sources.get(role), project=projects.get(role), capture_role=capture_role))
    boundary_receipt = None
    if suite['domain'] == 'game':
        boundary_receipt = directory / 'measurements' / state['final_job_id'] / 'visual-boundary.json'
        boundary = read_json(boundary_receipt)
        from .visual_boundary_v4 import cases as boundary_cases
        expected = boundary_cases()
        actual = read_json(boundary['records_path'])
        _need(len(actual) == len(expected) == 8 and [r['scenario'] for r in actual] == expected,
              'Every fixed wall case must remain in the final review matrix.')
        for record in actual:
            case = {'case_id': record['scenario']['id'], 'stage': 'boundary', 'role': 'regression', 'purpose': 'preservation'}
            row = _row(record, case, 'boundary_selected', len(rows), source=sources['selected'], project=sealed, capture_role='boundary')
            row['domain'] = 'game'
            row['boundary'] = True
            # Boundary workers retain their exact native projection identity.
            # The final view separately names the full sealed source; neither
            # original record nor worker receipt is rewritten.
            row['full_sealed_source_sha256'] = selected['source_sha256']
            rows.append(row)
    context = {'kind': 'SealedSessionFinalRawMatrix', 'version': VERSION, 'directory': str(directory),
        'suite': str(suite_directory), 'suite_sha256': digest(suite), 'domain': suite['domain'], 'stage': suite['stage'],
        'sealed_source': str(sealed), 'sealed_source_hashes': hashes, 'sealed_source_sha256': digest(hashes),
        'selected_candidate_id': selected['candidate_id'], 'sealed_at': state['sealed_at'],
        'authoring_parent': str(projects['before']), 'authoring_parent_source_hashes': suite['authoring_parent_source_hashes'],
        'original': str(original), 'original_source_hashes': suite['original_source_hashes'],
        'event_contract': suite.get('event_contract'), 'cases': deepcopy(suite['cases']),
        'expected_roles': list(FINAL_ROLES), 'boundary_receipt': str(boundary_receipt) if boundary_receipt else None,
        'behavior_numerical_pass': (state.get('final') or {}).get('pass'), 'heldout_status': suite['heldout_status'],
        'new_model_generation_claimed_by_aid': False}
    return context, rows


def _context_unchanged(directory):
    from . import suite_initial_acceptance as initial
    directory = _ordinary(directory)
    result = initial.load_result(directory)
    plan, suite = initial.load_plan(directory)
    _need(result.get('complete') is True, 'An incomplete initial SVG measurement cannot establish a final raw matrix.')
    cases = {case['case_id']: case for case in suite['cases']}
    projects = {'original': _ordinary(plan['suite']) / suite['original'],
                'before': _ordinary(plan['authoring_parent']), 'unchanged': _ordinary(plan['unchanged_source'])}
    sources = {role: suite_evidence._source_expectation(project,
        plan['original_source_hashes'] if role == 'original' else plan['authoring_parent_source_hashes'], 'svg')
        for role, project in projects.items()}
    rows = []
    for role in UNCHANGED_ROLES:
        for record in read_json(directory / 'indexes' / (role + '.json')):
            rows.append(_row(record, cases[record['case_id']], role, len(rows), source=sources.get(role), project=projects.get(role)))
    context = {'kind': 'UnchangedHistoricalSVGFinalRawMatrix', 'version': VERSION, 'directory': str(directory),
        'suite': plan['suite'], 'suite_sha256': plan['suite_sha256'], 'domain': 'svg', 'stage': 'svg',
        'sealed_source': plan['unchanged_source'], 'sealed_source_hashes': plan['authoring_parent_source_hashes'],
        'sealed_source_sha256': plan['unchanged_source_sha256'], 'selected_candidate_id': None,
        'authoring_parent': plan['authoring_parent'], 'authoring_parent_source_hashes': plan['authoring_parent_source_hashes'],
        'original': str(projects['original']), 'original_source_hashes': plan['original_source_hashes'],
        'event_contract': suite.get('event_contract'), 'cases': deepcopy(suite['cases']),
        'expected_roles': list(UNCHANGED_ROLES), 'boundary_receipt': None,
        'behavior_numerical_pass': result['numerical_pass'], 'initial_result_sha256': digest(result),
        'heldout_status': suite['heldout_status'],
        'initial_receipt_sha256': file_sha256(directory / 'result-binding.json'),
        'historical_generation': deepcopy(plan['historical_generation']), 'new_model_generation_claimed_by_aid': False}
    return context, rows


def _after_raw(context, rows, inventory):
    """Open and verify production/seal evidence after all image-only marking."""
    directory = _ordinary(context['directory'])
    if context['kind'] == 'SealedSessionFinalRawMatrix':
        from .review import audit_native_run
        state = Session(directory)._load()
        required = suite_evidence.required_bindings(directory, state, require_complete=True)
        native = audit_native_run(directory)
        _need(native.get('status') == 'pass', 'Actual completed native generation evidence is required.')
        for relative, sha in native['evidence_hashes'].items():
            path = safe_path(directory, relative)
            _need(file_sha256(path) == sha, 'Native generation evidence changed.', 'source_changed')
            inventory[str(path)] = sha
        for name in ('session.json', 'binding.json'):
            _file(directory / name, inventory)
        for job in {state['prepare_job_id'], state['final_job_id'], next(c['job_id'] for c in state['candidates'] if c['candidate_id'] == context['selected_candidate_id'])}:
            for name in ('job.json', 'binding.json', 'result.json'):
                _file(directory / 'jobs' / job / name, inventory)
        if context['boundary_receipt']:
            boundary = read_json(context['boundary_receipt'])
            _file(context['boundary_receipt'], inventory)
            native_root = _ordinary(boundary['native_root'])
            for name, sha in boundary['file_hashes'].items():
                path = safe_path(native_root, name)
                _need(file_sha256(path) == sha, 'Bound wall producer evidence changed.', 'source_changed')
                inventory[str(path)] = sha
    else:
        from . import suite_initial_acceptance as initial
        result = initial.load_result(directory)
        _need(digest(result) == context['initial_result_sha256'], 'Unchanged-source numerical result changed.')
        required = {(b['package_sha256'], b['observations_sha256']) for b in initial.required_observation_bindings(directory)}
        receipt = read_json(directory / 'result-binding.json')
        _file(directory / 'result-binding.json', inventory)
        for name, sha in receipt['file_hashes'].items():
            path = safe_path(directory, name)
            _need(file_sha256(path) == sha, 'Initial unchanged evidence changed.', 'source_changed')
            inventory[str(path)] = sha
        for path, sha in context['historical_generation']['files'].items():
            _need(file_sha256(_ordinary(path)) == sha, 'Historical generation link changed.', 'source_changed')
            inventory[str(_ordinary(path))] = sha
    actual = set()
    for row in rows:
        record = row['record']
        observed = read_json(record['observations']) if isinstance(record.get('observations'), str) else evidence.observations(record)
        actual.add((load_package(record['package'], verify='hashes')['package_sha256'], digest(observed)))
    _need(actual == required, 'Final raw matrix omits or replaces a required immutable observation.')
    for key in ('sealed_source', 'authoring_parent', 'original'):
        _source(context[key], context[key + '_hashes' if key == 'sealed_source' else key + '_source_hashes'], inventory)
    suite = media_suite.load_suite(context['suite'])
    _need(digest(suite) == context['suite_sha256'], 'Frozen final suite changed.', 'source_changed')
    for name in ('suite.json', 'suite-binding.json'):
        _file(_ordinary(context['suite']) / name, inventory)
    return {'required_observations': sorted(required), 'record_count': len(rows), 'source_binding_status': 'pass',
            'source_code_review_status': 'pending', 'native_generation_kind': context['kind']}


def _wall(image, side):
    """Independent column contrast runs; no Hough/template/produced boundary."""
    _need(side in ('left', 'right'), 'Declare the visible wall side; do not infer a physics contact.')
    values = image.astype(np.int16)
    contrast = np.max(np.abs(values[:, 1:] - values[:, :-1]), axis=2) >= 24
    height, width = image.shape[:2]
    candidates = []
    for x in range(contrast.shape[1]):
        if (side == 'right' and x + .5 <= width / 2) or (side == 'left' and x + .5 >= width / 2):
            continue
        ys = np.flatnonzero(contrast[:, x])
        if not len(ys):
            continue
        starts = np.r_[0, np.flatnonzero(np.diff(ys) > 1) + 1]
        ends = np.r_[starts[1:], len(ys)]
        for start, end in zip(starts, ends):
            if end - start >= 55:
                candidates.append((x + .5, int(ys[start]), int(ys[end - 1])))
    if not candidates:
        return {'status': 'unobservable', 'method': 'vertical_rgb_contrast_runs'}
    face = (min if side == 'right' else max)(value[0] for value in candidates)
    nearby = [value for value in candidates if abs(value[0] - face) <= 1]
    return {'status': 'unsigned', 'x': face, 'top': min(value[1] for value in nearby),
            'bottom': max(value[2] for value in nearby), 'method': 'vertical_rgb_contrast_runs'}


def _extract_boundary(bundle, output, inventory):
    """Add image-only wall marks before any source/measurement comparison."""
    record, manifest = bundle['record'], bundle['manifest']
    for row, path in zip(bundle['raw']['rows'], frame_paths(record['package'], manifest)):
        row['wall'] = _wall(read_image(path), record['scenario']['wall_side'])
        _need(file_sha256(path) == row['image_sha256'], 'Boundary source changed during raw marking.', 'source_changed')
        wall = row['wall']
        if 'x' in wall:
            row['connections']['independent_wall_edge'] = {'start': [wall['x'], wall['top']], 'end': [wall['x'], wall['bottom']]}
    write_json(output / 'raw' / record['id'] / 'points.json', {'rows': bundle['raw']['rows'],
        'package_sha256': manifest['package_sha256'], 'production_coordinates_used': False,
        'production_events_used': False, 'target_code_or_dom_read': False, 'status': 'unsigned'})


def _scope_boundary(bundle, inventory, contract):
    # The full boundary receipt/worker/frame bridge was checked by _after_raw.
    # Source identity retains both full sealed and native renderer projections.
    record, manifest = bundle['record'], bundle['manifest']
    observed, selection, events = (read_json(record[key]) if isinstance(record[key], str) else deepcopy(record[key])
                                    for key in ('observations', 'selection', 'events'))
    for key in ('analysis', 'observations', 'selection', 'events'):
        if isinstance(record.get(key), str):
            _file(record[key], inventory)
    _need(observed.get('package_sha256') == events.get('package_sha256') == manifest['package_sha256']
          and observed.get('inputs') == manifest['inputs'] and observed.get('image_size') == manifest['image_size']
          and events.get('observation_sha256') == digest(observed) and observed.get('selection') == selection,
          'Wall observations, selection, events or raw pixels differ.')
    _need(len(selection.get('objects', [])) == len(observed.get('objects', [])) == 1,
          'The fixed wall protocol has one declared character patch.')
    oid = selection['objects'][0]['id']
    mapping = {oid: 'character'}
    times = [frame['time_s'] for frame in manifest['frames']]
    active = {oid: raw.common._active(selection, oid, times)}
    rows = [{**row, 'points': {oid: deepcopy(row['points']['character'])}} for row in bundle['raw']['rows']]
    _need(all(len(obj['frames']) == len(times) and all(f['index'] == i and f['time_s'] == times[i]
          for i, f in enumerate(obj['frames'])) for obj in observed['objects']), 'Wall produced frame identity differs.')
    options = events.get('options', {})
    failures = []
    if events.get('motion_start_policy', {}).get('version') != contract['motion_policy'] or any(
            options.get(key) != contract[key] for key in ('rest_duration_s', 'rest_radius_px', 'max_gap_s', 'max_event_uncertainty_s')):
        failures.append({'code': 'event_definition_mismatch', 'record_id': record['id']})
    _need(options.get('boundary_tolerance_px') == 12., 'Preserve the existing 12px visible-wall policy.')
    bundle.update(observations=observed, event_set=events, mapping=mapping, rows=rows, active=active, selection=selection)
    return failures


def _boundary_events(bundle, contract):
    """Independently derive the existing pixel-distance relation and labels."""
    record, selection, rows = bundle['record'], bundle['selection'], bundle['rows']
    oid = next(iter(bundle['mapping']))
    item = selection['objects'][0]
    roi, offset = item.get('roi'), item.get('anchor')
    if not isinstance(roi, list) or len(roi) != 4 or not isinstance(offset, list) or len(offset) != 2:
        return {}, [{'code': 'raw_boundary_patch_geometry_unknown', 'record_id': record['id']}]
    width, height = roi[2:]
    _need(all(type(v) in (int, float) and math.isfinite(v) for v in (*offset, width, height)) and width > 0 and height > 0,
          'A finite declared visible patch and physical anchor are required.')
    relations, events, failures = [], [], []
    previous, previous_side = None, None
    for row in rows:
        point, wall = row['points'][oid], row.get('wall', {})
        if 'x' not in point or 'x' not in wall:
            failures.append({'code': 'raw_boundary_unobservable', 'record_id': record['id'], 'index': row['index']})
            previous = previous_side = None
            continue
        left, top = point['x'] - offset[0], point['y'] - offset[1]
        dx = max(left - wall['x'], wall['x'] - (left + width), 0.)
        dy = max(top - wall['bottom'], wall['top'] - (top + height), 0.)
        distance = math.hypot(dx, dy)
        signed = left + width / 2 - wall['x']
        touching = distance <= 12.
        relation = {'type': 'boundary_distance', 'object_id': oid, 'boundary_id': 'wall', 'index': row['index'],
            'time_s': row['time_s'], 'distance_px': distance, 'signed_center_distance_px': signed, 'visually_touching': touching}
        relations.append(relation)
        def transition(kind, earlier):
            frames = bundle['manifest']['frames']
            before, after = frames[earlier['index']], frames[row['index']]
            interval = [before.get('time_interval_s', [before['time_s'], before['time_s']])[0],
                        after.get('time_interval_s', [after['time_s'], after['time_s']])[1]]
            events.append({'type': kind, 'object_ids': [oid], 'boundary_id': 'wall',
                'indices': [earlier['index'], row['index']], 'observed_interval_s': interval,
                'uncertainty_s': interval[1] - interval[0], 'timing_status': 'resolved' if interval[1] - interval[0] <= .1 else 'unresolved'})
        if previous is not None and previous['index'] + 1 == row['index']:
            if previous['visually_touching'] != touching:
                transition('boundary_enter' if touching else 'boundary_leave', previous)
            if abs(signed) > 12.:
                if previous_side is not None and previous_side['signed_center_distance_px'] * signed < 0 \
                        and (distance <= max(width, height) or previous['visually_touching']):
                    transition('boundary_cross', previous_side)
                previous_side = relation
        elif abs(signed) > 12.:
            previous_side = relation
        previous = relation
    lookup = {relation['index']: relation for relation in relations}
    relation_checks = []
    for produced in bundle['event_set'].get('relations', []):
        if produced['type'] != 'boundary_distance':
            continue
        actual = lookup.get(produced['index'])
        passed = actual is not None and produced.get('object_id') == oid and produced.get('boundary_id') == 'wall'
        passed = passed and all(abs(actual[field] - produced[field]) <= 2. + 1e-9
            for field in ('distance_px', 'signed_center_distance_px')) and actual['visually_touching'] == produced['visually_touching']
        relation_checks.append({'index': produced['index'], 'raw': actual, 'pass': bool(passed)})
        if not passed:
            failures.append({'code': 'raw_boundary_distance_disagreement', 'record_id': record['id'], 'index': produced['index']})
    if sorted(check['index'] for check in relation_checks) != sorted(row['index'] for row in rows):
        failures.append({'code': 'raw_boundary_required_relation_missing', 'record_id': record['id'],
                         'required': len(rows), 'provided': len(relation_checks)})
    pools, produced_pools = defaultdict(list), defaultdict(list)
    for event in events:
        pools[raw._event_key(event)].append(event)
    for event in bundle['event_set'].get('events', []):
        if event['type'].startswith('boundary_'):
            produced_pools[raw._event_key(event)].append(event)
    checks = []
    for key in sorted(set(pools) | set(produced_pools), key=repr):
        expected, actual = produced_pools[key], pools[key]
        if len(expected) != len(actual):
            failures.append({'code': 'raw_boundary_event_occurrence_mismatch', 'record_id': record['id'], 'type': key[0]})
        for produced, detected in zip(expected, actual):
            interval = raw.common._interval(produced['time_interval_s'], 'wall event interval')
            error = max(abs(a - b) for a, b in zip(interval, detected['observed_interval_s']))
            uncertainty = max(produced.get('uncertainty_s', float('inf')), detected['uncertainty_s'], interval[1] - interval[0])
            passed = error <= .05 + 1e-9 and uncertainty <= .1 + 1e-9 \
                and produced.get('timing_status') == detected['timing_status'] == 'resolved'
            checks.append({'type': key[0], 'difference_s': error, 'uncertainty_s': uncertainty, 'pass': passed})
            if not passed:
                failures.append({'code': 'raw_boundary_event_interval_disagreement', 'record_id': record['id'], 'type': key[0]})
    # The classification uses the same published six-frame/6px/12px/5px
    # definition, applied here to independent raw points and contrast edges.
    measured = [(row, lookup[row['index']]['distance_px']) for row in rows if row['index'] >= 30 and row['index'] in lookup]
    approach, parallel, departure = [], [], []
    for start in range(len(measured) - 5):
        section = measured[start:start + 6]
        if any(b[0]['index'] != a[0]['index'] + 1 for a, b in zip(section, section[1:])):
            continue
        (first, a), (last, b) = section[0], section[-1]
        p, q = first['points'][oid], last['points'][oid]
        if a - b >= 6 and b <= 12:
            approach.append([first['index'], last['index']])
        if max(value for _, value in section) <= 12 and abs(q['x'] - p['x']) <= 2 and abs(q['y'] - p['y']) >= 5:
            parallel.append([first['index'], last['index']])
        if a <= 12 and b - a >= 5:
            departure.append([first['index'], last['index']])
    label = 'visible_departure_after_approach' if approach and departure and departure[-1][0] > approach[0][0] \
        else 'visible_parallel_motion' if parallel else 'visible_approach' if approach \
        else 'distant_motion' if not any(d <= 12 for _, d in measured) else 'visible_proximity_only'
    produced = read_json(record['analysis'])
    if len(measured) < 20 or produced.get('status') != 'observed' or produced.get('classification') != label:
        failures.append({'code': 'raw_boundary_classification_disagreement', 'record_id': record['id']})
    return {'record_id': record['id'], 'raw_events': events, 'relation_checks': relation_checks, 'event_checks': checks,
        'classification': label, 'approach_windows': approach, 'parallel_windows': parallel, 'departure_windows': departure,
        'physics_collision_inferred': False}, failures


def _review(context, rows, output):
    output = _ordinary(output)
    _need(not output.exists(), 'Preserve every earlier raw review.', 'output_exists')
    _need(context['domain'] in ('game', 'svg'), 'Final raw review supports game and SVG only.')
    normalized = [raw.common._record(row, index) for index, row in enumerate(rows)]
    _need(len({row['id'] for row in normalized}) == len(normalized), 'Duplicate raw recording identity.')
    expected = {(role, case['case_id']) for case in context['cases'] for role in context['expected_roles']}
    actual = {(row['role'], row['case_id']) for row in normalized if not row.get('boundary')}
    _need(actual == expected and sum(not row.get('boundary') for row in normalized) == len(expected),
          'Review the complete canonical case/producer matrix, including calibration, regression and validation.')
    boundaries = [row for row in normalized if row.get('boundary')]
    _need((len(boundaries) == 8 if context['domain'] == 'game' else not boundaries), 'Do not omit the sealed game wall matrix.')
    for row in normalized:
        _need(not output.is_relative_to(row['package']) and not row['package'].is_relative_to(output), 'Raw aid output overlaps its raw input.', 'unsafe_path')
    for name in ('suite', 'sealed_source', 'authoring_parent', 'original'):
        _need(not output.is_relative_to(_ordinary(context[name])), 'Raw aid cannot write into a frozen source tree.', 'unsafe_path')
    contract = raw._public_contract(context.get('event_contract'), context['domain'])
    code_sources, inventory = _code_sources(), {}
    output.mkdir(parents=True)
    write_json(output / 'matrix.json', context)
    bundles = []
    for row in normalized:
        bundle = raw._extract(row, output, inventory)
        if row.get('boundary'):
            _extract_boundary(bundle, output, inventory)
        bundles.append(bundle)
    write_json(output / 'extraction-complete.json', {'all_raw_images_marked_before_production_comparison': True,
        'production_coordinates_or_events_used': False,
        'raw_point_files': {b['record']['id']: file_sha256(output / 'raw' / b['record']['id'] / 'points.json') for b in bundles}})
    producer_binding = _after_raw(context, rows, inventory)
    failures, bindings, event_results, jump_results, boundary_results = [], [], [], [], []
    mapping_by_id, expected_objects = {}, {}
    for bundle in bundles:
        if bundle['record'].get('boundary'):
            failures.extend(_scope_boundary(bundle, inventory, contract))
        else:
            failures.extend(raw._scope(bundle, inventory, contract))
        name = bundle['record']['id']
        for oid, glyph in bundle['mapping'].items():
            _need(oid not in mapping_by_id or mapping_by_id[oid] == glyph, 'An object ID changed raw glyph identity across final sources.')
            mapping_by_id[oid] = glyph
        if bundle['record']['role'] == 'reference':
            expected_objects[bundle['record']['case_id']] = set(bundle['mapping'])
        write_json(output / 'measurements' / name / 'measurement.json', {'observations': bundle['observations'], 'events': bundle['event_set']})
        if bundle['record'].get('boundary'):
            view = {**bundle, 'event_set': {**bundle['event_set'],
                'events': [e for e in bundle['event_set']['events'] if not e['type'].startswith('boundary_')],
                'relations': [r for r in bundle['event_set']['relations'] if r['type'] != 'boundary_distance']}}
            compared, errors = raw._compare_events(view, contract)
            boundary, boundary_errors = _boundary_events(bundle, contract)
            boundary_results.append(boundary)
            compared['raw_events'].extend(boundary.get('raw_events', []))
            failures.extend(boundary_errors)
            jump = None
        else:
            compared, errors = raw._compare_events(bundle, contract)
            jump, jump_errors = raw._jump(bundle)
            failures.extend(jump_errors)
        event_results.append(compared)
        jump_results.append(jump)
        failures.extend(errors)
        bindings.append({'record_id': name, 'case_id': bundle['record']['case_id'], 'role': bundle['record']['role'],
            'capture_role': bundle['record']['capture_role'], 'stage': bundle['record']['stage'],
            'package_sha256': bundle['manifest']['package_sha256'], 'observations_sha256': digest(bundle['observations']),
            'events_sha256': digest(bundle['event_set']), 'source_sha256': bundle['record'].get('source_sha256'),
            'full_sealed_source_sha256': bundle['record'].get('full_sealed_source_sha256'), 'object_map': bundle['mapping'],
            'public_active_intervals_s': bundle['active']})
    for bundle in bundles:
        if not bundle['record'].get('boundary') and set(bundle['mapping']) != expected_objects.get(bundle['record']['case_id']):
            failures.append({'code': 'case_object_set_mismatch', 'record_id': bundle['record']['id']})
    # Object sets vary between SVG cases; every role must still contain every
    # object declared for its own exact reference case. No numeric gate changes.
    positions, errors = raw._positions(bundles)
    failures.extend(errors)
    sheets, contact = raw._sheets(bundles, event_results, jump_results, output)
    _need(code_sources == _code_sources(), 'Raw review sources changed during marking.', 'source_changed')
    for path, sha in inventory.items():
        _need(file_sha256(_ordinary(path)) == sha, 'Bound raw or producer evidence changed during review.', 'source_changed')
    result = {'kind': 'FinalOnlyUnsignedGameSVGReviewAid', 'schema_version': 1, 'version': VERSION,
        'status': 'pending_visual_review', 'numeric_status': 'pass' if not failures else 'fail',
        'domain': context['domain'], 'seal_kind': context['kind'], 'matrix_sha256': digest(context),
        'suite_sha256': context['suite_sha256'], 'sealed_source_sha256': context['sealed_source_sha256'],
        'selected_candidate_id': context.get('selected_candidate_id'), 'records': bindings,
        'positions': positions, 'events': event_results, 'jumps': jump_results, 'boundaries': boundary_results,
        'public_event_contract': contract, 'contact_policy': 'control_and_event_v1',
        'contact_selection_sha256': digest(contact), 'sheets': sheets, 'unsupported': [], 'failures': failures,
        'producer_binding': producer_binding, 'behavior_numerical_pass': context.get('behavior_numerical_pass'),
        'heldout_status': context.get('heldout_status', 'not_provided'),
        'source_code_review_status': 'pending', 'acceptance_granted': False, 'full_behavior_acceptance': False,
        'visual_inspection_claimed': False, 'production_coordinates_used_for_extraction': False,
        'production_events_used_for_contact_selection': False, 'new_model_generation_claimed_by_aid': False,
        'historical_acceptance_changed': False,
        'limitations': ['Unsigned artwork-specific aids require actual sheet inspection.',
            'Source hashes bind producer bytes; they do not claim a human source-code review.',
            'Raw numerical agreement is separate from behavior acceptance and model-generation evidence.',
            'Visible wall relationships do not establish physics collisions.']}
    write_json(output / 'result.json', result)
    receipt = {'kind': 'UnsignedRawReviewEvidenceReceipt', 'status': 'unsigned', 'result_sha256': digest(result),
        'source_files': inventory, 'code_sources': code_sources,
        'generated_files': {path.relative_to(output).as_posix(): file_sha256(path) for path in output.rglob('*') if path.is_file()},
        'matrix_sha256': digest(context), 'measurement_bindings_sha256': digest(bindings),
        'public_event_contract_sha256': digest(contract), 'contact_policy': 'control_and_event_v1',
        'contact_selection_sha256': digest(contact)}
    write_json(output / 'evidence-receipt.json', receipt)
    return {**result, 'output': str(output), 'receipt_path': str(output / 'evidence-receipt.json')}


def review_session(directory, output):
    """All three suite splits/four source roles plus eight game wall cases."""
    return _review(*_context_session(directory), output)


def review_unchanged_svg(initial_acceptance_directory, output):
    """All 112 unchanged-SVG producer records; historical generation stays so."""
    return _review(*_context_unchanged(initial_acceptance_directory), output)


def collect_records(*, session_directory=None, initial_acceptance_directory=None):
    """Return the immutable role records and authoritative source pointers."""
    _need((session_directory is None) != (initial_acceptance_directory is None), 'Choose one authoritative final seal type.')
    context, records = _context_session(session_directory) if session_directory is not None else _context_unchanged(initial_acceptance_directory)
    return {'records': records, 'suite_directory': context['suite'], 'authoring_parent': context['authoring_parent'],
            'sealed_source': context['sealed_source'], 'seal_kind': context['kind']}


def review_final_records(records, output, *, suite_directory, authoring_parent, sealed_source,
                         session_directory=None, initial_acceptance_directory=None):
    """Accept explicit records only when they equal the actual immutable seal."""
    _need((session_directory is None) != (initial_acceptance_directory is None), 'Choose one authoritative final seal type.')
    context, required = _context_session(session_directory) if session_directory is not None else _context_unchanged(initial_acceptance_directory)
    _need(_ordinary(suite_directory) == _ordinary(context['suite'])
          and _ordinary(authoring_parent) == _ordinary(context['authoring_parent'])
          and _ordinary(sealed_source) == _ordinary(context['sealed_source']) and digest(records) == digest(required),
          'Explicit final records or sources differ from their immutable seal.', 'wrong_final_seal')
    return _review(context, records, output)


def attest_review(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    """Only an explicit actual inspector can persist a separate decision."""
    return raw.common.attest_review(output, reviewer=reviewer, inspected_sheets=inspected_sheets,
                                   accepted=accepted, images_inspected=images_inspected)


def _current_record_bindings(rows):
    """Reconstruct exact role/case/source/event identities from the current seal."""
    bindings = []
    for index, value in enumerate(rows):
        record = raw.common._record(value, index)
        manifest = load_package(record['package'], verify='hashes')
        observed = read_json(record['observations']) if isinstance(record.get('observations'), str) else evidence.observations(record)
        events = read_json(record['events']) if isinstance(record.get('events'), str) else evidence.event_set(record)
        _need(isinstance(observed, dict) and isinstance(events, dict), 'Current sealed observations and events are required.')
        selection = record.get('selection', observed.get('selection'))
        if isinstance(selection, (str, Path)):
            selection = read_json(selection)
        ids = {obj['id'] for obj in observed['objects']}
        mapping = ({selection['objects'][0]['id']: 'character'} if record.get('boundary') else
            record.get('object_map') or record.get('intake_binding', {}).get('object_map') or {oid: oid for oid in ids})
        active = {oid: raw.common._active(selection, oid, [frame['time_s'] for frame in manifest['frames']]) for oid in mapping}
        bindings.append({'record_id': record['id'], 'case_id': record['case_id'], 'role': record['role'],
            'capture_role': record['capture_role'], 'stage': record['stage'],
            'package_sha256': manifest['package_sha256'], 'observations_sha256': digest(observed),
            'events_sha256': digest(events), 'source_sha256': record.get('source_sha256'),
            'full_sealed_source_sha256': record.get('full_sealed_source_sha256'), 'object_map': mapping,
            'public_active_intervals_s': active})
    return bindings


def validate_review(output, *, session_directory=None, initial_acceptance_directory=None):
    """Read-only whole-matrix verification after a real sheet attestation."""
    _need((session_directory is None) != (initial_acceptance_directory is None), 'Choose one authoritative final seal type.')
    context, rows = _context_session(session_directory) if session_directory is not None else _context_unchanged(initial_acceptance_directory)
    output = _ordinary(output)
    try:
        result, receipt, attestation = (read_json(output / name) for name in ('result.json', 'evidence-receipt.json', 'attestation.json'))
    except OSError as error:
        raise VisualError('incomplete_visual_review', 'A complete final aid and actual sheet inspection are required.') from error
    _need(result.get('kind') == 'FinalOnlyUnsignedGameSVGReviewAid' and result.get('version') == VERSION
          and result.get('matrix_sha256') == digest(context) and result.get('numeric_status') == 'pass'
          and not result.get('failures') and not result.get('unsupported') and result.get('sheets')
          and result.get('public_event_contract') == raw._public_contract(context.get('event_contract'), context['domain'])
          and result.get('acceptance_granted') is False and result.get('full_behavior_acceptance') is False
          and result.get('visual_inspection_claimed') is False
          and receipt.get('result_sha256') == digest(result) and receipt.get('matrix_sha256') == digest(context)
          and receipt.get('measurement_bindings_sha256') == digest(result.get('records'))
          and receipt.get('public_event_contract_sha256') == digest(result.get('public_event_contract'))
          and receipt.get('contact_policy') == result.get('contact_policy') == 'control_and_event_v1'
          and receipt.get('contact_selection_sha256') == result.get('contact_selection_sha256')
          and result.get('production_coordinates_used_for_extraction') is False
          and result.get('new_model_generation_claimed_by_aid') is False
          and attestation.get('kind') == 'ProspectiveRawVisualReviewAttestation' and attestation.get('status') == 'pass'
          and attestation.get('images_inspected') is True and attestation.get('accepted') is True
          and attestation.get('result_sha256') == digest(result)
          and attestation.get('evidence_receipt_sha256') == file_sha256(output / 'evidence-receipt.json')
          and attestation.get('inspected_sheets') == result['sheets'], 'Final raw inspection is pending, failed or belongs to another seal.')
    _need(receipt.get('code_sources') == _code_sources(), 'The complete current review code closure is required.', 'stale_review')
    for path, sha in {**receipt['source_files'], **receipt['code_sources']}.items():
        _need(file_sha256(_ordinary(path)) == sha, 'Final raw sources or helper code changed.', 'stale_review')
    for name, sha in receipt['generated_files'].items():
        _need(file_sha256(safe_path(output, name)) == sha, 'Final marks or contact sheets changed.', 'stale_review')
    producer = _after_raw(context, rows, {})
    _need(result['records'] == _current_record_bindings(rows),
          'Attested role, case, source, object identity or event binding differs from the current seal.')
    actual = {(row['package_sha256'], row['observations_sha256']) for row in result['records']}
    _need(actual == {tuple(pair) for pair in producer['required_observations']}
          and len(result['records']) == len(rows), 'Attested review omits a required current observation.')
    return {'kind': 'VerifiedFinalRawMeasurementInspection', 'status': 'pass', 'directory': str(output),
        'attestation_sha256': file_sha256(output / 'attestation.json'), 'result_sha256': digest(result),
        'matrix_sha256': digest(context), 'sealed_source_sha256': context['sealed_source_sha256'],
        'required_observations': producer['required_observations'], 'record_count': len(rows),
        'source_code_review_status': 'pending', 'full_behavior_acceptance': False}
