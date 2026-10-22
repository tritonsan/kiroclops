"""Real pixel fixtures for independent prospective panel review, without captures."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_raw_review as review
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class SuiteRawReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prospective raw review ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def fixture(self, *, role="reference", count=24, offset=0, step=2, stop_at=12,
                ambiguous_index=None, missing_index=None, repeating=False,
                object_id="bookmark", timing_uncertainty=0, analysis_file=False):
        self.counter += 1
        name = f"clip-{self.counter}"
        frames = self.root / f"frames-{self.counter}"
        frames.mkdir()
        color = cv2.cvtColor(np.uint8([[[93, 210, 220]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        paths, coordinates = [], []
        for i in range(count):
            x = 20 + offset + min(i, stop_at) * step
            image = np.full((100, 2200 if step > 10 else 180, 3), 15, np.uint8)
            if i != missing_index:
                image[30:50, x:x + 12] = color
            if i == ambiguous_index:
                image[65:85, 8:20] = color
            if not repeating:
                # Distinct full raw images even while the object rests.
                image[4:8, 5 + i:6 + i] = [240, 240, 240]
            path = frames / f"{i:03}.png"
            path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
            paths.append(path)
            coordinates.append([x + 5.5, 39.5])
        package = self.root / f"package-{self.counter}"
        times = [i * .04 for i in range(count)]
        frame_metadata = None if not timing_uncertainty else [{"timestamp_source": "fixture_clock",
            "uncertainty_s": timing_uncertainty, "time_interval_s": [t - timing_uncertainty, t + timing_uncertainty]} for t in times]
        manifest = create_package(package, paths, times, frame_metadata=frame_metadata)
        selection = {"objects": [{"id": object_id}]}
        observations = {"kind": "ObservationSet", "package_sha256": manifest["package_sha256"], "selection": selection,
            "objects": [{"id": object_id, "frames": [{"index": i, "time_s": times[i], "image_sha256": manifest["frames"][i]["sha256"],
                "valid": True, "x": xy[0], "y": xy[1]} for i, xy in enumerate(coordinates)]}]}
        events = [{"type": "motion_start", "object_ids": [object_id], "time_interval_s": [0, .04], "timing_status": "resolved"}]
        if count - 1 >= stop_at + 5:
            events.extend({"type": kind, "object_ids": [object_id], "time_interval_s": [stop_at * .04, (stop_at + 1) * .04],
                           "timing_status": "resolved"} for kind in ("stop", "settled"))
        event_set = {"kind": "EventSet", "package_sha256": manifest["package_sha256"], "observation_sha256": digest(observations),
            "motion_start_policy": {"version": review.raw_event_review.CONFIRMED},
            "options": {"rest_duration_s": .2, "max_gap_s": .12, "rest_radius_px": 1.5}, "events": events, "relations": []}
        measurement = {"observations": observations, "events": event_set}
        analysis = measurement
        if analysis_file:
            analysis = self.root / f"measurement-{self.counter}.json"
            write_json(analysis, measurement)
        return {"id": name, "role": role, "domain": "panel", "package": str(package), "analysis": analysis,
                "object_map": {object_id: "bookmark"}}, measurement

    def output(self):
        return self.root / f"review-{len(list(self.root.glob('review-*')))}"

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_pixels_are_extracted_for_all_records_before_any_measurement_read(self):
        first, _ = self.fixture(analysis_file=True)
        second, _ = self.fixture(role="initial", analysis_file=True)
        extraction = review.raw_panel_review_aid.raw_anchors
        original_reader = review._measurement
        counts = []
        with patch.object(review.raw_panel_review_aid, "raw_anchors", wraps=extraction) as marking:
            def measured(record, inventory):
                counts.append(marking.call_count)
                return original_reader(record, inventory)
            with patch.object(review, "_measurement", side_effect=measured):
                result = review.review_records([first, second], self.output())
        self.assertEqual(counts, [48, 48])
        self.assertEqual(result["status"], "pending_visual_review")
        self.assertEqual(result["numeric_status"], "pass")
        self.assertFalse(result["acceptance_granted"])
        self.assertFalse(result["production_coordinates_used_for_extraction"])

    def test_poisoning_produced_positions_does_not_change_raw_points(self):
        record, measured = self.fixture()
        output1 = self.output()
        before = review.review_records([record], output1)
        raw1 = read_json(output1 / "raw" / record["id"] / "points.json")
        for frame in measured["observations"]["objects"][0]["frames"]:
            frame["x"] += 19
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        output2 = self.output()
        after = review.review_records([record], output2)
        raw2 = read_json(output2 / "raw" / record["id"] / "points.json")
        self.assertEqual(raw1, raw2)
        self.assertEqual(before["numeric_status"], "pass")
        self.assertEqual(after["numeric_status"], "fail")
        self.assertEqual(after["positions"][0]["median_error_px"], 19)

    def test_distinct_image_coverage_is_grouped_by_object_and_role(self):
        first, _ = self.fixture(count=12, stop_at=100)
        second, _ = self.fixture(count=12, offset=30, stop_at=100)
        result = review.review_records([first, second], self.output())
        self.assertEqual(result["positions"][0]["raw_distinct_images"], 24)
        self.assertEqual(result["numeric_status"], "pass")
        initial, _ = self.fixture(role="initial", count=12, offset=50, stop_at=100)
        result = review.review_records([first, second, initial], self.output())
        self.assertEqual(result["numeric_status"], "fail")
        failed = next(g for g in result["positions"] if g["role"] == "initial")
        self.assertEqual(failed["raw_distinct_images"], 12)
        self.assertFalse(failed["pass"])

    def test_repeated_raw_images_cannot_satisfy_twenty_distinct_points(self):
        record, _ = self.fixture(repeating=True)
        result = review.review_records([record], self.output())
        self.assertEqual(result["positions"][0]["raw_distinct_images"], 13)
        self.assertEqual(result["numeric_status"], "fail")

    def test_position_tolerance_comes_only_from_independent_reference_extent(self):
        reference, _ = self.fixture(step=80)
        initial, measured = self.fixture(role="initial", step=80)
        for f in measured["observations"]["objects"][0]["frames"]:
            f["x"] += 4.5
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        result = review.review_records([reference, initial], self.output())
        group = next(g for g in result["positions"] if g["role"] == "initial")
        self.assertEqual(group["reference_motion_travel_px"], 960)
        self.assertEqual(group["tolerance_px"], 4.8)
        self.assertTrue(group["pass"])

    def test_initial_record_without_reference_remains_unqualified(self):
        record, _ = self.fixture(role="initial")
        result = review.review_records([record], self.output())
        self.assertEqual(result["numeric_status"], "fail")
        self.assertIsNone(result["positions"][0]["tolerance_px"])
        self.assertTrue(next(f for f in result["failures"] if f["code"] == "position_review_failed")["missing_reference"])

    def test_missing_object_pixel_ambiguity_and_missing_measurement_are_failures(self):
        for arguments, reason in [({"ambiguous_index": 8}, "raw_pixel_ambiguity"), ({"missing_index": 8}, "missing_raw_object")]:
            with self.subTest(reason=reason):
                record, _ = self.fixture(**arguments)
                result = review.review_records([record], self.output())
                self.assertEqual(result["numeric_status"], "fail")
                self.assertIn(reason, {r["reason"] for r in result["positions"][0]["missing_or_ambiguous"]})
        record, measured = self.fixture()
        measured["observations"]["objects"][0]["frames"][8]["valid"] = False
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        result = review.review_records([record], self.output())
        self.assertIn("missing_produced_position", {r["reason"] for r in result["positions"][0]["missing_or_ambiguous"]})

    def test_event_interval_uses_frozen_tolerance_and_counts_extra_raw_events(self):
        record, measured = self.fixture()
        measured["events"]["events"][0]["time_interval_s"] = [.06, .10]
        result = review.review_records([record], self.output())
        self.assertIn("event_interval_disagreement", {f["code"] for f in result["failures"]})
        measured["events"]["events"].pop()
        result = review.review_records([record], self.output())
        self.assertIn("event_occurrence_mismatch", {f["code"] for f in result["failures"]})
        self.assert_code("invalid_event_definition", review.review_records, [record], self.output(), {"event_tolerance_s": .2})

    def test_v3_and_point_two_rest_are_required_and_measured_policy_must_match(self):
        record, measured = self.fixture()
        self.assert_code("unsupported_event_definition", review.review_records, [record], self.output(), {"rest_duration_s": .25})
        self.assert_code("unsupported_event_definition", review.review_records, [record], self.output(), {"policy": review.raw_event_review.LEGACY})
        measured["events"]["motion_start_policy"]["version"] = review.raw_event_review.LEGACY
        result = review.review_records([record], self.output())
        self.assertIn("event_definition_mismatch", {f["code"] for f in result["failures"]})

    def test_recorder_uncertainty_above_point_one_is_preserved_and_detected(self):
        record, _ = self.fixture(timing_uncertainty=.04)
        result = review.review_records([record], self.output())
        self.assertIn("raw_event_uncertainty_exceeds_limit", {f["code"] for f in result["failures"]})
        first = result["events"][0]["raw_events"][0]
        self.assertAlmostEqual(first["uncertainty_s"], .12)
        self.assertEqual(first["observed_interval_s"], [-.04, .08])

    def test_relationship_uncertainty_cannot_be_overridden_by_visual_attestation(self):
        record, measured = self.fixture()
        # A wrong/missing peer is unresolved, independently of produced claims.
        measured["events"]["relations"] = [{"type": "relative_event_timing", "event_type": "motion_start",
            "object_ids": ["bookmark", "unseen"], "occurrence": 0, "lag_interval_s": [-.06184, .06184],
            "order": "overlapping_uncertainty", "uncertainty_s": .12368, "timing_status": "resolved"}]
        output = self.output()
        result = review.review_records([record], output)
        self.assertIn("relation_uncertainty_exceeds_limit", {f["code"] for f in result["failures"]})
        self.assert_code("review_not_qualified", review.attest_review, output, reviewer="Test reviewer",
                         inspected_sheets=[s["path"] for s in result["sheets"]], accepted=True, images_inspected=True)

    def test_every_image_code_measurement_and_sheet_is_hash_bound(self):
        record, _ = self.fixture(analysis_file=True)
        output = self.output()
        result = review.review_records([record], output)
        receipt = read_json(output / "evidence-receipt.json")
        self.assertEqual(len([p for p in receipt["source_files"] if p.endswith(".png")]), 24)
        self.assertIn(str(Path(record["analysis"])), receipt["source_files"])
        self.assertIn(str(Path(review.__file__).resolve()), receipt["code_sources"])
        self.assertIn(str(Path(review.raw_panel_review_aid.__file__).resolve()), receipt["code_sources"])
        for sheet in result["sheets"]:
            self.assertEqual(sheet["sha256"], file_sha256(output / sheet["path"]))
        self.assertEqual(receipt["result_sha256"], digest(read_json(output / "result.json")))

    def test_only_explicit_complete_inspection_can_attest_and_pending_aid_is_immutable(self):
        record, _ = self.fixture()
        output = self.output()
        result = review.review_records([record], output)
        sheets = [s["path"] for s in result["sheets"]]
        before = (output / "result.json").read_bytes()
        self.assert_code("incomplete_visual_review", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=sheets[:1], accepted=True, images_inspected=True)
        self.assert_code("invalid_review", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=sheets, accepted=True, images_inspected=False)
        self.assert_code("invalid_review", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=sheets, accepted=True)
        decision = review.attest_review(output, reviewer="Test reviewer", inspected_sheets=sheets, accepted=True, images_inspected=True)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual((output / "result.json").read_bytes(), before)
        self.assertEqual(read_json(output / "result.json")["status"], "pending_visual_review")
        self.assert_code("output_exists", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=sheets, accepted=True, images_inspected=True)
        self.assert_code("output_exists", review.review_records, [record], output)

    def test_changed_raw_sheet_or_source_cannot_be_attested(self):
        for changed_source in (False, True):
            with self.subTest(changed_source=changed_source):
                record, _ = self.fixture()
                output = self.output()
                result = review.review_records([record], output)
                path = Path(record["package"]) / "frames/000000.png" if changed_source else output / result["sheets"][0]["path"]
                path.write_bytes(path.read_bytes() + b"changed")
                self.assert_code("stale_review", review.attest_review, output, reviewer="Test reviewer",
                                 inspected_sheets=[s["path"] for s in result["sheets"]], accepted=True, images_inspected=True)

    def test_svg_and_game_are_explicitly_unsupported_and_cannot_pass(self):
        for domain in ("svg", "game"):
            with self.subTest(domain=domain):
                record, _ = self.fixture()
                record["domain"] = domain
                output = self.output()
                result = review.review_records([record], output)
                self.assertEqual(result["status"], "pending_visual_review")
                self.assertEqual(result["unsupported"][0]["domain"], domain)
                self.assertEqual(result["numeric_status"], "fail")
                self.assert_code("review_not_qualified", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=[], accepted=True, images_inspected=True)

    def test_nested_capture_record_and_public_object_map_work(self):
        record, _ = self.fixture(object_id="title", role="calibration")
        wrapper = {"case_id": "open-test", "role": "calibration", "record": {k: v for k, v in record.items() if k not in ("id", "role")}}
        result = review.review_records([wrapper], self.output(), {"event_options": {"motion_policy": review.raw_event_review.CONFIRMED,
            "rest_duration_s": .2, "max_gap_s": .12, "max_event_uncertainty_s": .1}})
        self.assertEqual(result["records"][0]["record_id"], "open-test")
        self.assertEqual(result["positions"][0]["object_id"], "title")
        self.assertEqual(result["numeric_status"], "pass")

    def test_wrong_package_and_unsafe_record_ids_are_refused(self):
        record, measured = self.fixture()
        record["id"] = "../escape"
        self.assert_code("unsafe_path", review.review_records, [record], self.output())
        record["id"] = "okay"
        measured["observations"]["package_sha256"] = "different"
        self.assert_code("wrong_result", review.review_records, [record], self.output())

    def test_inline_measurement_is_frozen_and_changed_public_scope_is_refused(self):
        record, measured = self.fixture()
        output = self.output()
        result = review.review_records([record], output)
        self.assertEqual(read_json(output / "measurements" / record["id"] / "measurement.json"), measured)
        before = file_sha256(output / "measurements" / record["id"] / "measurement.json")
        measured["observations"]["objects"][0]["frames"][0]["x"] = 90
        self.assertEqual(file_sha256(output / "measurements" / record["id"] / "measurement.json"), before)
        record["selection"] = {"objects": [{"id": "bookmark", "active_intervals_s": [[.1, .2]]}]}
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        self.assert_code("wrong_result", review.review_records, [record], self.output())
        self.assertEqual(result["status"], "pending_visual_review")

    def test_selected_identity_cannot_change_raw_glyph_between_records(self):
        first, _ = self.fixture()
        second, _ = self.fixture()
        second["object_map"] = {"bookmark": "handle"}
        self.assert_code("invalid_review", review.review_records, [first, second], self.output())

    def test_unmatched_produced_event_still_reports_its_excessive_uncertainty(self):
        record, measured = self.fixture()
        measured["events"]["events"].append({"type": "reversal", "object_ids": ["bookmark"],
                                            "time_interval_s": [.2, .32368]})
        result = review.review_records([record], self.output())
        self.assertIn("produced_event_uncertainty_exceeds_limit", {f["code"] for f in result["failures"]})
        self.assertEqual(result["numeric_status"], "fail")

    def test_missing_whole_selected_identity_cannot_disappear_from_a_role(self):
        first, measured = self.fixture()
        missing, _ = self.fixture(role="initial")
        measured["observations"]["objects"].append({"id": "handle", "frames": []})
        measured["observations"]["selection"]["objects"].append({"id": "handle"})
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        first["object_map"]["handle"] = "handle"
        result = review.review_records([first, missing], self.output())
        failure = next(f for f in result["failures"] if f["code"] == "missing_selected_objects")
        self.assertEqual(failure["record_id"], missing["id"])
        self.assertEqual(failure["objects"], ["handle"])
        self.assertEqual(result["numeric_status"], "fail")

    def test_compact_policy_reduces_sheets_and_freezes_twenty_group_controls(self):
        record, _ = self.fixture(count=96)
        full = review.review_records([record], self.output())
        output = self.output()
        compact = review.review_records([record], output, contact_policy="control_and_event_v1")
        self.assertLess(len(compact["sheets"]), len(full["sheets"]))
        self.assertEqual(compact["positions"], full["positions"])
        self.assertEqual(compact["events"], full["events"])
        self.assertEqual(compact["numeric_status"], "pass")
        self.assertEqual(compact["status"], "pending_visual_review")
        selection = read_json(output / "contact-selection.json")
        self.assertEqual(len(selection["controls"]), 20)
        self.assertEqual(len({c["image_sha256"] for c in selection["controls"]}), 20)
        self.assertFalse(selection["production_coordinates_or_events_used"])
        receipt = read_json(output / "evidence-receipt.json")
        self.assertEqual(receipt["contact_policy"], "control_and_event_v1")
        self.assertEqual(receipt["contact_selection_sha256"], digest(selection))
        self.assertEqual(len(read_json(output / "raw" / record["id"] / "points.json")["rows"]), 96)

    def test_compact_policy_off_sheet_source_corruption_and_missing_position_still_fail(self):
        record, measured = self.fixture(count=96)
        output = self.output()
        result = review.review_records([record], output, contact_policy="control_and_event_v1")
        selected = {i["index"] for i in read_json(output / "contact-selection.json")["selected_images"]}
        off_sheet = next(i for i in range(96) if i not in selected)
        measured["observations"]["objects"][0]["frames"][off_sheet]["valid"] = False
        measured["events"]["observation_sha256"] = digest(measured["observations"])
        failed = review.review_records([record], self.output(), contact_policy="control_and_event_v1")
        self.assertEqual(failed["numeric_status"], "fail")
        self.assertIn(off_sheet, {p["index"] for p in failed["positions"][0]["missing_or_ambiguous"]})
        source = Path(record["package"]) / f"frames/{off_sheet:06d}.png"
        source.write_bytes(source.read_bytes() + b"changed outside displayed samples")
        self.assert_code("stale_review", review.attest_review, output, reviewer="Test reviewer",
                         inspected_sheets=[s["path"] for s in result["sheets"]], accepted=True, images_inspected=True)

    def test_compact_selection_is_unchanged_by_produced_event_times(self):
        record, measured = self.fixture(count=96)
        first_output = self.output()
        review.review_records([record], first_output, contact_policy="control_and_event_v1")
        selection = read_json(first_output / "contact-selection.json")
        measured["events"]["events"][0]["time_interval_s"] = [.8, .84]
        output = self.output()
        result = review.review_records([record], output, contact_policy="control_and_event_v1")
        self.assertEqual(selection, read_json(output / "contact-selection.json"))
        self.assertEqual(result["numeric_status"], "fail")

    def test_compact_policy_requires_every_sheet_and_explicit_actual_inspection(self):
        record, _ = self.fixture(count=96)
        output = self.output()
        result = review.review_records([record], output, contact_policy="control_and_event_v1")
        sheets = [s["path"] for s in result["sheets"]]
        self.assert_code("invalid_review", review.attest_review, output, reviewer="Test reviewer", inspected_sheets=sheets, accepted=True)
        self.assert_code("incomplete_visual_review", review.attest_review, output, reviewer="Test reviewer",
                         inspected_sheets=sheets[:-1], accepted=True, images_inspected=True)
        self.assertFalse((output / "attestation.json").exists())
        self.assertEqual(read_json(output / "result.json")["status"], "pending_visual_review")

    def test_compact_policy_keeps_raw_event_support_input_and_ambiguity_neighbors(self):
        record, _ = self.fixture(count=96, ambiguous_index=50)
        manifest_path = Path(record["package"]) / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["inputs"] = {"provenance": "user_declared", "events": [{"action": "click", "selector": "#open-panel", "time_s": 2.8}]}
        manifest.pop("package_sha256")
        manifest["package_sha256"] = digest(manifest)
        write_json(manifest_path, manifest)
        record["analysis"]["observations"]["package_sha256"] = manifest["package_sha256"]
        record["analysis"]["events"]["package_sha256"] = manifest["package_sha256"]
        record["analysis"]["events"]["observation_sha256"] = digest(record["analysis"]["observations"])
        output = self.output()
        result = review.review_records([record], output, contact_policy="control_and_event_v1")
        selection = read_json(output / "contact-selection.json")
        indices = {i["index"] for i in selection["selected_images"]}
        self.assertTrue({49, 50, 51, 69, 70, 71}.issubset(indices))
        for event in result["events"][0]["raw_events"]:
            for index in event["indices"] + event.get("support_indices", []):
                self.assertTrue({n for n in (index - 1, index, index + 1) if 0 <= n < 96}.issubset(indices))
        self.assertEqual(result["numeric_status"], "fail")

    def test_unknown_contact_policy_is_refused_before_output_is_created(self):
        record, _ = self.fixture()
        output = self.output()
        self.assert_code("invalid_review", review.review_records, [record], output, contact_policy="skip_everything")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
