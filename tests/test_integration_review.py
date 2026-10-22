"""Adversarial operator-review contracts with mocked pixel reanalysis."""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import control_reuse, evaluation, review, storage
from scripts import review_candidate


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Visual Review café"
        self.directory = self.root / "sessions/review-test"
        self.candidate_root = self.directory / "candidates/candidate-01"
        self.baseline = self.directory / "baseline"
        self.baseline.mkdir(parents=True)
        (self.baseline / "project.godot").write_text("frozen config")
        (self.baseline / "player.gd").write_text("baseline controller")
        shutil.copytree(self.baseline, self.candidate_root / "project")
        (self.candidate_root / "project/player.gd").write_text("selected controller")
        mocked_root = patch.object(storage, "ROOT", self.root)
        mocked_root.start()
        self.addCleanup(mocked_root.stop)
        self.baseline_hashes = storage.project_hashes(self.baseline)
        self.candidate_hashes = storage.project_hashes(self.candidate_root / "project")
        self.runs = {role: {"calibration": [], "validation": []} for role in ("reference", "baseline", "candidate")}
        for role in self.runs:
            base = {"reference": self.directory / "reference", "baseline": self.directory / "baseline-runs",
                    "candidate": self.candidate_root / "runs"}[role]
            game, variant = ("tiny", "reference") if role == "reference" else ("slay", "before" if role == "baseline" else "after")
            hashes = self.baseline_hashes if role == "baseline" else self.candidate_hashes
            for split in ("calibration", "validation"):
                for hold in ((3, 24) if split == "calibration" else (8, 14)):
                    for offset in ((0,) if split == "calibration" else (0, 24)):
                        for repeat in (1, 2, 3):
                            run = base / split / f"{game}-{variant}-h{hold:02d}-x{offset}-r{repeat}"
                            (run / "frames").mkdir(parents=True)
                            for index in range(3):
                                (run / "frames" / f"{index:06d}.png").write_bytes(f"raw pixel fixture {index}".encode())
                            scenario = dict(split=split, hold_ticks=hold, spawn_offset=offset, repeat=repeat, start_tick=30)
                            manifest = {"game": game, "variant": variant, "status": "ok", "scenario": scenario,
                                        "frame_count": 3, "viewport": [640, 360], "fps": 60,
                                        "provenance": {"full_source_hashes_before": hashes, "full_source_hashes_after": hashes}}
                            storage.write_json(run / "manifest.json", manifest)
                            storage.write_json(run / "analysis.json", {
                                "game": game, "variant": variant, "status": "ok", "scenario": scenario,
                                "fps": 60, "body_height_px": 24, "camera_valid": True, "valid_ratio": 1,
                                "events": {"takeoff_frame": 0, "apex_frame": 1, "landing_frame": 2},
                                "metrics": {"height_body": 1.3}, "frames": [{"frame_index": 1, "x": 99, "y": 98}],
                                "annotation_validation": {"status": "pending"},
                                "provenance": {"manifest_sha256": storage.sha256(run / "manifest.json")}})
                            self.runs[role][split].append(str(run))
        self.candidate = {"candidate_id": "candidate-01", "status": "finalized", "source_hashes": self.candidate_hashes,
                          "calibration_runs": self.runs["candidate"]["calibration"],
                          "validation_runs": self.runs["candidate"]["validation"]}
        storage.write_json(self.candidate_root / "candidate.json", self.candidate)
        self.comparison = {"status": "pending", "success": False, "numerical_status": "pass",
                           "manual_validation_status": "pending", "measurement_status": "pending",
                           "gates": {"curve_rmse_body_max": .15}, "coverage": {"complete_triplets": 12},
                           "validation_scenarios": [{"after_rmse_body": .07, "pass": True}], "alignment": "takeoff only"}
        storage.write_json(self.directory / "final/comparison.json", self.comparison)
        self.state = {"session_id": "review-test", "status": "finished", "selected_candidate_id": "candidate-01",
                      "sealed_at": "2026-09-18T12:00:00Z", "active_job_id": None,
                      "baseline": str(self.baseline), "baseline_hashes": self.baseline_hashes,
                      "candidates": [self.candidate], "reference": self.runs["reference"], "baseline_runs": self.runs["baseline"],
                      "final": {"candidate_id": "candidate-01", "comparison_path": str(self.directory / "final/comparison.json"),
                                "numerical_status": "pass", "measurement_status": "pending", "smoke_status": "passed"}}
        storage.write_json(self.directory / "session.json", self.state)
        self.record = patch.object(evaluation, "record_manual_controls", side_effect=self.fake_record)
        self.record_mock = self.record.start()
        self.addCleanup(self.record.stop)
        self.reanalyze = patch.object(evaluation, "reanalyze_controls", side_effect=self.fake_reanalyze)
        self.reanalyze_mock = self.reanalyze.start()
        self.addCleanup(self.reanalyze.stop)
        self.compare = patch.object(evaluation, "final_comparison", side_effect=lambda *args: deepcopy(self.comparison))
        self.compare_mock = self.compare.start()
        self.addCleanup(self.compare.stop)

    def fake_record(self, run, annotations, points, event_frames=None, *, source="human"):
        path = evaluation.controls_path(run, annotations)
        storage.write_json(path, {"points": points, "events": event_frames, "source": source})
        return {"annotation_path": str(path)}

    def fake_reanalyze(self, run, annotations):
        report = storage.read_json(run / "analysis.json")
        report["review_count"] = report.get("review_count", 0) + 1
        storage.write_json(run / "analysis.json", report)
        return {"analysis_path": str(run / "analysis.json"), "measurements_unchanged": True}

    def control(self):
        run = Path(self.runs["candidate"]["validation"][0])
        return {"run": str(run.relative_to(self.candidate_root / "runs")),
                "points": [{"frame_index": 1, "x": 10, "y": 20, "evidence": "explicit screenshot observation"}],
                "event_frames": {"takeoff_frame": 0, "apex_frame": 1, "landing_frame": 2}}

    def test_no_controls_rechecks_all_roles_without_inventing_observations(self):
        result = review.review_session(self.directory)
        self.record_mock.assert_not_called()
        self.assertEqual(self.reanalyze_mock.call_count, 54)
        self.assertEqual([len(group) for group in self.compare_mock.call_args.args], [18, 18, 18])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["supplied_control_run_count"], 0)
        self.assertEqual(storage.project_hashes(self.candidate_root / "project"), self.candidate_hashes)
        for call in self.reanalyze_mock.call_args_list:
            run, annotations = call.args
            expected = self.directory / ("reference/annotations" if "tiny-reference" in run.name else "annotations")
            self.assertEqual(annotations, expected)
        receipt = storage.read_json(result["review_path"])
        self.assertEqual(receipt["supplied_controls"], [])
        self.assertTrue(receipt["measurements_unchanged"])

    def test_explicit_control_file_and_assistant_provenance_are_preserved_exactly(self):
        control = self.control()
        controls_file = self.directory / "observations.json"
        storage.write_json(controls_file, [control])
        result = review.review_session(self.directory, controls_file, source="assistant_visual")
        self.record_mock.assert_called_once()
        self.assertEqual(self.record_mock.call_args.args[2], control["points"])
        self.assertEqual(self.record_mock.call_args.args[3], control["event_frames"])
        self.assertEqual(self.record_mock.call_args.kwargs["source"], "assistant_visual")
        receipt = storage.read_json(result["review_path"])
        self.assertEqual(receipt["source"], "assistant_visual")
        self.assertEqual(receipt["controls_file_sha256"], storage.sha256(controls_file))
        self.assertEqual(receipt["supplied_controls"][0]["points"][0]["x"], 10)  # Never substitute tracker x=99.
        state = storage.read_json(self.directory / "session.json")
        self.assertEqual(state["final"]["manual_review_path"], result["review_path"])
        self.assertEqual(state["status"], "finished")

    def test_unsealed_unfinished_or_active_sessions_reject_before_any_write(self):
        for change, code in (({"sealed_at": None}, "review_not_ready"), ({"status": "ready"}, "review_not_ready"),
                             ({"selected_candidate_id": None}, "review_not_ready"), ({"active_job_id": "job-active"}, "session_busy")):
            state = deepcopy(self.state)
            state.update(change)
            storage.write_json(self.directory / "session.json", state)
            with self.subTest(change=change), self.assertRaises(storage.IntegrationError) as ctx:
                review.review_session(self.directory, [self.control()])
            self.assertEqual(ctx.exception.code, code)
        self.record_mock.assert_not_called()
        self.reanalyze_mock.assert_not_called()

    def test_control_path_escape_other_roles_and_unknown_threshold_overrides_rejected(self):
        controls = [dict(self.control(), run="../../../../baseline/player.gd"),
                    dict(self.control(), run=self.runs["baseline"]["validation"][0]),
                    dict(self.control(), min_score=0)]
        for control in controls:
            with self.subTest(control=control), self.assertRaises(storage.IntegrationError):
                review.review_session(self.directory, [control])
        with self.assertRaises(storage.IntegrationError):
            review.review_session(self.directory, [self.control()], source="tracker")
        self.record_mock.assert_not_called()
        self.reanalyze_mock.assert_not_called()

    def test_source_changes_and_mismatched_capture_provenance_are_rejected(self):
        path = self.candidate_root / "project/player.gd"
        original = path.read_bytes()
        path.write_text("modified after seal")
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory)
        self.assertEqual(ctx.exception.code, "experiment_integrity")
        path.write_bytes(original)
        manifest_path = Path(self.runs["candidate"]["calibration"][0]) / "manifest.json"
        manifest = storage.read_json(manifest_path)
        manifest["provenance"]["full_source_hashes_before"] = {"other": "source"}
        storage.write_json(manifest_path, manifest)
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory)
        self.assertEqual(ctx.exception.code, "experiment_integrity")
        self.record_mock.assert_not_called()

    def test_measurement_drift_rolls_back_analyses_annotations_and_final_metadata(self):
        run = Path(self.runs["reference"]["calibration"][0])
        original_report = (run / "analysis.json").read_bytes()
        original_session = (self.directory / "session.json").read_bytes()
        original_comparison = (self.directory / "final/comparison.json").read_bytes()
        def corrupt_measurement(run, annotations):
            result = self.fake_reanalyze(run, annotations)
            report = storage.read_json(run / "analysis.json")
            report["metrics"]["height_body"] = 999
            storage.write_json(run / "analysis.json", report)
            return result
        self.reanalyze_mock.side_effect = corrupt_measurement
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory, [self.control()])
        self.assertEqual(ctx.exception.code, "measurement_changed")
        self.assertEqual((run / "analysis.json").read_bytes(), original_report)
        self.assertEqual((self.directory / "session.json").read_bytes(), original_session)
        self.assertEqual((self.directory / "final/comparison.json").read_bytes(), original_comparison)
        self.assertEqual(list((self.directory / "annotations").glob("*.json")), [])
        self.assertEqual(list((self.directory / "final").glob("manual-review-*.json")), [])

    def test_changed_raw_pixels_are_detected_without_rewriting_raw_evidence(self):
        changed = Path(self.runs["candidate"]["validation"][0]) / "frames/000001.png"
        def corrupt_pixels(run, annotations):
            changed.write_bytes(b"external pixel modification")
            return self.fake_reanalyze(run, annotations)
        self.reanalyze_mock.side_effect = corrupt_pixels
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory)
        self.assertEqual(ctx.exception.code, "raw_evidence_changed")
        self.assertEqual(changed.read_bytes(), b"external pixel modification")

    def test_numerical_or_threshold_drift_is_not_published(self):
        original = (self.directory / "final/comparison.json").read_bytes()
        changed = deepcopy(self.comparison)
        changed["gates"]["curve_rmse_body_max"] = 100
        self.compare_mock.side_effect = lambda *args: changed
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory)
        self.assertEqual(ctx.exception.code, "numerical_result_changed")
        self.assertEqual((self.directory / "final/comparison.json").read_bytes(), original)

    def test_manual_rejection_keeps_numerical_pass_and_publishes_overall_failure(self):
        rejected = deepcopy(self.comparison)
        rejected.update(status="fail", success=False, manual_validation_status="fail", measurement_status="fail")
        self.compare_mock.side_effect = lambda *args: rejected
        def reject_controls(run, annotations):
            result = self.fake_reanalyze(run, annotations)
            report = storage.read_json(run / "analysis.json")
            report.update(status="rejected", errors=["independent manual annotation validation failed"],
                          annotation_validation={"status": "fail"})
            storage.write_json(run / "analysis.json", report)
            return result
        self.reanalyze_mock.side_effect = reject_controls
        result = review.review_session(self.directory, [self.control()])
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["measurement_status"], "fail")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(storage.read_json(self.directory / "session.json")["final"]["overall_status"], "fail")
        self.assertEqual(storage.read_json(self.directory / "final/comparison.json")["validation_scenarios"], self.comparison["validation_scenarios"])

    def test_missing_validation_scenario_rejects_incomplete_finalized_session(self):
        self.state["reference"]["validation"].pop()
        storage.write_json(self.directory / "session.json", self.state)
        with self.assertRaises(storage.IntegrationError) as ctx:
            review.review_session(self.directory)
        self.assertEqual(ctx.exception.code, "incomplete_experiment")

    def test_approved_reuse_is_operator_only_and_recorded_in_receipt(self):
        approved = self.root / "sessions/approved"
        reused = {"source_session": str(approved), "points": 3, "events": 1,
                  "skipped": {"pixel_mismatch": 2}, "evidence_sha256": {"session.json": "source-snapshot"}}
        with patch.object(control_reuse, "reuse_controls", return_value=reused) as reuse, \
                patch.object(control_reuse, "verify_source_unchanged") as verify:
            result = review.review_session(self.directory, reuse_controls_from=approved)
        self.assertEqual(reuse.call_args.args[0], approved)
        self.assertEqual({key: len(value) for key, value in reuse.call_args.args[1].items()},
                         {"reference": 18, "baseline": 18, "candidate": 18})
        verify.assert_called_once_with(reused)
        self.record_mock.assert_not_called()
        self.assertEqual(storage.read_json(result["review_path"])["reused_controls"], reused)
        self.assertNotIn("evidence_sha256", result["reused_controls"])  # CLI remains compact.

    def test_changed_reuse_source_rolls_back_target_annotations_and_review(self):
        run = Path(self.runs["candidate"]["validation"][0])
        annotation = evaluation.controls_path(run, self.directory / "annotations")
        original_state = (self.directory / "session.json").read_bytes()
        def reuse(*args):
            storage.write_json(annotation, {"copied_control": "must roll back"})
            return {"source_session": "approved", "points": 1, "events": 0, "skipped": {}}
        with patch.object(control_reuse, "reuse_controls", side_effect=reuse), \
                patch.object(control_reuse, "verify_source_unchanged",
                             side_effect=storage.IntegrationError("reuse_source_changed", "source changed")):
            with self.assertRaises(storage.IntegrationError) as ctx:
                review.review_session(self.directory, reuse_controls_from=self.root / "sessions/approved")
        self.assertEqual(ctx.exception.code, "reuse_source_changed")
        self.assertFalse(annotation.exists())
        self.assertEqual((self.directory / "session.json").read_bytes(), original_state)
        self.assertEqual(list((self.directory / "final").glob("manual-review-*.json")), [])

    def test_cli_preserves_explicit_source_and_reports_execution_errors(self):
        with patch.object(review_candidate, "review_session", return_value={"status": "fail", "measurement_status": "fail"}) as command, \
                patch("sys.stdout", new_callable=StringIO) as stdout:
            code = review_candidate.main(["--session", str(self.directory), "--source", "assistant_visual",
                                          "--reuse-controls-from", str(self.root / "sessions/approved")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "fail")
        self.assertEqual(command.call_args.kwargs["source"], "assistant_visual")
        self.assertEqual(command.call_args.kwargs["reuse_controls_from"], self.root / "sessions/approved")
        with patch.object(review_candidate, "review_session", side_effect=storage.IntegrationError("review_not_ready", "not sealed")), \
                patch("sys.stdout", new_callable=StringIO) as stdout:
            code = review_candidate.main(["--session", str(self.directory)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "review_not_ready")


if __name__ == "__main__":
    unittest.main()
