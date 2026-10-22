"""Small raw PNG controls; no application, model or real acceptance is run."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_generic_raw_review as review
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from tests.evidence import requires_evidence


class GenericRawReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="generic raw small ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.count = 0

    def game(self, role, *, count=24, repeating=False, missing=None, jump=False, uncertainty=0):
        self.count += 1
        directory = self.root / f"frames-{self.count}"
        directory.mkdir()
        paths, frames = [], []
        for i in range(count):
            x = 20 + min(i, 12)*2 if not jump else 20
            y = 100-round(20*np.sin(np.pi*(i-10)/40)) if jump and 10 <= i <= 50 else 100
            image = np.zeros((128, 228, 3), np.uint8)
            if i != missing:
                image[y, x:x+5] = [168, 119, 255]
                image[y+1, x:x+6] = [168, 119, 255]
            if not repeating:
                image[2:4, 5+i:6+i] = [150, 150, 150]
            path = directory / f"{i}.png"; cv2.imwrite(str(path), image); paths.append(path)
            frames.append({"index": i, "time_s": i*.04, "valid": True, "x": x+1., "y": y+2., "width": 6., "height": 3.})
        package = self.root / f"package-{self.count}"
        inputs = {"provenance": "recorder_verified", "events": [{"time_s": .4, "action": "jump", "pressed": True},
            {"time_s": .52, "action": "jump", "pressed": False}]} if jump else {"provenance": "recorder_verified", "events": []}
        metadata = None if not uncertainty else [{"timestamp_source": "fixture_clock", "uncertainty_s": uncertainty,
            "time_interval_s": [i*.04-uncertainty, i*.04+uncertainty]} for i in range(count)]
        manifest = create_package(package, paths, [i*.04 for i in range(count)], inputs, frame_metadata=metadata)
        selection = {"objects": [{"id": "character", "body_height_px": 7}], "background_rois": []}
        observed = {"package_sha256": manifest["package_sha256"], "image_size": manifest["image_size"], "inputs": manifest["inputs"],
                    "selection": selection, "objects": [{"id": "character", "frames": frames}]}
        events = extract_events(observed, selection, {})
        analysis = self.root / f"analysis-{self.count}.json"
        write_json(analysis, {"observations": observed, "events": events})
        row = {"id": f"record-{self.count}", "domain": "game", "role": role, "reference": role == "reference", "package": str(package),
               "scenario": {"id": "small", "mechanic": "jump" if jump else "ground"}, "analysis": str(analysis), "selection": selection,
               "observations": observed, "events": events, "source_sha256": digest({})}
        if role != "reference":
            row["expected_source"] = {"kind": "native", "sources": {}, "session_sources": {}}
        if jump:
            row["motion_events"] = {"takeoff_frame": 10, "apex_frame": 30, "landing_frame": 50}
        return row

    def svg(self, role, *, count=24, bad_edge=False):
        self.count += 1
        directory = self.root / f"svg-frames-{self.count}"; directory.mkdir()
        colors = [cv2.cvtColor(np.uint8([[[h, 210, 220]]]), cv2.COLOR_HSV2BGR)[0, 0] for h in (93, 21)]
        paths = []
        for i in range(count):
            image = np.full((220, 400, 3), [37, 25, 16], np.uint8)
            image[80:140, 50:140] = [79, 57, 38]; image[80:140, 260:350] = [79, 57, 38]
            image[98:118, 75:87] = colors[0]; image[98:118, 285:297] = colors[1]
            image[109:112, 140:260] = [176, 149, 128]
            image[4:6, 10+i:11+i] = [220, 220, 220]
            path = directory / f"{i}.png"; cv2.imwrite(str(path), image); paths.append(path)
        package = self.root / f"svg-package-{self.count}"
        manifest = create_package(package, paths, [i*.04 for i in range(count)], {"provenance": "recorder_verified", "events": []})
        selection = {"objects": [{"id": oid, "method": "contour"} for oid in "ab"],
                     "connections": [{"id": "a-b", "from_object": "a", "to_object": "b", "method": "line"}]}
        objects = [{"id": oid, "method": "contour", "frames": [{"index": i, "time_s": i*.04, "valid": True,
                    "x": x, "y": 107.5, "left": l, "top": 80., "width": 90., "height": 60., "geometry_method": "contour"}
                    for i in range(count)]} for oid, x, l in (("a", 80.5, 50.), ("b", 290.5, 260.))]
        connections = [{"id": "a-b", "from_object": "a", "to_object": "b", "frames": [
            {"index": i, "time_s": i*.04, "valid": True, "start": [140., 110.], "end": [259.+(9 if bad_edge else 0), 110.]}
            for i in range(count)]}]
        observed = {"package_sha256": manifest["package_sha256"], "image_size": manifest["image_size"], "inputs": manifest["inputs"],
                    "selection": selection, "objects": objects, "connections": connections}
        policy = {"event_options": {"rest_duration_s": .25}}
        events = extract_events(observed, selection, policy)
        analysis = self.root / f"svg-analysis-{self.count}.json"; write_json(analysis, {"observations": observed, "events": events})
        row = {"id": f"svg-{self.count}", "domain": "svg", "role": role, "reference": role == "reference", "package": str(package),
               "scenario": {"id": "svg-small", "domain": "svg"}, "analysis": str(analysis), "selection": selection,
               "observations": observed, "events": events, "source_sha256": digest({})}
        if role != "reference":
            row["expected_source"] = {"kind": "web", "sources": {}}
        return row

    def run_review(self, rows, output=None, policy=None):
        output = output or self.root / f"review-{self.count}"
        with patch("kiroclops.local_engine.suite_game.verify_reference_record", return_value={"status": "verified"}), \
             patch("kiroclops.local_engine.evidence._record_observation", return_value=("fixture", "fixture")):
            result = review.review_records(rows, output, policy)
        return result, output

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    @requires_evidence('artifacts/phase3a/tools')
    def test_game_all_roles_raw_points_and_twenty_controls_are_unsigned(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        result, output = self.run_review(rows)
        self.assertEqual(result["numeric_status"], "pass", result["failures"])
        self.assertEqual(len(result["positions"]), 3)
        self.assertTrue(all(p["raw_distinct_images"] >= 20 for p in result["positions"]))
        self.assertTrue(all(p["tolerance_px"] == 1. for p in result["positions"]))
        self.assertFalse(result["visual_inspection_claimed"])
        self.assertFalse(result["acceptance_granted"])
        self.assertFalse((output / "attestation.json").exists())
        self.assertEqual(len(read_json(output / "contact-selection.json")["controls"]), 60)

    @requires_evidence('artifacts/phase3a/tools')
    def test_all_raw_images_are_extracted_before_production_comparison(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        extract = review._raw_image; scope = review._scope; seen = []
        with patch.object(review, "_raw_image", wraps=extract) as marking:
            def after(bundle, inventory, contract):
                seen.append(marking.call_count)
                return scope(bundle, inventory, contract)
            with patch.object(review, "_scope", side_effect=after):
                self.run_review(rows)
        self.assertEqual(seen, [72, 72, 72])

    @requires_evidence('artifacts/phase3a/tools')
    def test_poisoning_positions_changes_verdict_not_independent_marks(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        before, first = self.run_review(rows, self.root / "first")
        for frame in rows[1]["observations"]["objects"][0]["frames"]:
            frame["x"] += 1.2
        rows[1]["events"]["observation_sha256"] = digest(rows[1]["observations"])
        after, second = self.run_review(rows, self.root / "second")
        self.assertEqual(read_json(first / "raw" / rows[1]["id"] / "points.json"), read_json(second / "raw" / rows[1]["id"] / "points.json"))
        self.assertEqual(before["numeric_status"], "pass")
        self.assertEqual(after["numeric_status"], "fail")
        self.assertAlmostEqual(next(g["median_error_px"] for g in after["positions"] if g["role"] == "original"), 1.2)

    @requires_evidence('artifacts/phase3a/tools')
    def test_repeated_or_missing_images_cannot_create_twenty_controls(self):
        rows = [self.game("reference", repeating=True), self.game("original"), self.game("before", missing=8)]
        result, output = self.run_review(rows)
        self.assertEqual(result["numeric_status"], "fail")
        self.assertEqual(next(g["raw_distinct_images"] for g in result["positions"] if g["role"] == "reference"), 13)
        with self.assertRaises(VisualError):
            review.attest_review(output, reviewer="test-only", inspected_sheets=[s["path"] for s in result["sheets"]], accepted=True, images_inspected=True)

    @requires_evidence('artifacts/phase3a/tools')
    def test_explicit_inspection_is_required_and_attestation_is_append_only(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        result, output = self.run_review(rows)
        self.assert_code("invalid_review", review.attest_review, output, reviewer="test-only", inspected_sheets=[], accepted=True)
        self.assert_code("incomplete_visual_review", review.attest_review, output, reviewer="test-only", inspected_sheets=[], accepted=True, images_inspected=True)
        # Tests attest a rejected decision only; no real visual review is claimed.
        rejected = review.attest_review(output, reviewer="test-only", inspected_sheets=[s["path"] for s in result["sheets"]], accepted=False, images_inspected=True)
        self.assertEqual(rejected["status"], "fail")
        self.assert_code("output_exists", review.attest_review, output, reviewer="test-only", inspected_sheets=[], accepted=False, images_inspected=True)

    @requires_evidence('artifacts/phase3a/tools')
    def test_jump_takeoff_apex_landing_use_raw_glyphs_and_frozen_two_frame_gate(self):
        rows = [self.game(role, count=70, jump=True) for role in ("reference", "original", "before")]
        result, _ = self.run_review(rows, self.root / "jump-first")
        self.assertEqual(len(result["jump_events"]), 3)
        self.assertEqual(result["jump_events"][0]["raw"], {"takeoff_frame": 10, "apex_frame": 30, "landing_frame": 50})
        self.assertTrue(all(c["pass"] for e in result["jump_events"] for c in e["checks"].values()))
        rows[1]["motion_events"]["apex_frame"] = 33
        failed, _ = self.run_review(rows, self.root / "jump-second")
        self.assertFalse(next(e for e in failed["jump_events"] if e["record_id"] == rows[1]["id"])["checks"]["apex_frame"]["pass"])

    @requires_evidence('artifacts/phase3a/tools')
    def test_svg_raw_card_geometry_and_edge_scope_are_checked(self):
        rows = [self.svg(role) for role in ("reference", "original", "before")]
        result, _ = self.run_review(rows, policy={"event_options": {"rest_duration_s": .25}})
        self.assertEqual(result["numeric_status"], "pass", result["failures"][:5])
        self.assertTrue(all(c["pass"] for e in result["events"] for c in e["relation_checks"]))
        self.assertTrue(any(c["type"] == "connection_attachment" for e in result["events"] for c in e["relation_checks"]))
        self.assertEqual(result["public_event_contract"]["rest_duration_s"], .25)

    @requires_evidence('artifacts/phase3a/tools')
    def test_svg_edge_disagreement_and_uncertain_game_events_fail(self):
        rows = [self.svg("reference"), self.svg("original", bad_edge=True), self.svg("before")]
        result, _ = self.run_review(rows, policy={"event_options": {"rest_duration_s": .25}})
        self.assertEqual(result["numeric_status"], "fail")
        self.assertTrue(any(f["code"] == "relation_unobservable_or_disagreement" for f in result["failures"]))
        game = [self.game("reference", uncertainty=.08), self.game("original"), self.game("before")]
        result, _ = self.run_review(game)
        self.assertEqual(result["numeric_status"], "fail")
        self.assertTrue(any(f["code"] == "raw_event_uncertainty_exceeds_limit" for f in result["failures"]))

    @requires_evidence('artifacts/phase3a/tools')
    def test_missing_producer_source_binding_and_role_are_rejected(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        rows[1].pop("expected_source")
        self.assert_code("needs_source_binding", self.run_review, rows)
        self.assert_code("incomplete_review", self.run_review, rows[:1], self.root / "incomplete")

    @requires_evidence('artifacts/phase3a/tools')
    def test_modified_sheet_or_raw_source_invalidates_attestation(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        result, output = self.run_review(rows)
        (output / result["sheets"][0]["path"]).write_bytes(b"changed")
        self.assert_code("stale_review", review.attest_review, output, reviewer="test-only", inspected_sheets=[s["path"] for s in result["sheets"]], accepted=False, images_inspected=True)

    def test_native_producer_inventory_binds_runtime_logs_and_raw_frame_bytes(self):
        raw = self.root / "native-raw"; raw.mkdir()
        frames = raw / "frames"; frames.mkdir()
        cv2.imwrite(str(frames / "000000.png"), np.zeros((8, 8, 3), np.uint8))
        engine = self.root / "engine"; engine.mkdir()
        log = engine / "stdout.log"; log.write_text("test-only")
        receipt = engine / "bridge.json"
        write_json(receipt, {"evidence_hashes": {"stdout.log": file_sha256(log)}})
        write_json(raw / "manifest.json", {"frames": [{"frame_index": 0}], "provenance": {"runtime_receipt": str(receipt)}})
        inventory = {}
        review._producer_inventory({"raw_run": str(raw)}, inventory)
        self.assertEqual(inventory[str(log)], file_sha256(log))
        self.assertEqual(inventory[str(frames / "000000.png")], file_sha256(frames / "000000.png"))
        log.write_text("changed")
        self.assert_code("source_changed", review._producer_inventory, {"raw_run": str(raw)}, {})

    def native_scope_fixture(self, role):
        """Real normalized package/common-worker ABI; producer receipts are mocked."""
        from kiroclops.local_engine import suite_game
        self.count += 1
        scenario = suite_game.scenarios("ground", "calibration")[0]
        source = self.root / f"scope-source-{self.count}.png"
        cv2.imwrite(str(source), np.zeros((2, 2, 3), np.uint8))
        inputs = {"provenance": "recorder_verified", "events": [
            {"time_s": edge["tick"]/60, "action": edge["action"], "pressed": edge["pressed"]}
            for edge in scenario["events"]]}
        package = self.root / f"scope-package-{self.count}"
        manifest = create_package(package, [source]*240, [i/60 for i in range(240)], inputs,
                                  {"scenario": scenario})
        selection = {"objects": [{"id": "character", "body_height_px": 16}], "background_rois": []}
        observed = {"package_sha256": manifest["package_sha256"], "image_size": manifest["image_size"],
            "inputs": manifest["inputs"], "selection": selection, "objects": [{"id": "character", "frames": [
                {"index": i, "time_s": i/60, "valid": True, "x": 1., "y": 1., "width": 1., "height": 1.}
                for i in range(240)]}]}
        events = extract_events(observed, selection, {})
        sources = {"player.gd": "a"*64, "player.gd.uid": "b"*64}
        expected = {"kind": "native", "sources": {"player.gd": "a"*64}, "session_sources": sources}
        projection = self.root / f"scope-analysis-{self.count}" / "analysis.json"
        write_json(projection, {"observations": observed, "events": events})
        write_json(projection.parent / "common-analysis/analysis.json", {
            "candidate_sha256": None if role == "reference" else digest(sources),
            "package_sha256": manifest["package_sha256"], "scenario": scenario,
            "observations": observed, "events": events})
        declaration = {"id": f"scope-{self.count}", "domain": "game", "role": role,
            "reference": role == "reference", "package": str(package), "scenario": scenario,
            "analysis": str(projection), "selection": selection, "observations": observed,
            "events": events, "source_sha256": digest(sources)}
        if role != "reference":
            declaration["expected_source"] = expected
        normalized = review.common._record(declaration, self.count)
        bundle = {"record": normalized, "manifest": manifest, "raw": {"rows": [
            {"index": i, "time_s": i/60, "points": {"character": {"status": "not_detected"}},
             "objects": {}, "connections": {}} for i in range(240)]}}
        return declaration, bundle

    def test_normalized_native_packages_enter_real_evidence_abi_without_mutating_sources(self):
        from kiroclops.local_engine import evidence
        for role in ("original", "before", "reference"):
            with self.subTest(role=role):
                declaration, bundle = self.native_scope_fixture(role)
                original = deepcopy(declaration)
                self.assertIsInstance(bundle["record"]["package"], Path)
                def producer(record, manifest, expected):
                    return None if expected["kind"] == "uploaded" else {"frame_edges": declaration["scenario"]["events"]}
                # Keep the real immutable observation verifier. Only absent native
                # process/Tiny producer receipts are replaced in this small fixture.
                with patch.object(evidence, "_record_source", side_effect=producer), \
                     patch.object(evidence, "_record_observation", wraps=evidence._record_observation) as checking, \
                     patch("kiroclops.local_engine.suite_game.verify_reference_record", return_value={"status": "verified"}):
                    failures = review._scope(bundle, {}, review._public_contract({}, "game"))
                self.assertEqual(failures, [])
                checking.assert_called_once()
                self.assertIsInstance(checking.call_args.args[0]["package"], str)
                self.assertEqual(checking.call_args.args[0]["source_sha256"], original["source_sha256"])
                self.assertIsInstance(bundle["record"]["package"], Path)
                self.assertEqual(declaration, original)
                if role != "reference":
                    self.assertEqual(bundle["record"]["expected_source"], original["expected_source"])

    @requires_evidence('artifacts/phase3a/tools')
    def test_readiness_requires_attestation_and_the_entire_original_before_matrix(self):
        rows = [self.game(role) for role in ("reference", "original", "before")]
        result, output = self.run_review(rows)
        self.assert_code("environment_not_ready", review.validate_review, output, self.root, {"domain": "game"})
        # Synthetic readiness fixture only, inside this unit test's temporary
        # directory. No public review receipt or application acceptance is made.
        write_json(output / "attestation.json", {"kind": "ProspectiveRawVisualReviewAttestation", "status": "pass",
            "images_inspected": True, "accepted": True, "result_sha256": digest(read_json(output / "result.json")),
            "evidence_receipt_sha256": file_sha256(output / "evidence-receipt.json"), "inspected_sheets": result["sheets"]})
        cases = [{"case_id": "case-1", "role": "calibration", "record": rows[0]}]
        initial = [{"case_id": "case-1", "role": role, "record": row} for role, row in zip(("original", "before"), rows[1:])]
        suite = {"domain": "game", "event_contract": None, "cases": cases, "initial_records": initial}
        with patch("kiroclops.local_engine.media_suite.record_from_suite", side_effect=lambda root, case: case["record"]):
            value = review.validate_review(output, self.root, suite)
            self.assertEqual(value["required_record_count"], 3)
            incomplete = {**suite, "initial_records": initial[:1]}
            self.assert_code("environment_not_ready", review.validate_review, output, self.root, incomplete)
            changed = deepcopy(suite); changed["initial_records"][0]["record"]["source_sha256"] = "other-source"
            self.assert_code("environment_not_ready", review.validate_review, output, self.root, changed)


if __name__ == "__main__":
    unittest.main()
