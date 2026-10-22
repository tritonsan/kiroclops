"""V5 panel recipes: early-interrupt and wide-reversal calibration, observable regression, unseen validation."""
import unittest

from kiroclops.local_engine import realistic_policy, suite_web_v2, suite_web_v3, suite_web_v4, suite_web_v5
from kiroclops.visual.schema import VisualError


def timings(scenario):
    return tuple(round(event["time_s"], 3) for event in scenario["events"])


def clicks(scenario):
    return [e["time_s"] for e in scenario["events"] if e["action"] == "click"]


class V5RecipeTests(unittest.TestCase):
    def setUp(self):
        self.splits = {split: suite_web_v5.scenarios("panel", split)
                       for split in ("calibration", "regression", "validation")}

    def test_counts_identity_and_labels(self):
        self.assertEqual((22, 44, 12), tuple(len(self.splits[s]) for s in ("calibration", "regression", "validation")))
        ids = [s["case_id"] for rows in self.splits.values() for s in rows]
        self.assertEqual(len(ids), len(set(ids)))
        for split, rows in self.splits.items():
            for scenario in rows:
                self.assertEqual(split, scenario["suite_split"])
                self.assertEqual(suite_web_v2.VERSION, scenario["suite_version"])
                self.assertEqual(suite_web_v5.V5_VERSION, scenario["v5_suite_version"])
                self.assertEqual(realistic_policy.POLICY_ID, scenario["acceptance_profile_id"])
                self.assertEqual(split == "validation", scenario["blind_validation"])

    def test_calibration_adds_early_interrupts_and_a_wide_reversal(self):
        v4 = {s["case_id"] for s in suite_web_v4.scenarios("panel", "calibration")}
        added = [s for s in self.splits["calibration"] if s["case_id"] not in v4]
        self.assertTrue(v4 <= {s["case_id"] for s in self.splits["calibration"]})
        self.assertEqual({("interrupt-early", 768, (.4, .62)), ("interrupt-early", 1280, (.4, .62)),
                          ("reverse-wide", 1280, (.4, .66, .92))},
                         {(s["recipe"], s["viewport"][0], timings(s)) for s in added})
        regression = {timings(s) for s in self.splits["regression"]}
        for scenario in added:
            self.assertNotIn(timings(scenario), regression)

    def test_regression_drops_only_the_unobservable_recipe(self):
        v4 = [s for split in ("regression", "validation") for s in suite_web_v4.scenarios("panel", split)]
        expected = {s["case_id"] for s in v4 if s["recipe"] not in suite_web_v5.UNOBSERVABLE_RECIPES}
        self.assertEqual(expected, {s["case_id"] for s in self.splits["regression"]})
        self.assertEqual(4, len(v4) - len(expected))
        self.assertFalse(any(s["recipe"] == "interrupt-v3" for s in self.splits["regression"]))

    def test_validation_widths_and_timings_were_never_used(self):
        earlier = [s for module, splits in ((suite_web_v2, ("calibration", "known_regression", "validation")),
                                            (suite_web_v3, ("calibration", "regression", "validation")),
                                            (suite_web_v4, ("calibration", "regression", "validation")))
                   for split in splits for s in module.scenarios("panel", split)]
        earlier += self.splits["calibration"] + self.splits["regression"]
        used_timings = {timings(s) for s in earlier}
        used_widths = {s["viewport"][0] for s in earlier}
        for scenario in self.splits["validation"]:
            self.assertNotIn(timings(scenario), used_timings)
            self.assertNotIn(scenario["viewport"][0], used_widths)
        self.assertEqual({896, 1360}, {s["viewport"][0] for s in self.splits["validation"]})

    def test_every_new_interrupt_leaves_an_observable_response_window(self):
        v4 = {s["case_id"] for s in suite_web_v4.scenarios("panel", "calibration")}
        new = self.splits["validation"] + [s for s in self.splits["calibration"] if s["case_id"] not in v4]
        for scenario in new:
            times = clicks(scenario)
            if len(times) >= 2:
                self.assertGreaterEqual(times[1] - times[0], .22 - 1e-9)

    def test_only_the_panel_is_changed(self):
        with self.assertRaises(VisualError):
            suite_web_v5.scenarios("svg", "calibration")


if __name__ == "__main__":
    unittest.main()
