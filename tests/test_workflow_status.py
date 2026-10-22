from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.workflow_status import progress_view, acceptance_view, AXES
from kiroclops.visual.schema import write_json


class WorkflowStatusTests(unittest.TestCase):
    def test_progress_is_small_readonly_snapshot_not_session_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); job = 'a' * 32
            write_json(root / 'session.json', {'status': 'evaluating', 'prepared': True,
                'budget': {'max_candidates': 3}, 'deadline_at': 100., 'active_job_id': job,
                'candidates': [{'candidate_id': 'candidate-01', 'status': 'running', 'source_path': 'PRIVATE'}]})
            write_json(root / f'jobs/{job}/job.json', {'job_id': job, 'operation': 'evaluate',
                'status': 'running', 'private_functional_truth': 'PRIVATE'})
            with patch('kiroclops.local_engine.session.Session.inspect', side_effect=AssertionError('Do not reconcile live captures')):
                view = progress_view(root, now=10.)
            self.assertEqual(view['remaining_seconds'], 90.)
            self.assertEqual(view['candidates_used'], 1)
            self.assertEqual(view['active_job'], {'operation': 'evaluate', 'status': 'running', 'candidate_id': None})
            self.assertNotIn('PRIVATE', str(view))

    def test_foreign_or_malformed_active_id_cannot_open_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for identifier in ('../secret', 'z' * 32, 'a' * 31):
                write_json(root / 'session.json', {'active_job_id': identifier})
                self.assertNotIn('active_job', progress_view(root))
            self.assertIsNone(progress_view(root / 'absent'))

    def test_completion_does_not_upgrade_unseen_or_visual_evidence(self):
        view = acceptance_view({'overall_acceptance': 'partial', 'acceptance_axes': {
            'model_completion': {'status': 'pass'}, 'observed_behavior': {'status': 'pass'},
            'unseen_validation': {'status': 'not_evaluated'}, 'independent_visual_review': {'status': 'incomplete'}}})
        self.assertFalse(view['certified_success'])
        self.assertEqual(set(view['axes']), set(AXES))
        self.assertEqual(view['axes']['source_review']['status'], 'unknown')
        self.assertEqual(view['axes']['unseen_validation']['status'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
