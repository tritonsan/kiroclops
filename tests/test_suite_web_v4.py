"""V4 panel recipes: wide calibration, unseen widths and timings, observable interrupt."""
import unittest

from kiroclops.local_engine import realistic_policy, suite_web_v2, suite_web_v3, suite_web_v4
from kiroclops.visual.schema import VisualError


def timings(scenario):
    return tuple(round(event["time_s"], 3) for event in scenario["events"])


class V4RecipeTests(unittest.TestCase):
    def setUp(self):
        self.splits = {split: suite_web_v4.scenarios("panel", split)
                       for split in ("calibration", "regression", "validation")}

    def test_counts_identity_and_labels(self):
        self.assertEqual((16, 36, 12), tuple(len(self.splits[s]) for s in ("calibration", "regression", "validation")))
        ids = [s["case_id"] for rows in self.splits.values() for s in rows]
        self.assertEqual(len(ids), len(set(ids)))
        for split, rows in self.splits.items():
            for scenario in rows:
                self.assertEqual(split, scenario["suite_split"])
                self.assertEqual(suite_web_v2.VERSION, scenario["suite_version"])
                self.assertEqual(suite_web_v4.V4_VERSION, scenario["v4_suite_version"])
                self.assertEqual(realistic_policy.POLICY_ID, scenario["acceptance_profile_id"])
                self.assertEqual(split == "validation", scenario["blind_validation"])

    def test_wide_calibration_and_known_regression(self):
        wide = [s for s in self.splits["calibration"] if s["viewport"] == [1280, 720]]
        self.assertEqual({("open-wide", (.4,)), ("interrupt-wide", (.4, .7))}, {(s["recipe"], timings(s)) for s in wide})
        v3 = {s["case_id"] for split in ("regression", "validation") for s in suite_web_v3.scenarios("panel", split)}
        self.assertEqual(v3, {s["case_id"] for s in self.splits["regression"]})

    def test_validation_widths_and_timings_were_never_used(self):
        used_timings = {timings(s) for module, splits in ((suite_web_v2, ("calibration", "known_regression", "validation")),
                                                           (suite_web_v3, ("calibration", "regression", "validation")))
                        for split in splits for s in module.scenarios("panel", split)}
        used_widths = {s["viewport"][0] for module, splits in ((suite_web_v2, ("calibration", "known_regression", "validation")),
                                                                (suite_web_v3, ("calibration", "regression", "validation")))
                       for split in splits for s in module.scenarios("panel", split)} | {1280}
        for scenario in self.splits["validation"]:
            self.assertNotIn(timings(scenario), used_timings)
            self.assertNotIn(scenario["viewport"][0], used_widths)
        self.assertEqual({1024, 1440}, {s["viewport"][0] for s in self.splits["validation"]})

    def test_interrupt_leaves_an_observable_response_window(self):
        for scenario in self.splits["validation"]:
            clicks = [e["time_s"] for e in scenario["events"] if e["action"] == "click"]
            if len(clicks) >= 2:
                self.assertGreaterEqual(clicks[1] - clicks[0], .2 - 1e-9)

    def test_only_the_panel_is_changed(self):
        with self.assertRaises(VisualError):
            suite_web_v4.scenarios("svg", "calibration")


if __name__ == "__main__":
    unittest.main()
