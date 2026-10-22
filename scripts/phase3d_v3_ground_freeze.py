"""Corrected V3 ground freeze for a preparation made by ``phase3d_v3_ground_parent.py calibrate``.

What happened to the first attempt:

- The bridge's own freeze of ``C:/kc3d/p/v3g01-cal`` wrote to ``C:/kc3d/p/v3g01``.
- It captured every private reference and created a suite and session.
- It then failed its final provenance re-check. That check sent the current V3a
  preparation script through the V2 snapshot redirect, which knows only V2
  bytes, and ``phase3d_v2_prepare.py`` gained the ground profile argument in V3a
  on purpose.
- No chain receipt was written. The attempt is preserved unchanged.

The calibration receipt pins the bridge's bytes, so the bridge cannot be
corrected in place. This module repeats every bridge check and keeps two kinds
of bound files apart:

- ``snapshot_bound_files``: repository sources that the V2 repair closure bound.
  They are verified against the frozen V2 snapshot, exactly as the bridge does.
- Every other bound file, including the current V3 producer and preparation
  scripts, is verified in place.

``check`` runs every pre- and post-freeze check against an existing freeze
without writing to it. It can also write a trial chain receipt to a separate
folder for consumer tests.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.media_suite import GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE
from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_ground_parent as v2
from scripts import phase3d_v2_prepare as prepare
from scripts import phase3d_v3_ground_parent as bridge

ROUTE = 'v3-ground-freeze-corrected-post-check-v1'


def _need(condition, message, code='parent_not_accepted'):
    if not condition:
        raise VisualError(code, message)


def _relative(name):
    try:
        return Path(name).resolve().relative_to(bridge.ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def preflight(previous):
    """Every pre-freeze check of ``bridge.freeze``, with the bound files split by how they are verified."""
    previous = _ordinary(previous)
    provenance = previous / 'repair-parent'
    receipt_path, plan_path = provenance / 'receipt.json', provenance / 'parent.json'
    _need(receipt_path.is_file() and plan_path.is_file(), 'The V3 parent preparation receipt is missing.')
    parent, receipt = read_json(plan_path), read_json(receipt_path)
    current, epoch, redirected = bridge.verify()
    _need(current == parent, 'The parent plan differs from current evidence.')
    _need(receipt.get('kind') == 'V3GroundParentReceipt' and receipt.get('schema_version') == 1
          and receipt.get('parent_sha256') == digest(parent) and receipt.get('historical_session_rewritten') is False
          and receipt.get('acceptance_profile') == GROUND_FLOOR_PROFILE
          and receipt.get('acceptance_rule') == GROUND_FLOOR_RULE
          and receipt.get('snapshot_receipt_sha256') == bridge.SNAPSHOT_RECEIPT_SHA256,
          'The V3 parent preparation receipt differs.')
    manifest = epoch['source_files']
    snapshot_bound, direct = {}, {}
    for name, sha in parent['consumed_files'].items():
        (snapshot_bound if _relative(name) in manifest else direct)[name] = sha
    with bridge.frozen_v2_sources(epoch):
        v2._file(plan_path, receipt.get('parent_file_sha256'), direct)
        v2._file(provenance / 'producer.py', receipt.get('producer_sha256'), direct)
        v2._file(provenance / 'snapshot-redirections.json', receipt.get('snapshot_redirections_sha256'), direct)
    _need(read_json(provenance / 'snapshot-redirections.json') == redirected,
          'The snapshot redirections changed since calibration.', 'evidence_changed')
    _need(receipt.get('producer_sha256') == file_sha256(bridge.__file__)
          and receipt.get('prepare_sha256') == file_sha256(prepare.__file__)
          and receipt.get('v2_bridge_sha256') == file_sha256(v2.__file__),
          'Use the exact inspected bridge and preparation revision.', 'producer_changed')
    for path in (receipt_path, Path(bridge.__file__), Path(__file__), Path(prepare.__file__), Path(v2.__file__)):
        direct[str(_ordinary(path))] = file_sha256(path)
    binding = read_json(previous / 'start-binding.json')
    _need(binding.get('stage') == 'ground' and binding.get('model_started') is False
          and binding.get('historical_session_rewritten') is False and binding.get('v3_bridge') is True
          and binding.get('acceptance_profile') == GROUND_FLOOR_PROFILE
          and binding.get('parent_origin_session') == parent['parent_origin_session']
          and binding.get('original_comparison_session') == parent['original_comparison_session']
          and binding.get('parent_acceptance_mode') == parent['parent_acceptance_mode']
          and _ordinary(binding['repaired_parent_receipt']) == receipt_path
          and binding.get('historical_candidate') == parent['historical_candidate'],
          'Ground calibration source/repair identity differs.')
    _need(binding.get('parent_source_sha256') == parent['parent_source_sha256']
          == digest(source_hashes(binding['parent']))
          and binding.get('original_source_sha256') == parent['original_source_sha256']
          == digest(source_hashes(binding['original'])), 'The ground preparation starting sources differ.', 'source_changed')
    for name in ('start-binding.json', 'calibration.json', 'recipes.json'):
        direct[str(previous / name)] = file_sha256(previous / name)
    for name in set(snapshot_bound) & set(direct):
        _need(snapshot_bound[name] == direct[name], 'One file is bound with two identities: ' + name, 'evidence_changed')
        # Unchanged since V2: checking the live bytes is the stricter of the two.
        del snapshot_bound[name]
    return {'previous': previous, 'parent': parent, 'epoch': epoch, 'redirected': redirected,
            'snapshot_bound': snapshot_bound, 'direct': direct}


def post_checks(context, result):
    """The bridge's post-freeze checks, with each bound file verified the way it was bound."""
    with bridge.frozen_v2_sources(context['epoch']):
        for name, sha in context['snapshot_bound'].items():
            v2._file(name, sha, {})
        v2.unchanged(context['parent'])
    for name, sha in context['direct'].items():
        path = _ordinary(name)
        _need(path.is_file() and file_sha256(path) == sha, 'Bound evidence changed or is missing: ' + name,
              'evidence_changed')
    parent = context['parent']
    _need(result.get('status') == 'pending_admission' and result.get('stage') == 'ground'
          and result.get('model_started') is False and result.get('acceptance_profile') == GROUND_FLOOR_PROFILE,
          'The freeze result is not a fresh floor-aware ground suite.', 'wrong_result')
    suite_path = _ordinary(result['suite']) / 'suite.json'
    suite = read_json(suite_path)
    state_path = _ordinary(result['session']) / 'session.json'
    state = read_json(state_path)
    _need(suite.get('stage') == 'ground' and suite.get('domain') == 'game'
          and suite.get('acceptance_profile') == GROUND_FLOOR_PROFILE
          and suite.get('game_acceptance_rule') == GROUND_FLOOR_RULE
          and digest(suite.get('authoring_parent_source_hashes')) == parent['parent_source_sha256']
          and digest(suite.get('original_source_hashes')) == parent['original_source_sha256']
          and state.get('experiment', {}).get('suite_sha256') == digest(suite)
          and state.get('started_at') is None and not state.get('candidates'),
          'The new V3 ground suite/session is not bound to the accepted jump parent and floor rule.', 'wrong_result')
    return {'suite_path': suite_path, 'suite': suite, 'state_path': state_path, 'state': state}


def _supersedes(report):
    if report is None:
        return None
    report = _ordinary(report)
    value = read_json(report)
    _need(value.get('kind') == 'V3GroundFreezeFailureDiagnostic' and value.get('session_started') is False,
          'A superseded attempt needs its failure diagnostic.', 'invalid_supersedes')
    return {'report': str(report), 'report_sha256': file_sha256(report), 'freeze': value['freeze'],
            'session': value['session'], 'failure': value['failure'], 'preserved': True}


def write_chain(output, context, result, checked, supersedes=None):
    output = _ordinary(output)
    path = output / 'ground-parent-chain.json'
    _need(not path.exists(), 'Preserve every earlier chain receipt.', 'output_exists')
    archive = output / 'ground-parent-producer.py'
    archive.write_bytes(Path(__file__).read_bytes())
    bridge_archive = output / 'ground-parent-calibration-bridge.py'
    bridge_archive.write_bytes(Path(bridge.__file__).read_bytes())
    snapshot = output / 'ground-parent-initial-session.json'
    snapshot.write_bytes(checked['state_path'].read_bytes())
    consumed = {**context['snapshot_bound'], **context['direct']}
    suite, state, suite_path = checked['suite'], checked['state'], checked['suite_path']
    chain = {'kind': 'V3GroundParentChainReceipt', 'schema_version': 1, 'status': 'pass', 'route': ROUTE,
             'parent': context['parent'], 'parent_sha256': digest(context['parent']),
             'preparation': str(context['previous']), 'session': result['session'], 'session_id': state['session_id'],
             'suite': result['suite'], 'suite_sha256': digest(suite), 'suite_file_sha256': file_sha256(suite_path),
             'suite_binding_sha256': file_sha256(suite_path.parent / 'suite-binding.json'),
             'source_epoch': suite.get('source_epoch'), 'acceptance_profile': GROUND_FLOOR_PROFILE,
             'acceptance_rule': deepcopy(GROUND_FLOOR_RULE),
             'v2_snapshot': {'receipt': str(bridge.SNAPSHOT_RECEIPT), 'receipt_sha256': bridge.SNAPSHOT_RECEIPT_SHA256,
                             'code_root': str(bridge.SNAPSHOT), 'tree_sha256': context['epoch']['source_tree_sha256'],
                             'redirected_files': context['redirected']},
             'snapshot_bound_files': sorted(context['snapshot_bound']),
             'initial_session_archive': str(snapshot), 'initial_session_archive_sha256': file_sha256(snapshot),
             'producer_archive': str(archive), 'producer_sha256': file_sha256(archive),
             'calibration_bridge_archive': str(bridge_archive),
             'calibration_bridge_sha256': file_sha256(bridge_archive), 'supersedes': supersedes,
             'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
             'canonical_session_promoted': False, 'historical_session_rewritten': False,
             'ground_behavior_acceptance': False, 'model_started': False,
             'scope': 'Prospective V3 ground parent provenance; measurement, review and source acceptance remain separate.'}
    write_json(path, chain)
    return path


def freeze(previous, identifier, root=Path('C:/kc3d'), supersedes_report=None):
    root = _ordinary(root)
    context = preflight(previous)
    supersedes = _supersedes(supersedes_report)
    _need(not (root / 'p' / identifier).exists(), 'Preserve every previous ground freeze.', 'output_exists')
    result = prepare.freeze(context['previous'], identifier, None, root, game_acceptance_profile=GROUND_FLOOR_PROFILE)
    checked = post_checks(context, result)
    path = write_chain(root / 'p' / identifier, context, result, checked, supersedes)
    return {**result, 'ground_parent_chain': str(path), 'ground_parent_chain_sha256': file_sha256(path)}


def check(previous, freeze_directory, trial_chain_output=None):
    """Every check against an existing freeze; never writes into it."""
    context = preflight(previous)
    freeze_directory = _ordinary(freeze_directory)
    result = read_json(freeze_directory / 'result.json')
    _need(_ordinary(result['suite']) == freeze_directory / 'suite', 'The freeze result belongs to another folder.',
          'wrong_result')
    checked = post_checks(context, result)
    summary = {'status': 'pass', 'freeze': str(freeze_directory), 'session': result['session'],
               'suite_sha256': digest(checked['suite']), 'snapshot_bound_files': len(context['snapshot_bound']),
               'direct_files': len(context['direct']), 'session_started': False, 'writes_to_freeze': False}
    if trial_chain_output is not None:
        trial = Path(trial_chain_output)
        _need(not trial.exists() and not _ordinary(trial).is_relative_to(freeze_directory),
              'Write a trial chain to a new folder outside the freeze.', 'output_exists')
        trial.mkdir(parents=True)
        path = write_chain(trial, context, result, checked)
        summary.update(trial_chain=str(path), trial_chain_sha256=file_sha256(path))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest='command', required=True)
    freezing = sub.add_parser('freeze')
    freezing.add_argument('--previous', type=Path, required=True)
    freezing.add_argument('--id', required=True)
    freezing.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    freezing.add_argument('--supersedes-report', type=Path)
    checking = sub.add_parser('check')
    checking.add_argument('--previous', type=Path, required=True)
    checking.add_argument('--freeze', type=Path, required=True)
    checking.add_argument('--trial-chain-output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'freeze':
            result = freeze(args.previous, args.id, args.root, args.supersedes_report)
        else:
            result = check(args.previous, args.freeze, args.trial_chain_output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
