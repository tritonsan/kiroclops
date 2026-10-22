import copy
import math
import unittest

from kiroclops.comparison import compare_runs, curve_rmse


def report(variant, *, amplitude=1., duration=60, manual=True, split="validation", hold=12, takeoff=30):
    run_id = f"fixture-{variant}-{hold}"
    frames = []
    for index in range(takeoff+duration+10):
        elapsed = index-takeoff
        height = amplitude*math.sin(math.pi*elapsed/duration) if 0 < elapsed < duration else 0.
        frames.append({"frame_index": index, "time_s": index/60, "valid": True, "height_body": height})
    validation = {"status": "pass" if manual else "pending", "points": [], "event_checks": []}
    if manual:
        validation["points"] = [{"run_id": run_id, "frame_index": i, "error_px": 0., "source": "human"} for i in range(20)]
        validation["event_checks"] = [{"source": "human", "pass": True}]
    return {"status": "ok", "game": "tiny" if variant == "reference" else "slay", "variant": variant,
            "run_id": run_id, "fps": 60, "body_height_px": 12, "valid_ratio": 1., "camera_valid": True,
            "scenario": {"split": split, "hold_ticks": hold, "repeat": 1, "spawn_offset": 0, "start_tick": 30},
            "events": {"takeoff_frame": takeoff, "apex_frame": takeoff+duration//2, "landing_frame": takeoff+duration},
            "metrics": {"height_body": amplitude, "rise_s": duration/120, "fall_s": duration/120},
            "frames": frames, "annotation_validation": validation}


def matrix(variant, **kwargs):
    reports = []
    for hold in (8, 14):
        for repeat in (1, 2, 3):
            for offset in (0, 24):
                item = report(variant, hold=hold, **kwargs)
                item["scenario"].update(repeat=repeat, spawn_offset=offset)
                reports.append(item)
    return reports


class ComparisonTests(unittest.TestCase):
    def test_measurable_improvement_passes_all_fixed_gates(self):
        result = compare_runs(matrix("reference"), matrix("before", amplitude=1.8), matrix("after"))
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "pass")

    def test_missing_independent_evidence_remains_pending(self):
        result = compare_runs(report("reference", manual=False), report("before", amplitude=1.8, manual=False), report("after", manual=False))
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "pending")

    def test_perfect_after_without_initial_error_cannot_claim_improvement(self):
        result = compare_runs(report("reference"), report("before"), report("after"))
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "fail")

    def test_no_per_jump_amplitude_normalization(self):
        error = curve_rmse(report("reference"), report("after", amplitude=2.))
        self.assertGreater(error, .6)

    def test_no_time_warp_and_union_duration_penalizes_long_jump(self):
        ref, slow = report("reference"), report("after", duration=120)
        self.assertGreater(curve_rmse(ref, slow), .3)
        result = compare_runs(ref, report("before", amplitude=2), slow)
        self.assertFalse(result["validation_scenarios"][0]["gates"]["rise"])

    def test_observed_takeoff_alignment_ignores_absolute_start_only(self):
        self.assertAlmostEqual(curve_rmse(report("reference"), report("after", takeoff=36)), 0., places=12)

    def test_manual_count_pools_calibration_and_validation_not_minimum_per_run(self):
        ref, before, after = matrix("reference", manual=False), matrix("before", amplitude=1.8, manual=False), matrix("after", manual=False)
        calibration_ref = report("reference", hold=6, split="calibration")
        calibration_before = report("before", hold=6, split="calibration")
        result = compare_runs([calibration_ref] + ref, [calibration_before] + before, after)
        self.assertTrue(result["success"])

    def test_one_good_triplet_does_not_claim_complete_phase_success(self):
        result = compare_runs(report("reference", hold=8), report("before", hold=8, amplitude=1.8), report("after", hold=8))
        self.assertTrue(result["validation_scenarios"][0]["pass"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["coverage"]["complete_triplets"], 1)

    def test_missing_frames_are_not_interpolated_over(self):
        after = report("after")
        after["frames"][45]["valid"] = False
        result = compare_runs(report("reference"), report("before", amplitude=1.8), after)
        self.assertEqual(result["status"], "fail")

    def test_calibration_only_cannot_prove_transfer(self):
        result = compare_runs(report("reference", split="calibration"), report("before", split="calibration"), report("after", split="calibration"))
        self.assertEqual(result["status"], "pending")


if __name__ == "__main__":
    unittest.main()
