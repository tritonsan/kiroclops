"""V3 web suite: recipes, retest-floor policy and the re-gated window comparison."""
from copy import deepcopy
import math
import unittest

from kiroclops.local_engine import realistic_policy, realistic_policy_v3, suite_web_v2, suite_web_v3
from kiroclops.visual.schema import VisualError, digest

OBJECTS = ("handle", "bookmark", "star")


def calibration_review(object_ids=OBJECTS):
    points = [{"object_id": oid, "frame_sha256": digest([oid, index]), "error_px": 1.,
               "role": "reference" if index % 2 == 0 else "original", "split": "calibration", "independent": True}
              for oid in object_ids for index in range(20)]
    return {"status": "pass", "basis": "independent_raw_pixels", "points": points}


def pointer_events(events):
    actual = deepcopy(events)
    point = [100., 120.]
    for index, event in enumerate(actual):
        if event["action"] != "drag_end":
            point = [100. + index * 10., 120.]
        event.update(client_x=point[0], client_y=point[1])
    return actual


def panel_record(scenario, scale=1., star_bump=0.):
    """Synthetic panel record; ``star_bump`` adds a smooth mid-motion bump that keeps the event order."""
    objects = []
    for oid, offset, delay, travel in (("handle", 0., 0., 0.), ("bookmark", 30., .08, 24.), ("star", 60., .18, 40.)):
        frames = []
        for index in range(181):
            t = index / 60
            x = 100. * scale * max(0., min(1., (t - .4) / .6)) + offset
            y = 30. + travel * scale * max(0., min(1., (t - .4 - delay) / .3))
            if oid == "star" and .62 < t < .82:
                y += star_bump * math.sin(math.pi * (t - .62) / .2)
            frames.append({"index": index, "time_s": t, "x": x, "y": y, "left": x - 5., "top": y - 5.,
                           "width": 10., "height": 10., "valid": True, "present": True, "expected_active": True})
        objects.append({"id": oid, "frames": frames, "active_intervals_s": [[0., 3.]], "valid_ratio": 1.})
    observations = {"schema_version": 1, "kind": "ObservationSet", "status": "ok", "measurement_status": "pass",
                    "package_sha256": "synthetic", "image_size": [960, 640], "objects": objects, "connections": [],
                    "inputs": {"provenance": "recorder_verified", "events": pointer_events(scenario["events"])}}
    return {"scenario": deepcopy(scenario), "observations": observations, "measurement_status": "ok",
            "functional": {"status": "pass"}, "suite_event_contract": suite_web_v2.event_contract("panel"),
            "events": {"events": [], "relations": []},
            "selection": {"objects": [{"id": oid} for oid in OBJECTS], "relative_motion": [
                {"object_id": "bookmark", "relative_to": "handle"}, {"object_id": "star", "relative_to": "handle"}]}}


def open_pair():
    scenarios = suite_web_v3.scenarios("panel", "calibration")
    return scenarios[0], scenarios[1]


class RecipeTests(unittest.TestCase):
    def test_counts_identity_and_capture_gate(self):
        calibration = suite_web_v3.scenarios("panel", "calibration")
        regression = suite_web_v3.scenarios("panel", "regression")
        validation = suite_web_v3.scenarios("panel", "validation")
        self.assertEqual((12, 24, 12), (len(calibration), len(regression), len(validation)))
        ids = [s["case_id"] for s in calibration + regression + validation]
        self.assertEqual(len(ids), len(set(ids)))
        for scenario in calibration + regression + validation:
            # The natural recorder keeps its V2 replay-boundary fix only for this value.
            self.assertEqual(suite_web_v2.VERSION, scenario["suite_version"])
            self.assertEqual(suite_web_v3.V3_VERSION, scenario["v3_suite_version"])
            self.assertEqual(realistic_policy_v3.POLICY_ID, scenario["acceptance_profile_id"])
        self.assertTrue(all(s["suite_split"] == "regression" and s["blind_validation"] is False for s in regression))
        self.assertTrue(all(s["suite_split"] == "validation" and s["blind_validation"] for s in validation))

    def test_narrow_calibration_and_unseen_validation(self):
        narrow = [s for s in suite_web_v3.scenarios("panel", "calibration") if s["viewport"] == [768, 640]]
        self.assertEqual(4, len(narrow))
        self.assertEqual({.7}, {s["events"][1]["time_s"] for s in narrow})
        validation = suite_web_v3.scenarios("panel", "validation")
        self.assertEqual({640, 1120}, {s["viewport"][0] for s in validation})
        used = {tuple(e["time_s"] for e in s["events"])
                for split in ("calibration", "known_regression", "validation")
                for s in suite_web_v2.scenarios("panel", split)}
        for scenario in validation:
            self.assertNotIn(tuple(e["time_s"] for e in scenario["events"]), used)
            self.assertNotIn(scenario["viewport"][0], {768, 960, 1280})

    def test_svg_stays_on_the_unchanged_v2_suite(self):
        # The SVG initial acceptance matches suite cases to the exact V2 recipe objects.
        for split in ("calibration", "regression", "validation"):
            with self.assertRaises(VisualError):
                suite_web_v3.scenarios("svg", split)
        with self.assertRaises(VisualError):
            realistic_policy_v3.build_policy("svg", calibration_review(("a", "b", "c")), {})


class PolicyTests(unittest.TestCase):
    def references(self, bump=0.):
        first, second = open_pair()
        return {first["case_id"]: panel_record(first), second["case_id"]: panel_record(second, star_bump=bump)}

    def test_build_validate_and_tamper(self):
        policy = realistic_policy_v3.build_policy("panel", calibration_review(), self.references())
        self.assertIs(policy, realistic_policy_v3.validate_policy(policy, domain="panel"))
        self.assertEqual(realistic_policy.build_policy("panel", calibration_review()), policy["base_policy"])
        self.assertTrue(policy["retest_floors_px"])
        self.assertTrue(all(value < 1e-9 for value in policy["retest_floors_px"].values()))
        changed = deepcopy(policy)
        changed["retest_floors_px"][next(iter(changed["retest_floors_px"]))] = 9.
        with self.assertRaises(VisualError):
            realistic_policy_v3.validate_policy(changed)
        changed = deepcopy(policy)
        changed["rules"]["retest_factor"] = 5.
        with self.assertRaises(VisualError):
            realistic_policy_v3.validate_policy(changed)

    def test_unpaired_recipes_fail_closed(self):
        first, _ = open_pair()
        with self.assertRaises(VisualError):
            realistic_policy_v3.build_policy("panel", calibration_review(), {first["case_id"]: panel_record(first)})

    def star_window(self, bump):
        policy = realistic_policy_v3.build_policy("panel", calibration_review(), self.references(bump=bump))
        first, _ = open_pair()
        star = {k: v for k, v in policy["retest_floors_px"].items()
                if k.startswith(first["case_id"] + "|star|handle|")}
        self.assertEqual(1, len(star))
        (key, retest), = star.items()
        row = {"reference_id": "star", "relative_to_reference": "handle", "segment_index": 0,
               "window_id": key.split("|")[3], "scale_px": 40., "limits": {"rmse_limit": 2. / 40.}}
        return policy, first["case_id"], row, retest

    def test_unstable_reference_window_gets_twice_its_retest(self):
        policy, case_id, row, retest = self.star_window(5.)
        self.assertGreater(2. * retest, 2.)
        self.assertAlmostEqual(2. * retest / 40., realistic_policy_v3.window_rmse_limit(policy, case_id, row))
        missing = dict(row, window_id="motion-99")
        self.assertIsNone(realistic_policy_v3.retest_px(policy, case_id, missing))
        self.assertEqual(2. / 40., realistic_policy_v3.window_rmse_limit(policy, case_id, missing))

    def test_small_retest_never_tightens_or_loosens_the_v1_limit(self):
        policy, case_id, row, retest = self.star_window(3.)
        self.assertGreater(retest, 0.)
        self.assertLessEqual(2. * retest, 2. + 1e-9)
        self.assertEqual(2. / 40., realistic_policy_v3.window_rmse_limit(policy, case_id, row))


class CompareTests(unittest.TestCase):
    def policy(self, bump):
        first, second = open_pair()
        references = {first["case_id"]: panel_record(first), second["case_id"]: panel_record(second, star_bump=bump)}
        return realistic_policy_v3.build_policy("panel", calibration_review(), references), first

    def test_stable_reference_keeps_the_v2_verdict(self):
        policy, scenario = self.policy(0.)
        reference = panel_record(scenario)
        candidate = panel_record(scenario, star_bump=8.)
        v2 = suite_web_v2.compare_records(reference, panel_record(scenario, .75), candidate,
                                          acceptance_policy=policy["base_policy"])
        v3 = suite_web_v3.compare_records(reference, panel_record(scenario, .75), candidate, acceptance_policy=policy)
        self.assertFalse(v2["pass"])
        self.assertEqual(v2["pass"], v3["pass"])
        self.assertEqual("per_window_pixel_failure", v3["failure_class"])

    def test_noisy_reference_window_is_judged_against_its_retest(self):
        policy, scenario = self.policy(5.)
        reference = panel_record(scenario)
        within = panel_record(scenario, star_bump=8.)
        v2 = suite_web_v2.compare_records(reference, panel_record(scenario, .75), within,
                                          acceptance_policy=policy["base_policy"])
        v3 = suite_web_v3.compare_records(reference, panel_record(scenario, .75), within, acceptance_policy=policy)
        self.assertFalse(v2["pass"])
        self.assertTrue(v3["pass"], v3["failure_class"])
        self.assertIsNone(v3["failure_class"])
        beyond = panel_record(scenario, star_bump=12.)
        self.assertFalse(suite_web_v3.compare_records(reference, panel_record(scenario, .75), beyond,
                                                      acceptance_policy=policy)["pass"])

    def test_foreign_v3_stamp_is_rejected(self):
        policy, scenario = self.policy(0.)
        candidate = panel_record(scenario)
        candidate["v3_acceptance_policy_sha256"] = "0" * 64
        with self.assertRaises(VisualError):
            suite_web_v3.compare_records(panel_record(scenario), panel_record(scenario, .75), candidate,
                                         acceptance_policy=policy)


if __name__ == "__main__":
    unittest.main()
