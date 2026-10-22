"""Start a prospective ground stage from append-only accepted jump evidence.

This bridge never turns the historical failed Session into a succeeded Session.
It binds the independently revalidated selected code as the new authoring parent
and retains the original 3B target as the improvement baseline. All recordings,
suite admission and native Kiro work still use the ordinary v2 implementation.
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

from kiroclops.local_engine.session import _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {'numeric', 'native', 'functional', 'raw', 'normal_raw', 'source', 'source_receipt', 'old_state'}
SOURCE_CHECKS = {'actual_code_authorship', 'no_coordinate_replay', 'no_private_reference_access',
                 'no_operator_coaching', 'protected_behavior_preserved'}


def _need(condition, message, code='parent_not_accepted'):
    if not condition:
        raise VisualError(code, message)


def _file(path, expected, consumed):
    path = _ordinary(path)
    _need(path.is_file() and isinstance(expected, str) and file_sha256(path) == expected,
          'Repair-parent evidence changed or is missing: ' + str(path), 'evidence_changed')
    consumed[str(path)] = expected
    return path


def resolve(closure, original, *, closure_receipt_sha256):
    """Validate selected jump acceptance without reconciling any old Session."""
    closure, original = map(_ordinary, (closure, original))
    _need((closure / 'summary.json').is_file() and (closure / 'receipt.json').is_file(),
          'The append-only jump repair closure is required.', 'missing_repair_closure')
    consumed = {}
    _need(isinstance(closure_receipt_sha256, str) and len(closure_receipt_sha256) == 64
          and all(c in '0123456789abcdef' for c in closure_receipt_sha256),
          'Supply the trusted repair receipt SHA-256 separately.', 'missing_closure_identity')
    _file(closure / 'receipt.json', closure_receipt_sha256, consumed)
    summary, receipt = (read_json(closure / name) for name in ('summary.json', 'receipt.json'))
    _need(receipt.get('kind') == 'RepairSummaryReceipt'
          and receipt.get('summary_sha256') == digest(summary)
          and receipt.get('evidence') == summary.get('evidence'), 'Repair closure receipt differs.')
    _file(closure / 'summary.json', receipt.get('summary_file_sha256'), consumed)
    consumed[str(closure / 'receipt.json')] = file_sha256(closure / 'receipt.json')
    _file(closure / 'producer.py', receipt.get('producer_sha256'), consumed)
    _need(summary.get('kind') == 'SelectedJumpAndRecorderRepairEvidenceSummary'
          and summary.get('schema_version') == 1 and summary.get('status') == 'repair_verified'
          and summary.get('selected_jump_revalidation', {}).get('status') == 'pass'
          and summary.get('historical_session_status') == 'finalization_failed',
          'This is not a completed selected-jump repair closure.')
    for field in ('historical_failed_job_rewritten', 'canonical_session_promoted',
                  'new_kiro_generation', 'target_behavior_edited_by_operator',
                  'thresholds_changed', 'full_phase3d_v2_acceptance'):
        _need(summary.get(field) is False, 'The repair closure has an incompatible scope: ' + field)
    evidence = summary.get('evidence')
    _need(isinstance(evidence, dict) and REQUIRED <= set(evidence), 'A mandatory repair axis is missing.')
    paths = {}
    for name, binding in evidence.items():
        _need(isinstance(binding, dict) and set(binding) == {'path', 'file_sha256'},
              'An evidence byte binding is malformed.')
        paths[name] = _file(binding['path'], binding['file_sha256'], consumed)
    proofs = {name: read_json(paths[name]) for name in REQUIRED}
    state, selected_sha = proofs['old_state'], summary.get('source_sha256')
    session = paths['old_state'].parent
    _need(state.get('status') == 'finalization_failed' and state.get('sealed_at')
          and state.get('experiment', {}).get('reference_mode') == 'media_suite'
          and state['experiment'].get('stage') == 'jump', 'The preserved source Session is not a sealed jump stage.')
    candidate_id = summary.get('candidate_id')
    _need(isinstance(candidate_id, str) and candidate_id in ('candidate-01', 'candidate-02', 'candidate-03')
          and state.get('selected_candidate_id') == candidate_id, 'The selected jump identity differs.')
    candidate = next((c for c in state.get('candidates', []) if c.get('candidate_id') == candidate_id), None)
    _need(candidate and candidate.get('status') == 'succeeded' and candidate.get('eligible') is True
          and candidate.get('source_sha256') == selected_sha, 'The selected calibration candidate is not eligible.')
    parent = session / 'candidates' / candidate_id / 'source'
    hashes = source_hashes(parent)
    _need(hashes and digest(hashes) == selected_sha, 'The independently accepted selected source changed.', 'source_changed')
    consumed.update({str(parent / path): sha for path, sha in hashes.items()})
    source = proofs['source']
    _need(source.get('kind') == 'IndependentSelectedJumpCodeSourceReview' and source.get('status') == 'pass'
          and source.get('candidate_id') == candidate_id and source.get('candidate_sha256') == selected_sha
          and source.get('source_hashes') == hashes and _ordinary(source['session']) == session,
          'Independent source review belongs to another selected jump.', 'wrong_candidate')
    checks = source.get('checks', [])
    _need(len(checks) == len(SOURCE_CHECKS) and {c.get('id') for c in checks} == SOURCE_CHECKS
          and all(c.get('verdict') == 'pass' for c in checks), 'A required source/protection check did not pass.')
    for field in ('authorship_status', 'static_source_status', 'native_status', 'functional_evidence_status',
                  'independent_raw_review_status', 'protected_behavior_status', 'selected_code_source_axis_status'):
        _need(source.get(field) == 'pass', 'A selected-code source axis did not pass: ' + field)
    _need(source.get('original_failed_job_unchanged') is True and source.get('historical_job_rewritten') is False
          and source.get('new_model_generation') is False and source.get('target_behavior_edited') is False,
          'The source review changed historical acceptance or code.')
    source_receipt = proofs['source_receipt']
    _need(source_receipt.get('kind') == 'IndependentSelectedJumpCodeSourceReviewReceipt'
          and source_receipt.get('status') == 'pass' and source_receipt.get('candidate_sha256') == selected_sha
          and source_receipt.get('source_review_sha256') == file_sha256(paths['source']),
          'Independent source byte receipt differs.')
    outputs = source_receipt.get('output_file_hashes')
    _need(isinstance(outputs, dict) and outputs and paths['source'].name in outputs
          and 'evidence-bindings.json' in outputs, 'The complete source review output receipt is missing.')
    for name, sha in outputs.items():
        _file(safe_path(paths['source_receipt'].parent, name, must_exist=False), sha, consumed)
    nested_path = paths['source_receipt'].parent / 'evidence-bindings.json'
    nested = read_json(nested_path)
    nested_files = nested.get('consumed_files')
    _need(nested.get('kind') == 'IndependentSelectedCodeSourceEvidenceBindings'
          and nested.get('schema_version') == 1 and isinstance(nested_files, dict) and nested_files
          and nested.get('consumed_files_sha256') == digest(nested_files)
          == source_receipt.get('consumed_files_sha256'), 'The nested source evidence inventory differs.')
    # The byte receipt binds source-review.json and this manifest together. The
    # manifest deliberately has no reverse source-review hash (a hash cycle).
    for name, sha in nested_files.items():
        _file(name, sha, consumed)
    producer = source_receipt.get('producer', {})
    _file(producer.get('path', ''), producer.get('sha256'), consumed)
    native, numerical, functions = proofs['native'], proofs['numeric'], proofs['functional']
    _need(native.get('kind') == 'AppendOnlyNativeReaudit' and native.get('status') == 'pass'
          and native.get('native', {}).get('status') == 'pass'
          and native['native'].get('model') == 'claude-opus-5.5' and native['native'].get('effort') == 'max'
          and native['native'].get('prompt_count') == 1 and native.get('original_failed_job_unchanged') is True
          and native.get('new_model_generation') is False and native.get('new_capture') is False,
          'Actual native authorship has not been verified.')
    _need(native.get('source_files') and str(paths['old_state']) in native['source_files'],
          'The native proof must byte-bind the preserved failed Session.')
    for name, sha in native['source_files'].items():
        _file(name, sha, consumed)
    _need(numerical.get('kind') == 'FailedFinalNumericalDiagnostic'
          and numerical.get('numerical_status') == 'pass' and numerical.get('passed') == numerical.get('required') == 24
          and numerical.get('candidate_id') == candidate_id and numerical.get('source_sha256') == selected_sha
          and numerical.get('session_id') == state.get('session_id')
          and numerical.get('retained_parent_behavior') is True, 'Complete selected-jump numeric acceptance is missing.')
    _need(functions.get('kind') == 'UnchangedGameSupplementalResult' and functions.get('status') == 'pass'
          and functions.get('source_sha256') == selected_sha and functions.get('candidate_id') == candidate_id
          and functions.get('visual_boundary_status') == 'pass' and functions.get('normal_level_status') == 'pass'
          and functions.get('capture_lease_released') is True and functions.get('original_failed_job_unchanged') is True,
          'Selected jump wall/normal preservation did not pass.')
    for name, kind in (('raw', 'VerifiedRemeasuredStaticWallGameRawReview'),
                       ('normal_raw', 'VerifiedNormalLevelRawInspection')):
        proof = proofs[name]
        _need(proof.get('kind') == kind and proof.get('status') == 'pass'
              and proof.get('source_sha256') == selected_sha, 'Independent raw inspection differs: ' + name)
    _need(proofs['raw'].get('record_count') == 128 and proofs['raw'].get('remeasured_wall_records') == 8
          and proofs['normal_raw'].get('mandatory_route') == 'normal-combined', 'Raw review matrix is incomplete.')
    old_job = session / 'jobs' / state['final_job_id'] / 'job.json'
    _need(str(old_job) in native['source_files'], 'The original failed finalization job is not byte-bound.')
    final_job = read_json(old_job)
    _need(final_job.get('status') == 'failed' and final_job.get('operation') == 'finalize'
          and final_job.get('candidate_id') == candidate_id and final_job.get('source_sha256') == selected_sha,
          'The original failed finalization was mutated.', 'historical_acceptance_changed')
    original_state_path = original.parent / 'session.json'
    _need(original_state_path.is_file(), 'The independent original comparison baseline is missing.', 'wrong_original')
    original_state = read_json(original_state_path)
    original_hashes = source_hashes(original)
    _need(original.name == 'baseline' and original_hashes == original_state.get('baseline_hashes'),
          'The original comparison baseline changed.', 'wrong_original')
    suite_path = _ordinary(state['reference']) / 'suite.json'
    _need(suite_path.is_file(), 'The original jump suite source binding is missing.', 'wrong_original')
    suite = read_json(suite_path)
    _need(suite.get('kind') == 'ReferenceMediaSuite' and suite.get('stage') == 'jump'
          and digest(suite) == state['experiment'].get('suite_sha256')
          and original_hashes == suite.get('original_source_hashes'),
          'Use the original target from the accepted selected-jump suite.', 'wrong_original')
    consumed[str(suite_path)] = file_sha256(suite_path)
    consumed[str(original_state_path)] = file_sha256(original_state_path)
    consumed.update({str(original / path): sha for path, sha in original_hashes.items()})
    return {'kind': 'ProspectiveGroundRepairedJumpParent', 'schema_version': 1, 'status': 'pass',
            'stage': 'ground', 'parent_stage': 'jump', 'closure': str(closure),
            'trusted_closure_receipt_sha256': closure_receipt_sha256,
            'parent': str(parent), 'original': str(original), 'parent_origin_session': str(session),
            'original_comparison_session': str(original.parent), 'historical_candidate': deepcopy(candidate),
            'parent_source_sha256': selected_sha, 'original_source_sha256': digest(original_hashes),
            'historical_session_status': 'finalization_failed', 'canonical_session_promoted': False,
            'new_model_generation': False, 'target_behavior_edited': False,
            'parent_acceptance_mode': 'append_only_mixed_epoch_selected_code_revalidation',
            'consumed_files': consumed, 'ground_behavior_acceptance': False}


def unchanged(plan):
    for name, sha in plan['consumed_files'].items():
        _file(name, sha, {})
    _need(digest(source_hashes(plan['parent'])) == plan['parent_source_sha256']
          and digest(source_hashes(plan['original'])) == plan['original_source_sha256'],
          'A frozen ground starting source changed.', 'source_changed')


@contextmanager
def _resolver(prepare, plan, output):
    """Process-local explicit resolver; pinned core files stay byte-identical."""
    previous = prepare.sources
    def bound(stage, jump_session=None):
        _need(stage == 'ground' and jump_session is None, 'This bridge accepts the ground stage only.')
        unchanged(plan)
        provenance = Path(output) / 'repair-parent'
        provenance.mkdir(parents=True, exist_ok=False)
        script = Path(__file__).resolve()
        (provenance / 'producer.py').write_bytes(script.read_bytes())
        write_json(provenance / 'parent.json', plan)
        write_json(provenance / 'receipt.json', {'kind': 'ProspectiveGroundParentReceipt', 'schema_version': 1,
            'parent_sha256': digest(plan), 'parent_file_sha256': file_sha256(provenance / 'parent.json'),
            'producer_sha256': file_sha256(provenance / 'producer.py'),
            'v2_prepare_sha256': file_sha256(prepare.__file__), 'historical_session_rewritten': False})
        return Path(plan['parent']), Path(plan['original']), deepcopy(plan['historical_candidate'])
    prepare.sources = bound
    try:
        yield
    finally:
        prepare.sources = previous


def calibrate(closure, identifier, root=Path('C:/kc3d'), original=None, *, closure_receipt_sha256):
    from scripts import phase3d_v2_prepare as prepare
    from scripts.phase3d_prepare import STARTS
    original = original or ROOT / STARTS['ground'][0] / 'baseline'
    plan = resolve(closure, original, closure_receipt_sha256=closure_receipt_sha256)
    output = _ordinary(root) / 'p' / identifier
    _need(not output.exists(), 'Preserve every previous ground attempt.', 'output_exists')
    with _resolver(prepare, plan, output):
        try:
            result = prepare.calibrate('ground', identifier, root)
        finally:
            unchanged(plan)
    binding_path = output / 'start-binding.json'
    binding = read_json(binding_path)
    binding.update(parent_origin_session=plan['parent_origin_session'],
        original_comparison_session=plan['original_comparison_session'],
        parent_acceptance_mode=plan['parent_acceptance_mode'],
        repaired_parent_receipt=str(output / 'repair-parent' / 'receipt.json'),
        historical_session_rewritten=False)
    write_json(binding_path, binding)
    return result


def freeze(previous, identifier, *, closure_receipt_sha256, root=Path('C:/kc3d')):
    """Carry the accepted-code parent proof across the ordinary suite freeze.

    Admission still verifies the ordinary measurement, capture and raw-review
    gates. This separate receipt makes the prospective chain available to the
    eventual source audit; it does not claim admission reads this new sidecar.
    """
    from scripts import phase3d_v2_prepare as prepare
    previous, root = map(_ordinary, (previous, root))
    provenance = previous / 'repair-parent'
    receipt_path, plan_path = provenance / 'receipt.json', provenance / 'parent.json'
    _need(receipt_path.is_file() and plan_path.is_file(), 'The prospective repaired-parent preparation receipt is missing.')
    parent, receipt = read_json(plan_path), read_json(receipt_path)
    _need(parent.get('kind') == 'ProspectiveGroundRepairedJumpParent' and parent.get('stage') == 'ground'
          and parent.get('parent_stage') == 'jump' and parent.get('status') == 'pass'
          and parent.get('trusted_closure_receipt_sha256') == closure_receipt_sha256,
          'The trusted parent chain differs from this ground preparation.')
    current = resolve(parent['closure'], parent['original'], closure_receipt_sha256=closure_receipt_sha256)
    _need(current == parent, 'The prospective repaired-parent plan differs from current evidence.')
    consumed = deepcopy(parent['consumed_files'])
    _need(receipt.get('kind') == 'ProspectiveGroundParentReceipt' and receipt.get('schema_version') == 1
          and receipt.get('parent_sha256') == digest(parent) and receipt.get('historical_session_rewritten') is False,
          'The prospective parent preparation receipt differs.')
    _file(plan_path, receipt.get('parent_file_sha256'), consumed)
    _file(provenance / 'producer.py', receipt.get('producer_sha256'), consumed)
    _need(receipt.get('producer_sha256') == file_sha256(__file__)
          and receipt.get('v2_prepare_sha256') == file_sha256(prepare.__file__),
          'Use the exact inspected parent bridge and preparation revision.', 'producer_changed')
    consumed[str(receipt_path)] = file_sha256(receipt_path)
    consumed[str(Path(__file__).resolve())] = file_sha256(__file__)
    consumed[str(Path(prepare.__file__).resolve())] = file_sha256(prepare.__file__)
    binding = read_json(previous / 'start-binding.json')
    _need(binding.get('stage') == 'ground' and binding.get('model_started') is False
          and binding.get('historical_session_rewritten') is False
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
        path = previous / name
        consumed[str(path)] = file_sha256(path)
    _need(not (root / 'p' / identifier).exists(), 'Preserve every previous ground freeze.', 'output_exists')
    result = prepare.freeze(previous, identifier, None, root)
    for name, sha in consumed.items():
        _file(name, sha, {})
    unchanged(parent)
    suite_path = _ordinary(result['suite']) / 'suite.json'
    suite = read_json(suite_path)
    state_path = _ordinary(result['session']) / 'session.json'
    state = read_json(state_path)
    _need(suite.get('stage') == 'ground' and suite.get('domain') == 'game'
          and digest(suite.get('authoring_parent_source_hashes')) == parent['parent_source_sha256']
          and digest(suite.get('original_source_hashes')) == parent['original_source_sha256']
          and state.get('experiment', {}).get('suite_sha256') == digest(suite)
          and state.get('started_at') is None and not state.get('candidates'),
          'The new ground suite/session is not bound to the accepted jump parent.', 'wrong_result')
    output = root / 'p' / identifier
    archive = output / 'ground-parent-producer.py'
    archive.write_bytes(Path(__file__).read_bytes())
    snapshot = output / 'ground-parent-initial-session.json'
    snapshot.write_bytes(state_path.read_bytes())
    chain = {'kind': 'ProspectiveGroundParentChainReceipt', 'schema_version': 1, 'status': 'pass',
        'parent': parent, 'parent_sha256': digest(parent), 'preparation': str(previous),
        'session': result['session'], 'session_id': state['session_id'], 'suite': result['suite'],
        'suite_sha256': digest(suite), 'suite_file_sha256': file_sha256(suite_path),
        'suite_binding_sha256': file_sha256(suite_path.parent / 'suite-binding.json'),
        'initial_session_archive': str(snapshot), 'initial_session_archive_sha256': file_sha256(snapshot),
        'producer_archive': str(archive), 'producer_sha256': file_sha256(archive),
        'consumed_files': consumed, 'consumed_files_sha256': digest(consumed),
        'canonical_session_promoted': False, 'historical_session_rewritten': False,
        'ordinary_admission_claimed_to_consume_sidecar': False,
        'ground_behavior_acceptance': False, 'model_started': False,
        'scope': 'Prospective selected-code parent provenance; ordinary measurement and source acceptance remain separate.'}
    path = output / 'ground-parent-chain.json'
    write_json(path, chain)
    return {**result, 'ground_parent_chain': str(path), 'ground_parent_chain_sha256': file_sha256(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--closure', type=Path, required=True)
    parser.add_argument('--closure-receipt-sha256', required=True)
    parser.add_argument('--original', type=Path)
    parser.add_argument('--id', required=True)
    parser.add_argument('--root', type=Path, default=Path('C:/kc3d'))
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--verify-only', action='store_true')
    operation.add_argument('--freeze-previous', type=Path)
    args = parser.parse_args()
    try:
        if args.verify_only:
            from scripts.phase3d_prepare import STARTS
            plan = resolve(args.closure, args.original or ROOT / STARTS['ground'][0] / 'baseline',
                           closure_receipt_sha256=args.closure_receipt_sha256)
            result = {key: plan[key] for key in ('status', 'parent', 'parent_source_sha256', 'historical_session_status')}
        elif args.freeze_previous:
            saved_parent = read_json(args.freeze_previous / 'repair-parent/parent.json')
            _need(_ordinary(saved_parent['closure']) == _ordinary(args.closure), 'The requested closure differs from the preparation.')
            result = freeze(args.freeze_previous, args.id, root=args.root,
                            closure_receipt_sha256=args.closure_receipt_sha256)
        else:
            full = calibrate(args.closure, args.id, args.root, args.original,
                             closure_receipt_sha256=args.closure_receipt_sha256)
            result = {key: full[key] for key in ('status', 'stage', 'output', 'model_started')}
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
