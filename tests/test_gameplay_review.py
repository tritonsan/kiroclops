"""Supplemental wall proof rejects the original smoke's grounded-jump false positive."""
from copy import deepcopy
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import gameplay_review as review, runner, storage
from scripts import review_gameplay

OLD_TRACE = [(0, "Fall"), (4, "Idle"), (66, "Run"), (91, "Jump"), (101, "Fall"),
             (102, "WallJump"), (125, "Idle"), (126, "Run"), (131, "Jump"), (141, "Fall"), (151, "Idle")]
GOOD_TRACE = [(0, "Fall"), (4, "Idle"), (66, "Run"), (91, "Jump"), (101, "Fall"),
              (102, "WallJump"), (116, "Jump"), (133, "Fall"), (158, "Idle")]


def events(pairs):
    return [{"physics_tick": tick, "state": state} for tick, state in pairs]


class WallTransitionTests(unittest.TestCase):
    def test_original_real_trace_is_subsequence_pass_but_not_wall_launch(self):
        trace = events(OLD_TRACE)
        self.assertTrue(runner._state_sequence(trace, ["Run", "Jump", "WallJump", "Jump", "Fall", "Idle"]))
        checks, transition = review.wall_transition_checks(trace)
        self.assertFalse(checks["adjacent_wall_to_jump_at_input"])
        self.assertIsNone(transition)

    def test_direct_timed_transition_and_later_ground_pass(self):
        checks, transition = review.wall_transition_checks(events(GOOD_TRACE))
        self.assertTrue(all(checks.values()))
        self.assertEqual(transition, {"wall_tick": 102, "jump_tick": 116, "command_physics_tick": 115})

    def test_wrong_time_intervening_idle_or_run_and_unordered_trace_fail(self):
        bad = [GOOD_TRACE[:6] + [(131, "Jump"), (141, "Fall"), (153, "Idle")],
               GOOD_TRACE[:6] + [(108, "Idle")] + GOOD_TRACE[6:],
               GOOD_TRACE[:6] + [(108, "Run")] + GOOD_TRACE[6:],
               GOOD_TRACE[:6] + [(99, "Jump"), (128, "Fall"), (153, "Idle")],
               GOOD_TRACE[:-1]]
        for trace in bad:
            with self.subTest(trace=trace):
                self.assertFalse(all(review.wall_transition_checks(events(trace))[0].values()))


class GameplayReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Wall Review café"
        self.directory = self.root / "sessions/test-wall"
        self.candidate_root = self.directory / "candidates/candidate-01"
        self.project = self.candidate_root / "project"
        for project in (self.project, self.directory / "baseline"):
            project.mkdir(parents=True)
            (project / "project.godot").write_text("frozen project")
            (project / "player.gd").write_text("actual sealed controller")
        root_mock = patch.object(storage, "ROOT", self.root)
        root_mock.start()
        self.addCleanup(root_mock.stop)
        self.hashes = storage.project_hashes(self.project)
        self.candidate = {"candidate_id": "candidate-01", "status": "finalized", "source_hashes": self.hashes,
                          "smoke": {"status": "passed", "old_evidence": "must remain unchanged"}}
        storage.write_json(self.candidate_root / "candidate.json", self.candidate)
        storage.write_json(self.directory / "session.json", {
            "status": "finished", "sealed_at": "previously sealed", "selected_candidate_id": "candidate-01",
            "active_job_id": None, "baseline": str(self.directory / "baseline"), "baseline_hashes": self.hashes,
            "candidates": [self.candidate], "final": {"candidate_id": "candidate-01", "numerical_status": "pass"}})
        storage.write_json(self.directory / "final/comparison.json", {"numerical_status": "pass", "original": "untouched"})
        storage.write_json(self.candidate_root / "smoke/smoke.json", {"status": "passed", "state_trace": events(OLD_TRACE)})
        self.trace = GOOD_TRACE
        self.capture_mock = patch.object(runner, "capture_project", side_effect=self.fake_capture)
        self.capture = self.capture_mock.start()
        self.addCleanup(self.capture_mock.stop)

    def fake_capture(self, project, run, **kwargs):
        run.mkdir(parents=True)
        (run / "frames").mkdir()
        config = review._expected_config(run)
        storage.write_json(run / "capture_config.json", config)
        active, frames = set(), []
        for index in range(240):
            if index == 30:
                active.add("ui_up")
            if index == 33:
                active.discard("ui_up")
            for event in review.EXTRA_INPUTS:
                if event["tick"] == index:
                    (active.add if event["pressed"] else active.discard)(event["action"])
            frames.append({"frame_index": index, "physics_tick": 60 + index, "time_s": index / 60, "actions": sorted(active)})
            (run / "frames" / f"{index:06d}.png").write_bytes(f"fake frame {index}".encode())
        storage.write_json(run / "manifest.json", {"status": "ok", "game": "slay", "variant": "after",
            "fps": 60, "frame_count": 240, "viewport": [640, 360], "normal_level": False, "settled": True,
            "scenario": {"hold_ticks": 3, "start_tick": 30, "split": "calibration", "repeat": 1, "spawn_offset": 0},
            "frames": frames, "provenance": {"full_source_hashes_before": self.hashes, "full_source_hashes_after": self.hashes}})
        (run / "engine.log").write_text("\n".join(f"KIROCLOPS_STATE physics_tick={tick} state={state}" for tick, state in self.trace))
        return run

    def originals(self):
        return {path: path.read_bytes() for path in (self.directory / "session.json", self.directory / "final/comparison.json",
                    self.candidate_root / "candidate.json", self.candidate_root / "smoke/smoke.json")}

    def test_additive_capture_binds_same_source_and_preserves_all_original_results(self):
        original = self.originals()
        result = review.review_wall_behavior(self.directory)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["full_source_hashes_before"], result["full_source_hashes_after"])
        self.assertEqual(self.originals(), original)
        self.assertEqual(self.capture.call_args.kwargs["extra_config"]["extra_inputs"][2],
                         {"tick": 55, "action": "ui_up", "pressed": True})
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "pass")
        with self.assertRaises(storage.IntegrationError):
            review.review_wall_behavior(self.directory)
        self.assertEqual(self.capture.call_count, 1)

    def test_false_positive_trace_is_retained_as_failed_supplemental_evidence(self):
        self.trace = OLD_TRACE
        result = review.review_wall_behavior(self.directory)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "fail")
        self.assertEqual(storage.read_json(self.directory / "final/comparison.json")["numerical_status"], "pass")

    def test_missing_receipt_is_unverified_without_capture(self):
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "unverified")
        self.capture.assert_not_called()

    def test_current_candidate_tamper_invalidates_review(self):
        review.review_wall_behavior(self.directory)
        (self.project / "player.gd").write_text("changed after review")
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "fail")

    def test_raw_image_log_or_input_recipe_tamper_invalidates_review(self):
        review.review_wall_behavior(self.directory)
        run = self.candidate_root / "supplemental/wall"
        for relative in ("frames/000050.png", "engine.log", "capture_config.json"):
            path = run / relative
            original = path.read_bytes()
            path.write_bytes(original + b" changed")
            with self.subTest(relative=relative):
                self.assertEqual(review.verify_wall_review(self.directory)["status"], "fail")
            path.write_bytes(original)
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "pass")

    def test_modified_manifest_source_hashes_or_invented_success_are_rejected(self):
        review.review_wall_behavior(self.directory)
        manifest_path = self.candidate_root / "supplemental/wall/manifest.json"
        manifest = storage.read_json(manifest_path)
        manifest["provenance"]["full_source_hashes_after"] = {"other": "source"}
        storage.write_json(manifest_path, manifest)
        self.assertEqual(review.verify_wall_review(self.directory)["status"], "fail")

    def test_source_mutation_during_capture_persists_error_not_success(self):
        def mutate(*args, **kwargs):
            result = self.fake_capture(*args, **kwargs)
            (self.project / "player.gd").write_text("changed during capture")
            return result
        self.capture.side_effect = mutate
        result = review.review_wall_behavior(self.directory)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "experiment_integrity")

    def test_unsealed_or_active_session_cannot_capture(self):
        state_path = self.directory / "session.json"
        original = storage.read_json(state_path)
        for update in ({"sealed_at": None}, {"status": "ready"}, {"active_job_id": "other-job"}):
            storage.write_json(state_path, dict(original, **update))
            with self.subTest(update=update), self.assertRaises(storage.IntegrationError):
                review.review_wall_behavior(self.directory)
        self.capture.assert_not_called()

    def test_cli_default_is_read_only_and_capture_requires_explicit_flag(self):
        with patch.object(review_gameplay, "review_wall_behavior") as capture, \
                patch.object(review_gameplay, "verify_wall_review", return_value={"status": "unverified"}), \
                patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(review_gameplay.main(["--session", str(self.directory)]), 2)
        capture.assert_not_called()
        with patch.object(review_gameplay, "review_wall_behavior") as capture, \
                patch.object(review_gameplay, "verify_wall_review", return_value={"status": "pass"}), \
                patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(review_gameplay.main(["--session", str(self.directory), "--capture"]), 0)
        capture.assert_called_once_with(self.directory)


if __name__ == "__main__":
    unittest.main()
