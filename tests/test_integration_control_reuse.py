"""Exact raw-pixel boundaries for operator-only cross-session annotations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import control_reuse, evaluation, storage


class ControlReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "café Review"
        patched_root = patch.object(storage, "ROOT", self.root)
        patched_root.start()
        self.addCleanup(patched_root.stop)
        self.source = self.root / "sessions/source"
        self.candidate_root = self.source / "candidates/candidate-01"
        self.origin = self.candidate_root / "runs/validation/slay-after-h08-x0-r1"
        self.target = self.root / "sessions/target/candidates/candidate-01/runs/validation/slay-after-h08-x0-r1"
        self.target_annotations = self.root / "sessions/target/annotations"
        baseline = self.source / "baseline"
        project = self.candidate_root / "project"
        for path in (baseline, project):
            path.mkdir(parents=True)
            (path / "player.gd").write_text("controller")
        hashes = storage.project_hashes(project)
        self.manifest = {"game": "slay", "variant": "after", "status": "ok", "fps": 60,
                         "viewport": [640, 360], "frame_count": 4,
                         "scenario": {"split": "validation", "hold_ticks": 8, "spawn_offset": 0, "start_tick": 30, "repeat": 1},
                         "provenance": {"full_source_hashes_before": hashes, "full_source_hashes_after": hashes}}
        for run in (self.origin, self.target):
            (run / "frames").mkdir(parents=True)
            for index in range(4):
                (run / "frames" / f"{index:06d}.png").write_bytes(f"raw independent frame {index}".encode())
            storage.write_json(run / "manifest.json", self.manifest)
        self.locked = {"schema_version": 1, "template_run": str(self.origin), "template_frame": 0,
                       "roi": [1, 1, 4, 4], "anchor_offset": [1, 1], "body_height_px": 24,
                       "background_rois": [[0, 0, 2, 2]], "control_points": [], "control_events": []}
        self.point = {"run_id": str(self.origin), "frame_index": 1, "x": 10, "y": 20,
                      "source": "assistant_visual", "evidence": "raw screenshot, observed independently",
                      "frame_sha256": storage.sha256(self.origin / "frames/000001.png")}
        self.event = {"run_id": str(self.origin), "source": "human", "takeoff_frame": 0, "apex_frame": 1,
                      "landing_frame": 2, "evidence": "raw consecutive images",
                      "frame_sha256": {name: storage.sha256(self.origin / "frames" / f"{index:06d}.png")
                                       for name, index in zip(control_reuse.EVENT_NAMES, (0, 1, 2))}}
        self.annotation = dict(deepcopy(self.locked), control_points=[self.point], control_events=[self.event])
        self.annotation_path = evaluation.controls_path(self.origin, self.source / "annotations")
        storage.write_json(self.annotation_path, self.annotation)
        self.report = {"game": "slay", "body_height_px": 24,
                       "frames": [{"frame_index": 1, "x": 10, "y": 20, "valid": True}],
                       "events": {name: index for name, index in zip(control_reuse.EVENT_NAMES, (0, 1, 2))},
                       "annotation_validation": {"event_status": "pass", "points": [
                           {"frame_index": 1, "run_id": str(self.origin), "source": "assistant_visual", "error_px": 0}]},
                       "provenance": {"manifest_sha256": storage.sha256(self.origin / "manifest.json"),
                                      "annotation_sha256": storage.sha256(self.annotation_path)}}
        storage.write_json(self.origin / "analysis.json", self.report)
        candidate = {"candidate_id": "candidate-01", "status": "finalized", "source_hashes": hashes,
                     "calibration_runs": [], "validation_runs": [str(self.origin)]}
        storage.write_json(self.candidate_root / "candidate.json", candidate)
        self.receipt = self.source / "final/manual-review.json"
        storage.write_json(self.receipt, {"candidate_id": "candidate-01", "measurement_status": "pass",
                                          "measurements_unchanged": True, "raw_evidence_unchanged": True, "sources_unchanged": True})
        self.state = {"status": "finished", "selected_candidate_id": "candidate-01", "sealed_at": "now",
                      "active_job_id": None, "candidates": [candidate], "baseline": str(baseline), "baseline_hashes": hashes,
                      "reference": {"calibration": [], "validation": []},
                      "baseline_runs": {"calibration": [], "validation": []},
                      "final": {"candidate_id": "candidate-01", "measurement_status": "pass", "manual_review_path": str(self.receipt)}}
        storage.write_json(self.source / "session.json", self.state)
        locked_mock = patch.object(evaluation, "_locked_annotation", return_value=self.locked)
        locked_mock.start()
        self.addCleanup(locked_mock.stop)

    def reuse(self, targets=None):
        targets = targets or [self.target]
        return control_reuse.reuse_controls(self.source, {"reference": [], "baseline": [], "candidate": targets},
                                             {run: self.target_annotations for run in targets})

    def annotation_after_reanalysis(self, run=None):
        return evaluation._build_annotation(run or self.target, self.target_annotations)[1]

    def source_bytes(self):
        return {str(path.relative_to(self.source)): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}

    def test_exact_pixels_copy_provenance_survive_reanalysis_without_source_writes(self):
        original = self.source_bytes()
        result = self.reuse()
        self.assertEqual((result["points"], result["events"]), (1, 1))
        annotation = self.annotation_after_reanalysis()
        point, event = annotation["control_points"][0], annotation["control_events"][0]
        self.assertEqual((point["x"], point["y"], point["source"]), (10, 20, "assistant_visual"))
        self.assertEqual(point["reuse_origin"]["run_id"], str(self.origin))
        self.assertEqual(point["reuse_origin"]["method"], control_reuse.POINT_METHOD)
        self.assertEqual(event["reuse_origin"]["method"], control_reuse.EVENT_METHOD)
        self.assertEqual(event["source"], "human")
        self.assertEqual(self.source_bytes(), original)

    def test_same_frame_index_with_different_png_is_not_reused(self):
        (self.target / "frames/000001.png").write_bytes(b"different pixels at same index")
        result = self.reuse()
        self.assertEqual((result["points"], result["events"]), (0, 0))
        self.assertEqual(result["skipped"]["pixel_mismatch"], 1)
        self.assertEqual(self.annotation_after_reanalysis()["control_points"], [])

    def test_identical_event_frames_are_insufficient_when_other_frame_differs(self):
        (self.target / "frames/000003.png").write_bytes(b"changed non-event frame")
        result = self.reuse()
        self.assertEqual((result["points"], result["events"]), (1, 0))
        self.assertEqual(result["skipped"]["sequence_mismatch"], 1)
        self.assertEqual(self.annotation_after_reanalysis()["control_events"], [])

    def test_stale_source_point_or_unverified_measurement_is_not_reused(self):
        for modify in (lambda report: report["frames"][0].update(valid=False),
                       lambda report: report["annotation_validation"]["points"][0].update(error_px=4)):
            report = deepcopy(self.report)
            modify(report)
            storage.write_json(self.origin / "analysis.json", report)
            with self.subTest(report=report):
                result = self.reuse()
                self.assertEqual(result["points"], 0)
                self.assertGreater(result["skipped"]["unverified_origin"], 0)

    def test_copied_observations_are_not_exported_as_fresh_manual_truth(self):
        self.annotation["control_points"][0]["reuse_origin"] = {"run_id": "older-observation"}
        storage.write_json(self.annotation_path, self.annotation)
        self.report["provenance"]["annotation_sha256"] = storage.sha256(self.annotation_path)
        storage.write_json(self.origin / "analysis.json", self.report)
        self.assertEqual(self.reuse()["points"], 0)

    def test_stored_copy_is_reverified_after_source_or_target_changes(self):
        self.reuse()
        (self.target / "frames/000001.png").write_bytes(b"target changed after first review")
        annotation = self.annotation_after_reanalysis()
        self.assertEqual(annotation["control_points"], [])
        self.assertEqual(annotation["control_events"], [])

    def test_source_report_changes_invalidate_existing_copy(self):
        self.reuse()
        self.report["extra_review"] = True
        storage.write_json(self.origin / "analysis.json", self.report)
        annotation = self.annotation_after_reanalysis()
        self.assertEqual(annotation["control_points"], [])
        self.assertEqual(annotation["control_events"], [])

    def test_source_must_be_finished_manually_reviewed_and_unmodified(self):
        for change in ({"status": "running"}, {"active_job_id": "active"}, {"sealed_at": None},
                       {"final": dict(self.state["final"], measurement_status="pending")}):
            storage.write_json(self.source / "session.json", dict(self.state, **change))
            with self.subTest(change=change), self.assertRaises(storage.IntegrationError):
                self.reuse()
        storage.write_json(self.source / "session.json", self.state)
        (self.candidate_root / "project/player.gd").write_text("edited source")
        with self.assertRaises(storage.IntegrationError):
            self.reuse()

    def test_validation_observations_never_enter_calibration_even_if_pixels_match(self):
        manifest = deepcopy(self.manifest)
        manifest["scenario"].update(split="calibration", hold_ticks=3)
        storage.write_json(self.target / "manifest.json", manifest)
        result = self.reuse()
        self.assertEqual((result["points"], result["events"]), (0, 0))

    def test_repeated_copies_keep_one_independent_origin(self):
        import shutil
        second = self.target.with_name("slay-after-h08-x0-r2")
        shutil.copytree(self.target, second)
        result = self.reuse([self.target, second])
        self.assertEqual(result["points"], 2)
        reports = []
        for run in (self.target, second):
            point = self.annotation_after_reanalysis(run)["control_points"][0]
            reports.append({"scenario": {"hold_ticks": 8}, "body_height_px": 24, "integration_provenance": {"control_origins": [point]},
                            "annotation_validation": {"status": "pending", "point_status": "pending", "event_status": "pass",
                                "tolerance_px": 1.2, "points": [{"run_id": str(run), "frame_index": 1, "source": "assistant_visual", "error_px": 0}]}})
        self.assertEqual(evaluation._role_manual(reports)["checked_point_count"], 1)


if __name__ == "__main__":
    unittest.main()
