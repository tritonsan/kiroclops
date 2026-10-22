import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import evaluation as evaluation
from kiroclops.integration.reporting import generate_session_report


def synthetic_report(variant, *, hold=8, repeat=1, offset=0, split="validation", amplitude=1., manual=False):
    run_id = f"{variant}-{split}-{hold}-{offset}-{repeat}"
    frames = [{"frame_index": index, "time_s": index/60., "valid": True,
               "height_body": amplitude*math.sin(math.pi*(index-30)/60) if 30 < index < 90 else 0.}
              for index in range(110)]
    validation = {"status": "pass" if manual else "pending", "point_status": "pass" if manual else "pending",
                  "event_status": "pass" if manual else "pending", "points": [], "event_checks": []}
    if manual:
        validation["points"] = [{"run_id": run_id, "frame_index": index, "error_px": 0., "source": "human"}
                                for index in range(20)]
        validation["event_checks"] = [{"source": "human", "pass": True}]
    return {"run_id": run_id, "status": "ok", "game": "tiny" if variant == "reference" else "slay", "variant": variant,
            "fps": 60, "body_height_px": 12, "valid_ratio": 1., "camera_valid": True,
            "scenario": {"hold_ticks": hold, "repeat": repeat, "spawn_offset": offset, "start_tick": 30, "split": split},
            "frames": frames, "events": {"takeoff_frame": 30, "apex_frame": 60, "landing_frame": 90},
            "metrics": {"height_body": amplitude, "rise_s": .5, "fall_s": .5}, "annotation_validation": validation}


def matrix(variant, *, split="validation", **kwargs):
    return [synthetic_report(variant, hold=hold, repeat=repeat, offset=offset, split=split, **kwargs)
            for hold in ((8, 14) if split == "validation" else (3, 24))
            for repeat in (1, 2, 3) for offset in ((0, 24) if split == "validation" else (0,))]


class EvaluationDecisionTests(unittest.TestCase):
    def test_numerical_pass_does_not_hide_unreviewed_candidate(self):
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8, manual=True), matrix("after"))
        self.assertEqual(result["legacy_status"], "pass")  # Legacy pools before+after visual checks.
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["measurement_status"], "pending")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["success"])
        self.assertIn("independent candidate visual validation incomplete", result["pending"])

    def test_candidate_manual_pass_and_pending_baseline_are_separate(self):
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), matrix("after", manual=True))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["manual_validation"]["baseline"]["status"], "pending")
        self.assertEqual(result["manual_validation"]["candidate"]["audited_validation_hold_ticks"], [8, 14])

    def test_candidate_calibration_points_cannot_replace_heldout_events(self):
        after = matrix("after") + matrix("after", split="calibration", manual=True)
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), after)
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["measurement_status"], "pending")

    def test_repeated_identical_clips_do_not_multiply_independent_observations(self):
        after = matrix("after", manual=True)
        for report in after:
            report["annotation_validation"]["points"] = report["annotation_validation"]["points"][:5]
            report["integration_provenance"] = {"control_origins": [
                {"frame_index": index, "reuse_origin": {"run_id": "original-visual-sheet", "frame_index": index}}
                for index in range(5)]}
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), after)
        self.assertEqual(result["manual_validation"]["candidate"]["checked_point_count"], 5)
        self.assertEqual(result["manual_validation"]["candidate"]["applied_point_count"], 60)
        self.assertEqual(result["measurement_status"], "pending")

    def test_real_baseline_control_failure_is_not_hidden(self):
        before = matrix("before", amplitude=1.8)
        before[0]["annotation_validation"]["status"] = "fail"
        result = evaluation.final_comparison(matrix("reference", manual=True), before, matrix("after", manual=True))
        self.assertEqual(result["manual_validation"]["baseline"]["status"], "fail")
        self.assertEqual(result["status"], "fail")

    def test_manual_only_rejection_keeps_numeric_result_and_original_failure(self):
        after = matrix("after", manual=True)
        after[0]["status"] = "rejected"
        after[0]["errors"] = ["independent manual annotation validation failed"]
        after[0]["annotation_validation"]["status"] = "fail"
        original = copy.deepcopy(after)
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), after)
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["manual_validation_status"], "fail")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(after, original)
        for changed in ({"errors": ["independent manual annotation validation failed", "camera shifted"]},
                        {"camera_valid": False}, {"valid_ratio": .94}):
            bad = copy.deepcopy(after)
            bad[0].update(changed)
            self.assertEqual(evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), bad)["numerical_status"], "fail")

    def test_missing_triplets_and_bad_tracking_never_pass(self):
        result = evaluation.final_comparison(matrix("reference", manual=True)[:1], matrix("before", amplitude=1.8)[:1], matrix("after", manual=True)[:1])
        self.assertEqual(result["numerical_status"], "pending")
        bad = matrix("after", manual=True)
        bad[0]["valid_ratio"] = .94
        result = evaluation.final_comparison(matrix("reference", manual=True), matrix("before", amplitude=1.8), bad)
        self.assertEqual(result["numerical_status"], "fail")

    def test_calibration_feedback_refuses_validation(self):
        with self.assertRaisesRegex(ValueError, "validation"):
            evaluation.calibration_feedback(matrix("reference"), matrix("before"), matrix("after"))

    def test_six_calibration_triplets_only_are_diagnostic(self):
        result = evaluation.calibration_feedback(matrix("reference", split="calibration"), matrix("before", split="calibration", amplitude=1.8), matrix("after", split="calibration"))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["diagnostic_only"])
        self.assertTrue(result["diagnostic_gates_pass"])
        self.assertEqual(len(result["scenario_checks"]), 6)
        self.assertEqual(result["mean_rmse_body"], 0)
        self.assertEqual(result["manual_status"], "pending")
        missing = evaluation.calibration_feedback([], [], [])
        self.assertEqual(missing["status"], "invalid")
        self.assertEqual(missing["manual_status"], "pending")


class RawEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(evaluation, "ROOT", self.root)
        self.root_patch.start()
        self.origin = self.make_run(self.root / "artifacts/runs/slay-before-h03-x0-r1")
        self.target = self.make_run(self.root / "session/candidate")
        self.annotations = self.root / "session/annotations"
        self.locked = {"schema_version": 1, "template_run": str(self.origin), "template_frame": 0,
                       "roi": [1, 1, 4, 4], "anchor_offset": [1, 1], "body_height_px": 24,
                       "background_rois": [[6, 6, 4, 4]], "control_points": [], "control_events": []}
        self.point = {"run_id": str(self.origin), "frame_index": 1, "x": 2, "y": 3,
                      "source": "assistant_visual", "frame_sha256": evaluation._sha(self.origin / "frames/000001.png")}
        self.event = {"run_id": str(self.origin), "source": "assistant_visual", "takeoff_frame": 0, "apex_frame": 1, "landing_frame": 2}
        self.locked["control_points"] = [self.point]
        self.locked["control_events"] = [self.event]
        self.source_report = {"run_id": str(self.origin), "game": "slay", "status": "ok", "events": {k: self.event[k] for k in ("takeoff_frame", "apex_frame", "landing_frame")},
                              "frames": [{"frame_index": 1, "x": 2, "y": 3, "valid": True}],
                              "annotation_validation": {"event_status": "pass", "points": [
                                  {"run_id": str(self.origin), "frame_index": 1, "source": "assistant_visual", "error_px": 0.}]}}
        evaluation._write(self.origin / "analysis.json", self.source_report)

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def make_run(self, path, *, game="slay", variant="after", split="calibration", hold=3, repeat=1, offset=0):
        (path / "frames").mkdir(parents=True)
        for index in range(3):
            (path / "frames" / f"{index:06d}.png").write_bytes(f"exact-raw-png-{index}".encode())
        evaluation._write(path / "manifest.json", {"game": game, "variant": variant, "frame_count": 3, "viewport": [20, 20],
                                                   "scenario": {"split": split, "hold_ticks": hold, "spawn_offset": offset, "repeat": repeat, "start_tick": 30}})
        return path

    def test_point_and_event_reuse_need_identical_raw_evidence(self):
        points, events, skipped = evaluation._reuse_controls(self.target, self.locked)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["run_id"], str(self.target))
        self.assertEqual(points[0]["reuse_origin"]["run_id"], str(self.origin))
        self.assertEqual(len(events), 1)
        (self.target / "frames/000000.png").write_bytes(b"different unannotated frame")
        points, events, _ = evaluation._reuse_controls(self.target, self.locked)
        self.assertEqual(len(points), 1)
        self.assertEqual(events, [])  # Matching event frame alone is not enough.
        (self.target / "frames/000001.png").write_bytes(b"different annotated frame")
        points, _, skipped = evaluation._reuse_controls(self.target, self.locked)
        self.assertEqual(points, [])
        self.assertEqual(skipped["pixel_mismatch"], 1)

    def test_changed_coordinates_or_unverified_origin_are_not_reused(self):
        changed = copy.deepcopy(self.locked)
        changed["control_points"][0]["y"] += 10
        points, _, _ = evaluation._reuse_controls(self.target, changed)
        self.assertEqual(points, [])
        self.source_report["annotation_validation"]["points"] = []
        evaluation._write(self.origin / "analysis.json", self.source_report)
        points, _, skipped = evaluation._reuse_controls(self.target, self.locked)
        self.assertEqual(points, [])
        self.assertEqual(skipped["unverified_origin"], 1)

    def test_calibration_does_not_read_heldout_origin(self):
        future = copy.deepcopy(self.locked)
        future["control_points"][0]["run_id"] = str(self.root / "artifacts/runs/slay-after-h08-x0-r1")
        future["control_events"] = []
        with patch.object(evaluation, "_sha", side_effect=AssertionError("heldout read")):
            self.assertEqual(evaluation._reuse_controls(self.target, future)[0], [])

    def test_direct_manual_record_stamps_hash_and_stale_point_is_rejected(self):
        with patch.object(evaluation, "_locked_annotation", return_value=self.locked):
            result = evaluation.record_manual_controls(self.target, self.annotations, [{"frame_index": 2, "x": 3, "y": 4}], [0, 1, 2])
            saved = evaluation._read(result["annotation_path"])
            self.assertEqual(saved["control_points"][-1]["frame_sha256"], evaluation._sha(self.target / "frames/000002.png"))
            (self.target / "frames/000002.png").write_bytes(b"modified after annotation")
            with self.assertRaisesRegex(ValueError, "stale raw PNG"):
                evaluation._build_annotation(self.target, self.annotations)

    def test_changed_locked_settings_and_generated_sources_are_rejected(self):
        with patch.object(evaluation, "_locked_annotation", return_value=self.locked):
            path, _, _ = evaluation._build_annotation(self.target, self.annotations)
            modified = evaluation._read(path)
            modified["body_height_px"] = 999
            evaluation._write(path, modified)
            with self.assertRaisesRegex(ValueError, "locked measurement"):
                evaluation._build_annotation(self.target, self.annotations)
            with self.assertRaisesRegex(ValueError, "human or assistant_visual"):
                evaluation.record_manual_controls(self.target, self.annotations, [], source="tracker")

    def test_manual_reanalysis_preserves_original_if_measurements_change(self):
        old = {"metrics": {"height_body": 1}, "frames": [1]}
        evaluation._write(self.target / "analysis.json", old)

        def fake_analysis(run, annotations, *, output_path):
            evaluation._write(output_path, {**old, "metrics": {"height_body": 2}})
            return {"analysis_path": str(output_path)}

        with patch.object(evaluation, "analyze_capture", side_effect=fake_analysis):
            with self.assertRaisesRegex(ValueError, "measurements"):
                evaluation.reanalyze_controls(self.target, self.annotations)
        self.assertEqual(evaluation._read(self.target / "analysis.json"), old)
        self.assertFalse((self.target / "analysis.manual-review.json").exists())

    def test_manual_reanalysis_can_change_validation_only(self):
        old = {"metrics": {"height_body": 1}, "frames": [1], "annotation_validation": {"status": "pending"}}
        evaluation._write(self.target / "analysis.json", old)

        def fake_analysis(run, annotations, *, output_path):
            evaluation._write(output_path, {**old, "annotation_validation": {"status": "pass"}})
            return {"analysis_path": str(output_path)}

        with patch.object(evaluation, "analyze_capture", side_effect=fake_analysis):
            result = evaluation.reanalyze_controls(self.target, self.annotations)
        self.assertTrue(result["measurements_unchanged"])
        self.assertEqual(evaluation._read(self.target / "analysis.json")["annotation_validation"]["status"], "pass")

    def test_prepare_reference_copies_raw_six_and_fits_fresh_only_calibration(self):
        frozen = {"calibration_runs": {}}
        originals = []
        for split, holds, offsets in (("calibration", (3, 24), (0,)), ("validation", (8, 14), (0, 24))):
            for hold in holds:
                for repeat in (1, 2, 3):
                    for offset in offsets:
                        original = self.make_run(self.root / f"artifacts/runs/tiny-reference-h{hold:02d}-x{offset}-r{repeat}", game="tiny", variant="reference", split=split, hold=hold, repeat=repeat, offset=offset)
                        analysis = synthetic_report("reference", hold=hold, split=split, repeat=repeat, offset=offset)
                        evaluation._write(original / "analysis.json", analysis)
                        originals.append((original, (original / "manifest.json").read_bytes(), (original / "analysis.json").read_bytes()))
                        frozen["calibration_runs"][original.relative_to(self.root).as_posix()] = {"manifest_sha256": evaluation._sha(original / "manifest.json"),
                            "measurement_sha256": evaluation._digest({key: analysis.get(key) for key in evaluation.MEASUREMENT_FIELDS})}
        evaluation._write(self.root / "artifacts/frozen_experiment.json", frozen)

        def fake_analyze(run, annotation):
            original = self.root / "artifacts/runs" / run.name
            analysis = evaluation._read(original / "analysis.json")
            evaluation._write(run / "analysis.json", analysis)
            return {"status": "ok", "manual_validation": {"status": "pending"}}

        with patch.object(evaluation, "_locked_annotation", return_value=self.locked), patch.object(evaluation, "analyze_capture", side_effect=fake_analyze), patch.object(evaluation, "fit_profile", return_value={"fresh": True}) as fitter:
            result = evaluation.prepare_reference(self.root / "session/reference")
            self.assertEqual(result["run_count"], 6)
            self.assertEqual(result["profile"], {"fresh": True})
            self.assertEqual(len(fitter.call_args.args[0]), 6)
            self.assertTrue(all("calibration" in str(path) for path in fitter.call_args.args[0]))
            fitter.reset_mock()
            validation = evaluation.prepare_reference(self.root / "session/reference", split="validation")
            self.assertEqual(validation["run_count"], 12)
            self.assertIsNone(validation["profile"])
            fitter.assert_not_called()
            corrupt = Path(result["runs"][0]) / "frames/000000.png"
            corrupt.write_bytes(b"corrupted cache")
            with self.assertRaisesRegex(ValueError, "cache raw evidence"):
                evaluation.prepare_reference(self.root / "session/reference")
        for original, manifest_bytes, analysis_bytes in originals:
            self.assertEqual((original / "manifest.json").read_bytes(), manifest_bytes)
            self.assertEqual((original / "analysis.json").read_bytes(), analysis_bytes)


class SessionReportingTests(unittest.TestCase):
    def test_selected_candidate_alone_is_visualized_but_all_attempts_keep_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            candidates = []
            for cid in ("c1", "c2"):
                run = session / f"candidates/{cid}/runs/calibration/hold03"
                evaluation._write(run / "manifest.json", {"variant": "after", "game": "slay", "scenario": {"hold_ticks": 3}})
                candidate = {"candidate_id": cid, "status": "evaluated", "calibration_runs": [str(run)], "feedback": {"mean_rmse_body": .1}}
                candidates.append(candidate)
                evaluation._write(session / f"candidates/{cid}/candidate.json", candidate)
                (session / f"candidates/{cid}/patch.diff").write_text(f"+ {cid}", encoding="utf-8")
            state = {"session_id": "many", "candidates": candidates, "selected_candidate_id": "c2"}
            evaluation._write(session / "session.json", state)
            from kiroclops.report import generate_report as legacy_report
            with patch("kiroclops.integration.reporting.generate_report", wraps=legacy_report) as render:
                page = generate_session_report(session)
                shown = render.call_args.args[0]
                self.assertEqual(shown, [Path(candidates[1]["calibration_runs"][0])])
            content = page.read_text(encoding="utf-8")
            self.assertIn("Video/curve candidate: c2 (selected candidate)", content)
            self.assertIn("Candidate c1", content)
            self.assertIn("Candidate c2", content)
            state["selected_candidate_id"] = None
            state["candidates"].append({"candidate_id": "c3", "status": "capturing"})
            evaluation._write(session / "session.json", state)
            with patch("kiroclops.integration.reporting.generate_report", wraps=legacy_report) as render:
                page = generate_session_report(session)
                self.assertEqual(render.call_args.args[0], [Path(candidates[1]["calibration_runs"][0])])
            self.assertIn("Video/curve candidate: c2 (latest finished attempt; no candidate selected yet)", page.read_text(encoding="utf-8"))

    def test_report_preserves_pending_and_copies_escaped_attempt_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            candidate = {"candidate_id": "c1", "status": "completed", "feedback": {"mean_rmse_body": .1}, "diff_path": "candidates/c1/patch.diff"}
            evaluation._write(session / "candidates/c1/candidate.json", candidate)
            (session / "candidates/c1/patch.diff").write_text("+ <script>alert('test')</script>", encoding="utf-8")
            state = {"session_id": "<script>bad()</script>", "status": "finalized", "mode": "tools", "selected_candidate_id": "c1", "candidates": [candidate],
                     "reference": {}, "baseline_runs": {}, "final": {"comparison_path": "final/comparison.json", "numerical_status": "pass", "measurement_status": "pending", "smoke_status": "pass"}}
            evaluation._write(session / "session.json", state)
            evaluation._write(session / "final/comparison.json", {"status": "pending", "numerical_status": "pass", "manual_validation_status": "pending"})
            page = generate_session_report(session)
            content = page.read_text(encoding="utf-8")
            self.assertIn("Numerical gates: pass", content)
            self.assertIn("Independent visual validation: pending", content)
            self.assertIn("Selected candidate: c1", content)
            self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", content)
            self.assertNotIn("<script>alert('test')", content)
            self.assertTrue((page.parent / "session-assets/candidate-000/patch.diff").exists())
            self.assertEqual(evaluation._read(session / "session.json"), state)

    def test_report_rejects_evidence_outside_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            evaluation._write(session / "session.json", {"reference": {"calibration": ["../outside"]}})
            with self.assertRaisesRegex(ValueError, "outside its session"):
                generate_session_report(session)


if __name__ == "__main__":
    unittest.main()
