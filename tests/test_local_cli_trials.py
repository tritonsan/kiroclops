"""The neutral CLI can reconnect to durable session jobs without running CV itself."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine.__main__ import main
from kiroclops.visual.schema import VisualError


class TrialCommandsTests(unittest.TestCase):
    def invoke(self, operation, *options, session=None):
        output = io.StringIO()
        with patch('kiroclops.local_engine.environment.configure_environment'), \
             patch('kiroclops.local_engine.session.Session') as factory, \
             contextlib.redirect_stdout(output):
            if session is not None:
                factory.return_value = session
            else:
                session = factory.return_value
            exit_code = main(['trial', operation, '--session', 'test session naïve', *options])
        return exit_code, json.loads(output.getvalue()), factory, session

    def test_async_submits_each_operation_once_and_never_polls(self):
        for operation in ('prepare', 'evaluate', 'finalize', 'report'):
            with self.subTest(operation=operation):
                from unittest.mock import Mock
                session = Mock()
                receipt = {'job_id': 'a' * 32, 'status': 'queued', 'candidate_id': None}
                session.submit.return_value = receipt
                options = ('--candidate', 'candidate-02') if operation == 'finalize' else ()
                code, result, factory, _ = self.invoke(operation, '--async', *options, session=session)
                self.assertEqual(0, code)
                self.assertEqual(receipt, result)
                factory.assert_called_once_with(Path('test session naïve'))
                session.submit.assert_called_once_with(operation, candidate_id='candidate-02' if options else None)
                session.get_job_feedback.assert_not_called()
                session.cancel_job.assert_not_called()

    def test_sync_default_keeps_polling_until_terminal(self):
        from unittest.mock import Mock
        session = Mock()
        session.submit.return_value = {'job_id': 'a' * 32, 'status': 'queued'}
        final = {'job_id': 'a' * 32, 'status': 'succeeded', 'result': {'neutral_capture': True, 'media_paths': ['raw/frame.png']}}
        session.get_job_feedback.side_effect = [{'status': 'running'}, final]
        code, result, _, _ = self.invoke('evaluate', session=session)
        self.assertEqual((0, final), (code, result))
        self.assertEqual(2, session.get_job_feedback.call_count)
        session.get_job_feedback.assert_called_with('a' * 32, wait_seconds=10)

    def test_sync_report_returns_native_deferral_without_polling(self):
        from unittest.mock import Mock
        session = Mock()
        receipt = {'job_id': None, 'status': 'deferred_to_runner', 'candidate_id': None}
        session.submit.return_value = receipt
        code, result, _, _ = self.invoke('report', session=session)
        self.assertEqual((0, receipt), (code, result))
        session.submit.assert_called_once_with('report', candidate_id=None)
        session.get_job_feedback.assert_not_called()

    def test_job_reconnects_only_to_requested_session_with_bounded_wait(self):
        from unittest.mock import Mock
        for options, wait in (((), 0), (('--wait-seconds', '0.25'), .25), (('--wait-seconds', '60'), 60)):
            with self.subTest(wait=wait):
                session = Mock()
                payload = {'status': 'running', 'progress': {'phase': 'capture'}}
                session.get_job_feedback.return_value = payload
                code, result, factory, _ = self.invoke('job', '--job-id', 'b' * 32, *options, session=session)
                self.assertEqual((0, payload), (code, result))
                factory.assert_called_once_with(Path('test session naïve'))
                session.get_job_feedback.assert_called_once_with('b' * 32, wait_seconds=wait)
                session.submit.assert_not_called()

    def test_cancel_delegates_only_the_requested_job(self):
        from unittest.mock import Mock
        session = Mock()
        session.cancel_job.return_value = {'job_id': 'c' * 32, 'status': 'cancelled'}
        code, result, _, _ = self.invoke('cancel', '--job-id', 'c' * 32, session=session)
        self.assertEqual(1, code)
        self.assertEqual('cancelled', result['status'])
        session.cancel_job.assert_called_once_with('c' * 32)
        session.submit.assert_not_called()
        session.get_job_feedback.assert_not_called()

    def test_inspect_stays_sanitized_service_response(self):
        from unittest.mock import Mock
        session = Mock()
        session.inspect.return_value = {'mode': 'baseline', 'remaining_seconds': 1700, 'status': 'prepared'}
        code, result, _, _ = self.invoke('inspect', session=session)
        self.assertEqual(0, code)
        self.assertEqual(session.inspect.return_value, result)
        session.submit.assert_not_called()
        session.get_job_feedback.assert_not_called()

    def test_foreign_or_unknown_job_error_does_not_fall_back_to_other_store(self):
        from unittest.mock import Mock
        session = Mock()
        session.get_job_feedback.side_effect = VisualError('unknown_job', 'No job in this session')
        code, result, factory, _ = self.invoke('job', '--job-id', 'd' * 32, session=session)
        self.assertEqual(1, code)
        self.assertEqual('unknown_job', result['code'])
        factory.assert_called_once_with(Path('test session naïve'))
        session.submit.assert_not_called()

    def test_invalid_flag_combinations_have_no_session_side_effects(self):
        cases = [
            ('inspect', '--async'), ('job', '--job-id', 'a' * 32, '--async'),
            ('cancel', '--job-id', 'a' * 32, '--async'),
            ('job',), ('cancel',), ('prepare', '--job-id', 'a' * 32),
            ('evaluate', '--candidate', 'candidate-01'), ('inspect', '--candidate', 'candidate-01'),
            ('cancel', '--job-id', 'a' * 32, '--candidate', 'candidate-01'),
            ('prepare', '--wait-seconds', '0'), ('inspect', '--wait-seconds', '0'),
            ('cancel', '--job-id', 'a' * 32, '--wait-seconds', '0'),
        ]
        cases += [('job', '--job-id', 'a' * 32, '--wait-seconds', value) for value in ('-1', '60.1', 'nan', 'inf')]
        for operation, *options in cases:
            with self.subTest(operation=operation, options=options):
                code, result, factory, _ = self.invoke(operation, *options)
                self.assertEqual(1, code)
                self.assertEqual('invalid_request', result['code'])
                factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
