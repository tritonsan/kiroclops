"""A stalled progress write cannot prevent owned-worker failure termination."""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine import session
from kiroclops.local_engine.backend import _atomic, _read
from kiroclops.visual.schema import VisualError


class FailurePublicationTests(unittest.TestCase):
    def test_committed_failure_returns_without_forcing_normal_worker_exit(self):
        calls = []
        with patch.object(session.os, "_exit") as exit_process:
            self.assertTrue(session._publish_owned_failure(lambda: calls.append("failed"), 1))
        self.assertEqual(["failed"], calls)
        exit_process.assert_not_called()

    def test_failed_persistence_exits_without_retry_or_success(self):
        calls = []

        def persist():
            calls.append("attempt")
            raise OSError("disk unavailable")

        with patch.object(session.os, "_exit") as exit_process:
            self.assertFalse(session._publish_owned_failure(persist, 1))
        self.assertEqual(["attempt"], calls)
        exit_process.assert_called_once_with(1)

    def test_blocked_persistence_has_bounded_wait_and_daemon_writer(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        daemon = []

        def persist():
            daemon.append(threading.current_thread().daemon)
            entered.set()
            release.wait(2)
            finished.set()

        try:
            with patch.object(session, "FAILURE_PUBLICATION_SECONDS", .030), \
                    patch.object(session.os, "_exit") as exit_process:
                started = time.monotonic()
                self.assertFalse(session._publish_owned_failure(persist, 1))
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(entered.is_set())
                self.assertFalse(finished.is_set())
                exit_process.assert_called_once_with(1)
                self.assertEqual([True], daemon)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_watchdog_exit_occurs_after_successful_failure_commit(self):
        for code in (124, 130):
            with self.subTest(exit_code=code):
                order = []
                with patch.object(session.os, "_exit", side_effect=lambda value: order.append(("exit", value))):
                    self.assertTrue(session._publish_owned_failure(
                        lambda: order.append("committed"), code, exit_after=True))
                self.assertEqual(["committed", ("exit", code)], order)


class OwnedSessionFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="owned failure ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        baseline = self.root / "baseline"
        (baseline / "src").mkdir(parents=True)
        (baseline / "index.html").write_text("<main>Panel</main>", encoding="utf-8")
        (baseline / "src/panel-motion.js").write_text("export const speed=1;", encoding="utf-8")
        reference = self.root / "reference"
        reference.mkdir()
        (reference / "reference.json").write_text("{}", encoding="utf-8")
        self.directory = self.root / "session"
        session.create_session(self.directory, "web_panel", baseline, reference, "Match motion",
            experiment={"experiment_id": "test", "capture_lock": str(self.root / "capture.lock"),
                        "calibration": ["open"]})
        self.client = session.Session(self.directory)
        with patch.object(session.Session, "_spawn"):
            self.job = self.client.submit("prepare")
        self.job_dir = self.directory / "jobs" / self.job["job_id"]

    def test_inprocess_failure_stays_synchronous_and_never_exits(self):
        def runner(*args):
            raise VisualError("capture_progress_failure", "sink failed")

        with patch.object(session.os, "_exit") as exit_process, \
                patch.object(session, "_publish_owned_failure") as publish_owned:
            session.run_session_job(self.directory, self.job["job_id"], runner=runner)
        result = _read(self.job_dir / "job.json")
        self.assertEqual("failed", result["status"])
        self.assertEqual("capture_progress_failure", result["error"]["code"])
        self.assertFalse((self.job_dir / "result.json").exists())
        exit_process.assert_not_called()
        publish_owned.assert_not_called()

    def _blocked_progress(self, *, watchdog):
        entered, release, progress_done = threading.Event(), threading.Event(), threading.Event()
        terminal_write_done, exit_seen = threading.Event(), threading.Event()
        exit_codes, callback_errors = [], []
        callback = None

        def atomic(path, value):
            if Path(path) == self.job_dir / "job.json":
                if value.get("progress", {}).get("phase") == "blocked" and value["status"] == "running":
                    entered.set()
                    release.wait(2)
                if value["status"] in {"failed", "cancelled"}:
                    try:
                        return _atomic(path, value)
                    finally:
                        terminal_write_done.set()
            return _atomic(path, value)

        def fake_exit(code):
            exit_codes.append(code)
            exit_seen.set()

        def runner(directory, job, check, progress, cancel):
            nonlocal callback

            def publish():
                try:
                    progress("blocked")
                except BaseException as exc:
                    callback_errors.append(exc)
                finally:
                    progress_done.set()

            callback = threading.Thread(target=publish, daemon=True)
            callback.start()
            self.assertTrue(entered.wait(1))
            if watchdog:
                _atomic(self.job_dir / "cancel.json", {"requested_at": time.time()})
                # The main evaluator stays occupied while the real watchdog
                # reaches its mocked exit despite the progress-held job lock.
                self.assertTrue(exit_seen.wait(1))
                return {"unexpected": "must never be committed"}
            raise VisualError("capture_progress_failure", "bounded publisher flush expired")

        try:
            with patch.object(session, "_atomic", side_effect=atomic), \
                    patch.object(session, "FAILURE_PUBLICATION_SECONDS", .030), \
                    patch.object(session.os, "_exit", side_effect=fake_exit), \
                    patch.object(session, "_identity", return_value="test-worker"), \
                    patch("kiroclops.local_engine.worker._contain_windows_children"):
                started = time.monotonic()
                session.run_session_job(self.directory, self.job["job_id"], runner=runner, owned_process=True)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(exit_seen.is_set())
                self.assertFalse(progress_done.is_set())
                self.assertEqual("running", _read(self.job_dir / "job.json")["status"])
                self.assertFalse((self.job_dir / "result.json").exists())
                self.assertTrue(exit_codes)
                self.assertEqual({130 if watchdog else 1}, set(exit_codes))
                # A real os._exit would end both writers. Release these test
                # threads explicitly before restoring mocks/removing fixtures.
                release.set()
                self.assertTrue(progress_done.wait(1))
                self.assertTrue(terminal_write_done.wait(1))
                callback.join(1)
                for writer in threading.enumerate():
                    if writer.name in {"session-failure", "session-deadline"}:
                        writer.join(1)
                        self.assertFalse(writer.is_alive())
                self.assertEqual([], callback_errors)
        finally:
            release.set()
            if callback is not None:
                callback.join(1)
            for writer in threading.enumerate():
                if writer.name in {"session-failure", "session-deadline"}:
                    writer.join(1)

    def test_main_failure_exits_when_real_progress_update_holds_lock(self):
        self._blocked_progress(watchdog=False)

    def test_cancellation_watchdog_exits_when_real_progress_update_holds_lock(self):
        self._blocked_progress(watchdog=True)


if __name__ == "__main__":
    unittest.main()
