"""A native turn must end before deterministic report packaging begins."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kiroclops.local_engine import cohort, launcher
from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json


class PostturnReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / 'session'
        self.session.mkdir()
        self.completed = {'status': 'completed', 'terminal_cleanup': {'status': 'pass'}}

    def tearDown(self):
        self.temp.cleanup()

    def test_runner_waits_for_terminal_finalization_with_explicit_result_access(self):
        session = Mock()
        session.get_job.side_effect = [
            {'job_id': 'final-01', 'operation': 'finalize', 'status': 'running',
             'candidate_id': 'candidate-02', 'source_sha256': 'selected-source'},
            {'job_id': 'final-01', 'operation': 'finalize', 'status': 'succeeded',
             'candidate_id': 'candidate-02', 'source_sha256': 'selected-source', 'finished_at': 12.0}]
        request = {'job_id': 'final-01', 'candidate_id': 'candidate-02',
                   'source_sha256': 'selected-source', 'validation_deadline_at': time.time()+20}
        result = launcher._await_preserved_finalization(session, request)
        self.assertEqual('terminal', result['status'])
        self.assertEqual('succeeded', result['job_status'])
        self.assertEqual('selected-source', result['source_sha256'])
        self.assertEqual(2, session.get_job.call_count)
        self.assertTrue(all(call.kwargs['allow_final_result'] for call in session.get_job.call_args_list))

    def test_runner_wait_has_bounded_validation_deadline(self):
        session = Mock()
        session.get_job.return_value = {'job_id': 'final-01', 'operation': 'finalize', 'status': 'running',
                                        'candidate_id': 'candidate-02', 'source_sha256': 'selected-source'}
        with self.assertRaises(VisualError) as error:
            launcher._await_preserved_finalization(session, {'job_id': 'final-01',
                'candidate_id': 'candidate-02', 'source_sha256': 'selected-source',
                'validation_deadline_at': time.time()-6})
        self.assertEqual('finalization_wait_timeout', error.exception.code)
        session.get_job.assert_called_once_with('final-01', wait_seconds=0.0, allow_final_result=True)

    def test_runner_wait_rejects_different_final_source(self):
        session = Mock()
        session.get_job.return_value = {'job_id': 'final-01', 'operation': 'finalize',
            'status': 'succeeded', 'candidate_id': 'candidate-02', 'source_sha256': 'other-source'}
        with self.assertRaises(VisualError) as error:
            launcher._await_preserved_finalization(session, {'job_id': 'final-01',
                'candidate_id': 'candidate-02', 'source_sha256': 'selected-source',
                'validation_deadline_at': time.time()+10})
        self.assertEqual('cleanup_binding_changed', error.exception.code)

    def test_incomplete_turn_never_starts_report(self):
        with patch.object(launcher, 'Session') as session:
            result = launcher._postturn_report(self.session, {'status': 'timeout'})
        self.assertEqual('not_run', result['status'])
        session.assert_not_called()
        self.assertFalse((self.session / 'report').exists())

    def test_active_job_rejects_report_before_native_audit(self):
        with patch.object(launcher, 'Session') as session, \
             patch('kiroclops.local_engine.review.audit_native_run') as audit:
            session.return_value.inspect.return_value = {'active_job_id': 'job-1'}
            with self.assertRaises(VisualError) as error:
                launcher._postturn_report(self.session, self.completed)
        self.assertEqual('job_active', error.exception.code)
        audit.assert_not_called()
        self.assertFalse((self.session / 'report').exists())

    def test_handoff_must_match_terminal_selected_source(self):
        record = {**self.completed, 'postturn_finalization': {
            'status': 'terminal', 'job_id': 'final-01', 'job_status': 'succeeded',
            'candidate_id': 'candidate-02', 'source_sha256': 'different-source'}}
        state = {'active_job_id': None, 'selected_candidate_id': 'candidate-02',
                 'final': {'job_id': 'final-01', 'status': 'succeeded'},
                 'candidates': [{'candidate_id': 'candidate-02', 'source_sha256': 'selected-source'}]}
        with patch.object(launcher, 'Session') as session, \
             patch('kiroclops.local_engine.review.audit_native_run') as audit:
            session.return_value.inspect.return_value = state
            with self.assertRaises(VisualError) as error:
                launcher._postturn_report(self.session, record)
        self.assertEqual('wrong_result', error.exception.code)
        audit.assert_not_called()
        self.assertFalse((self.session / 'report').exists())

    def test_report_follows_completed_native_audit_and_binds_files(self):
        events = []
        def audit(_):
            events.append('native_audit')
            return {'status': 'pass'}
        def build(directory):
            events.append('report')
            self.assertEqual(['native_audit', 'report'], events)
            output = Path(directory) / 'report'
            output.mkdir()
            write_json(output / 'report.json', {'native_run_status': 'pass',
                'kiro_run': {'status': 'completed'}, 'selected_candidate_id': 'candidate-02',
                'overall_acceptance': 'pending'})
            (output / 'index.html').write_text('<html></html>', encoding='utf-8')
            return {'status': 'built', 'data': str(output / 'report.json'),
                    'report': str(output / 'index.html')}
        with patch.object(launcher, 'Session') as session, \
             patch('kiroclops.local_engine.review.audit_native_run', side_effect=audit), \
             patch('kiroclops.local_engine.reporting.build_report', side_effect=build):
            session.return_value.inspect.return_value = {'active_job_id': None,
                'selected_candidate_id': 'candidate-02'}
            result = launcher._postturn_report(self.session, self.completed)
        self.assertEqual('pass', result['status'])
        self.assertEqual(file_sha256(Path(result['data'])), result['data_sha256'])
        self.assertEqual(file_sha256(Path(result['report'])), result['report_sha256'])
        self.assertEqual('pending', result['overall_acceptance'])

    def test_report_with_running_native_status_is_rejected(self):
        def build(directory):
            output = Path(directory) / 'report'
            output.mkdir()
            write_json(output / 'report.json', {'native_run_status': 'fail',
                'kiro_run': {'status': 'running'}, 'selected_candidate_id': None})
            (output / 'index.html').write_text('incomplete', encoding='utf-8')
            return {'data': str(output / 'report.json'), 'report': str(output / 'index.html')}
        with patch.object(launcher, 'Session') as session, \
             patch('kiroclops.local_engine.review.audit_native_run', return_value={'status': 'pass'}), \
             patch('kiroclops.local_engine.reporting.build_report', side_effect=build):
            session.return_value.inspect.return_value = {'active_job_id': None,
                'selected_candidate_id': None}
            with self.assertRaises(VisualError) as error:
                launcher._postturn_report(self.session, self.completed)
        self.assertEqual('invalid_report', error.exception.code)

    def test_launcher_cli_rejects_completed_turn_without_report(self):
        registration = self.root / 'registration.json'
        registration.write_text('{}', encoding='utf-8')
        argv = ['launcher', '--session', str(self.session), '--registration', str(registration)]
        with patch.object(sys, 'argv', argv), patch.object(launcher, 'run', return_value={
            'status': 'completed', 'postturn_report': {'status': 'failed', 'code': 'invalid_report'}}):
            self.assertEqual(1, launcher.main())

    def test_both_arm_instructions_end_before_report(self):
        for mode in ('tools', 'baseline'):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                target = directory / 'target'
                target.mkdir()
                # A legacy v1 session keeps its frozen 30-minute execution policy
                # without declaring one; unversioned state is treated as revised.
                write_json(directory / 'session.json', {'schema_version': 1, 'status': 'new', 'mode': mode,
                    'domain': 'game', 'target': str(target), 'goal': 'Match jump'})
                with patch.object(launcher, 'Session') as session:
                    session.return_value.inspect.return_value = {'editable_scope': ['player.gd']}
                    launcher.configure(directory)
                instructions = (target / '.kiro' / 'instructions.md').read_text(encoding='utf-8')
                prompt = (target / '.kiro' / 'prompt.md').read_text(encoding='utf-8')
                self.assertIn('end your turn', instructions)
                self.assertIn('builds the report afterward', instructions)
                self.assertIn('Once the finalization submission returns a job ID, end this turn promptly', instructions)
                self.assertIn('runner waits for that exact finalization job', instructions)
                self.assertNotIn('then build_report', instructions)
                self.assertIn('end the turn', prompt)
                self.assertIn('runner waits for the submitted job', prompt)

    def test_cohort_replays_exact_report_and_rejects_tampering(self):
        from kiroclops.local_engine.reporting import report_tree_receipt
        directory = self.session
        output = directory / 'report'
        output.mkdir()
        write_json(output / 'report.json', {'native_run_status': 'pass',
            'kiro_run': {'status': 'completed'}, 'selected_candidate_id': 'candidate-02',
            'candidate_sha256': 'selected-source', 'acceptance_details': {'pass': True}})
        (output / 'index.html').write_text('report', encoding='utf-8')
        (output / 'candidate.diff').write_text('actual source change', encoding='utf-8')
        receipt = {'status': 'pass', 'data': str(output / 'report.json'),
            'data_sha256': file_sha256(output / 'report.json'),
            'report': str(output / 'index.html'),
            'report_sha256': file_sha256(output / 'index.html'),
            'tree': report_tree_receipt(output)}
        stage = {'session': str(directory), 'postturn_report': receipt}
        state = {'candidates': [{'candidate_id': 'candidate-02', 'source_sha256': 'selected-source'}]}
        with patch.object(cohort, 'Session') as session:
            session.return_value._load.return_value = state
            self.assertEqual('pass', cohort._verify_postturn_report(stage, {'pass': True}, 'candidate-02')['native_run_status'])
            (output / 'candidate.diff').write_text('tampered source change', encoding='utf-8')
            with self.assertRaises(VisualError) as error:
                cohort._verify_postturn_report(stage, {'pass': True}, 'candidate-02')
        self.assertEqual('invalid_report', error.exception.code)


class CohortPostturnGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registration = self.root / 'registration.json'
        write_json(self.registration, {'registered': True})
        self.session = self.root / 'trial' / 'panel'
        row = {'id': 'panel-tools-01', 'domain': 'panel', 'mode': 'tools', 'repeat': 1,
               'status': 'registered', 'stages': [{'stage': 'panel', 'session': str(self.session),
                                                 'status': 'registered'}]}
        self.registry = {'rows': [row], 'sessions': [str(self.session)]}
        write_json(self.root / 'progress.json', {'registration_sha256': file_sha256(self.registration),
                                                 'rows': [row]})

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, report):
        launch = {'status': 'completed', 'terminal_cleanup': {'status': 'pass'},
                  'postturn_report': report}
        state = {'final': {'pass': True, 'job_id': 'final'},
                 'selected_candidate_id': 'candidate-01',
                 'candidates': [{'candidate_id': 'candidate-01', 'source_sha256': 'source-hash'}],
                 'budget': {'used_candidates': 1}}
        with patch.object(cohort, 'verify_registration', return_value=self.registry) as verify, \
             patch.object(cohort, '_prepare_stage'), patch.object(cohort, 'run', return_value=launch), \
             patch.object(cohort, '_stage_result', return_value={'pass': True, 'score': 0.04}), \
             patch.object(cohort, 'Session') as session, patch.object(cohort, 'summarize', return_value={}), \
             patch('kiroclops.local_engine.review.audit_native_run', return_value={'status': 'pass'}):
            session.return_value.inspect.return_value = state
            session.return_value._load.return_value = state
            cohort.run_cohort(self.registration)
            verify.assert_called_once_with(self.registration, full=False)
        return read_json(self.root / 'progress.json')['rows'][0]

    def test_completed_native_without_report_is_not_counted(self):
        row = self.run_case({'status': 'failed', 'code': 'invalid_report', 'message': 'No report'})
        self.assertEqual('failed', row['status'])
        self.assertTrue(row['stages'][0]['captured_numerical_pass'])
        self.assertFalse(row['stages'][0]['numerical_pass'])
        self.assertEqual('pass', row['stages'][0]['native_validation_status'])
        self.assertEqual('invalid_report', row['stages'][0]['error']['code'])

    def test_completed_native_with_report_is_eligible_for_numerical_pass(self):
        from kiroclops.local_engine.reporting import report_tree_receipt
        output = self.session / 'report'
        output.mkdir(parents=True)
        write_json(output / 'report.json', {'native_run_status': 'pass',
            'kiro_run': {'status': 'completed'}, 'selected_candidate_id': 'candidate-01',
            'candidate_sha256': 'source-hash', 'acceptance_details': {'pass': True, 'score': 0.04}})
        (output / 'index.html').write_text('report', encoding='utf-8')
        receipt = {'status': 'pass', 'data': str(output / 'report.json'),
                   'report': str(output / 'index.html'),
                   'data_sha256': file_sha256(output / 'report.json'),
                   'report_sha256': file_sha256(output / 'index.html'),
                   'tree': report_tree_receipt(output)}
        row = self.run_case(receipt)
        self.assertEqual('completed', row['status'])
        self.assertTrue(row['stages'][0]['numerical_pass'])
        self.assertEqual('pass', row['stages'][0]['native_validation_status'])

    def test_forged_report_receipt_cannot_advance_trial(self):
        row = self.run_case({'status': 'pass', 'data': 'bound', 'data_sha256': 'hash'})
        self.assertEqual('failed', row['status'])
        self.assertEqual('invalid_report', row['stages'][0]['error']['code'])


if __name__ == '__main__':
    unittest.main()
