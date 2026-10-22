"""The V3b reader's integrated sub-frame window rule and relative event key.

The same fixtures and expectations as the V2 diagnostics' corrected readers
(tests/test_phase3d_v2_panel_subframe_window_rule.py and
tests/test_phase3d_v2_panel_event_identity_diagnostic.py), applied to the live
reader itself.
"""
import unittest

from kiroclops.local_engine import realistic_review as review
from kiroclops.visual.schema import VisualError
from scripts.phase3d_v2_panel_event_identity_diagnostic import EVENT_FAILURE_CODES
from scripts.phase3d_v2_panel_subframe_window_rule import UNSAMPLED_REASON, WINDOW_CODE
from tests.test_phase3d_v2_panel_event_identity_diagnostic import bundle as event_bundle, selection
from tests.test_phase3d_v2_panel_subframe_window_rule import FRAME_S, MAPPING, bundle, produced, window


def reclassified(checks):
    return {oid for oid in MAPPING if window(checks, oid)["reason"] == review.SUBFRAME_REASON}


class IntegratedSubframeRuleTests(unittest.TestCase):
    def read(self, value):
        return review._position_checks([value])

    def test_window_between_two_images_is_not_observable_and_the_next_window_is_checked(self):
        checks, failures = self.read(bundle())
        self.assertEqual([], failures)
        for oid in MAPPING:
            row = window(checks, oid)
            self.assertEqual(("not_observable", review.SUBFRAME_REASON, None), (row["status"], row["reason"], row["pass"]))
            evidence = row["subframe_evidence"]
            self.assertEqual(review.SUBFRAME_RULE, evidence["rule"])
            self.assertEqual([30, 31], [image["index"] for image in evidence["bracketing_images"]])
            self.assertAlmostEqual(.006, evidence["window_duration_s"])
            self.assertAlmostEqual(FRAME_S, evidence["bracketing_interval_s"])
            self.assertTrue(evidence["response_checked_in_following_input_window"])
            self.assertTrue(window(checks, oid, "input-2")["pass"])

    def test_an_empty_window_as_long_as_an_image_interval_still_fails(self):
        checks, failures = self.read(bundle([index * FRAME_S for index in range(60) if not 34 <= index <= 37]))
        self.assertEqual({("input-3", UNSAMPLED_REASON)}, {(row["window_id"], row["reason"]) for row in failures})
        self.assertFalse(window(checks, "star", "input-3")["pass"])

    def test_a_short_window_inside_a_capture_stall_still_fails(self):
        checks, failures = self.read(bundle([index * FRAME_S for index in range(60) if not 25 <= index <= 35]))
        self.assertEqual(set(), reclassified(checks))
        self.assertEqual(3, sum(row["window_id"] == "input-1" for row in failures))

    def test_every_bracketing_image_condition_is_required_per_object(self):
        mutations = {
            "star": lambda value: produced(value, "star")[31].update(valid=False),
            "bookmark": lambda value: value["raw"]["rows"][30]["points"].update(b={}),
            "handle": lambda value: produced(value, "handle")[31].update(x=produced(value, "handle")[31]["x"] + 5.),
        }
        for oid, mutate in mutations.items():
            with self.subTest(object_id=oid):
                value = bundle()
                mutate(value)
                checks, failures = self.read(value)
                self.assertEqual(UNSAMPLED_REASON, window(checks, oid)["reason"])
                self.assertIn(oid, {row["object_id"] for row in failures if row["window_id"] == "input-1"})
                self.assertEqual(set(MAPPING) - {oid}, reclassified(checks))

    def test_an_object_inactive_in_a_bracketing_image_is_not_reclassified(self):
        value = bundle()
        value["active"]["star"] = [[.505, value["manifest"]["frames"][-1]["time_s"]]]
        checks, _ = self.read(value)
        self.assertEqual(UNSAMPLED_REASON, window(checks, "star")["reason"])
        self.assertEqual({"handle", "bookmark"}, reclassified(checks))

    def test_timing_contract_and_clock_uncertainty_guards(self):
        def missing_uncertainty(value):
            del value["manifest"]["inputs"]["events"][1]["clock_uncertainty_s"]

        def wide_uncertainty(value):
            value["manifest"]["inputs"]["events"][1]["clock_uncertainty_s"] = .003

        def wide_image_time(value):
            value["manifest"]["frames"][31]["time_interval_s"] = [.507, value["manifest"]["frames"][31]["time_s"]]

        def other_limits(value):
            value["manifest"]["metadata"]["timing"]["limits"]["maximum_motion_gap_s"] = .2

        cases = {"no_limits": lambda value: value["manifest"].update(metadata={}),
                 "other_limits": other_limits, "missing_uncertainty": missing_uncertainty,
                 "wide_uncertainty": wide_uncertainty, "wide_image_time": wide_image_time}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                value = bundle()
                mutate(value)
                checks, failures = self.read(value)
                self.assertEqual(set(), reclassified(checks))
                self.assertEqual(3, len(failures))

    def test_a_real_position_difference_survives_the_rule(self):
        value = bundle()
        for index in range(34, 38):
            produced(value, "bookmark")[index]["x"] += 3.
        checks, failures = self.read(value)
        self.assertEqual(set(MAPPING), reclassified(checks))
        failed = window(failures, "bookmark", "input-3")
        self.assertEqual(WINDOW_CODE, failed["code"])
        self.assertNotIn("reason", failed)


class IntegratedEventKeyTests(unittest.TestCase):
    def key(self, ids, parent="handle", public=None):
        return review.canonical_event_key({"type": "settled", "object_ids": ids, "relative_to_object_id": parent},
                                          public or selection())

    def test_one_and_two_id_relative_representations_are_identical(self):
        self.assertEqual(self.key(["bookmark"]), self.key(["bookmark", "handle"]))
        self.assertEqual(("settled", ("bookmark", "handle")), self.key(["bookmark"]))

    def test_absolute_single_identity_is_not_given_a_parent(self):
        self.assertEqual(("settled", ("handle",)), self.key(["handle"], None))

    def test_self_foreign_duplicate_and_wrong_second_identity_are_rejected(self):
        for ids, parent in ((["handle"], "handle"), (["bookmark"], "foreign"), (["bookmark", "bookmark"], "handle"),
                            (["bookmark", "star"], "handle"), (["bookmark", "handle", "handle"], "handle"),
                            (["foreign"], "handle"), (["bookmark", "handle"], None)):
            with self.subTest(ids=ids, parent=parent), self.assertRaises(VisualError):
                self.key(ids, parent)

    def test_undeclared_or_ambiguous_parent_contract_is_rejected(self):
        for relations in ([], [{"object_id": "bookmark", "relative_to": "star"}], selection()["relative_motion"] * 2):
            with self.subTest(relations=relations), self.assertRaises(VisualError):
                self.key(["bookmark"], public={**selection(), "relative_motion": relations})

    def test_malformed_identity_is_not_silently_deduplicated(self):
        for ids in (None, [], "bookmark", [1], [""]):
            with self.subTest(ids=ids), self.assertRaises(VisualError):
                self.key(ids)


class IntegratedEventPipelineTests(unittest.TestCase):
    def test_relative_events_in_either_identity_form_are_consumed_once(self):
        result = review._final_events([event_bundle()], {"event_options": {"rest_duration_s": .2}})
        checks, failures = result[0], result[3]
        self.assertEqual([], failures)
        self.assertTrue(checks and all(row["pass"] for row in checks))
        self.assertTrue(any(row.get("relation_type") == "relative_event_timing" for row in checks))

    def test_a_real_raw_produced_motion_difference_is_still_rejected(self):
        value = event_bundle()
        for frame in value["observations"]["objects"][1]["frames"]:
            frame["x"] += max(0, min(frame["index"] - 27, 6)) * 3.
        failures = review._final_events([value], {})[3]
        self.assertTrue(failures)
        self.assertTrue(any(row["code"] in EVENT_FAILURE_CODES for row in failures))

    def test_original_role_remains_diagnostic_and_unread_by_motion_gate(self):
        value = event_bundle("original")
        value["observations"] = None
        result = review._final_events([value], {})
        self.assertEqual(([], []), (result[0], result[3]))
        self.assertEqual("pixel_geometry_only", result[2][0]["scope"])


if __name__ == "__main__":
    unittest.main()
