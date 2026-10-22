"""The read-only UI progress view rides out the brief Windows sharing violation of an atomic replace."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kiroclops.local_engine import workflow_status
from kiroclops.visual.schema import write_json


class ProgressViewRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        write_json(self.directory/'session.json', {'status': 'prepared', 'prepared': True, 'candidates': [],
                                                   'budget': {'max_candidates': 3}})

    def test_transient_permission_error_is_retried(self):
        real = workflow_status.read_json
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(13, 'Permission denied', str(path))
            return real(path)
        with mock.patch.object(workflow_status, 'read_json', side_effect=flaky), \
                mock.patch.object(workflow_status.time, 'sleep') as sleep:
            view = workflow_status.progress_view(self.directory, now=0.)
        self.assertEqual('prepared', view['session_status'])
        self.assertEqual(3, len(calls))
        self.assertEqual(2, sleep.call_count)

    def test_lasting_permission_error_still_raises(self):
        with mock.patch.object(workflow_status, 'read_json', side_effect=PermissionError(13, 'Permission denied')), \
                mock.patch.object(workflow_status.time, 'sleep'), self.assertRaises(PermissionError):
            workflow_status.progress_view(self.directory)


if __name__ == '__main__':
    unittest.main()
