"""V3 ground final and normal-map raw reviews over the unchanged V2 consumers.

The V2 consumers bind a ``ProspectiveGroundParentChainReceipt``. The V3 bridge
(``scripts/phase3d_v3_ground_parent.py``) writes a ``V3GroundParentChainReceipt``
with the same provenance fields, plus:

- the floor-aware acceptance profile and its frozen rule;
- the V2 source snapshot used to verify the parent.

For one call at a time, this wrapper swaps only three things:

- the parent-chain reader;
- the consumer version names;
- the consumer source list.

Every review rule, record count, extraction and sheet renderer is the unchanged
V2 code. Outputs carry V3 version names, so the V2 validators reject them and
the V2 outputs keep their own meaning.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.media_suite import (GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE,
                                               GROUND_SPEED_RULE)
from kiroclops.local_engine.session import _ordinary
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json
from scripts import phase3d_v2_ground_final_raw as ground
from scripts import phase3d_v2_ground_normal_raw as normal
from scripts import phase3d_v3_ground_parent as bridge

FINAL_VERSION = 'v3-canonical-ground-static-wall-input-raw-v1'
NORMAL_VERSION = 'v3-canonical-ground-normal-raw-v1'
CHAIN_KIND = 'V3GroundParentChainReceipt'
# Each prospective parent chain kind is accepted only with its own frozen ground rule.
CHAIN_RULES = {CHAIN_KIND: (GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE),
               'V4GroundParentChainReceipt': (GROUND_SPEED_PROFILE, GROUND_SPEED_RULE)}
# Frozen canonical matrix and consumer version names per chain kind. V4 has 90 cases
# (60 ground, 30 protected jump) and 90 * 4 + 8 wall records.
CHAIN_CONSUMERS = {
    CHAIN_KIND: {'matrix': {'cases': 72, 'records': 296}, 'final': 'v3-canonical-ground-static-wall-input-raw-v1',
                 'normal': 'v3-canonical-ground-normal-raw-v1'},
    'V4GroundParentChainReceipt': {'matrix': {'cases': 90, 'records': 368},
                                   'final': 'v4-canonical-ground-static-wall-input-raw-v1',
                                   'normal': 'v4-canonical-ground-normal-raw-v1'},
}
_original = {'chain': ground._parent_chain, 'extra': ground._extra_sources,
             'final_version': ground.VERSION, 'normal_version': normal.VERSION, 'matrix': dict(ground.MATRIX),
             'chain_inputs': ground._chain_inputs}
from kiroclops.local_engine import suite_generic_raw_review as _generic  # noqa: E402
_original['anchor'] = _generic.game_anchor
_active = False


def _need(condition, message, code='wrong_parent_chain'):
    if not condition:
        raise VisualError(code, message)


def _check_consumed(value):
    """Files the V2 closure bound are checked against the frozen V2 snapshot; every other file in place."""
    files, bound = value.get('consumed_files'), value.get('snapshot_bound_files')
    _need(isinstance(files, dict) and files and isinstance(bound, list) and len(set(bound)) == len(bound)
          and set(bound) <= set(files), 'The chain must name which bound files are verified through the V2 snapshot.')
    if bound:
        snapshot = value.get('v2_snapshot') or {}
        _need(snapshot.get('receipt_sha256') == bridge.SNAPSHOT_RECEIPT_SHA256,
              'The chain names another V2 source snapshot.')
        epoch = bridge.verify_snapshot()
        _need(epoch['source_tree_sha256'] == snapshot.get('tree_sha256'), 'The V2 snapshot tree differs from the chain.')
        manifest, root = epoch['source_files'], bridge.ROOT.resolve()
        for name in bound:
            try:
                relative = Path(name).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                relative = None
            _need(relative in manifest and manifest[relative] == files[name],
                  'A snapshot-bound file differs from its frozen V2 bytes: ' + str(name))
    ground._check_files({name: sha for name, sha in files.items() if name not in set(bound)})


def _snapshot_chain_inputs(chain):
    """Chain inputs for the consumers' byte inventory: V2-bound sources resolve to their snapshot copies."""
    files, bound = chain['consumed_files'], set(chain.get('snapshot_bound_files', []))
    if not bound:
        return dict(files)
    manifest, root = bridge.verify_snapshot()['source_files'], bridge.ROOT.resolve()
    result = {}
    for name, sha in files.items():
        if name not in bound:
            result[name] = sha
            continue
        relative = Path(name).resolve().relative_to(root).as_posix()
        _need(manifest.get(relative) == sha, 'A snapshot-bound file differs from its frozen V2 bytes: ' + str(name))
        result[str(bridge.SNAPSHOT / relative)] = sha
    return result


def _parent_chain(path, expected_sha, context):
    """The V2 provenance checks, for the V3 receipt kind and its floor rule."""
    path = _ordinary(path)
    _need(isinstance(expected_sha, str) and len(expected_sha) == 64 and path.is_file()
          and file_sha256(path) == expected_sha, 'Supply the trusted V3 parent-chain byte identity.')
    value = read_json(path)
    profile, rule = CHAIN_RULES.get(value.get('kind'), (None, None))
    _need(profile is not None and value.get('schema_version') == 1 and value.get('status') == 'pass'
          and value.get('parent_sha256') == digest(value.get('parent'))
          and value.get('consumed_files_sha256') == digest(value.get('consumed_files'))
          and value.get('canonical_session_promoted') is False and value.get('historical_session_rewritten') is False
          and value.get('ground_behavior_acceptance') is False and value.get('model_started') is False
          and value.get('acceptance_profile') == profile
          and value.get('acceptance_rule') == rule,
          'The V3 accepted-code parent provenance or its floor rule is incomplete.')
    _need(_ordinary(value['session']) == _ordinary(context['directory'])
          and _ordinary(value['suite']) == _ordinary(context['suite'])
          and value.get('suite_sha256') == context['suite_sha256']
          and value['parent'].get('parent_stage') == 'jump' and value['parent'].get('stage') == 'ground'
          and value['parent'].get('parent_source_sha256') == digest(context['authoring_parent_source_hashes'])
          and value['parent'].get('original_source_sha256') == digest(context['original_source_hashes']),
          'Parent chain belongs to another ground seal, parent or original target.')
    _check_consumed(value)
    suite = _ordinary(context['suite'])
    archives = {str(suite / 'suite.json'): value['suite_file_sha256'],
                str(suite / 'suite-binding.json'): value['suite_binding_sha256'],
                value['initial_session_archive']: value['initial_session_archive_sha256'],
                value['producer_archive']: value['producer_sha256']}
    if value.get('calibration_bridge_archive') is not None:
        archives[value['calibration_bridge_archive']] = value.get('calibration_bridge_sha256')
    ground._check_files(archives)
    _need(read_json(suite / 'suite.json').get('acceptance_profile') == profile,
          'The sealed suite does not carry the floor-aware ground profile.')
    initial = read_json(value['initial_session_archive'])
    state = read_json(_ordinary(context['directory']) / 'session.json')
    _need(initial.get('session_id') == value.get('session_id') == state.get('session_id')
          and initial.get('started_at') is None and not initial.get('candidates')
          and initial.get('experiment', {}).get('suite_sha256') == context['suite_sha256'],
          'The archived fresh session intent differs.')
    return {'path': str(path), 'file_sha256': expected_sha, 'parent_sha256': value['parent_sha256'],
            'historical_parent_acceptance_mode': value['parent']['parent_acceptance_mode'],
            'canonical_history_promoted': False, 'chain_kind': value['kind'],
            'acceptance_profile': profile}


def _extra_sources():
    from scripts import phase3d_v2_native_release_identity as identity
    from scripts import phase3d_v4_native_environment as environment
    from scripts import prepare_game_raw_review_aid_v4 as anchor_v4
    files = (__file__, identity.__file__, environment.__file__, anchor_v4.__file__)
    return _original['extra']() | {str(Path(f).resolve()): file_sha256(f) for f in files}


@contextmanager
def _v3(kind=CHAIN_KIND):
    global _active
    _need(not _active, 'Use one V3 ground raw controller per process.', 'controller_active')
    consumer = CHAIN_CONSUMERS.get(kind)
    _need(consumer is not None, 'Unknown prospective parent chain kind.')
    _active = True
    ground._parent_chain, ground._extra_sources = _parent_chain, _extra_sources
    ground.VERSION, normal.VERSION = consumer['final'], consumer['normal']
    ground.MATRIX = dict(consumer['matrix'])
    ground._chain_inputs = _snapshot_chain_inputs
    if kind == 'V4GroundParentChainReceipt':
        from kiroclops.local_engine import suite_generic_raw_review as generic
        from scripts import prepare_game_raw_review_aid_v4 as anchor_v4
        generic.game_anchor = anchor_v4.anchor
    try:
        yield
    finally:
        from kiroclops.local_engine import suite_generic_raw_review as generic
        generic.game_anchor = _original['anchor']
        ground._chain_inputs = _original['chain_inputs']
        ground._parent_chain, ground._extra_sources = _original['chain'], _original['extra']
        ground.VERSION, normal.VERSION = _original['final_version'], _original['normal_version']
        ground.MATRIX = dict(_original['matrix'])
        _active = False


def _chain_kind(path):
    return read_json(path).get('kind') if path else CHAIN_KIND


@contextmanager
def _native_scope(kind, session):
    """The raw consumers replay native producer evidence: use the same mappings as the source review.

    Every prospective ground run uses Kiro CLI 2.27.1 (release identity mapping); the V4 runs
    also ran in the receipted one-server MCP environment.
    """
    from scripts import phase3d_v2_native_release_identity as identity
    from contextlib import nullcontext
    if kind == 'V4GroundParentChainReceipt':
        from scripts import phase3d_v4_native_environment as environment
        scope = environment.environment_policy([session])
    else:
        scope = nullcontext()
    with scope, identity.release_identity_policy():
        yield


def run(review, operation, **kwargs):
    module = ground if review == 'final' else normal
    kind = _chain_kind(kwargs.get('parent_chain'))
    if operation == 'attest':
        with _v3(kind):
            return module.attest(kwargs['output'], reviewer=kwargs['reviewer'], inspected_sheets=kwargs['inspected_sheets'],
                                 accepted=kwargs['accepted'], images_inspected=kwargs['images_inspected'])
    with _v3(kind), _native_scope(kind, kwargs['session']):
        if operation == 'prepare':
            return module.prepare(kwargs['session'], kwargs['output'], parent_chain=kwargs['parent_chain'],
                                  parent_chain_sha256=kwargs['parent_chain_sha256'])
        if operation == 'validate':
            return module.validate(kwargs['output'], session_directory=kwargs['session'],
                                   parent_chain=kwargs['parent_chain'], parent_chain_sha256=kwargs['parent_chain_sha256'])
        return module.attest(kwargs['output'], reviewer=kwargs['reviewer'], inspected_sheets=kwargs['inspected_sheets'],
                             accepted=kwargs['accepted'], images_inspected=kwargs['images_inspected'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('review', choices=('final', 'normal'))
    parser.add_argument('operation', choices=('prepare', 'validate', 'attest'))
    parser.add_argument('--session', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parent-chain', type=Path)
    parser.add_argument('--parent-chain-sha256')
    parser.add_argument('--reviewer')
    parser.add_argument('--inspection', type=Path, help='Actually viewed relative sheet paths: list[str].')
    parser.add_argument('--accepted', action='store_true')
    parser.add_argument('--images-inspected', action='store_true')
    args = parser.parse_args(argv)
    if args.operation == 'attest':
        _need(args.reviewer and args.inspection, 'The actual reviewer and inspected path journal are required.',
              'incomplete_visual_review')
        result = run(args.review, 'attest', output=args.output, reviewer=args.reviewer,
                     inspected_sheets=read_json(args.inspection), accepted=args.accepted,
                     images_inspected=args.images_inspected)
    else:
        _need(args.session and args.parent_chain and args.parent_chain_sha256,
              'The sealed session and trusted V3 parent-chain pin are required.')
        result = run(args.review, args.operation, session=args.session, output=args.output,
                     parent_chain=args.parent_chain, parent_chain_sha256=args.parent_chain_sha256)
    print(json.dumps({key: result.get(key) for key in ('kind', 'status', 'numeric_status', 'record_count',
                                                      'consumer_version', 'frame_count', 'full_behavior_acceptance')},
                     ensure_ascii=False))
    if 'sheets' in result:
        print(json.dumps({'sheets': len(result['sheets']), 'failures': len(result.get('failures', []))}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
