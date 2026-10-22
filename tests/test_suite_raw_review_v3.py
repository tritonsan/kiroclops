"""Independent rounded-paint and analytic photometry fixtures; no captures."""
from copy import deepcopy
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_raw_review_v2 as v2
from kiroclops.local_engine import suite_raw_review_v3 as v3
from kiroclops.visual.pixel_anchor import prepare_anchor, observe_anchor
from kiroclops.visual.schema import VisualError, digest, read_json, write_json, file_sha256
from tests import test_suite_raw_review as fixtures
from tests.test_suite_raw_review_v2 import alpha_rectangle, rows


def rounded_panel(x=30, y=40, w=170, h=150, *, radius=12, duplicate=False, clipped=False):
    image = np.full((360, 520, 3), [17, 22, 31], np.uint8)
    outer = [82, 69, 52]
    body, header = [43, 35, 27], [60, 45, 33]
    mask = np.zeros(image.shape[:2], np.uint8)
    def rounded(xx, yy):
        cv2.rectangle(mask, (xx+radius, yy), (xx+w-radius-1, yy+h-1), 1, -1)
        cv2.rectangle(mask, (xx, yy+radius), (xx+w-1, yy+h-radius-1), 1, -1)
        for cx, cy in ((xx+radius, yy+radius), (xx+w-radius-1, yy+radius),
                       (xx+radius, yy+h-radius-1), (xx+w-radius-1, yy+h-radius-1)):
            cv2.circle(mask, (cx, cy), radius, 1, -1)
    rounded(x, y)
    if duplicate:
        rounded(x+210, y+10)
    image[mask > 0] = outer
    inner = cv2.erode(mask, np.ones((3, 3), np.uint8))
    image[inner > 0] = body
    yy = np.indices(mask.shape)[0]
    image[(inner > 0) & (yy < y+32)] = header
    return image, {"x": float(x+24), "y": float(y+17), "color_bounds": [x+20, y+12, 9, 11]}, [x, y, w, h]


class SuiteRawReviewV3Tests(unittest.TestCase):
    def test_independent_first_palette_handles_two_observed_background_layers(self):
        yy, xx = np.indices((64, 80), dtype=float)
        rgb = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2RGB)[0, 0].tolist()
        fg = np.asarray(rgb[::-1], float)
        bg = np.where((xx < 32)[..., None], np.array([18, 25, 32.]), np.array([46, 40, 31.]))
        selection = {"objects": [{"id": "bookmark", "roi": [19, 17, 27, 31],
            "pixel_anchor": {"foreground_rgb": rgb}}]}
        origin, local_origin, first, local_first = None, None, None, None
        errors, local_errors = [], []
        for dx, dy in [(0, 0), (.13, .27), (.49, .51), (.93, .07), (1.11, -.41), (-.37, .83), (.55, .31)]:
            left, top, right, bottom = 22+dx, 20+dy, 42+dx, 44+dy
            alpha = np.maximum(0, np.minimum(xx+1, right)-np.maximum(xx, left))*np.maximum(0, np.minimum(yy+1, bottom)-np.maximum(yy, top))
            image = np.rint(bg+alpha[..., None]*(fg-bg)).astype(np.uint8)
            points, origin = v3.mixture_anchors(image, selection, origin)
            local, local_origin = v2.subpixel_anchors(image, local_origin)
            point = points["bookmark"]
            self.assertIn("x", point, point)
            current = [point["x"], point["y"]]
            first = first or current
            errors.append(math.dist([current[i]-first[i] for i in (0, 1)], [dx, dy]))
            local_point = [local["bookmark"]["x"], local["bookmark"]["y"]]
            local_first = local_first or local_point
            local_errors.append(math.dist([local_point[i]-local_first[i] for i in (0, 1)], [dx, dy]))
            self.assertEqual(point["quantization"], "none")
        self.assertLess(max(errors), .01)
        self.assertGreater(max(local_errors), .1)
        self.assertEqual(len(origin["bookmark"]["paint"]["background_palette_bgr"]), 2)

    def test_mixture_palette_and_missing_first_support_cannot_be_refit_later(self):
        rgb = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2RGB)[0, 0].tolist()
        selection = {"objects": [{"id": "bookmark", "roi": [19, 17, 27, 31], "pixel_anchor": {"foreground_rgb": rgb}}]}
        image = alpha_rectangle(0, 0)
        points, origin = v3.mixture_anchors(image, selection)
        calibration = deepcopy(origin)
        v3.mixture_anchors(alpha_rectangle(.31, .49), selection, origin)
        self.assertEqual(origin, calibration)
        missing, absent = v3.mixture_anchors(np.full_like(image, 25), selection)
        late, _ = v3.mixture_anchors(image, selection, absent)
        self.assertNotIn("x", late["bookmark"])

    def test_panel_drag_variable_width_scale_and_rounded_native_bounds(self):
        first, handle, expected = rounded_panel()
        calibration = v3._learn_panel(first, [31, 41, 168, 148])
        self.assertEqual(v3.panel_paint_bounds(first, handle, calibration), expected)
        for x, y, w, h, r in [(53, 67, 170, 150, 12), (110, 33, 230, 150, 14),
                             (40, 99, 125, 110, 10), (220, 50, 250, 240, 22)]:
            image, raw, expected = rounded_panel(x, y, w, h, radius=r)
            self.assertEqual(v3.panel_paint_bounds(image, raw, calibration), expected)

    def test_panel_identity_selects_contour_and_clipped_paint_is_unresolved(self):
        first, handle, _ = rounded_panel()
        calibration = v3._learn_panel(first, [31, 41, 168, 148])
        image, handle, expected = rounded_panel(duplicate=True)
        self.assertEqual(v3.panel_paint_bounds(image, handle, calibration), expected)
        image, handle, _ = rounded_panel(-5, 40)
        with self.assertRaises(VisualError) as raised:
            v3.panel_paint_bounds(image, handle, calibration)
        self.assertEqual(raised.exception.code, "ambiguous_raw_geometry")
        with self.assertRaises(VisualError):
            v3.panel_paint_bounds(first, {"status": "ambiguous"}, calibration)

    def test_public_padding_moves_by_raw_mass_not_integer_color_box_jitter(self):
        image = alpha_rectangle(0, 0)
        selection = {"objects": [{"id": "bookmark", "roi": [19, 17, 27, 31]}]}
        points, origins = v2.subpixel_anchors(image)
        raw, geometry = v3.raw_body_geometry(image, points, selection)
        initial = raw["bookmark"]["geometry_bounds"]
        for dx, dy in [(.49, .51), (1.11, -.41), (-.37, .83)]:
            image = alpha_rectangle(dx, dy)
            points, origins = v2.subpixel_anchors(image, origins)
            raw, geometry = v3.raw_body_geometry(image, points, selection, geometry)
            body = raw["bookmark"]["geometry_bounds"]
            self.assertEqual(body[2:], initial[2:])
            self.assertLess(math.dist([body[i]-initial[i] for i in (0, 1)], [dx, dy]), .01)
        self.assertEqual(geometry["bookmark"]["padding_px"], [3, 3, 4, 4])

    def test_late_or_changed_illustration_cannot_recalibrate_first_roi(self):
        image = alpha_rectangle(0, 0)
        selection = {"objects": [{"id": "bookmark", "roi": [19, 17, 27, 31]}]}
        missing, origin = v3.raw_body_geometry(image, {"bookmark": {"status": "ambiguous"}}, selection)
        points, _ = v2.subpixel_anchors(image)
        result, origin = v3.raw_body_geometry(image, points, selection, origin)
        self.assertNotIn("geometry_bounds", result["bookmark"])
        self.assertEqual(result["bookmark"]["geometry_status"], "ambiguous")
        result, origin = v3.raw_body_geometry(image, points, selection)
        altered = deepcopy(points)
        altered["bookmark"]["color_bounds"][2] += 3
        changed, _ = v3.raw_body_geometry(image, altered, selection, origin)
        self.assertNotIn("geometry_bounds", changed["bookmark"])

    def test_missing_body_is_not_replaced_by_handle_glyph_bounds(self):
        fixture = __import__("tests.test_suite_raw_review_v2", fromlist=["SuiteRawReviewV2Tests"]).SuiteRawReviewV2Tests()
        bundle = fixture.geometry_bundle()
        for row in bundle["rows"]:
            for point in row["points"].values():
                point["geometry_bounds"] = point["color_bounds"][:]
        self.assertFalse(v3._compare_geometry([bundle])[1])
        del bundle["rows"][8]["points"]["a"]["geometry_bounds"]
        _, failures = v3._compare_geometry([bundle])
        self.assertIn("raw_body_geometry_unobservable", {f["code"] for f in failures})

    def test_analytic_independent_and_production_photometry_both_keep_fractional_anchor(self):
        # Production is called ONLY in this synthetic diagnostic. The actual
        # aid module does not import/call it, read its coordinates or fit to it.
        initial = alpha_rectangle(0, 0)
        x, y, w, h = 19, 17, 27, 31
        rgb = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2RGB)[0, 0].tolist()
        reference = prepare_anchor(initial[y:y+h, x:x+w], {"method": "foreground_color_mass_v1", "foreground_rgb": rgb})
        independent_origin, first_independent, first_produced = None, None, None
        independent_errors, production_errors, differences = [], [], []
        for dx, dy in [(0, 0), (.13, .27), (.49, .51), (.93, .07), (1.11, -.41), (-.37, .83)]:
            image = alpha_rectangle(dx, dy)
            raw, independent_origin = v2.subpixel_anchors(image, independent_origin)
            point = [raw["bookmark"]["x"], raw["bookmark"]["y"]]
            produced = observe_anchor(image, x, y, w, h, reference)
            self.assertTrue(produced["valid"])
            measured = [produced["x"], produced["y"]]
            first_independent = first_independent or point
            first_produced = first_produced or measured
            delta = [point[i]-first_independent[i] for i in (0, 1)]
            production_delta = [measured[i]-first_produced[i] for i in (0, 1)]
            independent_errors.append(math.dist(delta, [dx, dy]))
            production_errors.append(math.dist(production_delta, [dx, dy]))
            differences.append(math.dist(delta, production_delta))
        self.assertLess(max(independent_errors), .01)
        self.assertLess(max(production_errors), .01)
        # Both are accurate to .01px on this fixture. That does not resolve a
        # real difference of .0017px straddling the strict rest threshold.
        self.assertLess(max(differences), .01)

    def test_interrupted_noisy_rest_keeps_strict_boundary_and_propagated_restart(self):
        production_like = rows([0, 3, 3, 4.49879, 4.49879, 4.49879, 4.49879, 4.49879, 4.501, 5.9999])
        independent_like = deepcopy(production_like)
        for row in independent_like[3:8]:
            row["points"]["a"]["x"] = 4.500489
        inputs = [{"time_s": .075, "action": "click", "selector": "#interrupt"}]
        first = v2.candidates_v3(production_like, ["a"], inputs=inputs)
        second = v2.candidates_v3(independent_like, ["a"], inputs=inputs)
        self.assertNotEqual([e["indices"] for e in first if e["type"] == "stop"],
                            [e["indices"] for e in second if e["type"] == "stop"])
        self.assertFalse([e for e in first+second if e["type"] == "overshoot"])

    def test_orchestration_extracts_before_measurements_and_hashes_new_code(self):
        fixture = fixtures.SuiteRawReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record, measured = fixture.fixture(count=24)
        selection = {"objects": [{"id": "bookmark", "roi": [17, 27, 18, 26]}]}
        measured["observations"]["selection"] = deepcopy(selection)
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        measured["events"]["options"].update(speed_threshold_px_s=5., overshoot_min_px=1.5, axis="auto")
        record["selection"] = selection
        output = fixture.output()
        original_reader = legacy_reader = v3.legacy._measurement
        with patch.object(v3.v2, "subpixel_anchors", wraps=v2.subpixel_anchors) as marking:
            def measurement(value, inventory):
                self.assertEqual(marking.call_count, 24)
                return original_reader(value, inventory)
            with patch.object(v3.legacy, "_measurement", side_effect=measurement):
                result = v3.review_records([record], output, contact_policy="control_and_event_v1")
        self.assertEqual(result["numeric_status"], "pass")
        self.assertEqual(result["status"], "pending_visual_review")
        self.assertFalse(result["acceptance_granted"])
        receipt = read_json(output / "evidence-receipt.json")
        self.assertIn(str(Path(v3.__file__).resolve()), receipt["code_sources"])
        self.assertIn(str(Path(v2.__file__).resolve()), receipt["code_sources"])
        self.assertEqual(receipt["result_sha256"], digest(read_json(output / "result.json")))
        self.assertFalse((output / "attestation.json").exists())

    def test_public_selection_must_be_explicit_not_borrowed_from_measurements(self):
        fixture = fixtures.SuiteRawReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record, _ = fixture.fixture()
        with self.assertRaises(VisualError) as raised:
            v3.review_records([record], fixture.output())
        self.assertEqual(raised.exception.code, "needs_public_selection")

    def metric_record(self, scale=1.):
        from kiroclops.local_engine import suite_web
        from kiroclops.visual.events import extract_events
        objects = []
        for oid, offset, delay, travel in (("handle", 0., 0., 0.), ("bookmark", 30., .08, 24.), ("star", 60., .18, 40.)):
            frames = []
            for index in range(181):
                t = index/60
                x = 100.*scale*max(0., min(1., (t-.4)/.6))+offset
                y = 30.+travel*scale*max(0., min(1., (t-.4-delay)/.3))
                frames.append({"index": index, "time_s": t, "x": x, "y": y, "left": x-5., "top": y-5.,
                    "width": 10., "height": 10., "valid": True, "present": True, "expected_active": True})
            objects.append({"id": oid, "frames": frames, "active_intervals_s": [[0., 3.]], "valid_ratio": 1.})
        observations = {"kind": "ObservationSet", "package_sha256": "synthetic", "image_size": [960, 640], "objects": objects,
            "connections": [], "inputs": {"provenance": "recorder_verified", "events": [{"action": "click", "selector": "#open-panel", "time_s": .4}]}}
        selection = {"objects": [{"id": obj["id"]} for obj in objects], "relative_motion": [
            {"object_id": "bookmark", "relative_to": "handle"}, {"object_id": "star", "relative_to": "handle"}]}
        contract = suite_web.event_contract("panel")
        return {"scenario": suite_web.scenarios("panel", "calibration")[0], "observations": observations,
            "selection": selection, "measurement_status": "ok", "functional": {"status": "pass"}, "suite_event_contract": contract,
            "events": extract_events(observations, selection, contract)}

    def admission_fixture(self):
        source = {"src/panel-motion.js": "fixture-source-sha"}
        bundles, records = [], []
        for role, scale in (("reference", 1.), ("original", .75), ("before", 1.)):
            record = self.metric_record(scale)
            record.update(id=role, role=role, source_sha256=digest(source), package=Path("unused-fixture-package"))
            bundles.append({"record": record, "manifest": {"metadata": {"source_hashes": source}},
                "observations": record["observations"], "event_set": record["events"]})
            records.append({"record_id": role, "role": role})
        result = {"version": v3.VERSION, "status": "pending_visual_review", "numeric_status": "fail", "records": records,
            "unsupported": [], "sheets": [{"path": "sheets/fixture.png", "sha256": "pending"}],
            "failures": [{"code": "event_interval_disagreement", "record_id": "original", "difference_s": .16}]}
        result["admission"] = v3._admission_proposal(result, bundles)
        return result, bundles

    def test_actual_scored_projection_identical_after_stored_and_derived_event_mutations(self):
        proof = v3.prove_original_events_unconsumed(self.metric_record(), self.metric_record(.75), self.metric_record())
        self.assertEqual(proof["status"], "pass")
        self.assertTrue(all(check["identical"] for check in proof["original_event_mutations"]))
        self.assertTrue(all(check["scoring_sha256"] == proof["scoring_sha256"] for check in proof["original_event_mutations"]))
        self.assertEqual(proof["profile_input_roles"], ["reference"])
        self.assertEqual(len(proof["original_derived_event_mutations"]), 3)
        for check in proof["original_derived_event_mutations"]:
            self.assertTrue(check["identical"])
            self.assertTrue(check["observations_and_inputs_unchanged"])
            self.assertEqual(check["scoring_sha256"], proof["scoring_sha256"])
            # The actual panel baseline contract has no required-event checks;
            # record that absence rather than claiming an event was consumed.
            self.assertEqual(check["original_extractor_intervention_count"], 0)
        self.assertIn(str(Path(__import__("kiroclops.local_engine.suite_web", fromlist=["compare_records"]).__file__).resolve()), proof["metric_sources"])

    def test_original_derived_event_interventions_change_diagnostics_only(self):
        from kiroclops.local_engine import evaluation
        reference, original, before = self.metric_record(), self.metric_record(.75), self.metric_record()
        original_sha = digest(original["observations"])
        native_contract = evaluation._contract

        def diagnostic_contract(ref, candidate=None):
            value = native_contract(ref, candidate)
            if candidate and digest(candidate["observations"]) == original_sha:
                value["required_event_types"] = ["motion_start"]
            return value

        with patch.object(evaluation, "_contract", side_effect=diagnostic_contract):
            proof = v3.prove_original_events_unconsumed(reference, original, before)
        self.assertEqual(proof["status"], "pass")
        for check in proof["original_derived_event_mutations"]:
            self.assertEqual(check["original_extractor_intervention_count"], 1)
            self.assertTrue(check["other_observations_passed_through"])
            self.assertTrue(check["baseline_event_diagnostics_changed"])
            self.assertFalse(check["whole_output_identical"])
            self.assertTrue(check["identical"])
            self.assertEqual(check["original_extractor_interventions"][0]["observations_sha256"], original_sha)
            self.assertTrue(all(item["observations_sha256"] != original_sha for item in check["other_observations_passed_through"]))

    def test_identical_observation_role_identity_cannot_be_intervened_selectively(self):
        record = self.metric_record()
        with self.assertRaises(VisualError) as raised:
            v3.prove_original_events_unconsumed(record, deepcopy(record), deepcopy(record))
        self.assertEqual(raised.exception.code, "metric_dependency_unproven")

    def test_dependency_proof_fails_if_a_metric_uses_original_derived_events(self):
        from kiroclops.local_engine import evaluation, suite_web
        reference, original, before = self.metric_record(), self.metric_record(.75), self.metric_record()
        original_sha = digest(original["observations"])
        native_contract, native_compare = evaluation._contract, suite_web.compare_records

        def diagnostic_contract(ref, candidate=None):
            value = native_contract(ref, candidate)
            if candidate and digest(candidate["observations"]) == original_sha:
                value["required_event_types"] = ["motion_start"]
            return value

        def wrongly_dependent_compare(*args, **kwargs):
            value = native_compare(*args, **kwargs)
            value["score"] = sum(check["candidate_count"] for check in value["original_pixel_comparison"]["event_checks"])
            return value

        with patch.object(evaluation, "_contract", side_effect=diagnostic_contract), \
             patch.object(suite_web, "compare_records", side_effect=wrongly_dependent_compare):
            proof = v3.prove_original_events_unconsumed(reference, original, before)
        self.assertEqual(proof["status"], "fail")
        self.assertFalse(next(check for check in proof["original_derived_event_mutations"]
                              if check["mutation"] == "absent_original_derived_events")["identical"])

    def test_admission_retains_original_failures_but_never_excludes_other_gates(self):
        result, bundles = self.admission_fixture()
        proposal = result["admission"]
        self.assertEqual(proposal["status"], "pass")
        self.assertEqual(result["numeric_status"], "fail")
        self.assertEqual(result["failures"], proposal["original_event_diagnostics"])
        self.assertFalse(proposal["original_event_measurements_qualified"])
        for failure in ({"code": "independent_geometry_review_failed", "role": "original"},
                        {"code": "position_review_failed", "role": "original"},
                        {"code": "event_interval_disagreement", "record_id": "reference"},
                        {"code": "relation_disagreement", "record_id": "before"},
                        {"code": "event_definition_mismatch", "record_id": "original"}):
            altered = deepcopy(result)
            altered["failures"].append(failure)
            self.assertEqual(v3._admission_proposal(altered, bundles)["status"], "fail")

    def frozen_admission_output(self):
        fixture = fixtures.SuiteRawReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        output = fixture.output()
        output.mkdir()
        (output/"sheets").mkdir()
        image_path = output/"sheets/fixture.png"
        image_path.write_bytes(cv2.imencode(".png", np.full((12, 12, 3), 30, np.uint8))[1].tobytes())
        result, _ = self.admission_fixture()
        result["sheets"][0]["sha256"] = file_sha256(image_path)
        policy = {"anchor_policy": "local_background_v2", "anchor_definition": v3._anchor_definition("local_background_v2")}
        result.update(anchor_policy="local_background_v2", policy_selection_sha256=digest(policy))
        write_json(output/"policy-selection.json", policy)
        write_json(output/"result.json", result)
        write_json(output/"evidence-receipt.json", {"result_sha256": digest(result), "source_files": {},
            "code_sources": {str(Path(v3.__file__).resolve()): file_sha256(v3.__file__)},
            "generated_files": {"result.json": file_sha256(output/"result.json"), "sheets/fixture.png": file_sha256(image_path),
                                "policy-selection.json": file_sha256(output/"policy-selection.json")},
            "anchor_policy": "local_background_v2", "anchor_policy_definition_sha256": digest(v3._anchor_definition("local_background_v2")),
            "policy_selection_sha256": digest(policy), "runtime_libraries": {"opencv": cv2.__version__, "numpy": np.__version__}})
        return output, result

    def test_distinct_admission_attestation_cannot_accept_whole_failed_review(self):
        output, result = self.frozen_admission_output()
        with self.assertRaises(VisualError):
            v3.attest_review(output, reviewer="actual inspector", inspected_sheets=["sheets/fixture.png"], accepted=True, images_inspected=True)
        with self.assertRaises(VisualError):
            v3.attest_admission(output, reviewer="actual inspector", inspected_sheets=[], admitted=True, images_inspected=True)
        with self.assertRaises(VisualError):
            v3.attest_admission(output, reviewer="actual inspector", inspected_sheets=["sheets/fixture.png"], admitted=True)
        decision = v3.attest_admission(output, reviewer="actual inspector", inspected_sheets=["sheets/fixture.png"], admitted=True, images_inspected=True)
        self.assertEqual(decision["kind"], "ProspectivePanelScoringAdmissionAttestation")
        self.assertNotIn("accepted", decision)
        self.assertFalse(decision["whole_raw_review_accepted"])
        self.assertFalse(decision["original_event_measurements_qualified"])
        self.assertFalse((output/"attestation.json").exists())
        self.assertEqual(read_json(output/"result.json"), result)
        self.assertFalse(v3.validate_admission(output)["whole_raw_review_accepted"])

    def test_any_scoring_dependency_change_invalidates_admission(self):
        output, _ = self.frozen_admission_output()
        v3.attest_admission(output, reviewer="actual inspector", inspected_sheets=["sheets/fixture.png"], admitted=True, images_inspected=True)
        inventory = v3._metric_inventory()
        inventory["changed_scoring_source.py"] = "another-code-identity"
        with patch.object(v3, "_metric_inventory", return_value=inventory):
            with self.assertRaises(VisualError) as raised:
                v3.validate_admission(output)
        self.assertEqual(raised.exception.code, "stale_admission")
        (output/"sheets/fixture.png").write_bytes(b"changed image")
        with self.assertRaises(VisualError) as raised:
            v3.validate_admission(output)
        self.assertEqual(raised.exception.code, "stale_review")


if __name__ == "__main__":
    unittest.main()
