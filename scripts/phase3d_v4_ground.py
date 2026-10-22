"""V4 ground stage: calibrate and freeze from the accepted V2 jump parent (PHASE3D_V4_PLAN.md).

Parent provenance is verified exactly as the V3 bridge does
(``scripts/phase3d_v3_ground_parent.py``): the V2 repair closure is checked
through the frozen V2 snapshot. Three things differ from the V3 path:

- the recipes come from the V4 registry (the V3 held-out ground recipes become
  known regression; r36, l42 and reverse30 are new);
- the suite carries the ``ground-floor-speed-aware-v1`` profile;
- the post-freeze checks verify V2-bound files through the snapshot and every
  other bound file in place (the correction made for v3g02).

``check`` runs every check against an existing freeze without writing to it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.media_suite import GROUND_SPEED_PROFILE, GROUND_SPEED_RULE
from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_ground_parent as v2
from scripts import phase3d_v2_prepare as prepare
from scripts import phase3d_v3_ground_parent as bridge

RECEIPT_KIND = 'V4GroundParentReceipt'
CHAIN_KIND = 'V4GroundParentChainReceipt'
ROUTE = 'v4-ground-freeze-v1'


def _need(condition, message, code='parent_not_accepted'):
    if not condition:
        raise VisualError(code, message)


def recipes():
    """V4 ground recipes plus the unchanged protected jump cases (as phase3d_prepare.recipes)."""
    from kiroclops.local_engine.suite_game import scenarios
    rows = [{'case_id': s['case_id'], 'role': split, 'stage': 'ground', 'purpose': 'primary', 'scenario': s}
            for split in ('calibration', 'regression', 'validation') for s in scenarios('ground', split, 'v4')]
    for split in ('calibration', 'regression', 'validation'):
        for s in scenarios('jump', split):
            rows.append({'case_id': s['case_id'], 'role': split, 'stage': 'jump', 'purpose': 'preservation',
                         'scenario': s})
    return rows


@contextmanager
def _v4_preparation(plan, output, epoch, redirected):
    """Bind the verified parent and the V4 recipes for this preparation only."""
    previous_sources, previous_recipes = prepare.sources, prepare.recipes

    def bound(stage, jump_session=None):
        _need(stage == 'ground' and jump_session is None, 'This preparation accepts the ground stage only.')
        v2.unchanged(plan)
        provenance = Path(output) / 'repair-parent'
        provenance.mkdir(parents=True, exist_ok=False)
        (provenance / 'producer.py').write_bytes(Path(__file__).read_bytes())
        write_json(provenance / 'parent.json', plan)
        write_json(provenance / 'snapshot-redirections.json', redirected)
        write_json(provenance / 'receipt.json', {
            'kind': RECEIPT_KIND, 'schema_version': 1,
            'parent_sha256': digest(plan), 'parent_file_sha256': file_sha256(provenance / 'parent.json'),
            'producer_sha256': file_sha256(provenance / 'producer.py'), 'bridge_sha256': file_sha256(bridge.__file__),
            'v2_bridge_sha256': file_sha256(v2.__file__), 'prepare_sha256': file_sha256(prepare.__file__),
            'snapshot_receipt_sha256': bridge.SNAPSHOT_RECEIPT_SHA256, 'snapshot_tree_sha256': epoch['source_tree_sha256'],
            'snapshot_redirections_sha256': file_sha256(provenance / 'snapshot-redirections.json'),
            'acceptance_profile': GROUND_SPEED_PROFILE, 'acceptance_rule': deepcopy(GROUND_SPEED_RULE),
            'recipes_registry': 'v4', 'historical_session_rewritten': False})
        return Path(plan['parent']), Path(plan['original']), deepcopy(plan['historical_candidate'])

    def v4_recipes(stage):
        _need(stage == 'ground', 'The V4 game recipes cover the ground stage only.', 'invalid_suite')
        return recipes()

    prepare.sources, prepare.recipes = bound, v4_recipes
    try:
        yield
    finally:
        prepare.sources, prepare.recipes = previous_sources, previous_recipes


def calibrate(identifier, root=Path('C:/kc3d')):
    plan, epoch, redirected = bridge.verify()
    output = _ordinary(root) / 'p' / identifier
    _need(not output.exists(), 'Preserve every previous ground attempt.', 'output_exists')
    with bridge.frozen_v2_sources(epoch), _v4_preparation(plan, output, epoch, redirected):
        try:
            result = prepare.calibrate('ground', identifier, root)
        finally:
            v2.unchanged(plan)
    binding_path = output / 'start-binding.json'
    binding = read_json(binding_path)
    binding.update(parent_origin_session=plan['parent_origin_session'],
                   original_comparison_session=plan['original_comparison_session'],
                   parent_acceptance_mode=plan['parent_acceptance_mode'],
                   repaired_parent_receipt=str(output / 'repair-parent' / 'receipt.json'),
                   acceptance_profile=GROUND_SPEED_PROFILE, v4_bridge=True, recipes_registry='v4',
                   historical_session_rewritten=False)
    write_json(binding_path, binding)
    return result


def _relative(name):
    try:
        return Path(name).resolve().relative_to(bridge.ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def preflight(previous):
    previous = _ordinary(previous)
    provenance = previous / 'repair-parent'
    receipt_path, plan_path = provenance / 'receipt.json', provenance / 'parent.json'
    _need(receipt_path.is_file() and plan_path.is_file(), 'The V4 parent preparation receipt is missing.')
    parent, receipt = read_json(plan_path), read_json(receipt_path)
    current, epoch, redirected = bridge.verify()
    _need(current == parent, 'The parent plan differs from current evidence.')
    _need(receipt.get('kind') == RECEIPT_KIND and receipt.get('schema_version') == 1
          and receipt.get('parent_sha256') == digest(parent) and receipt.get('historical_session_rewritten') is False
          and receipt.get('acceptance_profile') == GROUND_SPEED_PROFILE
          and receipt.get('acceptance_rule') == GROUND_SPEED_RULE and receipt.get('recipes_registry') == 'v4'
          and receipt.get('snapshot_receipt_sha256') == bridge.SNAPSHOT_RECEIPT_SHA256,
          'The V4 parent preparation receipt differs.')
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
    _need(receipt.get('producer_sha256') == file_sha256(__file__)
          and receipt.get('bridge_sha256') == file_sha256(bridge.__file__)
          and receipt.get('prepare_sha256') == file_sha256(prepare.__file__)
          and receipt.get('v2_bridge_sha256') == file_sha256(v2.__file__),
          'Use the exact inspected V4 preparation revision.', 'producer_changed')
    for path in (receipt_path, Path(__file__), Path(bridge.__file__), Path(prepare.__file__), Path(v2.__file__)):
        direct[str(_ordinary(path))] = file_sha256(path)
    binding = read_json(previous / 'start-binding.json')
    _need(binding.get('stage') == 'ground' and binding.get('model_started') is False
          and binding.get('historical_session_rewritten') is False and binding.get('v4_bridge') is True
          and binding.get('recipes_registry') == 'v4' and binding.get('acceptance_profile') == GROUND_SPEED_PROFILE
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
    _need(read_json(previous / 'recipes.json') == recipes(), 'The preparation recipes differ from the V4 registry.',
          'suite_recipe_mismatch')
    for name in ('start-binding.json', 'calibration.json', 'recipes.json'):
        direct[str(previous / name)] = file_sha256(previous / name)
    for name in set(snapshot_bound) & set(direct):
        _need(snapshot_bound[name] == direct[name], 'One file is bound with two identities: ' + name, 'evidence_changed')
        del snapshot_bound[name]
    return {'previous': previous, 'parent': parent, 'epoch': epoch, 'redirected': redirected,
            'snapshot_bound': snapshot_bound, 'direct': direct}


def post_checks(context, result):
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
          and result.get('model_started') is False and result.get('acceptance_profile') == GROUND_SPEED_PROFILE,
          'The freeze result is not a fresh V4 ground suite.', 'wrong_result')
    suite_path = _ordinary(result['suite']) / 'suite.json'
    suite = read_json(suite_path)
    state_path = _ordinary(result['session']) / 'session.json'
    state = read_json(state_path)
    _need(suite.get('stage') == 'ground' and suite.get('domain') == 'game'
          and suite.get('acceptance_profile') == GROUND_SPEED_PROFILE
          and suite.get('game_acceptance_rule') == GROUND_SPEED_RULE
          and digest(suite.get('authoring_parent_source_hashes')) == parent['parent_source_sha256']
          and digest(suite.get('original_source_hashes')) == parent['original_source_sha256']
          and state.get('experiment', {}).get('suite_sha256') == digest(suite)
          and state.get('started_at') is None and not state.get('candidates'),
          'The new V4 ground suite/session is not bound to the accepted jump parent and the V4 rule.', 'wrong_result')
    return {'suite_path': suite_path, 'suite': suite, 'state_path': state_path, 'state': state}


def write_chain(output, context, result, checked):
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
    chain = {'kind': CHAIN_KIND, 'schema_version': 1, 'status': 'pass', 'route': ROUTE,
             'parent': context['parent'], 'parent_sha256': digest(context['parent']),
             'preparation': str(context['previous']), 'session': result['session'], 'session_id': state['session_id'],
             'suite': result['suite'], 'suite_sha256': digest(suite), 'suite_file_sha256': file_sha256(suite_path),
             'suite_binding_sha256': file_sha256(suite_path.parent / 'suite-binding.json'),
             'source_epoch': suite.get('source_epoch'), 'acceptance_profile': GROUND_SPEED_PROFILE,
             'acceptance_rule': deepcopy(GROUND_SPEED_RULE), 'recipes_registry': 'v4',
             'v2_snapshot': {'receipt': str(bridge.SNAPSHOT_RECEIPT), 'receipt_sha256': bridge.SNAPSHOT_RECEIPT_SHA256,
                             'code_root': str(bridge.SNAPSHOT), 'tree_sha256': context['epoch']['source_tree_sha256'],
                             'redirected_files': context['redirected']},
             'snapshot_bound_files': sorted(context['snapshot_bound']),
             'initial_session_archive': str(snapshot), 'initial_session_archive_sha256': file_sha256(snapshot),
             'producer_archive': str(archive), 'producer_sha256': file_sha256(archive),
             'calibration_bridge_archive': str(bridge_archive),
             'calibration_bridge_sha256': file_sha256(bridge_archive), 'supersedes': None,
             'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
             'canonical_session_promoted': False, 'historical_session_rewritten': False,
             'ground_behavior_acceptance': False, 'model_started': False,
             'scope': 'Prospective V4 ground parent provenance; measurement, review and source acceptance remain separate.'}
    write_json(path, chain)
    return path


def freeze(previous, identifier, root=Path('C:/kc3d')):
    root = _ordinary(root)
    context = preflight(previous)
    _need(not (root / 'p' / identifier).exists(), 'Preserve every previous ground freeze.', 'output_exists')
    result = prepare.freeze(context['previous'], identifier, None, root, game_acceptance_profile=GROUND_SPEED_PROFILE)
    checked = post_checks(context, result)
    path = write_chain(root / 'p' / identifier, context, result, checked)
    return {**result, 'ground_parent_chain': str(path), 'ground_parent_chain_sha256': file_sha256(path)}


def check(previous, freeze_directory, trial_chain_output=None):
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
    calibration = sub.add_parser('calibrate')
    calibration.add_argument('--id', required=True)
    calibration.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    freezing = sub.add_parser('freeze')
    freezing.add_argument('--previous', type=Path, required=True)
    freezing.add_argument('--id', required=True)
    freezing.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    checking = sub.add_parser('check')
    checking.add_argument('--previous', type=Path, required=True)
    checking.add_argument('--freeze', type=Path, required=True)
    checking.add_argument('--trial-chain-output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'calibrate':
            full = calibrate(args.id, args.root)
            result = {key: full[key] for key in ('status', 'stage', 'output', 'model_started') if key in full}
        elif args.command == 'freeze':
            result = freeze(args.previous, args.id, args.root)
        else:
            result = check(args.previous, args.freeze, args.trial_chain_output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
