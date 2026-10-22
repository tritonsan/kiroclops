"""Scenario-level re-recording after host timing stalls (V6 plan WP2.1)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiroclops.local_engine import evaluation
from kiroclops.visual.schema import VisualError


class _TimingStall(RuntimeError):
    code = 'capture_timing_failure'


class CaptureRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'café'
        self.root.mkdir()
        patcher = mock.patch.object(evaluation, 'RETRY_PAUSE_S', 0.)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notes = []

    def fake_capture(self, outcomes):
        calls = []

        def capture(project, output, scenario, check, progress, cancel, variant, *, reference_selection=None, analyze=True):
            calls.append(str(output))
            output = Path(output)
            output.mkdir(parents=True)
            (output/'capture-error.json').write_text('{"status":"error"}', encoding='utf-8')
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return {'scenario': scenario, 'package': str(output/'package'), 'variant': variant}
        capture.calls = calls
        return capture

    def run_matrix(self, outcomes, attempts=None):
        capture = self.fake_capture(outcomes)
        with mock.patch.object(evaluation, '_capture_web', capture), \
                mock.patch('kiroclops.web_local.specs.scenarios', lambda domain, split: [{'id': 's1', 'domain': domain}]):
            kwargs = {} if attempts is None else {'attempts': attempts}
            if attempts is None:
                rows = evaluation._web_matrix('proj', self.root, 'panel', 'calibration', lambda: None,
                                              lambda text, i, n: self.notes.append(text), None, 'candidate')
            else:
                rows = [evaluation._capture_web_retrying('proj', self.root/'s1', {'id': 's1', 'domain': 'panel'}, lambda: None,
                                                         lambda *a: None, None, 'candidate', note=self.notes.append, **kwargs)]
        return rows, capture.calls

    def test_timing_stall_is_re_recorded_and_evidence_kept(self):
        rows, calls = self.run_matrix([_TimingStall('BeginFrame exceeded the120ms capture deadline'), None])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        row = rows[0]
        self.assertEqual([a.get('status', a.get('code')) for a in row['recording_retries']], ['capture_timing_failure', 'ok'])
        preserved = self.root/'s1~failed-attempt1'
        self.assertTrue((preserved/'capture-error.json').is_file())
        self.assertEqual(row['recording_retries'][0]['evidence'], str(preserved))
        self.assertTrue((self.root/'s1'/'capture-error.json').is_file())
        self.assertTrue(any('re-recording after capture_timing_failure' in n for n in self.notes))

    def test_measurement_failures_are_not_retried(self):
        with self.assertRaisesRegex(VisualError, 'ratio'):
            self.run_matrix([VisualError('tracking_failure', 'Object valid-frame ratio below contract minimum'), None])
        self.assertFalse((self.root/'s1~failed-attempt1').exists())

    def test_exhausted_attempts_fail_with_history(self):
        with self.assertRaisesRegex(VisualError, 'attempt 3 of 3; earlier attempts: 1=capture_timing_failure; 2=machine_not_quiet') as ctx:
            self.run_matrix([_TimingStall('one'), VisualError('machine_not_quiet', 'two'), _TimingStall('three')], attempts=3)
        self.assertEqual(ctx.exception.code, 'capture_timing_failure')
        self.assertTrue((self.root/'s1~failed-attempt1').is_dir())
        self.assertTrue((self.root/'s1~failed-attempt2').is_dir())

    def test_clean_capture_lists_a_single_attempt(self):
        rows, calls = self.run_matrix([None])
        self.assertEqual(rows[0]['recording_retries'], [{'attempt': 1, 'status': 'ok'}])


if __name__ == '__main__':
    unittest.main()
