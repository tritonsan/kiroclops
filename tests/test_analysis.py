import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.analysis import analyze_run, validate_controls, measure_jump, _template_bank, _match_bank
from kiroclops.annotation import annotate_run, annotate_controls, validate_annotation


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, *, camera_shift=False, occlusion=False, warmup=0):
        run = self.root / "reference"
        (run / "frames").mkdir(parents=True)
        rng = np.random.default_rng(112)
        background = rng.integers(10, 65, (140, 160, 3), dtype=np.uint8)
        sprite = rng.integers(110, 255, (10, 8, 3), dtype=np.uint8)
        frames, truth = [], []
        for index in range(110):
            height = round(24*np.sin(np.pi*(index-30)/40)) if 30 < index < 70 else 0
            image = background.copy()
            y = 100-height
            if not (occlusion and 45 <= index < 55):
                image[y:y+10, 60:68] = sprite
            if camera_shift and index >= 45:
                image = np.roll(image, 3, axis=1)
            cv2.imwrite(str(run / "frames" / f"{index:06d}.png"), image)
            frames.append({"frame_index": index, "physics_tick": index+warmup, "time_s": index/60, "actions": []})
            truth.append((64, y+5))
        manifest = {"schema_version": 1, "game": "tiny", "variant": "reference", "fps": 60,
                    "viewport": [160, 140], "frame_count": len(frames), "frames": frames,
                    "scenario": {"hold_ticks": 12, "start_tick": 30, "split": "calibration", "repeat": 1, "spawn_offset": 0},
                    "provenance": {"test_fixture": True}}
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        annotation_path = self.root / "annotation.json"
        annotation = annotate_run(run, annotation_path, roi=[60, 100, 8, 10], anchor_offset=[4, 5],
                                  body_height_px=10, background_rois=[[5, 5, 14, 14]])
        return run, annotation_path, annotation, truth

    def test_fixed_camera_tracks_pixels_without_manual_validation_claim(self):
        run, path, _, truth = self.fixture()
        result = analyze_run(run, path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["annotation_validation"]["status"], "pending")
        self.assertEqual(result["valid_ratio"], 1.)
        self.assertEqual(result["events"]["takeoff_frame"], 30)
        self.assertEqual(result["events"]["landing_frame"], 70)
        for frame, expected in zip(result["frames"], truth):
            self.assertEqual((frame["x"], frame["y"]), expected)
        self.assertAlmostEqual(result["metrics"]["height_body"], 2.4)

    def test_camera_motion_rejects_even_if_character_match_is_good(self):
        run, path, _, _ = self.fixture(camera_shift=True)
        result = analyze_run(run, path)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["camera_valid"])
        self.assertIn("fixed-camera condition failed", result["errors"])

    def test_engine_warmup_ticks_do_not_shift_capture_relative_input(self):
        run, path, _, _ = self.fixture(warmup=60)
        result = analyze_run(run, path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"]["takeoff_frame"], 30)
        self.assertEqual(result["frames"][30]["physics_tick"], 90)
        self.assertAlmostEqual(result["metrics"]["input_latency_s"], 1/60.)

    def test_idle_head_bob_before_input_is_not_a_jump(self):
        frames = []
        for i in range(90):
            height = 1 if 1 <= i <= 4 else (round(20*np.sin(np.pi*(i-30)/40)) if 30 < i < 70 else 0)
            frames.append({"frame_index": i, "physics_tick": i+60, "time_s": i/60,
                           "valid": True, "y": 100-height})
        events, metrics, errors = measure_jump(frames, {"start_tick": 30}, 60, 24)
        self.assertFalse(errors)
        self.assertEqual(events["takeoff_frame"], 30)
        self.assertEqual(events["landing_frame"], 70)
        self.assertAlmostEqual(metrics["input_latency_s"], 1/60)

    def test_grounded_animation_envelope_does_not_delay_landing(self):
        frames = []
        for i in range(90):
            if 30 <= i < 60:
                y = 100 - max(3, round(20*np.sin(np.pi*(i-29)/31)))
            else:
                y = 99 + (i//3)%3
            frames.append({"frame_index": i, "physics_tick": i+60, "time_s": i/60,
                           "valid": True, "y": y})
        events, metrics, errors = measure_jump(frames, {"start_tick": 30}, 60, 24)
        self.assertFalse(errors)
        self.assertEqual(events["takeoff_frame"], 29)
        self.assertEqual(events["landing_frame"], 60)
        self.assertEqual(metrics["input_latency_s"], 0.)

    def test_occlusion_is_not_filled_by_expected_jump(self):
        run, path, _, _ = self.fixture(occlusion=True)
        result = analyze_run(run, path)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any(not f["valid"] and f["height_body"] is None for f in result["frames"][45:55]))

    def test_independent_points_validate_and_generated_points_do_not(self):
        run, path, annotation, truth = self.fixture()
        annotation["control_points"] = [{"run_id": run.name, "frame_index": i, "x": truth[i][0], "y": truth[i][1],
                                          "source": "assistant_visual"} for i in (0, 31, 40, 50, 60, 70)]
        annotation["control_events"] = [{"run_id": run.name, "takeoff_frame": 30, "apex_frame": 50,
                                          "landing_frame": 70, "source": "human"}]
        path.write_text(json.dumps(annotation), encoding="utf-8")
        result = analyze_run(run, path)
        self.assertEqual(result["annotation_validation"]["status"], "pass")
        self.assertEqual(result["annotation_validation"]["control_point_count"], 6)
        for p in annotation["control_points"]:
            p["source"] = "algorithm"
        path.write_text(json.dumps(annotation), encoding="utf-8")
        result = analyze_run(run, path)
        self.assertEqual(result["annotation_validation"]["point_status"], "pending")
        self.assertEqual(result["annotation_validation"]["control_point_count"], 0)

    def test_bad_manual_point_cannot_be_marked_pass(self):
        frames = [{"frame_index": 0, "x": 5., "y": 5., "valid": True}]
        annotation = {"control_points": [{"run_id": "run", "frame_index": 0, "x": 99., "y": 99., "source": "human"}]}
        result = validate_controls(frames, {}, annotation, self.root / "run", {}, 10)
        self.assertEqual(result["status"], "fail")

    def test_annotation_rejects_out_of_bounds_and_duplicate_truth(self):
        _, _, annotation, _ = self.fixture()
        annotation["roi"] = [159, 100, 8, 10]
        with self.assertRaisesRegex(ValueError, "viewport"):
            validate_annotation(annotation, [160, 140])

    def test_rotation_bank_preserves_off_center_anchor(self):
        rng = np.random.default_rng(321)
        template = rng.integers(100, 250, (10, 8, 3), dtype=np.uint8)
        bank = _template_bank(template, [2, 3], [0, 90, 180, 270])
        canvas = np.zeros((60, 60, 3), dtype=np.uint8)
        rotated = np.rot90(template)
        canvas[20:28, 25:35] = rotated
        # A point (2,3) in 8x10 becomes (3,5) after CCW rotation.
        anchor, score, margin, angle = _match_bank(canvas, bank, (28, 25), 12)
        self.assertEqual(anchor, (28, 25))
        self.assertEqual(angle, 90)
        self.assertGreater(score, .99)
        self.assertGreater(margin, .03)

    def test_control_collection_uses_only_explicit_clicks_and_events(self):
        run, path, _, truth = self.fixture()
        with patch("kiroclops.annotation._select_point", side_effect=[list(truth[31]), list(truth[50])]):
            annotation = annotate_controls(run, path, [31, 50], event_frames=[30, 50, 70])
        self.assertEqual(len(annotation["control_points"]), 2)
        self.assertTrue(all(point["source"] == "human" for point in annotation["control_points"]))
        self.assertEqual(annotation["validation_status"], "pending")
        self.assertEqual(annotation["control_events"][0]["apex_frame"], 50)

    def test_cancelled_control_annotation_does_not_save_partial_truth(self):
        run, path, _, truth = self.fixture()
        original = path.read_bytes()
        with patch("kiroclops.annotation._select_point", side_effect=[list(truth[31]), ValueError("cancelled")]):
            with self.assertRaises(ValueError):
                annotate_controls(run, path, [31, 50])
        self.assertEqual(path.read_bytes(), original)

    def test_interactive_annotation_collects_missing_anchor_and_scale(self):
        run, _, _, _ = self.fixture()
        with patch.object(cv2, "selectROI", side_effect=[(60, 100, 8, 10), (60, 100, 8, 10), (5, 5, 14, 14)]), \
             patch("kiroclops.annotation._select_point", return_value=[64, 105]), \
             patch.object(cv2, "destroyAllWindows"):
            annotation = annotate_run(run, self.root / "interactive.json", interactive=True)
        self.assertEqual(annotation["anchor_offset"], [4, 5])
        self.assertEqual(annotation["body_height_px"], 10.)


if __name__ == "__main__":
    unittest.main()
