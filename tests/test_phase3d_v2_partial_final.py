"""Failed-job diagnostics cannot replace acceptance or invent missing work."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from scripts import phase3d_v2_partial_final as diagnostic
from kiroclops.local_engine.execution_policy import budget_from_policy, resolve_policy
from kiroclops.local_engine.session import JOB_BINDING_FIELDS
from kiroclops.visual.schema import VisualError, digest


class PartialFinalDiagnosticTests(unittest.TestCase):
    def fixture(self):
        policy = resolve_policy('phase3d-execution-v2', 'jump')
        hashes = {'player.gd': 'selected'}
        chosen = {'candidate_id': 'candidate-02', 'status': 'succeeded', 'eligible': True,
                  'source_sha256': digest(hashes)}
        state = {'schema_version': 2, 'domain': 'game', 'experiment': {'reference_mode': 'media_suite',
            'stage': 'jump', 'execution_policy': policy}, 'budget': budget_from_policy(policy),
            'candidates': [chosen], 'selected_candidate_id': chosen['candidate_id'],
            'final_job_id': 'final123', 'active_job_id': None, 'status': 'finalization_failed',
            'sealed_at': 99, 'deadline_at': 100, 'final': {'status': 'failed', 'pass': False,
            'job_id': 'final123', 'error': deepcopy(diagnostic.ERROR)}}
        job = {key: None for key in JOB_BINDING_FIELDS}
        job.update(job_id='final123', operation='finalize', status='failed', candidate_id=chosen['candidate_id'],
            source_sha256=chosen['source_sha256'], source_hashes=hashes, created_at=98, started_at=99,
            finished_at=110, error=deepcopy(diagnostic.ERROR), result_applied=True)
        return state, job, {'sha256': digest({key: job.get(key) for key in JOB_BINDING_FIELDS})}

    def test_exact_failed_sealed_job_is_eligible_for_diagnosis_only(self):
        state, job, binding = self.fixture()
        chosen, policy = diagnostic._terminal(state, job, binding, False)
        self.assertEqual(chosen['candidate_id'], 'candidate-02')
        self.assertEqual(policy['max_validation_seconds'], 3600)
        self.assertEqual(job['status'], 'failed')
        self.assertFalse(state['final']['pass'])

    def test_success_or_created_canonical_result_is_rejected(self):
        for mutate, exists in ((False, True), (True, False)):
            state, job, binding = self.fixture()
            if mutate:
                job['status'] = 'succeeded'
            with self.assertRaises(VisualError):
                diagnostic._terminal(state, job, binding, exists)

    def test_other_source_policy_or_failure_cannot_supply_receipt(self):
        for axis in ('candidate', 'policy', 'error', 'binding', 'unsealed'):
            state, job, binding = self.fixture()
            if axis == 'candidate':
                job['source_sha256'] = 'other'
            elif axis == 'policy':
                state['budget']['max_seconds'] = 1800
            elif axis == 'error':
                job['error'] = {'code': 'behavior_fail', 'message': 'Different failure'}
            elif axis == 'binding':
                binding['sha256'] = 'other'
            else:
                state['sealed_at'] = 101
            with self.subTest(axis=axis), self.assertRaises(VisualError):
                diagnostic._terminal(state, job, binding, False)

    def test_each_failed_curve_and_prior_accepted_behavior_stays_visible(self):
        cases = [{'case_id': name, 'stage': 'jump', 'role': role, 'purpose': 'primary'}
                 for name, role in (('known', 'regression'), ('new', 'validation'))]
        records = {role: {case['case_id']: {'id': role, 'name': case['case_id']} for case in cases}
                   for role in diagnostic.ROLES}
        calls = []

        def compare(reference, original, final, stage, **options):
            calls.append((reference['id'], original['id'], final['id'], options['require_improvement']))
            return {'pass': final['name'] == 'known', 'score': .1 if final['name'] == 'known' else .3}

        with patch.object(diagnostic.media_suite, '_compare', side_effect=compare), \
                patch.object(diagnostic.media_suite, '_parent_acceptance', return_value={'pass': True}):
            compared, parents = diagnostic._matrix(cases, records)
        self.assertEqual([row['pass'] for row in compared], [True, False])
        self.assertEqual([row['retained_reference_behavior'] for row in parents], [True, False])
        self.assertEqual(calls, [('reference', 'original', 'final', True), ('before', 'before', 'final', False)] * 2)
        self.assertEqual([row['role'] for row in compared], ['regression', 'validation'])


if __name__ == '__main__':
    unittest.main()
