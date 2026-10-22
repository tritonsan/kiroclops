"""Session integrity and bounded job lifecycle without launching Godot."""
from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import runner, session, storage, worker


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Workspace café"
        self.directory = self.root / "sessions/test-session"
        self.baseline = self.directory / "baseline"
        self.target = self.directory / "target"
        self.baseline.mkdir(parents=True)
        for module in (storage, session, runner):
            mocked = patch.object(module, "ROOT", self.root)
            mocked.start()
            self.addCleanup(mocked.stop)
        for module in (session, worker):
            mocked = patch.object(module, "process_identity", side_effect=lambda pid: f"process-{pid}")
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, contents in {
            "project.godot": "[application]\nconfig/name=\"Slay\"\n",
            "source/characters/player/player.gd": "extends CharacterBody2D\nvar velocity_test = 1\n",
            "source/characters/player/state_machine/states/jump.gd": "extends Node\n",
            "source/characters/player/player.tscn": "[gd_scene]\n",
            "source/maps/map_scenes/map_demo.tscn": "[gd_scene]\n",
            "assets/character.png": "original-pixels",
            "assets/character.png.import": "[params]\nfilter=false\n",
            "source/characters/player/player.gd.uid": "uid://player",
            "kiroclops_capture/capture.gd": "frozen fixture",
        }.items():
            path = self.baseline / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        storage.copy_project(self.baseline, self.target)
        self.state = {
            "schema_version": 2, "session_id": "test-session", "mode": "tools", "status": "ready",
            "target": str(self.target), "baseline": str(self.baseline),
            "baseline_hashes": storage.project_hashes(self.baseline),
            "candidates": [], "reference": {"calibration": []}, "baseline_runs": {},
            "selected_candidate_id": None, "active_job_id": None, "final": None,
            "started_at": None, "deadline_at": None,
        }
        storage.write_json(self.directory / "session.json", self.state)
        self.session = session.Session(self.directory)
        self.process = Mock(pid=54321)
        self.process.poll.return_value = None
        self.launch = patch.object(session.subprocess, "Popen", return_value=self.process)
        self.launch.start()
        self.addCleanup(self.launch.stop)

    def reserve_candidate(self, number=1):
        candidate_id = f"candidate-{number:02d}"
        self.session.update(lambda s: s["candidates"].append({"candidate_id": candidate_id, "status": "queued"}))
        return candidate_id

    def test_snapshot_is_isolated_and_new_controller_helpers_are_allowed(self):
        candidate_id = self.reserve_candidate()
        player = self.target / "source/characters/player/player.gd"
        player.write_text("extends CharacterBody2D\nvar new_jump = 42\n")
        helper = self.target / "source/characters/player/motion/helper.gd"
        helper.parent.mkdir()
        helper.write_text("extends RefCounted\n")
        cached = self.target / ".godot/imported/cache.bin"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"generated")
        root = session.create_candidate_snapshot(self.session, candidate_id)
        snapshot = root / "project"
        frozen = storage.project_hashes(snapshot)
        self.assertEqual(frozen, storage.read_json(root / "candidate.json")["source_hashes"])
        self.assertIn("source/characters/player/motion/helper.gd", frozen)
        self.assertFalse((snapshot / ".godot").exists())
        self.assertTrue((snapshot / "assets/character.png.import").exists())
        self.assertTrue((snapshot / "source/characters/player/player.gd.uid").exists())
        self.assertIn("new_jump", (root / "patch.diff").read_text())
        player.write_text("later edit")
        self.assertEqual(storage.project_hashes(snapshot), frozen)
        self.assertEqual(storage.project_hashes(self.baseline), self.state["baseline_hashes"])

    def test_fingerprint_is_identical_to_runner_and_ignores_generated_sidecars(self):
        original = storage.project_hashes(self.target)
        for name in ("new.gd.uid", "assets/character.png.import", ".runtime/log/config.json",
                     ".import/imported.bin", ".kiro/settings/mcp.json", ".kiroclops-reference/video.mp4",
                     ".kiroclops-evidence/frames.png", "discard.pyc", "Thumbs.db"):
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated")
        self.assertEqual(original, storage.project_hashes(self.target))
        self.assertEqual(storage.project_hashes(self.target), runner.source_hashes(self.target))
        self.assertNotIn("assets/character.png.import", original)

    def test_frozen_config_scene_assets_fixture_edits_and_controller_deletion_rejected(self):
        names = ["project.godot", "assets/character.png", "source/characters/player/player.tscn",
                 "kiroclops_capture/capture.gd", "source/maps/map_scenes/map_demo.tscn"]
        for name in names:
            path = self.target / name
            original = path.read_bytes()
            path.write_bytes(b"changed")
            with self.subTest(name=name), self.assertRaises(storage.IntegrationError) as ctx:
                storage.assert_allowed_changes(self.target, self.state["baseline_hashes"])
            self.assertEqual(ctx.exception.code, "experiment_integrity")
            path.write_bytes(original)
        (self.target / "source/characters/player/player.gd").unlink()
        with self.assertRaises(storage.IntegrationError) as ctx:
            storage.assert_allowed_changes(self.target, self.state["baseline_hashes"])
        self.assertEqual(ctx.exception.code, "experiment_integrity")

    def test_mutation_during_snapshot_copy_rejected(self):
        cid = self.reserve_candidate()
        real_copy = storage.copy_project
        def changing_copy(source, destination):
            result = real_copy(source, destination)
            (source / "source/characters/player/player.gd").write_text("changed during copy")
            return result
        with patch.object(session, "copy_project", side_effect=changing_copy):
            with self.assertRaises(storage.IntegrationError) as ctx:
                session.create_candidate_snapshot(self.session, cid)
        self.assertEqual(ctx.exception.code, "source_changed_during_snapshot")

    def test_compile_failures_consume_candidate_budget(self):
        with patch.object(runner, "import_project", side_effect=runner.RunnerError("import_error", "SCRIPT ERROR: test syntax error")), \
             patch.object(worker.traceback, "print_exc"):
            for number in range(1, 4):
                submitted = self.session.submit("evaluate")
                self.assertEqual(submitted["candidate_id"], f"candidate-{number:02d}")
                result = worker.run_job(self.directory, submitted["job_id"])
                self.assertEqual(result, 1)
                state = self.session.read()
                self.assertEqual(state["candidates"][-1]["status"], "failed")
                self.assertEqual(state["candidates"][-1]["error"]["code"], "import_error")
                self.assertEqual(state["status"], "ready")
                self.assertIsNone(state["active_job_id"])
        with self.assertRaises(storage.IntegrationError) as ctx:
            self.session.submit("evaluate")
        self.assertEqual(ctx.exception.code, "budget_exhausted")
        self.assertEqual(len(self.session.read()["candidates"]), 3)

    def test_best_eligible_candidate_sealed_once(self):
        candidates = [
            {"candidate_id": "candidate-01", "status": "evaluated", "feedback": {"eligible": True, "mean_rmse_body": .10}, "smoke": {"status": "passed"}},
            {"candidate_id": "candidate-02", "status": "evaluated", "feedback": {"eligible": True, "mean_rmse_body": .05}, "smoke": {"status": "passed"}},
            {"candidate_id": "candidate-03", "status": "evaluated", "feedback": {"eligible": False, "mean_rmse_body": .01}, "smoke": {"status": "failed"}},
        ]
        self.session.update(lambda s: s.update(candidates=candidates))
        with self.assertRaises(storage.IntegrationError) as ctx:
            self.session.submit("finalize", "candidate-01")
        self.assertEqual(ctx.exception.code, "candidate_not_best")
        self.assertIsNone(self.session.read()["selected_candidate_id"])
        submitted = self.session.submit("finalize")
        self.assertEqual(submitted["candidate_id"], "candidate-02")
        self.assertEqual(self.session.read()["selected_candidate_id"], "candidate-02")
        self.session.update(lambda s: s.update(active_job_id=None, status="ready"))
        for action in ("evaluate", "finalize"):
            with self.subTest(action=action), self.assertRaises(storage.IntegrationError) as ctx:
                self.session.submit(action)
            self.assertEqual(ctx.exception.code, "validation_sealed")

    def test_dead_worker_recovery_and_pid_reuse(self):
        submitted = self.session.submit("evaluate")
        job_path = self.directory / "jobs" / f"{submitted['job_id']}.json"
        self.assertEqual(storage.read_json(job_path)["worker_pid"], self.process.pid)
        with patch.object(session, "process_identity", return_value="different-creation-time"):
            self.session.recover()
        self.assertEqual(storage.read_json(job_path)["status"], "interrupted")
        self.assertIsNone(self.session.read()["active_job_id"])
        self.assertEqual(self.session.read()["status"], "ready")

    def test_worker_that_dies_before_identity_registration_is_recovered(self):
        submitted = self.session.submit("evaluate")
        job_path = self.directory / "jobs" / f"{submitted['job_id']}.json"
        job = storage.read_json(job_path)
        job.update(worker_identity=None, status="queued")
        storage.write_json(job_path, job)
        with patch.object(session, "process_identity", return_value=None):
            self.session.recover()
        self.assertEqual(storage.read_json(job_path)["status"], "interrupted")
        self.assertIsNone(self.session.read()["active_job_id"])

    def test_cancel_signals_only_requested_job_and_close_uses_owned_tree_fallback(self):
        submitted = self.session.submit("evaluate")
        result = self.session.cancel_job(submitted["job_id"])
        self.assertEqual(result["status"], "cancellation_requested")
        self.assertTrue((self.directory / "jobs" / f"{submitted['job_id']}.cancel").exists())
        self.process.wait.side_effect = subprocess.TimeoutExpired("worker", 25)
        with patch.object(runner, "_terminate_owned_tree") as terminate:
            self.session.close()
        terminate.assert_called_once_with(self.process)
        self.process.terminate.assert_not_called()

    def test_precancelled_worker_never_imports_or_captures(self):
        submitted = self.session.submit("evaluate")
        self.session.cancel_job(submitted["job_id"])
        # Let the real watcher notice the cancellation before the worker's
        # first progress boundary; no model, process or game is started.
        original_snapshot = worker.create_candidate_snapshot
        def allow_watch(*args, **kwargs):
            time.sleep(.3)
            return original_snapshot(*args, **kwargs)
        with patch.object(worker, "create_candidate_snapshot", side_effect=allow_watch), \
                patch.object(runner, "import_project", side_effect=lambda *a, **kw: runner._check_cancel(kw.get("cancel"))) as importer, \
                patch.object(runner, "capture_project") as capture, patch.object(worker.traceback, "print_exc"):
            self.assertEqual(worker.run_job(self.directory, submitted["job_id"]), 1)
        job = storage.read_json(self.directory / "jobs" / f"{submitted['job_id']}.json")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"]["code"], "cancelled")
        capture.assert_not_called()

    def test_clock_cannot_reset_and_expired_budget_rejects_submission(self):
        self.session.start_clock()
        with self.assertRaises(storage.IntegrationError) as ctx:
            self.session.start_clock()
        self.assertEqual(ctx.exception.code, "budget_locked")
        self.session.update(lambda s: s.update(deadline_at=time.time()-1))
        with self.assertRaises(storage.IntegrationError) as ctx:
            self.session.submit("evaluate")
        self.assertEqual(ctx.exception.code, "budget_exhausted")

    def test_session_path_requires_exact_workspace_child_and_identifiers_are_bounded(self):
        self.assertEqual(storage.session_path(self.directory), self.directory)
        for path in (self.root / "elsewhere/test-session", self.directory / "nested", self.root / "sessions"):
            with self.subTest(path=path), self.assertRaises(storage.IntegrationError):
                storage.session_path(path)
        for name in ("../escape", "bad/name", "", "x"*82):
            with self.subTest(name=name), self.assertRaises(storage.IntegrationError):
                storage.identifier(name)


class StorageIOTests(unittest.TestCase):
    def test_transient_permission_read_retries_without_retrying_invalid_json(self):
        with patch.object(Path, "read_text", side_effect=[PermissionError("busy"), PermissionError("busy"), '{"status":"ok"}']) as reader, \
                patch.object(storage.time, "sleep") as sleep:
            self.assertEqual(storage.read_json("ignored.json"), {"status": "ok"})
            self.assertEqual(reader.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
        with patch.object(Path, "read_text", return_value="invalid JSON") as reader, patch.object(storage.time, "sleep") as sleep:
            with self.assertRaises(json.JSONDecodeError):
                storage.read_json("ignored.json")
            reader.assert_called_once()
            sleep.assert_not_called()

    def test_atomic_replace_retries_then_publishes_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            real_replace = storage.os.replace
            attempts = []
            def busy_then_replace(source, destination):
                attempts.append(source)
                if len(attempts) < 3:
                    raise PermissionError("temporarily shared")
                real_replace(source, destination)
            with patch.object(storage.os, "replace", side_effect=busy_then_replace), patch.object(storage.time, "sleep"):
                storage.write_json(path, {"status": "ready café"})
            self.assertEqual(storage.read_json(path), {"status": "ready café"})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(list(Path(raw).glob("*.tmp")), [])

    def test_exhausted_replace_keeps_old_record_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            storage.write_json(path, {"status": "original"})
            with patch.object(storage.os, "replace", side_effect=PermissionError("busy")) as replace, \
                    patch.object(storage.time, "sleep"):
                with self.assertRaises(PermissionError):
                    storage.write_json(path, {"status": "new"})
            self.assertEqual(replace.call_count, 20)
            self.assertEqual(storage.read_json(path), {"status": "original"})
            self.assertEqual(list(Path(raw).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
