"""Actual visual math plus capture boundary tests for the prospective policy."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_policy, suite_web, suite_web_v2
from kiroclops.visual.schema import VisualError, digest, read_json


def policy(domain, object_ids):
    points = [{"object_id": oid, "frame_sha256": digest([oid, index]), "error_px": 1.,
               "role": "reference" if index % 2 == 0 else "original", "split": "calibration", "independent": True}
              for oid in object_ids for index in range(20)]
    return realistic_policy.build_policy(domain, {"status": "pass", "basis": "independent_raw_pixels", "points": points})


def actual_pointer_events(events):
    actual = deepcopy(events)
    point = [100., 120.]
    for index, event in enumerate(actual):
        if event["action"] != "drag_end":
            point = [100. + index * 10., 120.]
        event.update(client_x=point[0], client_y=point[1])
    return actual


def panel_record(scale=1., *, scenario=None, width=10.):
    scenario = scenario or suite_web_v2.scenarios("panel", "calibration")[0]
    objects = []
    for oid, offset, delay, travel in (("handle", 0., 0., 0.), ("bookmark", 30., .08, 24.), ("star", 60., .18, 40.)):
        frames = []
        for index in range(181):
            t = index / 60
            x = 100. * scale * max(0., min(1., (t - .4) / .6)) + offset
            y = 30. + travel * scale * max(0., min(1., (t - .4 - delay) / .3))
            frames.append({"index": index, "time_s": t, "x": x, "y": y, "left": x - width / 2, "top": y - 5.,
                           "width": width, "height": 10., "valid": True, "present": True, "expected_active": True})
        objects.append({"id": oid, "frames": frames, "active_intervals_s": [[0., 3.]], "valid_ratio": 1.})
    observations = {"schema_version": 1, "kind": "ObservationSet", "status": "ok", "measurement_status": "pass",
                    "package_sha256": "synthetic", "image_size": [960, 640], "objects": objects, "connections": [],
                    "inputs": {"provenance": "recorder_verified", "events": actual_pointer_events(scenario["events"])}}
    return {"scenario": deepcopy(scenario), "observations": observations, "measurement_status": "ok",
            "functional": {"status": "pass"}, "suite_event_contract": suite_web_v2.event_contract("panel"),
            "events": {"events": [], "relations": []},
            "selection": {"objects": [{"id": obj["id"]} for obj in objects], "relative_motion": [
                {"object_id": "bookmark", "relative_to": "handle"}, {"object_id": "star", "relative_to": "handle"}]}}


class RecipeTests(unittest.TestCase):
    def test_counts_and_approved_novel_timelines(self):
        for domain, count in (("panel", 8), ("svg", 4)):
            self.assertEqual(count, len(suite_web_v2.scenarios(domain, "calibration")))
            self.assertEqual(12, len(suite_web_v2.scenarios(domain, "validation")))
            self.assertEqual(12, len(suite_web_v2.scenarios(domain, "known")))
        panel = suite_web_v2.scenarios("panel", "validation")
        drag = next(s for s in panel if s["recipe"] == "drag-release-new")
        self.assertAlmostEqual(1.10, drag["events"][1]["time_s"])
        self.assertAlmostEqual(.28, drag["events"][-2]["time_s"] - drag["events"][1]["time_s"])
        self.assertAlmostEqual(1.43, drag["events"][-1]["time_s"])
        self.assertAlmostEqual(.35, drag["events"][1]["destination"]["travel_fraction"])
        svg = suite_web_v2.scenarios("svg", "validation")
        self.assertAlmostEqual(.69, next(s for s in svg if s["recipe"] == "three-to-five-new")["events"][-1]["time_s"])
        self.assertAlmostEqual(.71, next(s for s in svg if s["recipe"] == "six-to-four-new")["events"][-1]["time_s"])
        drag = next(s for s in svg if s["recipe"] == "five-to-six-drag-new")
        self.assertAlmostEqual(.68, drag["events"][1]["time_s"])
        self.assertAlmostEqual(.32, drag["events"][-2]["time_s"] - drag["events"][1]["time_s"])
        self.assertAlmostEqual(1.05, drag["events"][-1]["time_s"])

    def test_legacy_recipes_not_mutated(self):
        legacy = suite_web.scenarios("panel", "validation")
        prior = digest(legacy)
        suite_web_v2.scenarios("panel", "validation")[0]["events"][0]["time_s"] = 9
        self.assertEqual(prior, digest(suite_web.scenarios("panel", "validation")))
        self.assertNotEqual(legacy[0]["id"], suite_web_v2.scenarios("panel", "validation")[0]["id"])

    def test_legacy_scenario_has_no_replay_point_and_optional_point_is_validated(self):
        from kiroclops.web_local.specs import validate_scenario
        legacy = suite_web.scenarios("panel", "calibration")[0]
        before = deepcopy(legacy)
        validate_scenario(legacy)
        self.assertEqual(before, legacy)
        self.assertTrue(all("replay_client_point" not in e for e in legacy["events"]))
        for point in ([100.25, 120.5], [0, 0], [legacy["viewport"][0] - 1, legacy["viewport"][1] - 1]):
            candidate = deepcopy(legacy)
            candidate["events"][0]["replay_client_point"] = point
            validate_scenario(candidate)
        for point in ([True, 100], [float("nan"), 100], [100], "100,120", [legacy["viewport"][0], 100]):
            candidate = deepcopy(legacy)
            candidate["events"][0]["replay_client_point"] = point
            with self.assertRaises(ValueError):
                validate_scenario(candidate)


class NativePointerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_drag_uses_exact_client_points_and_releases_at_last_move(self):
        from kiroclops.web_local.capture import TrustedPointer
        class FakeCdp:
            def __init__(self):
                self.commands = []
            async def send(self, method, params):
                self.commands.append((method, deepcopy(params)))
                return {}
        cdp = FakeCdp()
        pointer = TrustedPointer(cdp)
        await pointer.down([321.25, 212.5])
        await pointer.move(365.75, 219.125)
        await pointer.move(410.5, 222.875)
        await pointer.up()
        self.assertEqual([(321.25, 212.5), (365.75, 219.125), (410.5, 222.875), (410.5, 222.875)],
                         [(params["x"], params["y"]) for _, params in cdp.commands])
        self.assertTrue(all(method == "Input.dispatchMouseEvent" for method, _ in cdp.commands))
        self.assertEqual(["mousePressed", "mouseMoved", "mouseMoved", "mouseReleased"],
                         [params["type"] for _, params in cdp.commands])
        self.assertEqual([1, 1, 1, 0], [params["buttons"] for _, params in cdp.commands])
        self.assertFalse(pointer.pressed)

    async def test_prepared_click_preserves_point_and_rejects_retargeting(self):
        from kiroclops.web_local.capture import CaptureError, TrustedPointer
        class FakeCdp:
            def __init__(self):
                self.commands = []
            async def send(self, method, params):
                self.commands.append(deepcopy(params))
                return {}
        cdp = FakeCdp()
        pointer = TrustedPointer(cdp)
        await pointer.move(43.25, 52.75)
        await pointer.finish_prepared_click([43.25, 52.75])
        self.assertEqual([(43.25, 52.75)] * 3, [(row["x"], row["y"]) for row in cdp.commands])
        with self.assertRaises(CaptureError) as caught:
            await pointer.finish_prepared_click([45.25, 52.75])
        self.assertEqual("invalid_input", caught.exception.code)


class ActualMathTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy("panel", ("handle", "bookmark", "star"))

    def compare(self, original=None, candidate=None, reference=None):
        return suite_web_v2.compare_records(reference or panel_record(), original or panel_record(.75),
                                           candidate or panel_record(), acceptance_policy=self.policy)

    def test_real_observation_curve_events_and_preservation_pass(self):
        result = self.compare()
        self.assertTrue(result["pass"], result)
        self.assertTrue(result["measurement_pass"])
        self.assertEqual("pass", result["required_order_status"])
        self.assertTrue(all(row["classification"] == "strong_improvement" for row in result["per_window_gates"]))
        self.assertTrue(all(row["pass"] for row in result["dynamics"] if row["observable"]))
        self.assertTrue(all(row["lag_tolerance_s"] == .15 for row in result["relative_timing"]))

    def test_zero_to_zero_passes_as_preserved_without_claimed_gain(self):
        result = self.compare(original=panel_record())
        self.assertTrue(result["pass"], result)
        self.assertTrue(all(row["classification"] == "preserved" for row in result["per_window_gates"]))
        self.assertTrue(all(row["improvement_fraction"] is None for row in result["per_window_gates"]))

    def test_already_good_small_degradation_is_bounded_by_absolute_quality(self):
        result = self.compare(original=panel_record(), candidate=panel_record(.99))
        self.assertTrue(result["pass"], result)
        self.assertTrue(all(row["classification"] == "preserved" for row in result["per_window_gates"]))

    def test_high_gain_cannot_mask_wrong_final_curve_or_endpoint(self):
        result = self.compare(original=panel_record(.1), candidate=panel_record(.8))
        self.assertFalse(result["pass"])
        self.assertTrue(any(row["improvement_fraction"] > .5 for row in result["per_window_gates"]))
        self.assertEqual("per_window_pixel_failure", result["failure_class"])

    def test_small_motion_has_two_pixel_curve_and_endpoint_floor(self):
        result = self.compare()
        bookmark = next(p for p in result["contract"]["object_pairs"] if p["reference_id"] == "bookmark")
        self.assertEqual(24., bookmark["reference_distance_px"])
        self.assertEqual(2., bookmark["curve_limit_px"])
        self.assertEqual(2., bookmark["endpoint_limit_px"])
        self.assertEqual(2., bookmark["size_limit_px"])

    def test_changed_size_and_broken_function_are_not_accepted(self):
        wrong_size = self.compare(candidate=panel_record(width=15.))
        self.assertFalse(wrong_size["pass"])
        broken = panel_record()
        broken["functional"]["status"] = "fail"
        self.assertEqual("application_integrity_failure", self.compare(candidate=broken)["failure_class"])

    def test_missing_floor_or_tampered_record_policy_is_rejected(self):
        with self.assertRaises(VisualError):
            suite_web_v2.compare_records(panel_record(), panel_record(.75), panel_record(), acceptance_policy=policy("panel", ("handle",)))
        candidate = panel_record()
        candidate["acceptance_policy_sha256"] = digest("other policy")
        with self.assertRaises(VisualError):
            self.compare(candidate=candidate)

    def test_input_history_mismatch_has_no_behavior_verdict(self):
        scenario = suite_web_v2.scenarios("panel", "calibration")[2]
        # Choose a two-command recipe independently of dictionary ordering.
        scenario = next(s for s in suite_web_v2.scenarios("panel", "calibration") if s["recipe"] == "open-close")
        reference, original, candidate = (panel_record(s, scenario=scenario) for s in (1., .75, 1.))
        candidate["observations"]["inputs"]["events"][-1]["time_s"] += .0504
        result = self.compare(original, candidate, reference)
        self.assertFalse(result["pass"])
        self.assertFalse(result["measurement_pass"])
        self.assertEqual("input_history_mismatch", result["failure_class"])

    def test_changed_actual_pointer_point_is_input_failure_even_with_exact_curves(self):
        reference, original, candidate = panel_record(), panel_record(.75), panel_record()
        candidate["observations"]["inputs"]["events"][0]["client_y"] += 4.
        result = self.compare(original, candidate, reference)
        self.assertFalse(result["pass"])
        self.assertFalse(result["measurement_pass"])
        self.assertEqual("input_history_mismatch", result["failure_class"])

    def test_original_stored_eventset_never_consumed_by_comparator(self):
        class OriginalRecord(dict):
            def __getitem__(self, key):
                if key == "events":
                    raise AssertionError("Original EventSet is diagnostic, not decision input")
                return super().__getitem__(key)
            def get(self, key, default=None):
                if key == "events":
                    raise AssertionError("Original EventSet is diagnostic, not decision input")
                return super().get(key, default)
        original = OriginalRecord(panel_record(.75))
        before = digest(self.compare(original=original))
        original["events"] = {"events": [{"type": "unresolved", "time_s": 999}], "unobservable": ["anything"]}
        self.assertEqual(before, digest(self.compare(original=original)))

    def test_wrong_animation_speed_cannot_be_timewarped(self):
        candidate = panel_record()
        for obj in candidate["observations"]["objects"]:
            for frame in obj["frames"]:
                # A 2x slow main panel is a real behavior difference.
                if obj["id"] == "handle":
                    frame["x"] = 100. * max(0., min(1., (frame["time_s"] - .4) / 1.2))
                    frame["left"] = frame["x"] - frame["width"] / 2
        result = self.compare(candidate=candidate)
        self.assertFalse(result["pass"])
        self.assertTrue(all(row["input_timing"]["duration_rescaled"] is False for row in result["pixel_comparison"]["object_windows"]))

    def test_relative_order_is_preserved_even_with_larger_lag_tolerance(self):
        def shifted_record(bookmark_delay=.08, star_delay=.18):
            record = panel_record()
            for obj in record["observations"]["objects"]:
                if obj["id"] == "handle":
                    continue
                delay = bookmark_delay if obj["id"] == "bookmark" else star_delay
                travel = 24. if obj["id"] == "bookmark" else 40.
                for frame in obj["frames"]:
                    frame["y"] = 30. + travel * max(0., min(1., (frame["time_s"] - .4 - delay) / .3))
                    frame["top"] = frame["y"] - 5.
            return record
        ref = panel_record()
        bounded = shifted_record(star_delay=.31)
        contract = suite_web_v2.comparison_contract(ref, bounded, self.policy)
        rows = suite_web_v2.relative_timing_checks(ref, bounded, contract)
        self.assertTrue(all(row["pass"] for row in rows), rows)
        reversed_order = shifted_record(bookmark_delay=.36)
        contract = suite_web_v2.comparison_contract(ref, reversed_order, self.policy)
        rows = suite_web_v2.relative_timing_checks(ref, reversed_order, contract)
        self.assertTrue(any(not row["gates"]["observed_order"] for row in rows), rows)

    def test_original_replay_times_must_match_reference_not_only_declared_sha(self):
        ref, original = panel_record(), panel_record(.75)
        execution, receipt = realistic_policy.replay_scenario(ref["scenario"], ref["observations"])
        original.update(execution_scenario=execution, input_replay=receipt)
        original["execution_scenario"]["events"][0]["time_s"] += .04
        original["input_replay"]["execution_scenario_sha256"] = digest(original["execution_scenario"])
        with self.assertRaises(VisualError) as caught:
            self.compare(original=original, reference=ref)
        self.assertEqual("suite_recipe_mismatch", caught.exception.code)

    def test_rehashed_pointer_replay_mutation_cannot_change_reference_path(self):
        ref, original = panel_record(), panel_record(.75)
        execution, receipt = realistic_policy.replay_scenario(ref["scenario"], ref["observations"])
        original.update(execution_scenario=execution, input_replay=receipt)
        original["execution_scenario"]["events"][0]["replay_client_point"][0] += 5.
        original["input_replay"]["execution_scenario_sha256"] = digest(original["execution_scenario"])
        with self.assertRaises(VisualError) as caught:
            self.compare(original=original, reference=ref)
        self.assertEqual("suite_recipe_mismatch", caught.exception.code)


def svg_record(scale=1.):
    scenario = suite_web_v2.scenarios("svg", "calibration")[0]
    objects = []
    for oid, base in (("a", 100.), ("b", 250.)):
        frames = []
        for index in range(181):
            t = index / 60
            x = base + 100. * scale * max(0., min(1., (t - .4) / .6))
            frames.append({"index": index, "time_s": t, "x": x, "y": 100.,
                           "left": x - 15., "top": 90., "width": 30., "height": 20.,
                           "valid": True, "present": True, "expected_active": True, "geometry_method": "contour"})
        objects.append({"id": oid, "method": "contour", "frames": frames,
                        "active_intervals_s": [[0., 3.]], "valid_ratio": 1.})
    spec = {"id": "edge-ab", "from_object": "a", "to_object": "b", "method": "line"}
    connection = {**spec, "frames": [{"index": a["index"], "time_s": a["time_s"], "valid": True,
                    "start": [a["x"] + 15., 100.], "end": [b["x"] - 15., 100.]}
                    for a, b in zip(objects[0]["frames"], objects[1]["frames"])]}
    return {"scenario": deepcopy(scenario), "measurement_status": "ok", "functional": {"status": "pass"},
            "suite_event_contract": suite_web_v2.event_contract("svg"),
            "selection": {"objects": [{"id": oid} for oid in ("a", "b")], "connections": [spec]},
            "observations": {"schema_version": 1, "kind": "ObservationSet", "status": "ok", "measurement_status": "pass",
                "package_sha256": "synthetic-svg", "image_size": [960, 640], "objects": objects, "connections": [connection],
                "inputs": {"provenance": "recorder_verified", "events": actual_pointer_events(scenario["events"])}},
            "events": {"events": [], "relations": []}}


class SvgActualMathTests(unittest.TestCase):
    def test_real_visible_connections_and_static_window_preservation(self):
        ref = svg_record()
        original, candidate = svg_record(.75), svg_record()
        result = suite_web_v2.compare_records(ref, original, candidate, acceptance_policy=policy("svg", ("a", "b")))
        self.assertTrue(result["pass"], result)
        self.assertTrue(result["svg_geometry_preservation"]["pass"])
        self.assertTrue(result["contract"]["require_connections"])
        self.assertFalse(result["original_contract"]["require_connections"])

    def test_detached_visible_connection_is_preservation_failure(self):
        candidate = svg_record()
        candidate["observations"]["connections"][0]["frames"][30]["start"][0] += 12.
        result = suite_web_v2.compare_records(svg_record(), svg_record(.75), candidate,
                                             acceptance_policy=policy("svg", ("a", "b")))
        self.assertFalse(result["pass"])
        self.assertTrue(result["measurement_pass"])
        self.assertEqual("preservation_violation", result["failure_class"])

    def test_original_derived_events_do_not_supply_svg_baseline_quality(self):
        from kiroclops.visual import comparison
        original = svg_record(.75)
        actual = comparison.extract_events
        def tracked(observations, *args, **kwargs):
            self.assertIsNot(original["observations"], observations)
            return actual(observations, *args, **kwargs)
        with patch.object(comparison, "extract_events", side_effect=tracked) as calls:
            result = suite_web_v2.compare_records(svg_record(), original, svg_record(),
                                                 acceptance_policy=policy("svg", ("a", "b")))
        self.assertTrue(result["pass"], result)
        self.assertEqual(2, calls.call_count)


class CaptureBoundaryTests(unittest.TestCase):
    def test_actual_replay_then_one_retry_preserves_first_receipt(self):
        scenario = next(s for s in suite_web_v2.scenarios("panel", "calibration") if s["recipe"] == "interrupt")
        reference = panel_record(scenario=scenario)
        reference["observations"]["inputs"]["events"][-1]["time_s"] += .019
        bad, good = deepcopy(reference), deepcopy(reference)
        bad["observations"]["inputs"]["events"][-1]["time_s"] += .05
        def capture(project, destination, execution, *args, **kwargs):
            self.assertAlmostEqual(.269, execution["events"][-1]["time_s"] - execution["events"][0]["time_s"])
            self.assertEqual([[event["client_x"], event["client_y"]] for event in reference["observations"]["inputs"]["events"]],
                             [event["replay_client_point"] for event in execution["events"]])
            record = deepcopy(bad if destination.name == "attempt-01" else good)
            record["scenario"] = deepcopy(execution)
            return record
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite_web, "capture_record", side_effect=capture) as mock_capture:
            output = Path(tmp) / "new-capture"
            result = suite_web_v2.capture_record("project", output, scenario, lambda: None, lambda *a: None,
                                                None, "candidate", reference_record=reference)
            self.assertEqual(2, mock_capture.call_count)
            self.assertEqual(scenario, result["scenario"])
            self.assertEqual("input_history_mismatch", read_json(output / "attempt-01/capture-attempt.json")["status"])
            self.assertEqual(2, read_json(output / "capture-replay.json")["selected_attempt"])
            self.assertEqual(2, len(result["capture_attempts"]))

    def test_two_mismatches_do_not_retry_until_pass(self):
        scenario = next(s for s in suite_web_v2.scenarios("panel", "calibration") if s["recipe"] == "interrupt")
        reference = panel_record(scenario=scenario)
        bad = deepcopy(reference)
        bad["observations"]["inputs"]["events"][-1]["time_s"] += .05
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite_web, "capture_record", return_value=bad) as capture:
            output = Path(tmp) / "new-capture"
            with self.assertRaises(VisualError) as caught:
                suite_web_v2.capture_record("project", output, scenario, lambda: None, lambda *a: None,
                                           None, "candidate", reference_record=reference)
            self.assertEqual("input_history_mismatch", caught.exception.code)
            self.assertEqual(2, capture.call_count)
            self.assertTrue((output / "attempt-01/capture-attempt.json").exists())
            self.assertTrue((output / "attempt-02/capture-attempt.json").exists())
            self.assertIsNone(read_json(output / "capture-replay.json")["selected_attempt"])


if __name__ == "__main__":
    unittest.main()
