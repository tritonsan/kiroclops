"""Fresh canonical-ground raw review with static wall and public input proofs.

This controller leaves frozen core files and historical review outcomes intact.
It measures every current sealed recording afresh. A separate, versioned
validator replays the canonical seal and exact review bytes; ordinary historical
review readers are not claimed to understand the controller extension.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import suite_final_raw_review as final, suite_generic_raw_review as raw
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.package import frame_paths, read_image
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_game_wall_review as wall
from scripts import raw_game_input_episode_review as episodes

VERSION = 'canonical-ground-static-wall-input-raw-v1'
# The complete canonical matrix: suite cases and raw records (four roles per case plus the
# eight wall recordings). Later epochs set their own frozen counts through their wrapper.
MATRIX = {'cases': 72, 'records': 296}


def _chain_inputs(chain):
    """Files the parent chain bound; a later epoch's wrapper maps V2-snapshot-bound sources."""
    return dict(chain['consumed_files'])
_active = False


def _need(condition, message, code='incomplete_review'):
    if not condition:
        raise VisualError(code, message)


def _extra_sources():
    return {str(Path(path).resolve()): file_sha256(path)
            for path in (__file__, wall.__file__, episodes.__file__)}


def _check_files(files):
    _need(isinstance(files, dict) and files, 'A nonempty byte evidence inventory is required.')
    for name, sha in files.items():
        path = _ordinary(name)
        _need(path.is_file() and file_sha256(path) == sha, 'Bound evidence changed: ' + name, 'stale_review')


def _parent_chain(path, expected_sha, context):
    path = _ordinary(path)
    _need(isinstance(expected_sha, str) and len(expected_sha) == 64 and path.is_file()
          and file_sha256(path) == expected_sha, 'Supply the trusted prospective parent-chain byte identity.', 'wrong_parent_chain')
    value = read_json(path)
    _need(value.get('kind') == 'ProspectiveGroundParentChainReceipt' and value.get('schema_version') == 1
          and value.get('status') == 'pass' and value.get('parent_sha256') == digest(value.get('parent'))
          and value.get('consumed_files_sha256') == digest(value.get('consumed_files'))
          and value.get('canonical_session_promoted') is False and value.get('historical_session_rewritten') is False
          and value.get('ground_behavior_acceptance') is False and value.get('model_started') is False,
          'The prospective accepted-code parent provenance is incomplete.', 'wrong_parent_chain')
    _need(_ordinary(value['session']) == _ordinary(context['directory'])
          and _ordinary(value['suite']) == _ordinary(context['suite'])
          and value.get('suite_sha256') == context['suite_sha256']
          and value['parent'].get('parent_stage') == 'jump'
          and value['parent'].get('stage') == 'ground'
          and value['parent'].get('parent_source_sha256') == digest(context['authoring_parent_source_hashes'])
          and value['parent'].get('original_source_sha256') == digest(context['original_source_hashes']),
          'Parent chain belongs to another ground seal, parent or original target.', 'wrong_parent_chain')
    _check_files(value['consumed_files'])
    suite = _ordinary(context['suite'])
    _check_files({str(suite / 'suite.json'): value['suite_file_sha256'],
        str(suite / 'suite-binding.json'): value['suite_binding_sha256'],
        value['initial_session_archive']: value['initial_session_archive_sha256'],
        value['producer_archive']: value['producer_sha256']})
    initial = read_json(value['initial_session_archive'])
    state = read_json(_ordinary(context['directory']) / 'session.json')
    _need(initial.get('session_id') == value.get('session_id') == state.get('session_id') and initial.get('started_at') is None
          and not initial.get('candidates') and initial.get('experiment', {}).get('suite_sha256') == context['suite_sha256'],
          'The archived fresh session intent differs.', 'wrong_parent_chain')
    return {'path': str(path), 'file_sha256': expected_sha, 'parent_sha256': value['parent_sha256'],
            'historical_parent_acceptance_mode': value['parent']['parent_acceptance_mode'],
            'canonical_history_promoted': False}


def _extract_static_wall(bundle, output, inventory):
    """Image-only finite face before production observations are opened."""
    record, manifest = bundle['record'], bundle['manifest']
    paths = frame_paths(record['package'], manifest)
    images = [read_image(path) for path in paths]
    geometry_error = None
    try:
        geometry = wall.static_wall(images, [frame['time_s'] for frame in manifest['frames']],
                                    manifest['inputs'], record['scenario']['wall_side'])
    except VisualError as exc:
        # Unobservable/unstable geometry stays an explicit raw failure. A point
        # is never synthesized and the ordinary per-frame relation gate fails.
        geometry = {'status': 'unobservable', 'method': 'public_preinput_static_contrast_density'}
        geometry_error = {'code': exc.code, 'message': str(exc)}
    for row, image, path in zip(bundle['raw']['rows'], images, paths):
        supported, count = wall._supported(image, geometry, row['points'].get('character')) if 'x' in geometry else (False, 0)
        row['wall'] = ({**geometry, 'frame_support_pixels': count} if supported else
                       {'status': 'unobservable', 'method': geometry['method']})
        row['connections'].pop('independent_wall_edge', None)
        if supported:
            row['connections']['independent_wall_edge'] = {
                'start': [geometry['x'], geometry['top']], 'end': [geometry['x'], geometry['bottom']]}
        _need(file_sha256(path) == row['image_sha256'], 'A raw wall image changed during extraction.', 'stale_review')
    write_json(Path(output) / 'raw' / record['id'] / 'points.json', {
        'rows': bundle['raw']['rows'], 'package_sha256': manifest['package_sha256'],
        'production_coordinates_used': False, 'production_events_used': False,
        'target_code_or_dom_read': False, 'status': 'unsigned',
        'wall_geometry_error': geometry_error, 'wall_geometry': geometry})


@contextmanager
def _controller(parent_chain, parent_chain_sha256):
    """Dedicated process-local semantics, restored even after any failure."""
    global _active
    _need(not _active, 'Use one independent ground controller per process.', 'controller_active')
    original = {'version': final.VERSION, 'context': final._context_session,
        'sources': final._code_sources, 'extract': final._extract_boundary,
        'raw_events': raw._raw_events, 'compare': raw._compare_events}
    _active = True
    try:
        def context(directory):
            value, rows = original['context'](directory)
            _need(value.get('domain') == 'game' and value.get('stage') == 'ground'
                  and value.get('kind') == 'SealedSessionFinalRawMatrix'
                  and len(value.get('cases', [])) == MATRIX['cases'] and len(rows) == MATRIX['records'],
                  'This consumer requires the complete canonical ground and protected-jump matrix.', 'wrong_final_seal')
            value['independent_consumer_version'] = VERSION
            value['prospective_parent_chain'] = _parent_chain(parent_chain, parent_chain_sha256, value)
            return value, rows

        def sources():
            return original['sources']() | _extra_sources()

        def raw_events(bundle, contract):
            events, relations = original['raw_events'](bundle, contract)
            _need(bundle['record']['domain'] == 'game' and len(bundle['mapping']) == 1,
                  'Ground raw input semantics need the single independently observed game object.')
            filtered = episodes.filter_raw_events(bundle['rows'], events, bundle['manifest']['inputs'],
                object_id=next(iter(bundle['mapping'])), rest_duration_s=contract['rest_duration_s'],
                rest_radius_px=contract['rest_radius_px'], max_gap_s=contract['max_gap_s'])
            bundle['independent_public_input_episode_review'] = filtered
            return filtered['events'], relations

        def compare(bundle, contract):
            compared, failures = original['compare'](bundle, contract)
            filtered = bundle['independent_public_input_episode_review']
            failures.extend(wall._unsupported_failures(bundle['record']['id'], filtered))
            if filtered['public_input_provenance'] != 'recorder_verified':
                failures.append({'code': 'raw_input_history_not_recorder_verified',
                    'record_id': bundle['record']['id'], 'provenance': filtered['public_input_provenance']})
            compared['public_input_episode_review'] = filtered
            return compared, failures

        final.VERSION, final._context_session = VERSION, context
        final._code_sources, final._extract_boundary = sources, _extract_static_wall
        raw._raw_events, raw._compare_events = raw_events, compare
        yield
    finally:
        final.VERSION, final._context_session = original['version'], original['context']
        final._code_sources, final._extract_boundary = original['sources'], original['extract']
        raw._raw_events, raw._compare_events = original['raw_events'], original['compare']
        _active = False


def prepare(session_directory, output, *, parent_chain, parent_chain_sha256):
    session_directory, output = map(_ordinary, (session_directory, output))
    extra = _extra_sources()
    with _controller(parent_chain, parent_chain_sha256):
        result = final.review_session(session_directory, output)
    # Archive the exact added semantics; every ordinary and extended helper is
    # also byte-pinned by the normal final aid code_sources receipt.
    archives = []
    for number, (origin, sha) in enumerate(extra.items()):
        target = output / 'consumer-sources' / f'{number:02}-{Path(origin).name}'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(origin).read_bytes())
        _need(file_sha256(target) == sha, 'A consumer source changed during archival.', 'stale_review')
        archives.append({'origin': origin, 'sha256': sha, 'archive': str(target)})
    _need(extra == _extra_sources(), 'Independent consumer sources changed.', 'stale_review')
    consumer = {'kind': 'CanonicalGroundRawConsumerBinding', 'schema_version': 1, 'version': VERSION,
        'session_directory': str(session_directory), 'parent_chain': str(_ordinary(parent_chain)),
        'parent_chain_sha256': parent_chain_sha256, 'extra_sources': extra, 'archives': archives,
        'record_count': len(result['records']), 'fresh_all_current_records': True,
        'historical_measurement_reuse': False, 'ordinary_reader_compatibility_claimed': False,
        'full_behavior_acceptance': False, 'new_model_generation': False,
        'normal_combined_route_is_separate_gate': True}
    write_json(output / 'consumer-binding.json', consumer)
    receipt = read_json(output / 'evidence-receipt.json')
    receipt['consumer_binding_sha256'] = digest(consumer)
    receipt['generated_files'] = {path.relative_to(output).as_posix(): file_sha256(path)
        for path in sorted(output.rglob('*')) if path.is_file() and path != output / 'evidence-receipt.json'}
    write_json(output / 'evidence-receipt.json', receipt)
    return result


def attest(output, *, reviewer, inspected_sheets, accepted, images_inspected=False):
    return final.attest_review(output, reviewer=reviewer, inspected_sheets=inspected_sheets,
                              accepted=accepted, images_inspected=images_inspected)


def validate(output, *, session_directory, parent_chain, parent_chain_sha256):
    output, session_directory = map(_ordinary, (output, session_directory))
    consumer, receipt = (read_json(output / name) for name in ('consumer-binding.json', 'evidence-receipt.json'))
    _need(consumer.get('kind') == 'CanonicalGroundRawConsumerBinding' and consumer.get('schema_version') == 1
          and consumer.get('version') == VERSION and consumer.get('record_count') == MATRIX['records']
          and consumer.get('fresh_all_current_records') is True and consumer.get('historical_measurement_reuse') is False
          and consumer.get('full_behavior_acceptance') is False and consumer.get('new_model_generation') is False
          and _ordinary(consumer['session_directory']) == session_directory
          and _ordinary(consumer['parent_chain']) == _ordinary(parent_chain)
          and consumer.get('parent_chain_sha256') == parent_chain_sha256
          and receipt.get('consumer_binding_sha256') == digest(consumer)
          and consumer.get('extra_sources') == _extra_sources(), 'The independent ground consumer identity differs.', 'stale_review')
    _check_files(consumer['extra_sources'])
    for item in consumer.get('archives', []):
        _need(consumer['extra_sources'].get(item.get('origin')) == item.get('sha256'), 'A producer archive belongs to another source.')
        _need(_ordinary(item['archive']).is_relative_to(output / 'consumer-sources'),
              'A producer archive must remain inside this review output.', 'unsafe_path')
        _check_files({item['archive']: item['sha256']})
    _need(len(consumer.get('archives', [])) == len(consumer['extra_sources'])
          and {item['origin'] for item in consumer['archives']} == set(consumer['extra_sources']),
          'An added consumer source archive is missing or duplicated.')
    with _controller(parent_chain, parent_chain_sha256):
        proof = final.validate_review(output, session_directory=session_directory)
    # The source audit can replay this inventory without treating the custom
    # receipt as an ordinary historical reader's accepted finalization.
    chain = read_json(parent_chain)
    _need(file_sha256(parent_chain) == parent_chain_sha256,
          'The trusted parent chain changed during final validation.', 'stale_review')
    consumed = {**_chain_inputs(chain), **receipt['source_files'], **receipt['code_sources'],
        **consumer['extra_sources'],
        **{str(safe_path(output, name)): sha for name, sha in receipt['generated_files'].items()},
        **{item['archive']: item['sha256'] for item in consumer['archives']},
        str(_ordinary(parent_chain)): parent_chain_sha256,
        str(output / 'consumer-binding.json'): file_sha256(output / 'consumer-binding.json'),
        str(output / 'evidence-receipt.json'): file_sha256(output / 'evidence-receipt.json'),
        str(output / 'attestation.json'): file_sha256(output / 'attestation.json'),
        str(_ordinary(chain['suite']) / 'suite.json'): chain['suite_file_sha256'],
        str(_ordinary(chain['suite']) / 'suite-binding.json'): chain['suite_binding_sha256'],
        chain['initial_session_archive']: chain['initial_session_archive_sha256'],
        chain['producer_archive']: chain['producer_sha256']}
    return {**proof, 'kind': 'VerifiedCanonicalGroundRawInspection', 'consumer_version': VERSION,
        'parent_chain_sha256': parent_chain_sha256, 'consumer_binding_sha256': digest(consumer),
        'consumer_binding_file_sha256': consumed[str(output / 'consumer-binding.json')],
        'evidence_receipt_sha256': consumed[str(output / 'evidence-receipt.json')],
        'producer_source_sha256': digest(consumer['extra_sources']),
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'standalone_validator_required': True, 'ordinary_reader_compatibility_claimed': False,
        'full_behavior_acceptance': False, 'normal_combined_route_is_separate_gate': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'validate', 'attest'))
    parser.add_argument('--session', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parent-chain', type=Path)
    parser.add_argument('--parent-chain-sha256')
    parser.add_argument('--reviewer')
    parser.add_argument('--inspection', type=Path)
    parser.add_argument('--accepted', action='store_true')
    parser.add_argument('--images-inspected', action='store_true')
    args = parser.parse_args()
    if args.operation == 'attest':
        _need(args.reviewer and args.inspection, 'Actual inspector and viewed-image journal are required.')
        result = attest(args.output, reviewer=args.reviewer, inspected_sheets=read_json(args.inspection),
                        accepted=args.accepted, images_inspected=args.images_inspected)
    else:
        _need(args.session and args.parent_chain and args.parent_chain_sha256, 'The sealed session and trusted parent chain are required.')
        result = (prepare(args.session, args.output, parent_chain=args.parent_chain,
                          parent_chain_sha256=args.parent_chain_sha256) if args.operation == 'prepare' else
                  validate(args.output, session_directory=args.session, parent_chain=args.parent_chain,
                           parent_chain_sha256=args.parent_chain_sha256))
    print(json.dumps({key: result.get(key) for key in ('kind', 'status', 'numeric_status', 'record_count',
        'consumer_version', 'full_behavior_acceptance')}, ensure_ascii=False))
    if 'failures' in result:
        print(json.dumps({'failures': len(result['failures']), 'sheets': len(result['sheets'])}))


if __name__ == '__main__':
    main()
