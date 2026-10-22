"""A failed audit remains visible without becoming certified success."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import launcher
from kiroclops.visual.schema import VisualError, read_json, write_json


class EvidenceReportFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='report review ')
        self.root = Path(self.temp.name)
        self.record = {'status': 'completed', 'terminal_cleanup': {'status': 'pass'}}

    def tearDown(self):
        self.temp.cleanup()

    def render(self, directory, *, native='fail', overall='fail', certified=False):
        root = Path(directory)/'report'
        root.mkdir()
        write_json(root/'report.json', {
            'native_run_status': native, 'kiro_run': {'status': 'completed'},
            'selected_candidate_id': 'candidate-01', 'overall_acceptance': overall,
            'acceptance_details': {'calibration_pass': True, 'pass': False},
            'acceptance_summary': {'certified_success': certified,
                                   'status': 'not_fully_validated'}})
        (root/'index.html').write_text('Recorded motion passed; audit incomplete.', encoding='utf-8')
        return {'report': str(root/'index.html'), 'data': str(root/'report.json')}

    def run_report(self, renderer):
        with patch.object(launcher, 'Session') as session, \
             patch('kiroclops.local_engine.review.audit_native_run', side_effect=
                   VisualError('invalid_native_evidence', 'Missing guard decision')), \
             patch('kiroclops.local_engine.reporting.build_report', side_effect=renderer):
            session.return_value.inspect.return_value = {
                'active_job_id': None, 'selected_candidate_id': 'candidate-01'}
            return launcher._postturn_report(self.root, self.record)

    def test_completed_run_with_missing_audit_still_renders_failed_evidence(self):
        result = self.run_report(self.render)
        self.assertEqual('incomplete', result['status'])
        self.assertEqual('built', result['render_status'])
        self.assertEqual('fail', result['native_evidence_status'])
        self.assertEqual('invalid_native_evidence', result['native_evidence_error']['code'])
        self.assertGreater(result['tree']['file_count'], 0)
        data = read_json(result['data'])
        self.assertTrue(data['acceptance_details']['calibration_pass'])
        self.assertFalse(data['acceptance_summary']['certified_success'])
        self.assertEqual('fail', data['overall_acceptance'])

    def test_failed_native_audit_cannot_be_relabelled_verified_by_renderer(self):
        with self.assertRaises(VisualError) as caught:
            self.run_report(lambda directory: self.render(directory, native='pass'))
        self.assertEqual('invalid_report', caught.exception.code)

    def test_failed_native_audit_cannot_certify_success_in_either_schema(self):
        for label in ('legacy', 'axes'):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                with self.assertRaises(VisualError) as caught:
                    self.run_report(lambda directory: self.render(directory,
                        overall='pass' if label == 'legacy' else 'fail',
                        certified=label == 'axes'))
                self.assertEqual('invalid_report', caught.exception.code)

    def test_source_failure_during_render_is_not_swallowed_as_an_audit_warning(self):
        with self.assertRaises(VisualError) as caught:
            self.run_report(lambda directory: (_ for _ in ()).throw(
                VisualError('source_changed', 'Frozen candidate changed')))
        self.assertEqual('source_changed', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
