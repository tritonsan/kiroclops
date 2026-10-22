"""Prospective ground parent accepts proof, never rewrites failed history."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_ground_parent as bridge


class ParentFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.closure = self.root / 'closure'
        self.session = self.root / 'jump-session'
        self.parent = self.session / 'candidates/candidate-02/source'
        self.original = self.root / 'original-session/baseline'
        self.parent.mkdir(parents=True)
        self.original.mkdir(parents=True)
        (self.parent / 'player.gd').write_text('extends Node\n# independently authored selected source\n')
        (self.original / 'player.gd').write_text('extends Node\n# original comparison target\n')
        self.hashes = source_hashes(self.parent)
        self.sha = digest(self.hashes)
        self.candidate = {'candidate_id': 'candidate-02', 'source_sha256': self.sha,
                          'status': 'succeeded', 'eligible': True}
        self.suite = self.root / 'jump-suite'
        suite = {'kind': 'ReferenceMediaSuite', 'stage': 'jump',
                 'original_source_hashes': source_hashes(self.original)}
        write_json(self.suite / 'suite.json', suite)
        self.state = {'status': 'finalization_failed', 'sealed_at': 1234, 'reference': str(self.suite),
            'experiment': {'stage': 'jump', 'reference_mode': 'media_suite', 'suite_sha256': digest(suite)},
            'selected_candidate_id': 'candidate-02', 'candidates': [self.candidate],
            'session_id': 'test-jump-session', 'final_job_id': 'old-job'}
        write_json(self.session / 'session.json', self.state)
        self.final_job = self.session / 'jobs/old-job/job.json'
        write_json(self.final_job, {'status': 'failed', 'operation': 'finalize',
            'candidate_id': 'candidate-02', 'source_sha256': self.sha})
        write_json(self.original.parent / 'session.json', {'baseline_hashes': source_hashes(self.original)})
        self.producer = self.root / 'source-producer.py'
        self.producer.write_text('# independent source review producer\n')
        self.paths = {'old_state': self.session / 'session.json'}
        for key in bridge.REQUIRED - {'old_state'}:
            self.paths[key] = self.root / 'evidence' / (key + '.json')
        source = {'kind': 'IndependentSelectedJumpCodeSourceReview', 'status': 'pass',
            'candidate_id': 'candidate-02', 'candidate_sha256': self.sha, 'source_hashes': self.hashes,
            'session': str(self.session), 'checks': [{'id': key, 'verdict': 'pass'} for key in bridge.SOURCE_CHECKS],
            'original_failed_job_unchanged': True, 'historical_job_rewritten': False,
            'new_model_generation': False, 'target_behavior_edited': False}
        for name in ('authorship_status', 'static_source_status', 'native_status', 'functional_evidence_status',
                     'independent_raw_review_status', 'protected_behavior_status', 'selected_code_source_axis_status'):
            source[name] = 'pass'
        write_json(self.paths['source'], source)
        write_json(self.paths['native'], {'kind': 'AppendOnlyNativeReaudit', 'status': 'pass',
            'native': {'status': 'pass', 'model': 'claude-opus-5.5', 'effort': 'max', 'prompt_count': 1},
            'original_failed_job_unchanged': True, 'new_model_generation': False, 'new_capture': False, 'source_files': {
                str(self.paths['old_state']): file_sha256(self.paths['old_state']),
                str(self.final_job): file_sha256(self.final_job)}})
        write_json(self.paths['numeric'], {'kind': 'FailedFinalNumericalDiagnostic',
            'numerical_status': 'pass', 'passed': 24, 'required': 24, 'candidate_id': 'candidate-02',
            'source_sha256': self.sha, 'session_id': self.state['session_id'], 'retained_parent_behavior': True})
        write_json(self.paths['functional'], {'kind': 'UnchangedGameSupplementalResult', 'status': 'pass',
            'source_sha256': self.sha, 'candidate_id': 'candidate-02', 'visual_boundary_status': 'pass',
            'normal_level_status': 'pass', 'capture_lease_released': True, 'original_failed_job_unchanged': True})
        write_json(self.paths['raw'], {'kind': 'VerifiedRemeasuredStaticWallGameRawReview', 'status': 'pass',
            'source_sha256': self.sha, 'record_count': 128, 'remeasured_wall_records': 8})
        write_json(self.paths['normal_raw'], {'kind': 'VerifiedNormalLevelRawInspection', 'status': 'pass',
            'source_sha256': self.sha, 'mandatory_route': 'normal-combined'})
        self.nested_only = self.root / 'nested-only-evidence.txt'
        self.nested_only.write_text('actual earlier byte evidence, not just a top-level proof')
        self.nested_path = self.paths['source_receipt'].parent / 'evidence-bindings.json'
        self.nested_files = {str(path): file_sha256(path) for key, path in self.paths.items()
                             if key != 'source_receipt'}
        self.nested_files[str(self.nested_only)] = file_sha256(self.nested_only)
        self.write_nested_receipt()
        self.closure.mkdir()
        (self.closure / 'producer.py').write_text('# selected code repair summary producer\n')
        self.summary = {'kind': 'SelectedJumpAndRecorderRepairEvidenceSummary', 'schema_version': 1,
            'status': 'repair_verified', 'selected_jump_revalidation': {'status': 'pass'},
            'historical_session_status': 'finalization_failed', 'candidate_id': 'candidate-02', 'source_sha256': self.sha}
        for field in ('historical_failed_job_rewritten', 'canonical_session_promoted', 'new_kiro_generation',
                      'target_behavior_edited_by_operator', 'thresholds_changed', 'full_phase3d_v2_acceptance'):
            self.summary[field] = False
        self.rebind()

    def write_nested_receipt(self):
        write_json(self.nested_path, {'kind': 'IndependentSelectedCodeSourceEvidenceBindings',
            'schema_version': 1, 'consumed_files': self.nested_files,
            'consumed_files_sha256': digest(self.nested_files)})
        write_json(self.paths['source_receipt'], {'kind': 'IndependentSelectedJumpCodeSourceReviewReceipt',
            'status': 'pass', 'candidate_sha256': self.sha,
            'source_review_sha256': file_sha256(self.paths['source']),
            'consumed_files_sha256': digest(self.nested_files),
            'producer': {'path': str(self.producer), 'sha256': file_sha256(self.producer)},
            'output_file_hashes': {'source.json': file_sha256(self.paths['source']),
                                  'evidence-bindings.json': file_sha256(self.nested_path)}})

    def rebind(self, *, trust=True):
        self.summary['evidence'] = {key: {'path': str(path), 'file_sha256': file_sha256(path)}
                                    for key, path in self.paths.items()}
        write_json(self.closure / 'summary.json', self.summary)
        write_json(self.closure / 'receipt.json', {'kind': 'RepairSummaryReceipt',
            'summary_sha256': digest(self.summary), 'summary_file_sha256': file_sha256(self.closure / 'summary.json'),
            'evidence': self.summary['evidence'], 'producer_sha256': file_sha256(self.closure / 'producer.py')})
        if trust:
            self.trusted_receipt_sha = file_sha256(self.closure / 'receipt.json')


class RepairedGroundParentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc ground café ')
        self.addCleanup(self.temp.cleanup)
        self.fixture = ParentFixture(self.temp.name)

    def resolve(self):
        return bridge.resolve(self.fixture.closure, self.fixture.original,
                              closure_receipt_sha256=self.fixture.trusted_receipt_sha)

    def rejects(self, code=None):
        with self.assertRaises(VisualError) as caught:
            self.resolve()
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_valid_parent_is_bound_without_promoting_history(self):
        old = self.fixture.paths['old_state'].read_bytes()
        result = self.resolve()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['parent_source_sha256'], self.fixture.sha)
        self.assertEqual(result['parent_origin_session'], str(self.fixture.session))
        self.assertEqual(result['original_comparison_session'], str(self.fixture.original.parent))
        self.assertFalse(result['canonical_session_promoted'])
        self.assertFalse(result['ground_behavior_acceptance'])
        self.assertEqual(self.fixture.paths['old_state'].read_bytes(), old)

    def test_changed_selected_source_is_rejected(self):
        (self.fixture.parent / 'player.gd').write_text('different code')
        self.rejects('source_changed')

    def test_tampered_evidence_byte_is_rejected(self):
        self.fixture.paths['raw'].write_text('{}')
        self.rejects('evidence_changed')

    def test_missing_closure_is_rejected(self):
        (self.fixture.closure / 'receipt.json').unlink()
        self.rejects('missing_repair_closure')

    def test_missing_required_axis_is_rejected_even_if_receipt_rebound(self):
        del self.fixture.paths['normal_raw']
        self.fixture.rebind()
        self.rejects()

    def test_ground_proof_cannot_be_used_as_jump_parent(self):
        self.fixture.state['experiment']['stage'] = 'ground'
        write_json(self.fixture.paths['old_state'], self.fixture.state)
        native = read_json(self.fixture.paths['native'])
        native['source_files'][str(self.fixture.paths['old_state'])] = file_sha256(self.fixture.paths['old_state'])
        write_json(self.fixture.paths['native'], native)
        self.fixture.rebind()
        self.rejects()

    def test_mutated_old_session_is_rejected(self):
        self.fixture.state['status'] = 'finalized'
        write_json(self.fixture.paths['old_state'], self.fixture.state)
        self.rejects('evidence_changed')

    def test_mutated_old_failed_job_is_rejected(self):
        write_json(self.fixture.final_job, {'status': 'succeeded'})
        self.rejects('evidence_changed')

    def test_mismatched_raw_source_fails_even_with_updated_byte_receipt(self):
        raw = read_json(self.fixture.paths['raw'])
        raw['source_sha256'] = '0' * 64
        write_json(self.fixture.paths['raw'], raw)
        self.fixture.rebind()
        self.rejects()

    def test_missing_source_protection_check_is_rejected(self):
        value = read_json(self.fixture.paths['source'])
        value['checks'].pop()
        write_json(self.fixture.paths['source'], value)
        self.fixture.rebind()
        self.rejects()

    def test_original_baseline_remains_independently_bound(self):
        (self.fixture.original / 'player.gd').write_text('not the original baseline')
        self.rejects('wrong_original')

    def test_nested_consumed_byte_tamper_is_rejected(self):
        self.fixture.nested_only.write_text('changed underlying raw proof')
        self.rejects('evidence_changed')

    def test_nested_digest_mismatch_is_rejected_even_with_rebound_outer_receipt(self):
        nested = read_json(self.fixture.nested_path)
        nested['consumed_files_sha256'] = '0' * 64
        write_json(self.fixture.nested_path, nested)
        receipt = read_json(self.fixture.paths['source_receipt'])
        receipt['output_file_hashes']['evidence-bindings.json'] = file_sha256(self.fixture.nested_path)
        write_json(self.fixture.paths['source_receipt'], receipt)
        self.fixture.rebind()
        self.rejects()

    def test_missing_nested_binder_is_rejected(self):
        self.fixture.nested_path.unlink()
        self.rejects('evidence_changed')

    def test_different_intact_original_baseline_is_rejected(self):
        alternate = Path(self.temp.name) / 'other-original/baseline'
        alternate.mkdir(parents=True)
        (alternate / 'player.gd').write_text('different but internally intact baseline')
        write_json(alternate.parent / 'session.json', {'baseline_hashes': source_hashes(alternate)})
        with self.assertRaises(VisualError) as caught:
            bridge.resolve(self.fixture.closure, alternate, closure_receipt_sha256=self.fixture.trusted_receipt_sha)
        self.assertEqual(caught.exception.code, 'wrong_original')

    def test_self_consistent_replacement_cannot_change_external_trust_pin(self):
        (self.fixture.closure / 'producer.py').write_text('# substituted producer')
        self.fixture.rebind(trust=False)
        self.rejects('evidence_changed')

    def test_external_closure_identity_is_required(self):
        with self.assertRaises(VisualError) as caught:
            bridge.resolve(self.fixture.closure, self.fixture.original, closure_receipt_sha256=None)
        self.assertEqual(caught.exception.code, 'missing_closure_identity')

    def make_preparation(self):
        from scripts import phase3d_v2_prepare as prepare
        plan = self.resolve()
        previous = Path(self.temp.name) / 'p/calibration'
        previous.mkdir(parents=True)
        with bridge._resolver(prepare, plan, previous):
            parent, original, candidate = prepare.sources('ground')
        write_json(previous / 'calibration.json', {'stage': 'ground', 'model_started': False})
        write_json(previous / 'recipes.json', [])
        write_json(previous / 'start-binding.json', {'stage': 'ground', 'model_started': False,
            'parent': str(parent), 'original': str(original), 'historical_candidate': candidate,
            'parent_source_sha256': plan['parent_source_sha256'],
            'original_source_sha256': plan['original_source_sha256'],
            'parent_origin_session': plan['parent_origin_session'],
            'original_comparison_session': plan['original_comparison_session'],
            'parent_acceptance_mode': plan['parent_acceptance_mode'],
            'repaired_parent_receipt': str(previous / 'repair-parent/receipt.json'),
            'historical_session_rewritten': False})
        return previous, plan

    def fake_freeze(self, previous, identifier, review, root):
        parent, plan = self.make_fake_suite_start(previous)
        output = Path(root) / 'p' / identifier
        suite = output / 'suite'
        manifest = {'kind': 'ReferenceMediaSuite', 'stage': 'ground', 'domain': 'game',
            'authoring_parent_source_hashes': source_hashes(parent),
            'original_source_hashes': source_hashes(plan['original'])}
        write_json(suite / 'suite.json', manifest)
        write_json(suite / 'suite-binding.json', {'suite_sha256': digest(manifest)})
        session = Path(root) / 's' / identifier
        write_json(session / 'session.json', {'session_id': 'new-ground', 'started_at': None, 'candidates': [],
            'experiment': {'suite_sha256': digest(manifest)}})
        return {'status': 'pending_admission', 'stage': 'ground', 'session': str(session), 'suite': str(suite)}

    @staticmethod
    def make_fake_suite_start(previous):
        binding = read_json(Path(previous) / 'start-binding.json')
        parent = read_json(Path(previous) / 'repair-parent/parent.json')
        return binding['parent'], parent

    def test_freeze_checks_parent_and_retains_explicit_provenance_sidecar(self):
        from scripts import phase3d_v2_prepare as prepare
        previous, plan = self.make_preparation()
        old_history = self.fixture.paths['old_state'].read_bytes()
        with patch.object(prepare, 'freeze', side_effect=self.fake_freeze) as frozen:
            result = bridge.freeze(previous, 'frozen', root=Path(self.temp.name),
                closure_receipt_sha256=self.fixture.trusted_receipt_sha)
        self.assertEqual(frozen.call_count, 1)
        chain = read_json(result['ground_parent_chain'])
        self.assertEqual(chain['parent_sha256'], digest(plan))
        self.assertFalse(chain['ordinary_admission_claimed_to_consume_sidecar'])
        self.assertFalse(chain['canonical_session_promoted'])
        self.assertFalse(chain['ground_behavior_acceptance'])
        self.assertEqual(self.fixture.paths['old_state'].read_bytes(), old_history)
        self.assertEqual(Path(chain['producer_archive']).read_bytes(), Path(bridge.__file__).read_bytes())

    def test_freeze_rejects_tampered_archive_before_recording(self):
        from scripts import phase3d_v2_prepare as prepare
        previous, _ = self.make_preparation()
        (previous / 'repair-parent/producer.py').write_text('# substituted bridge')
        with patch.object(prepare, 'freeze') as frozen:
            with self.assertRaises(VisualError) as caught:
                bridge.freeze(previous, 'frozen', root=Path(self.temp.name),
                              closure_receipt_sha256=self.fixture.trusted_receipt_sha)
        self.assertEqual(caught.exception.code, 'evidence_changed')
        frozen.assert_not_called()

    def test_freeze_rejects_wrong_parent_copy_before_recording(self):
        from scripts import phase3d_v2_prepare as prepare
        previous, _ = self.make_preparation()
        binding = read_json(previous / 'start-binding.json')
        binding['parent_source_sha256'] = '0' * 64
        write_json(previous / 'start-binding.json', binding)
        with patch.object(prepare, 'freeze') as frozen:
            with self.assertRaises(VisualError) as caught:
                bridge.freeze(previous, 'frozen', root=Path(self.temp.name),
                              closure_receipt_sha256=self.fixture.trusted_receipt_sha)
        self.assertEqual(caught.exception.code, 'source_changed')
        frozen.assert_not_called()

    def test_process_resolver_archives_source_and_restores_on_exception(self):
        plan = self.resolve()
        output = Path(self.temp.name) / 'new-output'
        output.mkdir()
        previous = lambda *a: ('original resolver',)
        prepare = SimpleNamespace(sources=previous, __file__=__file__)
        with self.assertRaisesRegex(RuntimeError, 'test exception'):
            with bridge._resolver(prepare, plan, output):
                parent, original, candidate = prepare.sources('ground')
                self.assertEqual(parent, self.fixture.parent)
                self.assertEqual(original, self.fixture.original)
                self.assertEqual(candidate, self.fixture.candidate)
                self.assertEqual((output / 'repair-parent/producer.py').read_bytes(), Path(bridge.__file__).read_bytes())
                raise RuntimeError('test exception')
        self.assertIs(prepare.sources, previous)
        bridge.unchanged(plan)

    def test_unchanged_check_detects_history_change_after_resolution(self):
        plan = self.resolve()
        self.fixture.paths['old_state'].write_text('{}')
        with self.assertRaises(VisualError):
            bridge.unchanged(plan)


if __name__ == '__main__':
    unittest.main()
