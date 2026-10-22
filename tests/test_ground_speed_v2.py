"""Synthetic native-pixel tests only; no historical held-out data is read."""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from kiroclops.integration import ground_speed_v2 as speed
from kiroclops.ground.analysis import SCENARIOS


def report(velocity=7.67, height=7, *, sid="r48", variant="reference", offset=0.23, sign=1, trajectory=None):
    events = [{"tick": tick, "action": action, "pressed": pressed} for tick, action, pressed in SCENARIOS[sid]]
    frames = []
    for i in range(240):
        position = trajectory(i) if trajectory else sign*velocity*height*i/60
        x = float(np.floor(position+offset+0.5)) + 100.5
        frames.append({"frame_index": i, "time_s": i/60, "valid": True,
                       "x": x, "x_body": (x-100.5)/height, "mirrored": sign < 0})
    return {"status": "ok", "game": "tiny" if variant == "reference" else "slay", "variant": variant,
            "run_id": "synthetic", "fps": 60, "body_height_px": height, "camera_valid": True,
            "valid_ratio": 1., "scenario": {"id": sid, "split": "calibration" if sid in ("r06", "r48") else "validation",
                "repeat": 1, "spawn_offset": 0, "events": events, "warmup_ticks": 60, "frame_count": 240},
            "frames": frames}


class GroundSpeedV2Tests(unittest.TestCase):
    def test_equal_velocities_at_different_native_scales_pass_without_widening_tolerance(self):
        for offset in (0.01, 0.31, 0.81):
            with self.subTest(offset=offset):
                result = speed.compare_speeds(report(offset=offset), report(height=24, variant="after", offset=1-offset))
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["relative_error_limit"], .1)
                self.assertLess(result["relative_error_interval"][1], .1)
                self.assertFalse(result["grants_legacy_acceptance"])

    def test_equal_velocity_feasible_interval_contains_actual_synthetic_value(self):
        result = speed.estimate_speed(report())
        low, high = result["feasible_speed_interval_body_s"]
        self.assertLessEqual(low, 7.67)
        self.assertGreaterEqual(high, 7.67)
        self.assertLessEqual(result["stationarity"]["full_window"]["feasible_line_max_residual_px"], .5+1e-10)
        self.assertEqual(result["window"]["frame_indices"], list(range(46,78)))

    def test_true_large_speed_mismatch_fails(self):
        result = speed.compare_speeds(report(), report(velocity=7.67*1.20, height=24, variant="after"))
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["relative_error_interval"][0], .1)

    def test_just_over_ten_percent_never_passes_even_when_rounding_is_ambiguous(self):
        for mismatch in (1.101, 1.11, 1.13):
            result = speed.compare_speeds(report(), report(velocity=7.67*mismatch, height=24, variant="after"))
            self.assertIn(result["status"], {"fail", "indeterminate"})
            self.assertIsNot(result["pass"], True)

    def test_boundary_crossing_uncertainty_is_indeterminate_not_relaxed_pass(self):
        result = speed.compare_speeds(report(velocity=6, height=6), report(velocity=6.6, height=6, variant="after"))
        self.assertEqual(result["status"], "indeterminate")
        self.assertLessEqual(result["relative_error_interval"][0], .1)
        self.assertGreater(result["relative_error_interval"][1], .1)
        self.assertIsNone(result["pass"])

    def test_fixed_window_rejects_resolvable_continuous_acceleration(self):
        accelerating = report(trajectory=lambda i: .15*i*i)
        result = speed.estimate_speed(accelerating)
        self.assertEqual(result["status"], "model_mismatch")
        self.assertIsNone(result["speed_body_s"])
        self.assertEqual(result["window"]["frame_indices"], list(range(46,78)))
        self.assertFalse(result["stationarity"]["window_adapted_after_observation"])

    def test_reversal_still_accelerating_in_fixed_last_two_thirds_does_not_pass(self):
        values, position, velocity = [], 0., 7.5
        for tick in range(240):
            if 54 <= tick < 78:
                velocity = max(-7.5, velocity-1.)  # 15 ticks to reverse fully
            position += velocity/60
            values.append(position*24)
        candidate = report(height=24, sid="reverse24", variant="after", trajectory=lambda i: values[i])
        estimate = speed.estimate_speed(candidate)
        self.assertEqual(estimate["window"]["frame_indices"], list(range(62,78)))
        self.assertEqual(estimate["status"], "model_mismatch")
        reference = deepcopy(candidate); reference["variant"] = "reference"
        result = speed.compare_speeds(reference, candidate)
        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["pass"])

    def test_steady_reference_nonstationary_target_cannot_opt_out(self):
        target = report(height=24, variant="after", trajectory=lambda i: .15*i*i)
        result = speed.compare_speeds(report(), target)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["pass"])
        self.assertEqual(result["reference_eligibility"]["status"], "constant_speed_supported_at_native_resolution")
        self.assertFalse(result["reference_eligibility"]["nonstationarity_automatically_exempted"])

    def test_equal_artificial_motion_exposes_short18_uncertainty_but_resolves_left30(self):
        for sid in ("r18", "l30"):
            directions, position, velocity, values = set(), 0., 0., []
            events = SCENARIOS[sid]
            for tick in range(240):
                for event_tick, action, pressed in events:
                    if event_tick == tick:
                        directions.add(action) if pressed else directions.discard(action)
                direction = int("right" in directions)-int("left" in directions)
                wanted = direction*7.5
                velocity += min(1., max(-1., wanted-velocity))
                position += velocity/60
                values.append(position)
            ref = report(sid=sid, trajectory=lambda i: values[i]*7)
            target = report(sid=sid, height=24, variant="after", trajectory=lambda i: values[i]*24)
            result = speed.compare_speeds(ref, target)
            with self.subTest(sid=sid):
                if sid == "r18":
                    self.assertEqual(result["status"], "indeterminate")
                    self.assertGreater(result["relative_error_interval"][1], .1)
                    self.assertLess(result["speed_relative_error"], .1)
                    self.assertIsNone(result["pass"])
                else:
                    self.assertEqual(result["status"], "pass")
                    self.assertLessEqual(result["relative_error_interval"][1], .1)

    def test_six_tick_press_is_not_applicable_not_a_speed_acceptance(self):
        result = speed.compare_speeds(report(sid="r06"), report(sid="r06", height=24, variant="after"))
        self.assertEqual(result["status"], "not_applicable")
        self.assertFalse(result["speed_eligible"])
        self.assertIsNone(result["pass"])
        self.assertIsNone(result["reference"]["window"])

    def test_horizontal_mirror_is_a_coordinate_reflection_not_an_extra_sign_change(self):
        left = report(sid="l30", sign=-1)
        target = report(sid="l30", sign=-1, height=24, variant="after")
        result = speed.compare_speeds(left, target)
        self.assertEqual(result["status"], "pass")
        self.assertLess(result["reference"]["speed_body_s"], 0)
        for item in target["frames"]: item["mirrored"] = not item["mirrored"]
        again = speed.compare_speeds(left, target)
        self.assertEqual(again["relative_error_interval"], result["relative_error_interval"])

    def test_wrong_commanded_direction_is_a_real_speed_failure(self):
        result = speed.compare_speeds(report(), report(sign=-1, height=24, variant="after"))
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["relative_error_interval"][0], 1.)

    def test_stationary_reference_does_not_provide_a_division_based_pass(self):
        result = speed.compare_speeds(report(velocity=0), report(velocity=0, height=24, variant="after"))
        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["relative_error_interval"])

    def test_bad_selected_frame_cannot_be_interpolated_or_shorten_window(self):
        value = report()
        value["frames"][55].update(valid=False, x_body=None)
        value["valid_ratio"] = 239/240
        result = speed.estimate_speed(value)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["window"]["sample_count"], 32)
        self.assertIsNone(result["speed_body_s"])

    def test_nonfinite_wrong_timing_camera_or_inconsistent_scale_is_invalid(self):
        for mutation in (lambda r:r["frames"][60].update(x=float("nan")),
                         lambda r:r["frames"][60].update(time_s=99),
                         lambda r:r.update(camera_valid=False),
                         lambda r:r.update(body_height_px=0),
                         lambda r:r["frames"][60].update(x_body=999)):
            value = report(); mutation(value)
            self.assertEqual(speed.estimate_speed(value)["status"], "invalid")

    def test_different_slot_is_invalid_even_if_speeds_match(self):
        target = report(height=24, variant="after")
        target["scenario"]["repeat"] = 2
        self.assertEqual(speed.compare_speeds(report(), target)["status"], "invalid")

    def test_window_policy_is_explicit_deterministic_and_not_result_dependent(self):
        policy = speed.SpeedPolicy(maximum_samples=24)
        value = report()
        before = json.dumps(value, sort_keys=True)
        a, b = speed.estimate_speed(value, policy=policy), speed.estimate_speed(value, policy=policy)
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(value, sort_keys=True), before)
        self.assertEqual(a["window"]["frame_indices"], list(range(54,78)))
        self.assertNotEqual(policy.record()["sha256"], speed.DEFAULT_POLICY.record()["sha256"])
        self.assertEqual(policy.record()["relative_error_limit"], .1)
        self.assertEqual(policy.record()["rounding_half_width_px"], .5)

    def test_short_fixed_window_insufficient_and_invalid_policies_rejected(self):
        policy = speed.SpeedPolicy(discard_numerator=2, discard_denominator=3)
        result = speed.estimate_speed(report(sid="r18"), policy=policy)
        self.assertEqual(result["status"], "insufficient_data")
        for kwargs in ({"minimum_samples":2}, {"minimum_samples":3}, {"maximum_samples":8},
                       {"discard_numerator":3}, {"minimum_held_ticks":True}):
            with self.assertRaises(ValueError): speed.SpeedPolicy(**kwargs)

    def test_malformed_scenario_returns_invalid_without_accidental_exception(self):
        value = report()
        value["scenario"] = None
        self.assertEqual(speed.compare_speeds(value, report(variant="after"))["status"], "invalid")

    def test_old_eight_frame_rounding_bound_is_recovered_as_math_not_adopted_as_tolerance(self):
        values = [float(np.floor(7.67*7*i/60+.23+.5)) for i in range(8)]
        line = speed._line(values, 60)
        self.assertAlmostEqual(line["ols_rounding_bound_px_s"]/7, 80/(7*7))
        longer = speed.estimate_speed(report())
        self.assertLess(longer["quantization"]["ols_worst_case_bound_body_s"], 80/(7*7))

    def test_long_hold_policy_eligibility_is_input_only_and_short_slots_are_explicit_na(self):
        for sid, duration in (("r06",6),("r18",18),("reverse24",24)):
            result = speed.compare_speeds(report(sid=sid), report(sid=sid, height=24, variant="after", trajectory=lambda i:.15*i*i), policy=speed.LONG_HOLD_POLICY)
            with self.subTest(sid=sid):
                self.assertEqual(result["status"], "not_applicable")
                self.assertIsNone(result["pass"])
                self.assertFalse(result["speed_eligible"])
                self.assertEqual(result["reference"]["eligibility_basis"], {"source":"input schedule only","last_held_ticks":duration,"minimum_held_ticks":30})
                self.assertFalse(result["grants_legacy_acceptance"])

    def test_long_hold_policy_equal_right48_and_mirrored_left30_pass(self):
        for sid, direction, count in (("r48",1,32),("l30",-1,20)):
            result = speed.compare_speeds(report(sid=sid, sign=direction), report(sid=sid, sign=direction, height=24, variant="after"), policy=speed.LONG_HOLD_POLICY)
            with self.subTest(sid=sid):
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["reference"]["window"]["sample_count"], count)
                self.assertEqual(result["relative_error_limit"], .1)

    def test_long_hold_policy_resolvable_twelve_plus_percent_speed_errors_fail(self):
        for sid, direction, factor in (("r48",1,1.12),("l30",-1,1.15)):
            result = speed.compare_speeds(report(sid=sid, sign=direction), report(velocity=7.67*factor, sid=sid, sign=direction, height=24, variant="after"), policy=speed.LONG_HOLD_POLICY)
            with self.subTest(sid=sid):
                self.assertEqual(result["status"], "fail")
                self.assertGreater(result["relative_error_interval"][0], .1)

    def test_long_hold_policy_twelve_percent_at_exact_rounding_boundary_is_never_accepted(self):
        result = speed.compare_speeds(report(sid="l30", sign=-1), report(velocity=7.67*1.12, sid="l30", sign=-1, height=24, variant="after"), policy=speed.LONG_HOLD_POLICY)
        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["pass"])
        self.assertAlmostEqual(result["relative_error_interval"][0], .1)
        self.assertGreater(result["relative_error_interval"][1], .1)

    def test_long_hold_policy_eligible_target_nonstationarity_cannot_create_exemption(self):
        result = speed.compare_speeds(report(), report(height=24, variant="after", trajectory=lambda i:.15*i*i), policy=speed.LONG_HOLD_POLICY)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["speed_eligible"])
        self.assertEqual(speed.DEFAULT_POLICY.minimum_held_ticks, 18)
        self.assertNotEqual(speed.DEFAULT_POLICY.record()["sha256"], speed.LONG_HOLD_POLICY.record()["sha256"])


if __name__ == "__main__":
    unittest.main()
