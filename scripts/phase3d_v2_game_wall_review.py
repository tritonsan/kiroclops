"""New image-only wall consumer; prior failed raw aid and core helpers are immutable.

The complete byte-bound first aid is reused for its 120 unchanged pixel/event
claims and source proofs. Only eight static-wall claims and their public-input
motion semantics are remeasured. This does not promote the failed Session.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import shutil
import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from kiroclops.local_engine import suite_final_raw_review as final, suite_generic_raw_review as raw
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.package import load_package, frame_paths, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

VERSION = 'independent-static-wall-input-review-v2'


def _need(value, message, code='incomplete_review'):
    if not value:
        raise VisualError(code, message)


def _sources():
    from scripts import raw_game_input_episode_review as inputs
    return {str(Path(p).resolve()): file_sha256(p) for p in (__file__, inputs.__file__)}


def _edge(image, side):
    """Finite high-density contrast segments tolerate gaps in textured edges."""
    _need(side in ('left', 'right'), 'Declare the visible wall side.')
    values = image.astype(np.int16)
    contrast = np.max(np.abs(values[:, 1:] - values[:, :-1]), axis=2) >= 24
    candidates = []
    for x in range(contrast.shape[1]):
        if (side == 'right' and x + .5 <= image.shape[1] / 2) or (side == 'left' and x + .5 >= image.shape[1] / 2):
            continue
        ys = np.flatnonzero(contrast[:, x])
        if not len(ys):
            continue
        starts = np.r_[0, np.flatnonzero(np.diff(ys) > 5) + 1]
        ends = np.r_[starts[1:], len(ys)]
        for start, end in zip(starts, ends):
            lo, hi = int(ys[start]), int(ys[end-1])
            density = (end-start) / (hi-lo+1)
            if end-start >= 55 and hi-lo+1 >= 55 and density >= .75:
                candidates.append({'x': x+.5, 'top': lo, 'bottom': hi,
                    'support_pixels': int(end-start), 'density': density})
    if not candidates:
        return {'status': 'unobservable', 'method': 'finite_contrast_density'}
    face = (min if side == 'right' else max)(c['x'] for c in candidates)
    eligible = [c for c in candidates if c['x'] == face]
    value = max(eligible, key=lambda c: (c['support_pixels'], c['bottom']-c['top']))
    return {'status': 'unsigned', 'method': 'finite_contrast_density', **value}


def static_wall(images, times, manifest_inputs, side):
    """Pool only public pre-input images; never use produced coordinates."""
    _need(manifest_inputs.get('provenance') == 'recorder_verified', 'Wall pooling needs recorded input times.')
    starts = [e['time_s'] for e in manifest_inputs.get('events', [])]
    _need(starts and len(images) == len(times), 'Wall input or image history is incomplete.')
    selected = [i for i, t in enumerate(times) if t < min(starts)]
    _need(len(selected) >= 3, 'At least three pre-input raw images are required.')
    pool = np.median(np.stack([images[i] for i in selected]), axis=0).astype(np.uint8)
    wall = _edge(pool, side)
    _need(wall['status'] == 'unsigned', 'A finite visible face is not measurable.', 'raw_boundary_unobservable')
    # Every independent pre-input face must agree; pooling cannot hide camera drift.
    individual = [_edge(images[i], side) for i in selected]
    _need(all(w.get('status') == 'unsigned' and w['x'] == wall['x']
        and abs(w['top']-wall['top']) <= 1 and abs(w['bottom']-wall['bottom']) <= 1 for w in individual),
        'The pre-input static wall geometry is unstable.', 'raw_camera_motion')
    x = int(wall['x']-.5)
    delta = pool[:, x+1].astype(np.int16)-pool[:, x].astype(np.int16)
    return {**wall, 'method': 'public_preinput_static_contrast_density', 'side': side,
        'edge_delta_rgb': delta.tolist(), 'preinput_indices': selected, 'production_coordinates_used': False}


def _supported(image, wall, point=None):
    """Current raw support must retain face orientation and static texture.

    Only a small raw-glyph neighborhood may occlude the line. The remaining
    visible pixel signature distinguishes the inward and outward faces, and
    rejects camera shifts; a count of arbitrary contrast is insufficient.
    """
    values = image.astype(np.int16)
    x = int(wall['x']-.5)
    actual = values[:, x+1]-values[:, x]
    expected = np.asarray(wall.get('edge_delta_rgb', []), dtype=np.int16)
    if expected.shape != actual.shape:
        return False, 0
    available = np.ones(len(actual), dtype=bool)
    if isinstance(point, dict) and 'x' in point and abs(point['x']-wall['x']) <= 16:
        available &= np.abs(np.arange(len(actual))-point['y']) > 20
    scope = (np.arange(len(actual)) >= max(0, wall['top']-3)) & (np.arange(len(actual)) <= min(len(actual)-1, wall['bottom']+3)) & available
    expected_edge = np.max(np.abs(expected), axis=1) >= 24
    same = np.max(np.abs(actual-expected), axis=1) <= 24
    count = int(np.count_nonzero(scope & expected_edge & same))
    disagreement = int(np.count_nonzero(scope & ~same))
    # At most two raw edge pixels may differ; preserve the 2px geometry gate.
    return count >= 30 and disagreement <= 2, count


def _verify_parent(directory):
    directory = _ordinary(directory)
    result, receipt, matrix = (read_json(directory / n) for n in ('result.json', 'evidence-receipt.json', 'matrix.json'))
    _need(result.get('kind') == 'UnsignedMixedEpochGameSupplementalRawAid'
        and result.get('numeric_status') == 'fail' and result.get('historical_final_status') == 'failed'
        and result.get('full_behavior_acceptance') is False and result.get('unsupported') == []
        and receipt['result_sha256'] == digest(result) and receipt['matrix_sha256'] == digest(matrix)
        and receipt['measurement_bindings_sha256'] == digest(result['records'])
        and len(result['records']) == 128 and result['producer_binding']['source_binding_status'] == 'pass',
        'The exact complete failed predecessor aid is required.')
    boundary = {r['record_id'] for r in result['records'] if r['role'] == 'boundary_selected'}
    _need(len(boundary) == 8 and all(f.get('record_id') in boundary for f in result['failures'])
        and all(p['pass'] for p in result['positions']), 'Unchanged non-wall failures cannot be reused as success.')
    _need(all(e['record_id'] not in boundary or all(c['pass'] for c in e['relation_checks']) for e in result['events']),
        'This consumer cannot repair unrelated relation claims.')
    files = {**receipt['source_files'], **receipt['code_sources']}
    files.update({str(safe_path(directory, n)): sha for n, sha in receipt['generated_files'].items()})
    files[str(directory/'evidence-receipt.json')] = file_sha256(directory/'evidence-receipt.json')
    for path, sha in files.items():
        _need(file_sha256(path) == sha, 'Predecessor source, evidence, marks or code changed.', 'stale_review')
    return result, receipt, matrix, files


def _bundles(parent, old, matrix):
    boundary = read_json(Path(matrix['supplemental'])/'visual-boundary.json')
    records = read_json(boundary['records_path'])
    bindings = [r for r in old['records'] if r['role'] == 'boundary_selected']
    _need(len(records) == len(bindings) == 8, 'Every fixed wall case is required.')
    bundles = []
    for record, binding in zip(records, bindings):
        _need(record['scenario']['id'] == binding['case_id'], 'Wall case order changed.')
        normalized = raw.common._record({**record, 'id': binding['record_id'], 'domain': 'game',
            'role': 'boundary_selected', 'case_id': binding['case_id'], 'capture_role': 'boundary',
            'stage': 'boundary', 'boundary': True, 'full_sealed_source_sha256': matrix['sealed_source_sha256']}, 0)
        manifest = load_package(normalized['package'], verify='hashes')
        _need(manifest['package_sha256'] == binding['package_sha256'], 'A different wall package cannot reuse marks.')
        proposed = read_json(parent/'raw'/normalized['id']/'points.json')
        _need(proposed['package_sha256'] == manifest['package_sha256'] and proposed['production_coordinates_used'] is False
            and proposed['production_events_used'] is False and len(proposed['rows']) == len(manifest['frames']),
            'Exact independent raw glyph proposals are required.')
        paths = frame_paths(normalized['package'], manifest)
        images = [read_image(p) for p in paths]
        rows = deepcopy(proposed['rows'])
        _need(all(row['index'] == i and row['time_s'] == frame['time_s'] and row['image_sha256'] == frame['sha256']
            for i, (row, frame) in enumerate(zip(rows, manifest['frames']))), 'Raw frame binding changed.')
        wall = static_wall(images, [f['time_s'] for f in manifest['frames']], manifest['inputs'], record['scenario']['wall_side'])
        failures = []
        for row, image in zip(rows, images):
            supported, count = _supported(image, wall, row['points']['character'])
            row['wall'] = {**wall, 'frame_support_pixels': count} if supported else {'status': 'unobservable', 'method': wall['method']}
            row['connections'].pop('independent_wall_edge', None)
            if supported:
                row['connections']['independent_wall_edge'] = {'start': [wall['x'], wall['top']], 'end': [wall['x'], wall['bottom']]}
            else:
                failures.append({'code': 'raw_static_boundary_not_visible', 'record_id': normalized['id'], 'index': row['index']})
        bundles.append({'record': normalized, 'manifest': manifest, 'raw': {'rows': rows}, 'geometry': wall,
            'raw_geometry_failures': failures, 'paths': paths})
    return bundles


def _motion_compare(bundle, independent):
    produced = [e for e in bundle['event_set']['events'] if not e['type'].startswith('boundary_')]
    pools, expected = defaultdict(list), defaultdict(list)
    for event in independent:
        pools[raw._event_key(event)].append(event)
    for event in produced:
        expected[raw._event_key(event)].append(event)
    checks, failures = [], []
    for key in sorted(set(pools)|set(expected), key=repr):
        actual = sorted(pools[key], key=lambda e: e['observed_interval_s'])
        source = sorted(expected[key], key=lambda e: e['time_interval_s'])
        if len(actual) != len(source):
            failures.append({'code': 'event_occurrence_mismatch', 'record_id': bundle['record']['id'],
                'event_type': key[0], 'objects': list(key[1]), 'produced': len(source), 'raw': len(actual)})
        for value, detected in zip(source, actual):
            interval = raw.common._interval(value['time_interval_s'], 'produced event')
            error = max(abs(a-b) for a, b in zip(interval, detected['observed_interval_s']))
            width = max(interval[1]-interval[0], value.get('uncertainty_s', 0), detected['uncertainty_s'])
            passed = error <= .05+1e-9 and width <= .1+1e-9 and value.get('timing_status') == detected['timing_status'] == 'resolved'
            checks.append({'type': key[0], 'objects': list(key[1]), 'produced_interval_s': interval,
                'raw_interval_s': detected['observed_interval_s'], 'difference_s': error, 'max_uncertainty_s': width, 'pass': passed})
            if not passed:
                failures.append({'code': 'event_interval_or_uncertainty_disagreement', 'record_id': bundle['record']['id'], 'event_type': key[0]})
    return checks, failures


def _proposal(row):
    return digest({k: row.get(k) for k in ('points', 'objects', 'connections', 'wall')})


def _verify_tile_groups(unique):
    for occurrences in unique.values():
        _need(len({(r['image_sha256'], r['raw_proposal_sha256']) for r in occurrences}) == 1,
            'A shared tile has different current raw geometry; do not hide it by predecessor dedup.', 'contact_identity_mismatch')


def _unsupported_failures(record_id, filtered):
    return [{'code': 'raw_input_episode_binding_unresolved', 'record_id': record_id, **item}
        for item in filtered['unsupported']]


def _sheets(parent, old, bundles, output):
    contact = read_json(parent/'contact-selection.json')
    contact['parent_contact_selection_sha256'] = digest(contact)
    contact['policy'] = 'unchanged_exact_pixels_plus_remeasured_static_wall_v2'
    by_id = {b['record']['id']: b for b in bundles}
    changed_pages = set()
    unique = defaultdict(list)
    for occurrence in contact['selected_images']:
        unique[occurrence['tile_index']].append(occurrence)
        if occurrence['record_id'] in by_id:
            row = by_id[occurrence['record_id']]['rows'][occurrence['index']]
            occurrence['parent_raw_proposal_sha256'] = occurrence['raw_proposal_sha256']
            occurrence['raw_proposal_sha256'] = _proposal(row)
            occurrence['reasons'] = ['parent_image_only_contact_candidate:'+v for v in occurrence['reasons']]
            changed_pages.add(occurrence['tile_index']//12)
    _verify_tile_groups(unique)
    sheets = []
    for page, saved in enumerate(old['sheets']):
        relative = saved['path']; target = safe_path(output, relative, must_exist=False)
        if page not in changed_pages:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(parent, relative), target)
        else:
            image = read_image(safe_path(parent, relative))
            for tile_index in range(page*12, min(page*12+12, contact['unique_tile_count'])):
                occurrences = unique[tile_index]; first = occurrences[0]
                if first['record_id'] not in by_id:
                    continue
                bundle = by_id[first['record_id']]; index = first['index']
                tile = raw._tile(read_image(bundle['paths'][index]), bundle['manifest']['frames'][index],
                    f"tile{tile_index:03} {bundle['record']['role']} +{len(occurrences)-1} exact repeats", bundle['rows'][index], 'game')
                slot = tile_index-page*12; y, x = (slot//3)*230, (slot%3)*600
                image[y:y+230, x:x+600] = tile
            raw.common._save_image(target, image)
        sheets.append({'path': relative, 'sha256': file_sha256(target), 'same_parent_sheet': page not in changed_pages})
    write_json(output/'contact-selection.json', contact)
    return sheets, contact, sorted(changed_pages)


def prepare(parent, output):
    from scripts.raw_game_input_episode_review import filter_raw_events
    parent, output = _ordinary(parent), _ordinary(output)
    _need(not output.exists() and not output.is_relative_to(parent) and not parent.is_relative_to(output), 'Preserve the first raw aid.', 'output_exists')
    sources = _sources()
    old, receipt, matrix, inventory = _verify_parent(parent)
    # All eight image-only geometry passes finish before production measurements are opened.
    bundles = _bundles(parent, old, matrix)
    output.mkdir(parents=True)
    write_json(output/'matrix.json', matrix)
    geometry_failures = [f for b in bundles for f in b['raw_geometry_failures']]
    for b in bundles:
        write_json(output/'raw'/b['record']['id']/'points.json', {'rows': b['raw']['rows'],
            'package_sha256': b['manifest']['package_sha256'], 'production_coordinates_used': False,
            'production_events_used': False, 'target_code_or_dom_read': False, 'status': 'unsigned'})
    write_json(output/'extraction-complete.json', {'all_eight_wall_geometry_passes_before_production_read': True,
        'parent_glyph_proposals_reused_exact': True, 'production_coordinates_or_events_used': False})
    contract = old['public_event_contract']; failures = list(geometry_failures)
    events = [deepcopy(e) for e in old['events'] if not e['record_id'].startswith('boundary_selected-')]
    boundaries, decisions = [], []
    for bundle in bundles:
        failures.extend(final._scope_boundary(bundle, inventory, contract))
        independent, relations = raw._raw_events(bundle, contract)
        _need(not relations, 'The wall motion consumer cannot waive other relation types.')
        filtered = filter_raw_events(bundle['rows'], independent, bundle['manifest']['inputs'],
            object_id=next(iter(bundle['mapping'])), rest_duration_s=contract['rest_duration_s'],
            rest_radius_px=contract['rest_radius_px'], max_gap_s=contract['max_gap_s'])
        failures.extend(_unsupported_failures(bundle['record']['id'], filtered))
        checks, errors = _motion_compare(bundle, filtered['events']); failures.extend(errors)
        boundary, errors = final._boundary_events(bundle, contract); failures.extend(errors)
        boundaries.append(boundary)
        events.append({'record_id': bundle['record']['id'], 'raw_events': filtered['events']+boundary['raw_events'],
            'event_checks': checks, 'relation_checks': [], 'raw_relations': [],
            'public_input_episode_review': filtered})
        decisions.append({'record_id': bundle['record']['id'], 'geometry': bundle['geometry'], 'public_input_review': filtered})
    sheets, contact, changed_pages = _sheets(parent, old, bundles, output)
    _need(sources == _sources(), 'New review source changed during execution.', 'source_changed')
    result = {**deepcopy(old), 'kind': 'UnsignedRemeasuredStaticWallGameRawAid', 'version': VERSION,
        'numeric_status': 'pass' if not failures else 'fail', 'events': events, 'boundaries': boundaries,
        'failures': failures, 'sheets': sheets, 'contact_selection_sha256': digest(contact),
        'parent_aid': str(parent), 'parent_result_sha256': digest(old), 'parent_numeric_status': 'fail',
        'parent_failure_count': len(old['failures']), 'remeasured_wall_records': len(bundles),
        'reused_unchanged_records': 120, 'changed_sheet_pages': changed_pages,
        'same_pixels_and_claims_reused_only_with_full_byte_receipt': True,
        'decisions': decisions, 'historical_acceptance_changed': False, 'full_behavior_acceptance': False}
    write_json(output/'result.json', result)
    write_json(output/'evidence-receipt.json', {'kind': 'UnsignedRawReviewEvidenceReceipt', 'status': 'unsigned',
        'result_sha256': digest(result), 'source_files': inventory, 'code_sources': {**receipt['code_sources'], **sources},
        'generated_files': {p.relative_to(output).as_posix(): file_sha256(p) for p in sorted(output.rglob('*')) if p.is_file()},
        'matrix_sha256': digest(matrix), 'measurement_bindings_sha256': digest(result['records']),
        'contact_selection_sha256': digest(contact), 'public_event_contract_sha256': digest(contract)})
    return result


def validate(output):
    output = _ordinary(output)
    result, receipt, attestation = (read_json(output/n) for n in ('result.json', 'evidence-receipt.json', 'attestation.json'))
    _need(result.get('kind') == 'UnsignedRemeasuredStaticWallGameRawAid' and result.get('version') == VERSION
        and result['numeric_status'] == 'pass' and not result['failures'] and not result['unsupported']
        and result['full_behavior_acceptance'] is False and result['historical_final_status'] == 'failed'
        and receipt['result_sha256'] == digest(result) and attestation['status'] == 'pass'
        and attestation['images_inspected'] is True and attestation['accepted'] is True
        and attestation['result_sha256'] == digest(result) and attestation['inspected_sheets'] == result['sheets']
        and attestation['evidence_receipt_sha256'] == file_sha256(output/'evidence-receipt.json'), 'The exact actual image review is required.')
    _need(all(receipt['code_sources'].get(p) == sha for p, sha in _sources().items()), 'New consumer code changed.', 'stale_review')
    for path, sha in {**receipt['source_files'], **receipt['code_sources']}.items():
        _need(file_sha256(path) == sha, 'Source or raw evidence changed.', 'stale_review')
    for name, sha in receipt['generated_files'].items():
        _need(file_sha256(safe_path(output, name)) == sha, 'Generated evidence changed.', 'stale_review')
    parent = _ordinary(result['parent_aid']); old = read_json(parent/'result.json')
    _need(digest(old) == result['parent_result_sha256'] and old['numeric_status'] == 'fail'
        and old['records'] == result['records'] and old['producer_binding'] == result['producer_binding']
        and old['positions'] == result['positions'] and old['jumps'] == result['jumps'], 'Prior failure or reused claims changed.')
    return {'kind': 'VerifiedRemeasuredStaticWallGameRawReview', 'status': 'pass', 'directory': str(output),
        'source_sha256': read_json(output/'matrix.json')['sealed_source_sha256'],
        'matrix_sha256': result['matrix_sha256'], 'record_count': 128, 'required_observations': result['producer_binding']['required_observations'],
        'parent_numeric_status': 'fail', 'remeasured_wall_records': 8, 'reused_unchanged_records': 120,
        'attestation_sha256': file_sha256(output/'attestation.json'), 'full_behavior_acceptance': False,
        'source_code_review_status': 'pending', 'historical_final_status': 'failed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'validate'))
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = prepare(args.parent, args.output) if args.operation == 'prepare' else validate(args.output)
    print({k: value.get(k) for k in ('kind', 'status', 'numeric_status', 'record_count', 'changed_sheet_pages')})
    if 'failures' in value: print('failures', len(value['failures']), 'sheets', len(value['sheets']))

if __name__ == '__main__':
    main()
