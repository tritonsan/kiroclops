"""Fresh normal-map raster review for a canonical sealed ground session.

This standalone consumer does not accept or manufacture the historical failed
jump supplemental envelope. Native function checks and full ground behavior
acceptance remain separate from an actual inspection of these raw images.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import json
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts import phase3d_v2_ground_final_raw as ground, prepare_game_raw_review_aid as glyph
from kiroclops.local_engine import evidence, suite_final_raw_review as final
from kiroclops.local_engine import suite_generic_raw_review as generic, suite_raw_review as common, suite_game_demo as demo
from kiroclops.local_engine import session as session_module, source_inventory, reliability
from kiroclops.local_engine.session import Session, _ordinary
from kiroclops.portable_v2 import runtime, reporting
from kiroclops.integration import storage
from kiroclops.visual.package import read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

VERSION = 'canonical-ground-normal-raw-v1'
KIND = 'UnsignedCanonicalGroundNormalRawAid'


def _need(condition, message, code='incomplete_visual_review'):
    if not condition:
        raise VisualError(code, message)


def _code_sources():
    # The renderer's transitive raw-helper closure is included, as are the
    # actual native/video readers and the separate prospective-parent reader.
    modules = (glyph, ground, generic, common, demo, evidence, runtime, runtime.private, reporting, storage,
               session_module, source_inventory, reliability)
    return final._code_sources() | ground._extra_sources() | {
        str(Path(module.__file__).resolve()): file_sha256(module.__file__) for module in modules} | {
        str(Path(__file__).resolve()): file_sha256(__file__)}


def _put(inventory, path, expected=None):
    path = _ordinary(path)
    actual = file_sha256(path)
    _need(expected is None or actual == expected, 'Bound normal input changed: ' + str(path), 'source_changed')
    _need(str(path) not in inventory or inventory[str(path)] == actual,
          'One input has conflicting byte identities.', 'source_changed')
    inventory[str(path)] = actual


def _authority(session_directory, parent_chain, parent_chain_sha256):
    directory = _ordinary(session_directory)
    state = Session(directory)._load()
    context, rows = final._context_session(directory)
    _need(context.get('kind') == 'SealedSessionFinalRawMatrix' and context.get('domain') == 'game'
          and context.get('stage') == 'ground' and len(context.get('cases', [])) == ground.MATRIX['cases']
          and len(rows) == ground.MATRIX['records']
          and state.get('status') == 'finalized' and state.get('active_job_id') is None,
          'A complete canonical sealed ground final is required.', 'wrong_final_seal')
    parent = ground._parent_chain(parent_chain, parent_chain_sha256, context)
    chosen = next((item for item in state['candidates']
                   if item['candidate_id'] == context['selected_candidate_id']), None)
    _need(chosen is not None and state.get('selected_candidate_id') == chosen['candidate_id']
          and chosen.get('eligible') is True and chosen.get('status') == 'succeeded'
          and state.get('sealed_at') is not None and (state.get('final') or {}).get('status') == 'succeeded'
          and state['final'].get('job_id') == state.get('final_job_id'),
          'The ground candidate and completed final seal differ.', 'wrong_final_seal')
    jobs = (state.get('prepare_job_id'), chosen['job_id'], state.get('final_job_id'))
    _need(len(set(jobs)) == 3 and all(isinstance(item, str) for item in jobs),
          'Canonical preparation, evaluation and final jobs must be distinct.', 'wrong_final_seal')
    results = [evidence._completed_job(directory, state, job, operation, candidate)
        for job, operation, candidate in zip(jobs, ('prepare', 'evaluate', 'finalize'), (None, chosen, chosen))]
    _need(all(result.get('suite_sha256') == context['suite_sha256'] for result in results)
          and results[1].get('eligible') is True and results[2].get('pass') is state['final'].get('pass'),
          'Completed jobs belong to another ground suite or selection.', 'wrong_final_seal')
    normal_directory = directory / 'measurements' / jobs[2] / 'normal-level'
    normal = demo.verify(normal_directory, source_sha256=context['sealed_source_sha256'])
    selected = _ordinary(context['sealed_source'])
    _need(normal.get('status') == 'pass' and normal.get('mandatory_route') == 'normal-combined'
          and _ordinary(normal['selected_source']) == selected
          and normal.get('full_source_hashes') == context['sealed_source_hashes']
          and normal.get('source_sha256') == context['sealed_source_sha256']
          and normal.get('native_source_hashes') == runtime.source_hashes(selected)
          == runtime.source_hashes(_ordinary(normal['native_root']) / 'project')
          and read_json(normal_directory / 'result.json') == normal
          and results[2].get('normal_level') == normal and results[2].get('normal_level_status') == 'pass',
          'Normal routes do not use the canonical selected ground renderer.', 'wrong_candidate')
    inventory = {}
    for path in (directory / 'session.json', directory / 'binding.json',
                 Path(context['suite']) / 'suite.json', Path(context['suite']) / 'suite-binding.json',
                 normal_directory / 'result.json'):
        _put(inventory, path)
    for job in jobs:
        for name in ('job.json', 'binding.json', 'result.json'):
            _put(inventory, directory / 'jobs' / job / name)
    for name, sha in context['sealed_source_hashes'].items():
        _put(inventory, safe_path(selected, name), sha)
    for name, sha in normal['file_hashes'].items():
        _put(inventory, safe_path(_ordinary(normal['native_root']), name), sha)
    for route in normal['scenarios']:
        for path in (route['video'], Path(route['video']).with_suffix('.source.json')):
            _put(inventory, path)
    chain = read_json(parent_chain)
    for name, sha in ground._chain_inputs(chain).items():
        _put(inventory, name, sha)
    for path, sha in ((parent_chain, parent_chain_sha256),
                      (Path(chain['suite']) / 'suite.json', chain['suite_file_sha256']),
                      (Path(chain['suite']) / 'suite-binding.json', chain['suite_binding_sha256']),
                      (chain['initial_session_archive'], chain['initial_session_archive_sha256']),
                      (chain['producer_archive'], chain['producer_sha256'])):
        _put(inventory, path, sha)
    authority = {'kind': 'CanonicalGroundNormalRawAuthority', 'schema_version': 1, 'version': VERSION,
        'session': str(directory), 'session_id': state['session_id'], 'selected_candidate_id': chosen['candidate_id'],
        'final_job_id': jobs[2], 'suite_sha256': context['suite_sha256'],
        'suite_directory': str(_ordinary(context['suite'])),
        'authoring_parent': str(_ordinary(context['authoring_parent'])),
        'original': str(_ordinary(context['original'])),
        'canonical_context_sha256': digest(context), 'source_sha256': context['sealed_source_sha256'],
        'normal_directory': str(normal_directory), 'normal_result_sha256': digest(normal),
        'prospective_parent_chain': parent, 'mandatory_route': 'normal-combined',
        'normal_status': normal['status'], 'slope_diagnostic_status': normal['slope_diagnostic_status'],
        'full_behavior_acceptance': False, 'historical_acceptance_changed': False}
    return authority, normal, inventory


def _contacts(rows, events):
    """Replay the raw-only deterministic controls and public-input coverage."""
    distinct, valid = {}, {}
    for row in rows:
        point = row['point']
        if 'x' in point or 'y' in point:
            _need(all(key in point and isinstance(point[key], (int, float)) and not isinstance(point[key], bool)
                      and np.isfinite(point[key]) for key in ('x', 'y')),
                  'Normal glyph coordinates must be finite independently marked image points.')
        distinct.setdefault(row['image_sha256'], row['index'])
        if 'x' in row['point'] and 'y' in row['point']:
            valid.setdefault(row['image_sha256'], row['index'])
    reasons = defaultdict(set)
    def near(index, reason):
        for i in range(max(0, index - 2), min(240, index + 3)):
            reasons[i].add(reason)
    controls = list(valid.values())
    for value in sorted({round(i) for i in np.linspace(0, len(controls) - 1, min(20, len(controls)))}):
        reasons[controls[value]].add('distinct_raw_glyph_control')
    for event in events:
        near(event['tick'], 'public_input:' + event['action'] + (' press' if event['pressed'] else ' release'))
    for i in range(1, 239):
        section = rows[i - 1:i + 2]
        if all('y' in row['point'] for row in section):
            a, b, c = (row['point']['y'] for row in section)
            if abs(b - a) >= 1 and abs(c - b) >= 1 and (b - a) * (c - b) < 0:
                near(i, 'image_vertical_turn_candidate')
        if i >= 36:
            points = [row['point'] for row in rows[i - 6:i + 1]]
            if all('x' in point and 'y' in point for point in points):
                if max(p['x'] for p in points) - min(p['x'] for p in points) <= 1 \
                        and max(p['y'] for p in points) - min(p['y'] for p in points) <= 1:
                    if rows[i - 7]['point'].get('x') != points[0]['x']:
                        near(i, 'image_rest_candidate')
    near(0, 'clip_boundary'); near(239, 'clip_boundary')
    tile_ids, selected_images = {}, []
    for index in sorted(reasons):
        row = rows[index]
        key = row['image_sha256'], digest(row['point'])
        if key not in tile_ids:
            tile_ids[key] = len(tile_ids)
        selected_images.append({**row, 'reasons': sorted(reasons[index]), 'tile_index': tile_ids[key]})
    return selected_images, len(tile_ids), len(distinct), len(valid)


def _route(route, source_sha256, output, inventory):
    name, run = route['id'], _ordinary(route['raw_run'])
    manifest = read_json(run / 'manifest.json')
    _need(name in ('normal-combined', 'normal-slope') and len(manifest['frames']) == 240,
          'Both full fixed normal-map recordings are required.')
    rows = []
    # Complete raw pass first. Engine coordinates and landing/braking flags
    # never enter the glyph estimator or contact-frame selection.
    for index, frame in enumerate(manifest['frames']):
        path = run / 'frames' / f'{index:06d}.png'
        _put(inventory, path, manifest['raw_frame_hashes'][path.name])
        found = glyph.anchor(read_image(path))
        point = found[0] if len(found) == 1 else {'status': 'ambiguous' if found else 'not_detected', 'count': len(found)}
        rows.append({'index': index, 'time_s': frame['time_s'], 'image_sha256': inventory[str(path)], 'point': point})
    selected_images, tile_count, distinct_count, valid_count = _contacts(rows, route['scenario']['events'])
    failures = []
    for measure, count in (('distinct_images', distinct_count), ('distinct_glyph_controls', valid_count)):
        if count < 20:
            failures.append({'code': 'normal_raw_controls_insufficient', 'route': name,
                             'measure': measure, 'actual': count, 'required': 20})
    tiles, sheets = [], []
    for row in selected_images:
        if row['tile_index'] == len(tiles):
            path = run / 'frames' / f"{row['index']:06d}.png"
            image = read_image(path)
            _put(inventory, path, row['image_sha256'])
            geometry = {'points': {'character': row['point']}, 'objects': {}, 'connections': {}}
            tiles.append(generic._tile(image, {'index': row['index'], 'time_s': row['time_s']}, name, geometry, 'game'))
    _need(len(tiles) == tile_count, 'The contact rendering plan lost a raw tile.')
    for page, start in enumerate(range(0, len(tiles), 12)):
        batch = tiles[start:start + 12]
        batch.extend(np.full((230, 600, 3), 24, np.uint8) for _ in range((-len(batch)) % 3))
        relative = f'sheets/{name}-{page:02}.png'
        common._save_image(output / relative, np.vstack([np.hstack(batch[i:i + 3]) for i in range(0, len(batch), 3)]))
        sheets.append({'path': relative, 'sha256': file_sha256(output / relative)})
    record = {'route': name, 'mandatory': name == 'normal-combined', 'raw_run': str(run),
        'source_sha256': source_sha256, 'manifest_sha256': file_sha256(run / 'manifest.json'),
        'video_sha256': route['video_sha256'], 'frames': rows, 'distinct_image_count': distinct_count,
        'distinct_glyph_control_count': valid_count, 'selected_images': selected_images, 'unique_tile_count': len(tiles),
        'dedup_key': ['image_sha256', 'independent_glyph_proposal_sha256'],
        'public_input_timeline': route['scenario']['events'], 'public_input_provenance': 'recorder_verified',
        'physics_landing_inferred': False, 'physics_braking_inferred': False,
        'functional_status': route['gameplay']['status']}
    return record, sheets, failures


def prepare(session_directory, output, *, parent_chain, parent_chain_sha256):
    output = _ordinary(output)
    _need(not output.exists(), 'Preserve prior normal review outcomes.', 'output_exists')
    authority, normal, inventory = _authority(session_directory, parent_chain, parent_chain_sha256)
    for path in (authority['session'], authority['suite_directory'], authority['authoring_parent'], authority['original'],
                 authority['normal_directory'], normal['native_root'], normal['selected_source'], parent_chain):
        path = _ordinary(path)
        _need(not output.is_relative_to(path) and not path.is_relative_to(output),
              'Normal review output overlaps an immutable input.', 'unsafe_path')
    sources = _code_sources()
    output.mkdir(parents=True)
    records, sheets, failures = [], [], []
    for route in normal['scenarios']:
        record, pages, errors = _route(route, authority['source_sha256'], output, inventory)
        records.append(record); sheets.extend(pages); failures.extend(errors)
    _need([record['route'] for record in records] == ['normal-combined', 'normal-slope'], 'Exact two-route matrix required.')
    _need(sources == _code_sources(), 'Normal consumer changed during raw marking.', 'source_changed')
    for path, sha in inventory.items():
        _need(file_sha256(path) == sha, 'Normal input changed during review.', 'source_changed')
    archives = []
    for number, (origin, sha) in enumerate(sorted(sources.items())):
        target = output / 'producer-sources' / f'{number:03}-{Path(origin).name}'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(origin).read_bytes())
        _need(file_sha256(target) == sha, 'Normal producer changed during archival.', 'source_changed')
        archives.append({'origin': origin, 'sha256': sha, 'archive': target.relative_to(output).as_posix()})
    write_json(output / 'authority.json', authority)
    aid = {'kind': KIND, 'schema_version': 1, 'version': VERSION, 'status': 'pending_visual_review',
        'numeric_status': 'pass' if not failures else 'fail', 'failures': failures, 'unsupported': [],
        'authority_sha256': digest(authority), 'source_sha256': authority['source_sha256'], 'records': records, 'sheets': sheets,
        'production_coordinates_used_for_proposals': False, 'production_events_used_for_contact_selection': False,
        'visual_inspection_claimed': False, 'full_behavior_acceptance': False, 'historical_acceptance_changed': False,
        'ordinary_reader_compatibility_claimed': False, 'new_model_generation': False,
        'scope': 'Fresh normal-level images only; native function checks and full ground acceptance are separate.'}
    write_json(output / 'result.json', aid)
    receipt = {'kind': 'CanonicalGroundNormalRawEvidenceReceipt', 'schema_version': 1, 'version': VERSION,
        'result_sha256': digest(aid), 'authority_sha256': digest(authority),
        'source_files': inventory, 'code_sources': sources, 'producer_archives': archives,
        'generated_files': {path.relative_to(output).as_posix(): file_sha256(path)
                            for path in sorted(output.rglob('*')) if path.is_file()}}
    write_json(output / 'evidence-receipt.json', receipt)
    return aid


def attest(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    return common.attest_review(output, reviewer=reviewer, inspected_sheets=inspected_sheets,
                               accepted=accepted, images_inspected=images_inspected)


def validate(output, *, session_directory, parent_chain, parent_chain_sha256):
    output = _ordinary(output)
    authority, normal, inventory = _authority(session_directory, parent_chain, parent_chain_sha256)
    aid, receipt, attestation, saved = (read_json(output / name)
        for name in ('result.json', 'evidence-receipt.json', 'attestation.json', 'authority.json'))
    _need(aid.get('kind') == KIND and aid.get('schema_version') == 1 and aid.get('version') == VERSION
          and aid.get('numeric_status') == 'pass' and not aid.get('failures') and not aid.get('unsupported')
          and aid.get('sheets') and aid.get('authority_sha256') == digest(authority) and saved == authority
          and aid.get('source_sha256') == authority['source_sha256']
          and aid.get('full_behavior_acceptance') is False and aid.get('historical_acceptance_changed') is False
          and aid.get('ordinary_reader_compatibility_claimed') is False and aid.get('new_model_generation') is False
          and aid.get('visual_inspection_claimed') is False
          and aid.get('production_coordinates_used_for_proposals') is False
          and aid.get('production_events_used_for_contact_selection') is False,
          'Normal raw aid is pending, failed or belongs to another canonical seal.')
    _need(receipt.get('kind') == 'CanonicalGroundNormalRawEvidenceReceipt' and receipt.get('schema_version') == 1
          and receipt.get('version') == VERSION and receipt.get('result_sha256') == digest(aid)
          and receipt.get('authority_sha256') == digest(authority) and receipt.get('source_files') == inventory
          and receipt.get('code_sources') == _code_sources(), 'Normal raw receipt identity changed.', 'stale_review')
    _need(attestation.get('kind') == 'ProspectiveRawVisualReviewAttestation' and attestation.get('status') == 'pass'
          and attestation.get('accepted') is True and attestation.get('images_inspected') is True
          and attestation.get('result_sha256') == digest(aid) and attestation.get('inspected_sheets') == aid['sheets']
          and attestation.get('evidence_receipt_sha256') == file_sha256(output / 'evidence-receipt.json'),
          'Every bound normal contact sheet requires actual inspection.')
    _need([row.get('route') for row in aid['records']] == ['normal-combined', 'normal-slope']
          and all(len(row.get('frames', [])) == 240 and row.get('distinct_glyph_control_count', 0) >= 20
                  and row.get('distinct_image_count', 0) >= 20 for row in aid['records']),
          'Normal inspection omits a complete route or its raw controls.')
    expected_sheets = []
    for row, route in zip(aid['records'], normal['scenarios']):
        manifest = read_json(_ordinary(route['raw_run']) / 'manifest.json')
        _need(row.get('source_sha256') == authority['source_sha256'] and row.get('raw_run') == str(_ordinary(route['raw_run']))
              and row.get('public_input_timeline') == route['scenario']['events']
              and row.get('public_input_provenance') == 'recorder_verified'
              and row.get('video_sha256') == route['video_sha256']
              and row.get('manifest_sha256') == file_sha256(_ordinary(route['raw_run']) / 'manifest.json')
              and row.get('mandatory') is (route['id'] == 'normal-combined')
              and row.get('functional_status') == route['gameplay']['status']
              and row.get('physics_landing_inferred') is False and row.get('physics_braking_inferred') is False,
              'Normal image review inputs, source or separate functional result differ.', 'stale_review')
        for index, (point, frame) in enumerate(zip(row['frames'], manifest['frames'])):
            _need(point.get('index') == index and point.get('time_s') == frame['time_s']
                  and point.get('image_sha256') == manifest['raw_frame_hashes'][f'{index:06d}.png'],
                  'Normal review frame identities differ from the recorded input raster.', 'stale_review')
        contacts, tile_count, distinct_count, valid_count = _contacts(row['frames'], route['scenario']['events'])
        _need(contacts == row.get('selected_images') and tile_count == row.get('unique_tile_count')
              and distinct_count == row['distinct_image_count'] and valid_count == row['distinct_glyph_control_count'],
              'Normal raw controls omit input neighborhoods or differ from their exact frame/tile identities.')
        expected_sheets.extend(f"sheets/{route['id']}-{page:02}.png" for page in range((tile_count + 11) // 12))
    _need([sheet['path'] for sheet in aid['sheets']] == expected_sheets,
          'Normal contact sheets omit or replace part of the exact rendering plan.')
    archives = receipt.get('producer_archives', [])
    _need(len(archives) == len(receipt['code_sources'])
          and {row['origin'] for row in archives} == set(receipt['code_sources']), 'Producer closure archive incomplete.')
    for row in archives:
        path = safe_path(output, row['archive'])
        _need(path.is_relative_to(output / 'producer-sources') and row['sha256'] == receipt['code_sources'].get(row['origin'])
              and file_sha256(path) == row['sha256'], 'Normal producer archive changed.', 'stale_review')
    required_generated = {'result.json', 'authority.json'} | set(expected_sheets) | {row['archive'] for row in archives}
    _need(required_generated <= set(receipt['generated_files']), 'Normal result, authority, sheet or archive byte binding is missing.')
    for sheet in aid['sheets']:
        _need(sheet['sha256'] == receipt['generated_files'][sheet['path']], 'Declared normal sheet has another generated byte identity.')
    consumed = {**receipt['source_files'], **receipt['code_sources']}
    for relative, sha in receipt['generated_files'].items():
        _put(consumed, safe_path(output, relative), sha)
    for path, sha in list(consumed.items()):
        _need(file_sha256(path) == sha, 'Normal raw evidence or producer changed.', 'stale_review')
    for path in (output / 'evidence-receipt.json', output / 'attestation.json'):
        _put(consumed, path)
    return {'kind': 'VerifiedCanonicalGroundNormalRawInspection', 'schema_version': 1, 'version': VERSION,
        'status': 'pass', 'source_sha256': authority['source_sha256'], 'session_directory': authority['session'],
        'final_job_id': authority['final_job_id'], 'directory': str(output), 'result_sha256': digest(aid),
        'authority_sha256': digest(authority), 'parent_chain_sha256': parent_chain_sha256,
        'evidence_receipt_sha256': consumed[str(output / 'evidence-receipt.json')],
        'attestation_sha256': consumed[str(output / 'attestation.json')],
        'producer_source_sha256': digest(receipt['code_sources']),
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'mandatory_route': 'normal-combined', 'slope_route_is_diagnostic': True, 'record_count': 2, 'frame_count': 480,
        'mandatory_route_status': authority['normal_status'],
        'slope_diagnostic_status': authority['slope_diagnostic_status'],
        'standalone_validator_required': True, 'ordinary_reader_compatibility_claimed': False,
        'full_behavior_acceptance': False, 'historical_acceptance_changed': False, 'new_model_generation': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'attest', 'validate'))
    parser.add_argument('--session', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parent-chain', type=Path)
    parser.add_argument('--parent-chain-sha256')
    parser.add_argument('--reviewer')
    parser.add_argument('--inspection', type=Path, help='Actually viewed relative sheet paths: list[str].')
    parser.add_argument('--accepted', action='store_true')
    parser.add_argument('--images-inspected', action='store_true')
    args = parser.parse_args()
    if args.operation == 'attest':
        _need(args.reviewer and args.inspection, 'The actual reviewer and inspected path journal are required.')
        result = attest(args.output, reviewer=args.reviewer, inspected_sheets=read_json(args.inspection),
                        accepted=args.accepted, images_inspected=args.images_inspected)
    else:
        _need(args.session and args.parent_chain and args.parent_chain_sha256, 'Canonical session and trusted parent-chain pin required.')
        result = (prepare(args.session, args.output, parent_chain=args.parent_chain, parent_chain_sha256=args.parent_chain_sha256)
            if args.operation == 'prepare' else validate(args.output, session_directory=args.session,
                parent_chain=args.parent_chain, parent_chain_sha256=args.parent_chain_sha256))
    print(json.dumps({key: result.get(key) for key in ('kind', 'status', 'numeric_status', 'source_sha256',
        'record_count', 'frame_count', 'full_behavior_acceptance')}, ensure_ascii=False))
    if 'sheets' in result:
        print(json.dumps({'sheets': len(result['sheets']), 'failures': len(result.get('failures', []))}))


if __name__ == '__main__':
    main()
