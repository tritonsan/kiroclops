"""Start the V3 ground stage from the accepted V2 jump parent.

The V2 repair closure (C:/kc3d/a/v2-repair-closure-01) byte-binds repository
source files by absolute path. The user-approved V3 source epoch edits some of
those files, so a live check reports changed evidence even though the V2 bytes
are intact. This bridge reuses the unchanged V2 bridge logic and resolves every
bound repository source file that is part of the frozen V2 snapshot
(C:/kc3d/code/v2-final) to its byte copy there. The snapshot is first verified
against its epoch manifest and its pinned receipt. Every other bound file
(sessions, evidence, the original baseline and the accepted parent) is still
checked in place.

Freeze creates a V3 ground suite with the frozen floor-aware gain profile.
Nothing historical is rewritten and no ground behavior acceptance is claimed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.media_suite import GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE
from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_ground_parent as v2

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = Path('C:/kc3d/code/v2-final')
SNAPSHOT_RECEIPT = ROOT / 'artifacts/phase3d-v3/v2-source-epoch-01/receipt.json'
SNAPSHOT_RECEIPT_SHA256 = 'dd3aff7b00e817e5a4585a2c5ae920ac401917d66ffddf7dbfd63bdb53297c97'
CLOSURE = Path('C:/kc3d/a/v2-repair-closure-01')
CLOSURE_RECEIPT_SHA256 = 'dfa0c0bb01cb8b4d9d9413d4057fb04b26f2830c9f55e53c553f75b0d2b5961b'
ORIGINAL = ROOT / 'artifacts/phase3b/kiro-integration-01/game/session/baseline'
ACCEPTED_PARENT_SHA256 = '41c1f3156b778b6f10e7ddc39ca6f33bede9bd087f6a8d2e9031095f1658cfd3'


def _need(condition, message, code='parent_not_accepted'):
    if not condition:
        raise VisualError(code, message)


def _tree_digest(value):
    """Same canonical digest the snapshot producer used for its manifest."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def verify_snapshot():
    """The frozen V2 source copy, only while every byte still matches its pinned manifest."""
    _need(SNAPSHOT_RECEIPT.is_file() and file_sha256(SNAPSHOT_RECEIPT) == SNAPSHOT_RECEIPT_SHA256,
          'The pinned V2 source snapshot receipt changed or is missing.', 'evidence_changed')
    receipt = read_json(SNAPSHOT_RECEIPT)
    epoch_path = SNAPSHOT / 'epoch.json'
    _need(epoch_path.is_file() and receipt.get('kind') == 'SourceSnapshotReceipt'
          and file_sha256(epoch_path) == receipt.get('code_root_epoch_sha256') == receipt.get('epoch_sha256'),
          'The frozen V2 epoch manifest differs from its receipt.', 'evidence_changed')
    epoch = read_json(epoch_path)
    manifest = epoch.get('source_files')
    _need(epoch.get('kind') == 'FrozenPhase3DV2FinalSources' and isinstance(manifest, dict) and manifest
          and _tree_digest(manifest) == epoch.get('source_tree_sha256') == receipt.get('source_tree_sha256')
          and len(manifest) == receipt.get('file_count'),
          'The frozen V2 source manifest is inconsistent.', 'evidence_changed')
    for relative, sha in manifest.items():
        _need(file_sha256(SNAPSHOT / relative) == sha, 'Frozen V2 source copy changed: ' + relative, 'evidence_changed')
    before = epoch.get('v2_verify_only_before_edits', {})
    _need(before.get('status') == 'pass' and before.get('parent_source_sha256') == ACCEPTED_PARENT_SHA256,
          'The snapshot does not record a passing V2 bridge check before the V3 edits.', 'evidence_changed')
    return epoch


@contextmanager
def frozen_v2_sources(epoch):
    """Process-local: bound repository sources are checked against their frozen V2 bytes."""
    manifest = epoch['source_files']
    root = ROOT.resolve()
    original = v2._file
    redirected = {}

    def resolved(path, expected, consumed):
        candidate = Path(path)
        try:
            relative = candidate.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            relative = None
        if relative is not None and relative in manifest:
            _need(manifest[relative] == expected, 'The snapshot and the closure disagree on ' + relative,
                  'evidence_changed')
            target = original(SNAPSHOT / relative, expected, {})
            consumed[str(_ordinary(candidate))] = expected
            redirected[str(_ordinary(candidate))] = str(target)
            return target
        return original(path, expected, consumed)

    v2._file = resolved
    try:
        yield redirected
    finally:
        v2._file = original


def verify():
    epoch = verify_snapshot()
    with frozen_v2_sources(epoch) as redirected:
        plan = v2.resolve(CLOSURE, ORIGINAL, closure_receipt_sha256=CLOSURE_RECEIPT_SHA256)
        v2.unchanged(plan)
    _need(plan['parent_source_sha256'] == ACCEPTED_PARENT_SHA256, 'The accepted jump parent changed.', 'source_changed')
    return plan, epoch, dict(redirected)


@contextmanager
def _sources(prepare, plan, output, epoch, redirected):
    """Bind the verified parent as the ground authoring source for this preparation only."""
    previous = prepare.sources

    def bound(stage, jump_session=None):
        _need(stage == 'ground' and jump_session is None, 'This bridge accepts the ground stage only.')
        v2.unchanged(plan)
        provenance = Path(output) / 'repair-parent'
        provenance.mkdir(parents=True, exist_ok=False)
        (provenance / 'producer.py').write_bytes(Path(__file__).read_bytes())
        write_json(provenance / 'parent.json', plan)
        write_json(provenance / 'snapshot-redirections.json', redirected)
        write_json(provenance / 'receipt.json', {
            'kind': 'V3GroundParentReceipt', 'schema_version': 1,
            'parent_sha256': digest(plan), 'parent_file_sha256': file_sha256(provenance / 'parent.json'),
            'producer_sha256': file_sha256(provenance / 'producer.py'),
            'v2_bridge_sha256': file_sha256(v2.__file__), 'prepare_sha256': file_sha256(prepare.__file__),
            'snapshot_receipt_sha256': SNAPSHOT_RECEIPT_SHA256, 'snapshot_tree_sha256': epoch['source_tree_sha256'],
            'snapshot_redirections_sha256': file_sha256(provenance / 'snapshot-redirections.json'),
            'acceptance_profile': GROUND_FLOOR_PROFILE, 'acceptance_rule': deepcopy(GROUND_FLOOR_RULE),
            'historical_session_rewritten': False})
        return Path(plan['parent']), Path(plan['original']), deepcopy(plan['historical_candidate'])

    prepare.sources = bound
    try:
        yield
    finally:
        prepare.sources = previous


def calibrate(identifier, root=Path('C:/kc3d')):
    from scripts import phase3d_v2_prepare as prepare
    plan, epoch, redirected = verify()
    output = _ordinary(root) / 'p' / identifier
    _need(not output.exists(), 'Preserve every previous ground attempt.', 'output_exists')
    with frozen_v2_sources(epoch), _sources(prepare, plan, output, epoch, redirected):
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
                   acceptance_profile=GROUND_FLOOR_PROFILE, v3_bridge=True,
                   historical_session_rewritten=False)
    write_json(binding_path, binding)
    return result


def freeze(previous, identifier, root=Path('C:/kc3d')):
    """Carry the snapshot-verified parent proof across an ordinary V3 suite freeze."""
    from scripts import phase3d_v2_prepare as prepare
    previous, root = map(_ordinary, (previous, root))
    provenance = previous / 'repair-parent'
    receipt_path, plan_path = provenance / 'receipt.json', provenance / 'parent.json'
    _need(receipt_path.is_file() and plan_path.is_file(), 'The V3 parent preparation receipt is missing.')
    parent, receipt = read_json(plan_path), read_json(receipt_path)
    current, epoch, redirected = verify()
    _need(current == parent, 'The parent plan differs from current evidence.')
    _need(receipt.get('kind') == 'V3GroundParentReceipt' and receipt.get('schema_version') == 1
          and receipt.get('parent_sha256') == digest(parent) and receipt.get('historical_session_rewritten') is False
          and receipt.get('acceptance_profile') == GROUND_FLOOR_PROFILE
          and receipt.get('acceptance_rule') == GROUND_FLOOR_RULE
          and receipt.get('snapshot_receipt_sha256') == SNAPSHOT_RECEIPT_SHA256,
          'The V3 parent preparation receipt differs.')
    consumed = deepcopy(parent['consumed_files'])
    with frozen_v2_sources(epoch):
        v2._file(plan_path, receipt.get('parent_file_sha256'), consumed)
        v2._file(provenance / 'producer.py', receipt.get('producer_sha256'), consumed)
        v2._file(provenance / 'snapshot-redirections.json', receipt.get('snapshot_redirections_sha256'), consumed)
    _need(read_json(provenance / 'snapshot-redirections.json') == redirected,
          'The snapshot redirections changed since calibration.', 'evidence_changed')
    _need(receipt.get('producer_sha256') == file_sha256(__file__)
          and receipt.get('prepare_sha256') == file_sha256(prepare.__file__)
          and receipt.get('v2_bridge_sha256') == file_sha256(v2.__file__),
          'Use the exact inspected bridge and preparation revision.', 'producer_changed')
    for path in (receipt_path, Path(__file__).resolve(), Path(prepare.__file__).resolve(), Path(v2.__file__).resolve()):
        consumed[str(path)] = file_sha256(path)
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
        consumed[str(previous / name)] = file_sha256(previous / name)
    _need(not (root / 'p' / identifier).exists(), 'Preserve every previous ground freeze.', 'output_exists')
    result = prepare.freeze(previous, identifier, None, root, game_acceptance_profile=GROUND_FLOOR_PROFILE)
    with frozen_v2_sources(epoch):
        for name, sha in consumed.items():
            v2._file(name, sha, {})
        v2.unchanged(parent)
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
    output = root / 'p' / identifier
    archive = output / 'ground-parent-producer.py'
    archive.write_bytes(Path(__file__).read_bytes())
    snapshot = output / 'ground-parent-initial-session.json'
    snapshot.write_bytes(state_path.read_bytes())
    chain = {'kind': 'V3GroundParentChainReceipt', 'schema_version': 1, 'status': 'pass',
             'parent': parent, 'parent_sha256': digest(parent), 'preparation': str(previous),
             'session': result['session'], 'session_id': state['session_id'], 'suite': result['suite'],
             'suite_sha256': digest(suite), 'suite_file_sha256': file_sha256(suite_path),
             'suite_binding_sha256': file_sha256(suite_path.parent / 'suite-binding.json'),
             'source_epoch': suite.get('source_epoch'), 'acceptance_profile': GROUND_FLOOR_PROFILE,
             'acceptance_rule': deepcopy(GROUND_FLOOR_RULE),
             'v2_snapshot': {'receipt': str(SNAPSHOT_RECEIPT), 'receipt_sha256': SNAPSHOT_RECEIPT_SHA256,
                             'code_root': str(SNAPSHOT), 'tree_sha256': epoch['source_tree_sha256'],
                             'redirected_files': redirected},
             'initial_session_archive': str(snapshot), 'initial_session_archive_sha256': file_sha256(snapshot),
             'producer_archive': str(archive), 'producer_sha256': file_sha256(archive),
             'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
             'canonical_session_promoted': False, 'historical_session_rewritten': False,
             'ground_behavior_acceptance': False, 'model_started': False,
             'scope': 'Prospective V3 ground parent provenance; measurement, review and source acceptance remain separate.'}
    path = output / 'ground-parent-chain.json'
    write_json(path, chain)
    return {**result, 'ground_parent_chain': str(path), 'ground_parent_chain_sha256': file_sha256(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('verify')
    calibration = sub.add_parser('calibrate')
    calibration.add_argument('--id', required=True)
    calibration.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    freezing = sub.add_parser('freeze')
    freezing.add_argument('--previous', type=Path, required=True)
    freezing.add_argument('--id', required=True)
    freezing.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    args = parser.parse_args()
    try:
        if args.command == 'verify':
            plan, epoch, redirected = verify()
            result = {'status': plan['status'], 'parent': plan['parent'],
                      'parent_source_sha256': plan['parent_source_sha256'],
                      'historical_session_status': plan['historical_session_status'],
                      'snapshot_tree_sha256': epoch['source_tree_sha256'],
                      'redirected_repository_files': len(redirected)}
        elif args.command == 'calibrate':
            full = calibrate(args.id, args.root)
            result = {key: full[key] for key in ('status', 'stage', 'output', 'model_started') if key in full}
        else:
            result = freeze(args.previous, args.id, args.root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
