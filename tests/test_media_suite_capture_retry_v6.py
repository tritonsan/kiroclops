"""Suite cases are re-recorded after host timing stalls (V6 plan WP2.1, media_suite path)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiroclops.local_engine import evaluation, media_suite
from kiroclops.visual.schema import VisualError


class _Stall(RuntimeError):
    code = 'capture_timing_failure'


class SuiteCaptureRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'suite café naïve'
        self.root.mkdir()
        patcher = mock.patch.object(evaluation, 'RETRY_PAUSE_S', 0.)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.case = {'case_id': 'cal-open-r1', 'stage': 'panel', 'scenario': {'id': 'open', 'domain': 'panel'}}
        self.notes = []

    def fake(self, outcomes):
        def once(project, case, destination, check, progress, cancel, variant, reference, *, prepared_targets=None, job_id=None, acceptance_policy=None):
            destination = Path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination/'capture-error.json').write_text('{}', encoding='utf-8')
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return {'scenario': case['scenario'], 'package': str(destination/'package')}
        return once

    def capture(self, outcomes):
        with mock.patch.object(media_suite, '_capture_once', self.fake(outcomes)):
            return media_suite._capture('proj', self.case, self.root/'candidate'/'cal-open-r1', lambda: None,
                                        lambda *a: self.notes.append(a[0]), None, 'candidate', {'selection': {}})

    def test_stall_is_re_recorded_and_attempts_listed(self):
        record = self.capture([_Stall('BeginFrame exceeded the120ms capture deadline'), None])
        self.assertEqual([a.get('status', a.get('code')) for a in record['recording_retries']], ['capture_timing_failure', 'ok'])
        self.assertTrue((self.root/'candidate'/'cal-open-r1~failed-attempt1'/'capture-error.json').is_file())
        self.assertTrue((self.root/'candidate'/'cal-open-r1'/'capture-error.json').is_file())
        self.assertTrue(any('re-recording after capture_timing_failure' in n for n in self.notes))

    def test_other_failures_pass_through_and_exhaustion_reports_history(self):
        with self.assertRaisesRegex(VisualError, 'identities differ'):
            self.capture([VisualError('tracking_failure', 'Input identities differ'), None])
        self.assertFalse((self.root/'candidate'/'cal-open-r1~failed-attempt1').exists())
        with self.assertRaisesRegex(VisualError, 'attempt 3 of 3') as caught:
            self.capture([_Stall('a'), _Stall('b'), _Stall('c')])
        self.assertEqual(caught.exception.code, 'capture_timing_failure')
        self.assertTrue((self.root/'candidate'/'cal-open-r1~failed-attempt2').is_dir())


if __name__ == '__main__':
    unittest.main()
