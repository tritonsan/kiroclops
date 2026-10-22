"""A sealed final candidate can finish locally after the native turn ends."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine.backend import _atomic, _digest, _read
from kiroclops.local_engine.session import BINDING_FIELDS, Session, create_session, finalize_validation_deadline, run_session_job
from kiroclops.local_engine import trial_cleanup as cleanup
from kiroclops.local_engine import launcher
from kiroclops.visual.schema import VisualError


class FinalizeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="finalize handoff ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        baseline = self.root / "baseline"
        (baseline / "src").mkdir(parents=True)
        (baseline / "src/panel-motion.js").write_text("export const speed=1", encoding="utf-8")
        reference = self.root / "reference"
        reference.mkdir()
        (reference / "recording").write_text("reference pixels", encoding="utf-8")
        self.directory = self.root / "session"
        create_session(self.directory, "web_panel", baseline, reference, "Match motion",
                       experiment={"capture_lock": str(self.root / "capture.lock")})
        self.session = Session(self.directory)

    def submit(self, operation, result=None):
        with patch.object(Session, "_spawn"):
            submitted = self.session.submit(operation)
        if result is not None:
            run_session_job(self.directory, submitted["job_id"], runner=lambda *args: result)
            self.session.get_job(submitted["job_id"])
        return submitted

    def sealed(self):
        self.submit("prepare", {"ready": True})
        self.submit("evaluate", {"eligible": True, "score": .05})
        return self.submit("finalize")

    def test_final_validation_survives_expired_authoring_clock(self):
        final = self.sealed()
        state = self.session._load()
        job = _read(self.directory / "jobs" / final["job_id"] / "job.json")
        self.assertEqual(1800, state["budget"]["max_validation_seconds"])
        self.assertEqual(job["created_at"] + 1800, finalize_validation_deadline(state, job))
        state["deadline_at"] = time.time() - 1
        _atomic(self.directory / "session.json", state)
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        result = self.session.get_job(final["job_id"])
        self.assertEqual("succeeded", result["status"])
        self.assertTrue(result["result"]["pass"])
        with self.assertRaises(VisualError) as error:
            self.session.submit("evaluate")
        self.assertEqual("session_sealed", error.exception.code)

    def test_separate_validation_deadline_still_fails_closed(self):
        final = self.sealed()
        state = self.session._load()
        job = _read(self.directory / "jobs" / final["job_id"] / "job.json")
        after_validation = finalize_validation_deadline(state, job) + 1
        with patch("kiroclops.local_engine.session.time.time", return_value=after_validation):
            run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        result = self.session.get_job(final["job_id"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("validation_budget_exhausted", result["error"]["code"])

    def test_validation_budget_is_bound_and_old_v1_budget_remains_readable(self):
        state = self.session._load()
        state["budget"]["max_validation_seconds"] = 3600
        _atomic(self.directory / "session.json", state)
        with self.assertRaises(VisualError) as error:
            Session(self.directory)
        self.assertEqual("session_binding_changed", error.exception.code)

        # Emulate an existing v1 session whose bound budget predates this field.
        state["budget"].pop("max_validation_seconds")
        _atomic(self.directory / "session.json", state)
        _atomic(self.directory / "binding.json",
                {"sha256": _digest({key: state[key] for key in BINDING_FIELDS})})
        self.session = Session(self.directory)
        final = self.sealed()
        job = _read(self.directory / "jobs" / final["job_id"] / "job.json")
        self.assertEqual(job["created_at"] + 1800,
                         finalize_validation_deadline(self.session._load(), job))

    def test_native_turn_cannot_read_heldout_result_after_early_completion(self):
        final = self.sealed()
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        launch = self.directory / "kiro" / "launch.json"
        _atomic(launch, {"status": "running"})
        visible = self.session.inspect()
        self.assertEqual("sealed", visible["status"])
        self.assertEqual(final["job_id"], visible["active_job_id"])
        self.assertIsNone(visible["final"])
        self.assertEqual("deferred_to_runner", self.session.get_job(final["job_id"])["status"])
        self.assertIsNone(self.session.get_job(final["job_id"])["result"])
        self.assertTrue(self.session.get_job(final["job_id"], allow_final_result=True)["result"]["pass"])
        self.assertEqual("deferred_to_runner", self.session.submit("finalize")["status"])

    def test_completed_turn_preserves_only_bound_final_job(self):
        final = self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        self.assertTrue(request["preserved_finalization"])
        self.assertTrue(request["had_pending_job"])
        self.assertFalse((self.directory / "jobs" / final["job_id"] / "cancel.json").exists())
        self.assertEqual(finalize_validation_deadline(self.session._load(),
                         _read(self.directory / "jobs" / final["job_id"] / "job.json")),
                         request["validation_deadline_at"])
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        result = cleanup.finish_terminal_cleanup(self.session, request, timeout=.2)
        self.assertEqual("pass", result["status"])
        self.assertEqual("succeeded", result["terminal_job_status"])
        self.assertFalse((self.directory / "jobs" / final["job_id"] / "cancel.json").exists())

    def test_terminal_worker_and_capture_lease_are_free_before_client_close(self):
        final = self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        self.assertEqual("succeeded", self.session.get_job(final["job_id"])["status"])
        released = launcher._await_preserved_worker_release(self.session, request)
        self.assertTrue(released["worker_exit_verified_before_client_close"])
        self.assertTrue(released["capture_lease_released_before_client_close"])

    def test_worker_release_rejects_nonterminal_final_job(self):
        self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        with self.assertRaises(VisualError) as error:
            launcher._await_preserved_worker_release(self.session, request)
        self.assertEqual("cleanup_binding_changed", error.exception.code)

    def test_terminal_worker_release_has_five_second_grace(self):
        final = self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        self.assertEqual("succeeded", self.session.get_job(final["job_id"])["status"])
        with patch("kiroclops.local_engine.backend._alive", return_value=True), \
                patch("kiroclops.local_engine.launcher.time.time", side_effect=[1000.0, 1006.0]):
            with self.assertRaises(VisualError) as error:
                launcher._await_preserved_worker_release(self.session, request)
        self.assertEqual("finalization_wait_timeout", error.exception.code)

    def test_pending_prepare_cannot_be_preserved(self):
        self.submit("prepare")
        with self.assertRaises(VisualError) as error:
            cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        self.assertEqual("cleanup_binding_changed", error.exception.code)

    def test_timeout_abort_cancels_exact_queued_final_without_spawning(self):
        final = self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        with patch.object(Session, "_spawn") as spawned:
            cleanup.cancel_preserved_finalization(self.session, request)
            receipt = cleanup.finish_terminal_cleanup(self.session, request, timeout=.2)
        spawned.assert_not_called()
        self.assertEqual("pass", receipt["status"])
        self.assertEqual("cancelled", self.session.get_job(final["job_id"])["status"])

    def test_preserved_pending_worker_is_not_killed_by_cleanup_barrier(self):
        final = self.sealed()
        job_path = self.directory / "jobs" / final["job_id"] / "job.json"
        job = _read(job_path)
        job.update(status="running", worker={"pid": 987654, "identity": "owned-process"})
        _atomic(job_path, job)
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        with patch.object(cleanup, "_alive", return_value=True), patch.object(cleanup, "_terminate_owned") as killed:
            receipt = cleanup.finish_terminal_cleanup(self.session, request, timeout=.05)
        self.assertEqual("unconfirmed", receipt["status"])
        self.assertEqual("terminal_cleanup_unconfirmed", receipt["error"]["code"])
        self.assertFalse((job_path.parent / "cancel.json").exists())
        killed.assert_not_called()

    def test_late_worker_identity_is_adopted_once_then_frozen(self):
        final = self.sealed()
        request = cleanup.begin_terminal_cleanup(self.session, preserve_finalization=True)
        job_path = self.directory / "jobs" / final["job_id"] / "job.json"
        job = _read(job_path)
        job.update(status="succeeded", worker={"pid": 987654, "identity": "first-process"})
        _atomic(job_path, job)
        with patch.object(cleanup, "_alive", return_value=False):
            receipt = cleanup.finish_terminal_cleanup(self.session, request, timeout=.2)
        self.assertEqual("pass", receipt["status"])
        self.assertEqual(job["worker"], request["worker"])
        job["worker"] = {"pid": 987654, "identity": "different-process"}
        _atomic(job_path, job)
        with patch.object(cleanup, "_terminate_owned") as killed:
            receipt = cleanup.finish_terminal_cleanup(self.session, request, timeout=.1)
        self.assertEqual("cleanup_binding_changed", receipt["error"]["code"])
        killed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
