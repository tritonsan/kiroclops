"""Real Windows lock and retained-process-handle regressions; no game/model."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kiroclops.capture import process_options
from kiroclops.integration import session, storage

PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows byte locks and process handles')
class WindowsStorageLifecycleTests(unittest.TestCase):
    def child(self, code, *args):
        process = subprocess.Popen([sys.executable, '-u', '-c', code, *map(str, args)], cwd=PROJECT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'PYTHONPATH': str(PROJECT), 'PYTHONUTF8': '1'}, **process_options())
        self.addCleanup(self.stop_child, process)
        return process

    @staticmethod
    def stop_child(process):
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
            process.stdin = None
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def test_contended_initial_read_retries_until_other_process_unlocks(self):
        code = ('from kiroclops.integration.storage import file_lock\n'
                'import sys\nprint("attempt",flush=True)\n'
                'with file_lock(sys.argv[1],timeout=3): print("acquired",flush=True)\n')
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'shared.lock'
            with storage.file_lock(path):
                process = self.child(code, path)
                self.assertEqual(process.stdout.readline().strip(), 'attempt')
                time.sleep(.2)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn('acquired', stdout)

    def test_contended_initial_read_times_out_with_session_busy(self):
        code = ('from kiroclops.integration.storage import file_lock,IntegrationError\n'
                'import sys\nprint("attempt",flush=True)\n'
                'try:\n with file_lock(sys.argv[1],timeout=.15): print("incorrect-acquire")\n'
                'except IntegrationError as exc: print(exc.code,flush=True)\n')
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'shared.lock'
            with storage.file_lock(path):
                process = self.child(code, path)
                self.assertEqual(process.stdout.readline().strip(), 'attempt')
                stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), 'session_busy')
            # Timed-out contenders must leave the lock reusable.
            with storage.file_lock(path, timeout=.1):
                pass

    def test_retained_popen_handle_after_exit_cannot_keep_a_job_queued(self):
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        process = self.child('import sys\nprint("ready",flush=True)\nsys.stdin.readline()\nsys.exit(259)')
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        identity = storage.process_identity(process.pid)
        self.assertIsNotNone(identity)
        process.stdin.write('\n'); process.stdin.flush()
        self.assertEqual(api.WaitForSingleObject(int(process._handle), 5000), 0)
        # Deliberately retain Popen._handle and never poll/wait. Windows still
        # allows OpenProcess/GetProcessTimes on this terminated process object.
        self.assertIsNone(process.returncode)
        self.assertIsNone(storage.process_identity(process.pid))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / 'sessions/retained-handle'
            state = {'session_id': directory.name, 'status': 'evaluating', 'active_job_id': 'job-exited',
                     'reference': {'calibration': []}, 'selected_candidate_id': None,
                     'candidates': [{'candidate_id': 'candidate-01', 'status': 'queued'}]}
            storage.write_json(directory / 'session.json', state)
            path = directory / 'jobs/job-exited.json'
            storage.write_json(path, {'job_id': 'job-exited', 'status': 'queued',
                'worker_pid': process.pid, 'worker_identity': identity, 'candidate_id': 'candidate-01'})
            with patch.object(storage, 'ROOT', root), patch.object(session, 'ROOT', root):
                selected = session.Session(directory)
                selected.recover()
                self.assertEqual(storage.read_json(path)['status'], 'interrupted')
                self.assertIsNone(selected.read()['active_job_id'])
                self.assertEqual(len(selected.read()['candidates']), 1)


if __name__ == '__main__':
    unittest.main()
