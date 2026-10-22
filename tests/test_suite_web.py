"""Prospective suite boundaries and rejection of masked window failures."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from kiroclops.local_engine import suite_web
from kiroclops.visual.events import extract_events
from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import capture_scenarios, QUALIFIED_CAPTURE_POLICY, validate_scenario


class RecipeTests(unittest.TestCase):
    def test_recipe_counts_distinct_splits_and_native_policies(self):
        identifiers = []
        for domain, count in (("panel", 8), ("svg", 4)):
            calibration = suite_web.scenarios(domain, "calibration")
            validation = suite_web.scenarios(domain, "validation")
            known = suite_web.scenarios(domain, "known_regression")
            self.assertEqual(count, len(calibration))
            self.assertEqual(12, len(validation))
            self.assertEqual(12, len(known))
            for case in calibration + validation + known:
                validate_scenario(case)
                self.assertEqual("natural_browser_raf_v1", case["frame_driver"])
                identifiers.append(case["case_id"])
            self.assertTrue(all(c["blind_validation"] for c in validation))
            self.assertTrue(all(not c["blind_validation"] for c in known))
            old = capture_scenarios(domain, "validation", QUALIFIED_CAPTURE_POLICY)
            for original, regression in zip(old, known):
                self.assertEqual(original["events"], regression["events"])
                self.assertEqual(original["viewport"], regression["viewport"])
                self.assertEqual(original["version"], regression["version"])
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_blind_dynamics_are_new_and_pointer_is_released(self):
        for domain in ("panel", "svg"):
            cases = suite_web.scenarios(domain, "validation")
            drag = next(c for c in cases if "drag" in c["recipe"])
            moves = [e for e in drag["events"] if e["action"] == "drag_move"]
            self.assertEqual(8, len(moves))
            self.assertAlmostEqual(.30, moves[-1]["time_s"] - next(e["time_s"] for e in drag["events"] if e["action"] == "drag_start"))
            self.assertEqual("drag_end", drag["events"][-1]["action"])
            self.assertAlmostEqual(1.35 if domain == "panel" else .95, drag["events"][-1]["time_s"])
        panel = suite_web.scenarios("panel", "validation")
        self.assertEqual([.4, .58, .81], [e["time_s"] for e in next(c for c in panel if c["recipe"] == "reverse-new")["events"]])
        svg = suite_web.scenarios("svg", "validation")
        self.assertEqual(.62, next(c for c in svg if c["recipe"] == "three-to-five-new")["events"][-1]["time_s"])
        self.assertEqual(.63, next(c for c in svg if c["recipe"] == "six-to-four-new")["events"][-1]["time_s"])

    def test_calls_are_independent_and_bad_split_fails(self):
        first = suite_web.scenarios("panel", "calibration")
        first[0]["events"][0]["time_s"] = 7
        self.assertEqual(.4, suite_web.scenarios("panel", "calibration")[0]["events"][0]["time_s"])
        with self.assertRaises(VisualError):
            suite_web.scenarios("panel", "hidden")


class ComparisonTests(unittest.TestCase):
    def record(self):
        return {"scenario": suite_web.scenarios("panel", "calibration")[1],
                "observations": {"inputs": {"provenance": "recorder_verified", "events": [
                    {"action": "click", "selector": "#open-panel", "time_s": .4}]}},
                "selection": {}, "measurement_status": "ok", "functional": {"status": "pass"}}

    def row(self, window, error, *, position=True):
        return {"reference_id": "star", "window_id": window, "segment_index": 0,
                "position_rmse": error, "size_rmse": 0., "endpoint_error": 0.,
                "gates": {"coverage": True, "position": position, "size": True, "endpoint": True}, "pass": position}

    def compare(self, old_rows, new_rows, *, order=None, dynamics=None, original=None, candidate=None, require=True, domain="panel"):
        ref = self.record()
        if domain == "svg":
            ref["scenario"] = suite_web.scenarios("svg", "calibration")[0]
        original = original or deepcopy(ref)
        candidate = candidate or deepcopy(ref)
        pixels = [{"object_windows": old_rows, "measurement_status": "pass", "success": True},
                  {"object_windows": new_rows, "measurement_status": "pass", "success": True}]
        order = order if order is not None else [{"pass": True, "reference": {"timing_status": "resolved"}, "candidate": {"timing_status": "resolved"}}]
        with patch("kiroclops.local_engine.evaluation._contract", return_value={
                "object_pairs": [{"reference_id": "star", "static": False}],
                "windows": [{"id": "initial-preservation", "start_s": -.25, "end_s": 0.}]}), \
             patch("kiroclops.visual.comparison.compare_observations", side_effect=pixels), \
             patch("kiroclops.local_engine.evaluation._dynamic_checks", return_value=dynamics or []), \
             patch("kiroclops.local_engine.evaluation._relative_timing_checks", return_value=order), \
             patch("kiroclops.local_engine.geometry_acceptance.svg_preservation", return_value={"measurement_status": "ok", "pass": True}):
            return suite_web.compare_records(ref, original, candidate, require)

    def test_bad_window_cannot_hide_inside_successful_mean(self):
        result = self.compare([self.row("opening", .2), self.row("closing", .04)],
                              [self.row("opening", .001), self.row("closing", .025)])
        self.assertLess(result["score"], .03)
        self.assertFalse(result["pass"])
        self.assertFalse(result["per_window_gates"][1]["gain_pass"])

    def test_absolute_failure_rejected_even_when_aggregate_says_success(self):
        result = self.compare([self.row("opening", .2), self.row("closing", .2)],
                              [self.row("opening", .001), self.row("closing", .04, position=False)])
        self.assertFalse(result["pass"])
        self.assertFalse(result["per_window_gates"][1]["absolute_pass"])

    def test_required_order_unobservable_is_distinct_from_behavior(self):
        order = [{"pass": False, "reference": {"timing_status": "unresolved"}, "candidate": {"timing_status": "resolved"}}]
        result = self.compare([self.row("one", .2)], [self.row("one", .01)], order=order)
        self.assertFalse(result["pass"])
        self.assertFalse(result["measurement_pass"])
        self.assertEqual("insufficient_observability", result["required_order_status"])
        self.assertEqual("required_order_unobservable", result["failure_class"])
        missing = [{"pass": False, "reference": None, "candidate": None}]
        self.assertEqual("required_order_unobservable", self.compare([self.row("one", .2)], [self.row("one", .01)], order=missing)["failure_class"])

    def test_interrupt_settling_is_censored_and_not_required(self):
        result = self.compare([self.row("one", .2)], [self.row("one", .01)],
                              dynamics=[{"observable": False, "pass": False, "reason": "reference_interrupted_or_unsettled"}])
        self.assertTrue(result["pass"])
        self.assertEqual(.20, result["contract"]["event_options"]["rest_duration_s"])
        self.assertEqual(result["suite_event_contract"], result["independent_review_contract"])

    def test_zero_original_error_never_manufactures_gain(self):
        self.assertFalse(self.compare([self.row("one", 0.)], [self.row("one", 0.)])["pass"])
        self.assertTrue(self.compare([self.row("one", 0.)], [self.row("one", 0.)], require=False)["pass"])

    def test_svg_pre_input_rest_is_preserved_and_response_gain_still_required(self):
        original = [self.row("initial-preservation", 0.), self.row("motion-0", .04)]
        bad = self.compare(original, [self.row("initial-preservation", 0.), self.row("motion-0", .025)], domain="svg")
        self.assertFalse(bad["pass"])
        self.assertEqual("not_applicable", bad["per_window_gates"][0]["gain_applicability"])
        self.assertEqual("initial_preservation", bad["per_window_gates"][0]["gain_reason"])
        self.assertFalse(bad["per_window_gates"][1]["gain_pass"])
        good = self.compare(original, [self.row("initial-preservation", 0.), self.row("motion-0", .01)], domain="svg")
        self.assertTrue(good["pass"])
        self.assertEqual("required", good["per_window_gates"][1]["gain_applicability"])
        damaged_rest = self.compare(original, [self.row("initial-preservation", .04, position=False), self.row("motion-0", .01)], domain="svg")
        self.assertFalse(damaged_rest["pass"])
        no_gain = self.compare([self.row("motion-0", 0.)], [self.row("motion-0", 0.)], domain="svg")
        self.assertFalse(no_gain["pass"])
        self.assertEqual("no_demonstrable_gain", no_gain["per_window_gates"][0]["gain_reason"])

    def test_wrong_recipe_or_unverified_input_is_rejected(self):
        candidate = self.record()
        candidate["scenario"]["events"][0]["time_s"] = .41
        with self.assertRaises(VisualError):
            self.compare([], [], candidate=candidate)
        candidate = self.record()
        candidate["observations"]["inputs"]["provenance"] = "user_declared"
        with self.assertRaises(VisualError):
            self.compare([], [], candidate=candidate)

    def test_changed_policy_rejected(self):
        candidate = self.record()
        candidate["suite_event_contract"] = suite_web.event_contract("panel")
        candidate["suite_event_contract"]["event_options"]["rest_duration_s"] = .25
        with self.assertRaises(VisualError):
            self.compare([], [], candidate=candidate)


class CapturePolicyTests(unittest.TestCase):
    def test_suite_analysis_and_review_use_same_contract_once(self):
        scenario = suite_web.scenarios("panel", "calibration")[0]
        raw = {"scenario": scenario, "package": "unused", "source_sha256": "source", "functional": {"status": "pass"}}
        manifest = {"inputs": {"events": [{"time_s": .4}]}}
        selection = {"objects": [{"id": "handle"}]}
        with tempfile.TemporaryDirectory() as temp, \
             patch("kiroclops.local_engine.evaluation._capture_web", return_value=raw), \
             patch("kiroclops.visual.package.load_package", return_value=manifest), \
             patch("kiroclops.web_local.selection.select_package_from_pixels", return_value=selection), \
             patch("kiroclops.local_engine.evaluation._lifecycle_bindings", return_value={}), \
             patch("kiroclops.local_engine.evaluation._analysis", return_value={"measurement_status": "ok", "observations": {}, "events": {}}) as analyze:
            result = suite_web.capture_record("project", Path(temp), scenario, lambda: None, lambda *a: None, None, "candidate")
            self.assertEqual(1, analyze.call_count)
            self.assertEqual(.20, analyze.call_args.kwargs["contract"]["event_options"]["rest_duration_s"])
            self.assertEqual(result["suite_event_contract"], result["independent_review_contract"])

    def test_functional_oracle_remains_separate_and_detects_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "index.html").write_text("<html></html>", encoding="utf-8")
            source = Path(temp) / "app.js"
            source.write_text("old", encoding="utf-8")
            async def mutate(*args):
                source.write_text("changed", encoding="utf-8")
                return {"status": "pass", "checks": [], "page_errors": []}
            with patch.object(suite_web, "_functional_browser_checks", side_effect=mutate):
                with self.assertRaises(VisualError) as caught:
                    suite_web.functional_checks(temp)
                self.assertEqual("source_changed", caught.exception.code)
            with patch.object(suite_web, "_functional_browser_checks", new=AsyncMock(return_value={"status": "pass", "checks": [{"pass": True}], "page_errors": []})):
                result = suite_web.functional_checks(temp)
                self.assertEqual("separate_private_browser_oracle", result["checks_source"])
                self.assertNotIn("observations", result)


class RealPixelPolicyTests(unittest.TestCase):
    def test_rest_duration_changes_actual_event_observability(self):
        times = [0., .05, .1, .15, .2, .21, .23, .25, .28]
        xs = [0.] * 6 + [3., 6., 9.]
        obj = {"id": "handle", "frames": [
            {"index": i, "time_s": t, "valid": True, "x": x, "y": 10., "width": 4., "height": 4.}
            for i, (t, x) in enumerate(zip(times, xs))]}
        policy = suite_web.event_contract("panel")
        events = extract_events({"objects": [obj]}, contract=policy)
        start = next(e for e in events["events"] if e["type"] == "motion_start")
        self.assertEqual(suite_web.PANEL_EVENT_POLICY, events["motion_start_policy"]["version"])
        self.assertEqual(.2, events["options"]["rest_duration_s"])
        self.assertEqual("observed_rest", start["start_context"])
        longer = deepcopy(policy)
        longer["event_options"]["rest_duration_s"] = .25
        censored = extract_events({"objects": [obj]}, contract=longer)
        self.assertEqual("clip_or_gap_censored", next(e for e in censored["events"] if e["type"] == "motion_start")["start_context"])

    def test_real_relative_order_and_per_window_pixel_comparison(self):
        def record(scale):
            objects = []
            for oid, offset, delay, travel in (("handle", 0., 0., 0.), ("bookmark", 30., .08, 24.), ("star", 60., .18, 40.)):
                frames = []
                for index in range(181):
                    t = index / 60
                    x = 100. * scale * max(0., min(1., (t - .4) / .6)) + offset
                    y = 30. + travel * scale * max(0., min(1., (t - .4 - delay) / .3))
                    frames.append({"index": index, "time_s": t, "x": x, "y": y, "left": x - 5., "top": y - 5.,
                                   "width": 10., "height": 10., "valid": True, "present": True, "expected_active": True})
                objects.append({"id": oid, "frames": frames, "active_intervals_s": [[0., 3.]], "valid_ratio": 1.})
            observations = {"schema_version": 1, "kind": "ObservationSet", "status": "ok", "measurement_status": "pass",
                            "package_sha256": "synthetic", "image_size": [960, 640], "objects": objects, "connections": [],
                            "inputs": {"provenance": "recorder_verified", "events": [{"action": "click", "selector": "#open-panel", "time_s": .4}]}}
            return {"scenario": suite_web.scenarios("panel", "calibration")[0], "observations": observations,
                    "measurement_status": "ok", "functional": {"status": "pass"}, "suite_event_contract": suite_web.event_contract("panel"),
                    "selection": {"objects": [{"id": obj["id"]} for obj in objects], "relative_motion": [
                        {"object_id": "bookmark", "relative_to": "handle"}, {"object_id": "star", "relative_to": "handle"}]}}
        result = suite_web.compare_records(record(1.), record(.75), record(1.))
        self.assertTrue(result["pass"], result)
        self.assertEqual(3, len(result["per_window_gates"]))
        self.assertTrue(all(r["improvement_fraction"] == 1. for r in result["per_window_gates"]))
        self.assertEqual("pass", result["required_order_status"])
        self.assertEqual("before", result["relative_timing"][0]["reference"]["order"])


if __name__ == "__main__":
    unittest.main()
