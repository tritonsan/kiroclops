import json
from pathlib import Path
import tempfile
import unittest

from kiroclops.fitting import fit_profile, simulate_profile


def synthetic_report(hold, family, *, cap=12, split="calibration", takeoff=32):
    """Independent step simulation supplies known ground truth for fitter tests."""
    velocity, height, frames = 4., 0., []
    landing = None
    for tick in range(180):
        if tick > takeoff and landing is None:
            if family == "held_velocity" and tick < 30+min(hold, cap):
                velocity = 4.
            else:
                velocity -= 14./60
            height += velocity/60
            if height <= 0:
                height = 0.
                landing = tick
        frames.append({"frame_index": tick, "physics_tick": tick, "time_s": tick/60,
                       "valid": True, "height_body": height})
    apex = max(range(len(frames)), key=lambda i: frames[i]["height_body"])
    return {"schema_version": 1, "run_id": f"fixture-{family}-{hold}", "status": "ok", "fps": 60,
            "game": "tiny", "variant": "reference", "valid_ratio": 1., "camera_valid": True,
            "scenario": {"hold_ticks": hold, "start_tick": 30, "split": split},
            "events": {"takeoff_frame": takeoff, "apex_frame": apex, "landing_frame": landing},
            "frames": frames, "annotation_validation": {"status": "pending"}}


class FittingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_reports(self, family, *, split="calibration"):
        paths = []
        for hold in (6, 24):
            path = self.root / f"{hold}.json"
            path.write_text(json.dumps(synthetic_report(hold, family, split=split)), encoding="utf-8")
            paths.append(path)
        return paths

    def test_ballistic_selected_without_unneeded_hold_parameter(self):
        result = fit_profile(self.write_reports("ballistic"), self.root / "profile.json")
        self.assertEqual(result["family"], "ballistic")
        self.assertAlmostEqual(result["v0_body_s"], 4., places=8)
        self.assertAlmostEqual(result["gravity_body_s2"], 14., places=8)
        self.assertEqual(result["validation_status"], "pending")

    def test_held_velocity_selected_and_absolute_release_phase_preserved(self):
        result = fit_profile(self.write_reports("held_velocity"), self.root / "profile.json")
        self.assertEqual(result["family"], "held_velocity")
        self.assertEqual(result["hold_ticks"], 12)
        self.assertAlmostEqual(result["v0_body_s"], 4., places=8)
        self.assertAlmostEqual(result["gravity_body_s2"], 14., places=8)
        prediction = simulate_profile(result, [3, 4], 6, input_start_tick=30, takeoff_tick=32)
        self.assertAlmostEqual(prediction[0], 3*4/60)
        self.assertLess(prediction[1], 4*4/60)

    def test_validation_data_is_refused(self):
        with self.assertRaisesRegex(ValueError, "validation data"):
            fit_profile(self.write_reports("ballistic", split="validation"), self.root / "profile.json")

    def test_absolute_physics_warmup_preserves_release_timing(self):
        paths = self.write_reports("held_velocity")
        for path in paths:
            data = json.loads(path.read_text())
            for frame in data["frames"]:
                frame["physics_tick"] += 60
            path.write_text(json.dumps(data), encoding="utf-8")
        result = fit_profile(paths, self.root / "profile.json")
        self.assertEqual(result["family"], "held_velocity")
        self.assertEqual(result["hold_ticks"], 12)
        self.assertAlmostEqual(result["v0_body_s"], 4., places=8)

    def test_rejected_tracking_is_refused(self):
        paths = self.write_reports("ballistic")
        data = json.loads(paths[0].read_text())
        data["status"] = "rejected"
        paths[0].write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            fit_profile(paths, self.root / "profile.json")


if __name__ == "__main__":
    unittest.main()
