"""Prospective policy qualification, immutable tolerances and real input replay."""
from copy import deepcopy
import unittest

from kiroclops.local_engine import realistic_policy as policy
from kiroclops.visual.schema import VisualError, digest


def review(object_ids=("a",), *, error=1., count=20):
    return {"status": "pass", "basis": "independent_raw_pixels", "points": [
        {"object_id": oid, "frame_sha256": digest([oid, i]), "error_px": error,
         "role": "reference" if i % 2 == 0 else "original", "split": "calibration", "independent": True}
        for oid in object_ids for i in range(count)]}


def observation(events, step=.02):
    actual = deepcopy(events)
    for index, event in enumerate(actual):
        event.setdefault("client_x", 100. + index * 10.)
        event.setdefault("client_y", 120.)
    return {"inputs": {"provenance": "recorder_verified", "events": actual},
            "objects": [{"id": "a", "frames": [{"time_s": i * step} for i in range(30)]}]}


class PolicyTests(unittest.TestCase):
    def test_qualified_floor_uses_independent_p95_and_minimum_two(self):
        values = review(("a", "b"))
        for point in values["points"]:
            if point["object_id"] == "b":
                point["error_px"] = 2.5
        frozen = policy.build_policy("panel", values)
        self.assertEqual({"a": 2., "b": 2.5}, frozen["floors_px"])
        self.assertEqual(digest(values), frozen["calibration"]["review_sha256"])
        self.assertEqual(frozen["policy_sha256"], policy.policy_sha256(frozen))

    def test_twenty_distinct_points_are_required(self):
        for values in (review(count=19), review(count=0)):
            with self.assertRaises(VisualError) as caught:
                policy.build_policy("panel", values)
            self.assertEqual("calibration_unqualified", caught.exception.code)
        values = review()
        values["points"][-1]["frame_sha256"] = values["points"][0]["frame_sha256"]
        with self.assertRaises(VisualError):
            policy.build_policy("panel", values)

    def test_bad_measurement_does_not_raise_tolerance(self):
        for values in (review(error=3.01), review(error=float("nan")), review(error=-1)):
            with self.assertRaises(VisualError):
                policy.build_policy("panel", values)

    def test_heldout_or_production_points_cannot_calibrate_floor(self):
        for key, value in (("split", "validation"), ("independent", False), ("role", "candidate")):
            values = review()
            values["points"][0][key] = value
            with self.assertRaises(VisualError):
                policy.build_policy("panel", values)

    def test_tampered_policy_or_wrong_domain_is_rejected(self):
        frozen = policy.build_policy("panel", review())
        frozen["rules"]["curve_fraction"] = .2
        frozen["policy_sha256"] = digest({k: v for k, v in frozen.items() if k != "policy_sha256"})
        with self.assertRaises(VisualError):
            policy.validate_policy(frozen)
        frozen = policy.build_policy("panel", review())
        frozen["floors_px"]["a"] = 3
        with self.assertRaises(VisualError):
            policy.validate_policy(frozen)
        with self.assertRaises(VisualError):
            policy.validate_policy(policy.build_policy("panel", review()), domain="svg")

    def test_small_and_large_motion_limits_share_natural_pixel_floor(self):
        frozen = policy.build_policy("panel", review())
        small = policy.pixel_limits(frozen, "a", 24., 24.)
        self.assertEqual(2., small["curve_limit_px"])
        self.assertEqual(2. / 24., small["rmse_limit"])
        self.assertEqual(2., small["endpoint_limit_px"])
        large = policy.pixel_limits(frozen, "a", 400., 400.)
        self.assertEqual(20., large["curve_limit_px"])
        self.assertEqual(12., large["endpoint_limit_px"])
        self.assertEqual(8., large["settle_band_px"])
        self.assertEqual(20., large["overshoot_limit_px"])
        with self.assertRaises(VisualError):
            policy.pixel_limits(frozen, "unreviewed", 100.)

    def test_gain_is_diagnostic_and_zero_error_can_be_preserved(self):
        zero = policy.gain_diagnostic(0., 0., absolute_pass=True, limit=.03)
        self.assertEqual("preserved", zero["classification"])
        self.assertIsNone(zero["improvement_fraction"])
        self.assertFalse(zero["gain_required"])
        for after, expected in ((.075, "meaningful_improvement"), (.05, "strong_improvement"), (.08, "below_meaningful_gain")):
            result = policy.gain_diagnostic(.1, after, absolute_pass=True, limit=.03)
            self.assertEqual(expected, result["classification"])
        bad = policy.gain_diagnostic(.1, .001, absolute_pass=False, limit=.03)
        self.assertEqual("strong_improvement", bad["classification"])
        self.assertFalse(bad["gain_required"])


class InputHistoryTests(unittest.TestCase):
    def events(self, second=.6):
        return [{"time_s": .4, "action": "click", "selector": "#open-panel"},
                {"time_s": second, "action": "click", "selector": "#close-panel"}]

    def test_first_input_global_offset_is_allowed_without_warp(self):
        reference = observation(self.events())
        other = deepcopy(reference)
        for event in other["inputs"]["events"]:
            event["time_s"] += 1.
        result = policy.input_history_check(reference, other)
        self.assertTrue(result["pass"])
        self.assertFalse(result["duration_rescaled"])

    def test_interruption_history_difference_is_not_behavior_error(self):
        result = policy.input_history_check(observation(self.events()), observation(self.events(.6504)))
        self.assertEqual("input_history_mismatch", result["status"])
        self.assertFalse(result["pass"])
        self.assertAlmostEqual(.0504, result["maximum_difference_s"])

    def test_tolerance_is_capped_at_thirty_and_never_below_twenty_ms(self):
        for step, expected in ((.005, .020), (.016, .024), (.05, .030)):
            result = policy.input_history_check(observation(self.events(), step), observation(self.events(), step))
            self.assertAlmostEqual(expected, result["tolerance_s"])

    def test_pointer_order_and_destination_must_match(self):
        a = observation(self.events())
        b = deepcopy(a)
        b["inputs"]["events"][1]["selector"] = "#open-panel"
        self.assertEqual("input_identity_mismatch", policy.input_history_check(a, b)["reason"])

    def test_physical_pointer_path_is_compared_even_when_identity_and_times_match(self):
        a = observation(self.events())
        b = deepcopy(a)
        b["inputs"]["events"][1]["client_x"] += 8.
        result = policy.input_history_check(a, b)
        self.assertFalse(result["pass"])
        self.assertEqual("input_history_mismatch", result["status"])
        self.assertFalse(result["differences"][1]["pointer_match"])
        self.assertEqual(8., result["differences"][1]["pointer_difference_px"])

    def test_pointer_quantization_allows_one_pixel_but_not_more(self):
        a = observation(self.events())
        b = deepcopy(a)
        b["inputs"]["events"][0]["client_x"] += 1.
        b["inputs"]["events"][0]["client_y"] -= 1.
        self.assertTrue(policy.input_history_check(a, b)["pass"])
        b["inputs"]["events"][0]["client_x"] += .01
        self.assertFalse(policy.input_history_check(a, b)["pass"])

    def test_missing_or_nonfinite_pointer_counterpart_cannot_qualify(self):
        a = observation(self.events())
        for value in (None, float("nan"), float("inf")):
            b = deepcopy(a)
            b["inputs"]["events"][0]["client_x"] = value
            with self.assertRaises(VisualError) as caught:
                policy.input_history_check(a, b)
            self.assertEqual("unverified_suite_inputs", caught.exception.code)
        b = deepcopy(a)
        del b["inputs"]["events"][0]["client_x"]
        with self.assertRaises(VisualError) as caught:
            policy.input_history_check(a, b)
        self.assertEqual("unverified_suite_inputs", caught.exception.code)

    def test_both_histories_without_actual_pointer_evidence_are_rejected(self):
        a, b = observation(self.events()), observation(self.events())
        for record in (a, b):
            for event in record["inputs"]["events"]:
                del event["client_x"]
                del event["client_y"]
        with self.assertRaises(VisualError) as caught:
            policy.input_history_check(a, b)
        self.assertEqual("unverified_suite_inputs", caught.exception.code)

    def test_adjacent_interval_difference_cannot_hide_inside_first_input_bound(self):
        events = self.events()
        events.append({"time_s": .8, "action": "click", "selector": "#open-panel"})
        a, b = observation(events), observation(deepcopy(events))
        b["inputs"]["events"][1]["time_s"] += .029
        b["inputs"]["events"][2]["time_s"] -= .029
        result = policy.input_history_check(a, b)
        self.assertFalse(result["pass"])
        self.assertLess(result["maximum_difference_s"], .030)
        self.assertAlmostEqual(.058, result["maximum_adjacent_difference_s"])

    def test_unverified_or_unordered_history_is_rejected(self):
        a = observation(self.events())
        b = deepcopy(a)
        b["inputs"]["provenance"] = "user_declared"
        with self.assertRaises(VisualError):
            policy.input_history_check(a, b)
        b = observation(self.events(.3))
        with self.assertRaises(VisualError):
            policy.input_history_check(a, b)

    def test_replay_preserves_actual_relative_intervals_and_canonical_recipe(self):
        from kiroclops.local_engine.suite_web_v2 import scenarios
        canonical = next(c for c in scenarios("panel", "calibration") if c["recipe"] == "interrupt")
        reference = observation(deepcopy(canonical["events"]))
        reference["inputs"]["events"][0]["time_s"] += .019
        reference["inputs"]["events"][1]["time_s"] += .040
        prior = deepcopy(canonical)
        execution, receipt = policy.replay_scenario(canonical, reference)
        self.assertEqual(prior, canonical)
        self.assertAlmostEqual(.271, execution["events"][1]["time_s"] - execution["events"][0]["time_s"])
        self.assertEqual(digest(canonical), receipt["canonical_scenario_sha256"])
        self.assertEqual(digest(execution), receipt["execution_scenario_sha256"])
        self.assertFalse(receipt["duration_rescaled"])
        self.assertEqual([[e["client_x"], e["client_y"]] for e in reference["inputs"]["events"]],
                         [e["replay_client_point"] for e in execution["events"]])
        self.assertTrue(all("replay_client_point" not in e for e in canonical["events"]))

    def test_replay_requires_finite_actual_points_inside_reference_viewport(self):
        from kiroclops.local_engine.suite_web_v2 import scenarios
        canonical = scenarios("panel", "calibration")[0]
        valid = observation(canonical["events"])
        for key, value in (("client_x", None), ("client_y", float("nan")), ("client_x", float("inf"))):
            reference = deepcopy(valid)
            reference["inputs"]["events"][0][key] = value
            with self.assertRaises(VisualError) as caught:
                policy.replay_scenario(canonical, reference)
            self.assertEqual("unverified_suite_inputs", caught.exception.code)
        reference = deepcopy(valid)
        del reference["inputs"]["events"][0]["client_y"]
        with self.assertRaises(VisualError):
            policy.replay_scenario(canonical, reference)
        for key, value in (("client_x", -1.), ("client_y", -1.),
                           ("client_x", canonical["viewport"][0]), ("client_y", canonical["viewport"][1])):
            reference = deepcopy(valid)
            reference["inputs"]["events"][0][key] = value
            with self.assertRaises(ValueError):
                policy.replay_scenario(canonical, reference)


if __name__ == "__main__":
    unittest.main()
