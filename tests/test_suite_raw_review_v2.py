"""Small analytic motion/color fixtures; no native capture or model is started."""
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_raw_review_v2 as v2
from kiroclops.local_engine.suite_raw_review_v2 import candidates_v3, subpixel_anchors
from kiroclops.visual.raw_event_review import candidates as legacy_candidates
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json
from tests import test_suite_raw_review as fixtures


def rows(xs, dt=.05):
    return [{"index": i, "time_s": i * dt, "points": {"a": {"x": x, "y": 0}}} for i, x in enumerate(xs)]


def alpha_rectangle(dx, dy, *, background=(18, 25, 32)):
    """Exact per-pixel area integration of a translated colored rectangle."""
    yy, xx = np.indices((64, 80), dtype=float)
    left, top, right, bottom = 22 + dx, 20 + dy, 42 + dx, 44 + dy
    alpha = np.maximum(0, np.minimum(xx + 1, right) - np.maximum(xx, left)) * np.maximum(0, np.minimum(yy + 1, bottom) - np.maximum(yy, top))
    foreground = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2BGR)[0, 0].astype(float)
    bg = np.asarray(background, dtype=float)
    return np.rint(bg + alpha[..., None] * (foreground - bg)).astype(np.uint8)


class SuiteRawReviewV2Tests(unittest.TestCase):
    def test_fractional_raw_mass_tracks_analytic_alpha_translation_without_rounding(self):
        origin = None
        first = None
        errors = []
        legacy_errors = []
        shifts = [(0, 0), (.13, .27), (.49, .51), (.93, .07), (1.11, -.41), (-.37, .83), (2.04, 1.52)]
        for dx, dy in shifts:
            points, origin = subpixel_anchors(alpha_rectangle(dx, dy), origin)
            point = points["bookmark"]
            if first is None:
                first = point
            observed = [point["x"] - first["x"], point["y"] - first["y"]]
            errors.append(math.dist(observed, [dx, dy]))
            legacy_errors.append(math.dist([point["legacy_HSV_center"][i] - first["legacy_HSV_center"][i] for i in (0, 1)], [dx, dy]))
            self.assertEqual(point["quantization"], "none")
        self.assertLess(max(errors), .01)
        self.assertGreater(max(legacy_errors), .25)

    def test_fractional_interruption_is_measured_per_image_without_temporal_smoothing(self):
        origin = None
        points = []
        shifts = [0., .31, .69, 1.21, 1.71, 1.13, .52, -.12]
        for x in shifts:
            proposal, origin = subpixel_anchors(alpha_rectangle(x, 0), origin)
            points.append(proposal["bookmark"]["x"])
        for i, x in enumerate(shifts):
            self.assertAlmostEqual(points[i] - points[0], x, delta=.01)
        self.assertLess(points[5], points[4])
        self.assertLess(points[6], points[5])

    def test_asymmetric_alpha_glyph_translation_at_two_local_backgrounds(self):
        base = np.zeros((72, 90), dtype=np.float32)
        cv2.fillPoly(base, [np.array([[27, 21], [41, 26], [50, 39], [32, 47], [23, 35]])], .7)
        base[28:36, 30:36] = 0
        foreground = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2BGR)[0, 0].astype(float)
        for background in ([18, 25, 32], [60, 65, 70]):
            origin, first = None, None
            for dx, dy in [(0, 0), (.25, .5), (.5, -.25), (1.25, .75), (.75, .25), (-.25, -.5)]:
                alpha = cv2.warpAffine(base, np.float32([[1, 0, dx], [0, 1, dy]]), (90, 72), flags=cv2.INTER_LINEAR)
                image = np.rint(np.asarray(background) + alpha[..., None] * (foreground - background)).astype(np.uint8)
                points, origin = subpixel_anchors(image, origin)
                current = [points["bookmark"]["x"], points["bookmark"]["y"]]
                first = first or current
                self.assertLess(math.dist([current[i] - first[i] for i in (0, 1)], [dx, dy]), .01)

    def test_missing_and_two_same_color_glyphs_remain_ambiguous(self):
        image = alpha_rectangle(0, 0)
        image[4:17, 3:14] = image[25, 25]
        points, _ = subpixel_anchors(image)
        self.assertNotIn("x", points["bookmark"])
        points, _ = subpixel_anchors(np.full((64, 80, 3), 25, np.uint8))
        self.assertNotIn("x", points["bookmark"])

    def test_streaming_rest_can_start_at_departure_image_not_next_image(self):
        trace = rows([0, 2, 2, 2, 2, 2, 2, 2])
        old = next(e for e in legacy_candidates(trace, ["a"]) if e["type"] == "stop")
        new = next(e for e in candidates_v3(trace, ["a"]) if e["type"] == "stop")
        self.assertEqual(old["indices"], [2, 3])
        self.assertEqual(new["indices"], [1, 2])
        self.assertEqual(new["support_indices"], [1, 2, 3, 4, 5])

    def test_public_command_interrupts_endpoint_excursion_without_dropping_motion(self):
        trace = rows([0, 3, 6, 9, 5, 5, 5, 5, 5, 5, 5])
        uninterrupted = candidates_v3(trace, ["a"])
        interrupted = candidates_v3(trace, ["a"], inputs=[{"time_s": .12, "action": "click", "selector": "#close-panel"}])
        self.assertIn("overshoot", {e["type"] for e in uninterrupted})
        self.assertNotIn("overshoot", {e["type"] for e in interrupted})
        for kind in ("motion_start", "stop", "settled", "reversal"):
            self.assertEqual([e["indices"] for e in uninterrupted if e["type"] == kind],
                             [e["indices"] for e in interrupted if e["type"] == kind])

    def test_flat_observed_overshoot_peak_keeps_its_broad_time_interval(self):
        trace = rows([0, 3, 7, 9, 9, 9, 9, 5, 5, 5, 5, 5, 5])
        peak = next(e for e in candidates_v3(trace, ["a"]) if e["type"] == "overshoot")
        self.assertEqual(peak["peak_indices"], [2, 7])
        self.assertAlmostEqual(peak["peak_uncertainty_s"], .25)
        self.assertTrue(set(peak["peak_indices"]).issubset(peak["support_indices"]))

    def test_strict_departure_threshold_is_not_relaxed_at_subpixel_boundary(self):
        self.assertFalse([e for e in candidates_v3(rows([0, 1.5, 1.5]), ["a"]) if e["type"] == "motion_start"])
        self.assertTrue([e for e in candidates_v3(rows([0, 1.501, 1.501]), ["a"]) if e["type"] == "motion_start"])

    def test_real_later_departure_gap_and_lifetime_are_preserved(self):
        trace = rows([0] * 5 + [4, 8] + [8] * 8 + [12, 16] + [16] * 8)
        self.assertEqual(2, sum(e["type"] == "motion_start" for e in candidates_v3(trace, ["a"])))
        limited = candidates_v3(trace, ["a"], active_intervals_s={"a": [[0, .65]]})
        self.assertEqual(1, sum(e["type"] == "motion_start" for e in limited))
        trace[7]["points"]["a"] = {"status": "ambiguous"}
        result = candidates_v3(trace[:11], ["a"])
        self.assertFalse([e for e in result if e["type"] == "stop"])

    def test_unsupported_definition_is_rejected(self):
        with self.assertRaises(VisualError):
            candidates_v3(rows([0, 3]), ["a"], rest_duration=.25)
        with self.assertRaises(VisualError):
            candidates_v3(rows([0, 3]), ["a"], policy="observed-rest-band-departure-v2")

    def test_v2_orchestration_preserves_independent_pixel_boundary_and_pending_gate(self):
        fixture = fixtures.SuiteRawReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record, measured = fixture.fixture(count=24)
        measured["events"]["options"].update(speed_threshold_px_s=5., overshoot_min_px=1.5, axis="auto")
        output = fixture.output()
        original_reader = v2.legacy._measurement
        extraction = v2.subpixel_anchors
        with patch.object(v2, "subpixel_anchors", wraps=extraction) as marking:
            def measurement(value, inventory):
                self.assertEqual(marking.call_count, 24)
                return original_reader(value, inventory)
            with patch.object(v2.legacy, "_measurement", side_effect=measurement):
                result = v2.review_records([record], output, contact_policy="control_and_event_v1")
        self.assertEqual(result["numeric_status"], "pass")
        self.assertEqual(result["status"], "pending_visual_review")
        self.assertFalse(result["acceptance_granted"])
        receipt = read_json(output / "evidence-receipt.json")
        self.assertIn(str(Path(v2.__file__).resolve()), receipt["code_sources"])
        self.assertIn(str(Path(v2.mass.__code__.co_filename).resolve()), receipt["code_sources"])
        self.assertEqual(receipt["result_sha256"], digest(read_json(output / "result.json")))
        source = Path(record["package"]) / "frames/000003.png"
        self.assertEqual(receipt["source_files"][str(source)], file_sha256(source))
        self.assertFalse((output / "attestation.json").exists())

    def geometry_bundle(self, count=24):
        raw_rows, relations = [], []
        for index in range(count):
            raw_rows.append({"index": index, "time_s": index * .04, "image_sha256": f"distinct-image-{index}",
                "points": {"a": {"x": 5 + index, "y": 10, "color_bounds": [index, 5, 10, 10]},
                           "b": {"x": 20 + index, "y": 10, "color_bounds": [15 + index, 5, 10, 10]}}})
            relations.extend([{"type": "relative_anchor", "object_ids": ["a", "b"], "index": index,
                "time_s": index * .04, "dx_px": -15, "dy_px": 0},
                {"type": "object_pair_geometry", "object_ids": ["a", "b"], "index": index, "time_s": index * .04,
                 "anchor_distance_px": 15, "edge_distance_px": 5, "overlap_area_px2": 0,
                 "intersection_over_union": 0, "visually_overlapping": False}])
        return {"record": {"id": "synthetic", "role": "reference", "reference": True}, "rows": raw_rows,
                "mapping": {"a": "bookmark", "b": "handle"}, "event_set": {"relations": relations}}

    def test_independent_geometry_gate_checks_vectors_edges_overlap_and_twenty_images(self):
        bundle = self.geometry_bundle()
        groups, failures = v2._compare_geometry([bundle])
        self.assertFalse(failures)
        self.assertTrue(all(g["pass"] for g in groups))
        for value in bundle["event_set"]["relations"]:
            if value["type"] == "relative_anchor":
                value["dx_px"] += 2
            else:
                value["edge_distance_px"] += 2
        groups, failures = v2._compare_geometry([bundle])
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(not g["pass"] for g in groups))
        bundle = self.geometry_bundle(count=19)
        self.assertEqual(len(v2._compare_geometry([bundle])[1]), 2)

    def test_missing_raw_geometry_and_wrong_overlap_cannot_be_ignored(self):
        bundle = self.geometry_bundle()
        bundle["rows"][8]["points"]["a"] = {"status": "ambiguous"}
        self.assertEqual(len(v2._compare_geometry([bundle])[1]), 2)
        bundle = self.geometry_bundle()
        bundle["event_set"]["relations"][1]["visually_overlapping"] = True
        groups, failures = v2._compare_geometry([bundle])
        self.assertEqual(len(failures), 1)
        self.assertEqual(next(g for g in groups if g["type"] == "object_pair_geometry")["boolean_disagreements"], 1)

    def test_missing_raw_timing_support_is_distinct_from_uncertainty_over_one_tenth(self):
        trace = rows([0, 2, 2, 2, 2, 2, 2, 2])
        for row in trace:
            row["points"]["b"] = {"x": 0, "y": 0}
        bundle = {"record": {"id": "synthetic"}, "rows": trace, "mapping": {"a": "bookmark", "b": "handle"},
            "active": {}, "observations": {"selection": {}}, "manifest": {"frames": [{"index": r["index"], "time_s": r["time_s"]} for r in trace],
            "inputs": {"events": []}}, "event_set": {"events": [], "relations": [{"type": "relative_event_timing", "object_ids": ["a", "b"],
            "event_type": "motion_start", "occurrence": 0, "lag_interval_s": [0, .04], "order": "overlapping_uncertainty",
            "uncertainty_s": .04, "timing_status": "resolved"}]}}
        contract = {"axis": "auto", "rest_duration_s": .2, "max_gap_s": .12, "rest_radius_px": 1.5,
                    "speed_threshold_px_s": 5., "overshoot_min_px": 1.5, "event_tolerance_s": .05}
        _, failures = v2._compare_events(bundle, contract)
        self.assertIn("missing_raw_relation_support", {f["code"] for f in failures})
        self.assertNotIn("relation_uncertainty_exceeds_limit", {f["code"] for f in failures})


if __name__ == "__main__":
    unittest.main()
