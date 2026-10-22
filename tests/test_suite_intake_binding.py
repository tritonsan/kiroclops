"""Portable user clips bind explicit source roles without capture or model work."""
from copy import deepcopy
import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import media_suite, suite_intake, suite_intake_binding as bridge
from kiroclops.local_engine.intake import prepare_intake, resume_selection
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class IntakeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intake bridge ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.project("parent")
        self.original = self.project("original", code="return 0")
        self.intake = self.root / "intake-suite"
        self.output = self.root / "executable"
        self.count = 0
        suite_intake.draft(self.intake, "panel", "Transfer the panel movement", ["open"])

    def project(self, name, domain="panel", code="return 1"):
        root = self.root / name
        (root / "src").mkdir(parents=True)
        entry, export, ids = (("panel-motion.js", "advanceMotion", ["open-panel", "close-panel", "reset"])
                              if domain == "panel" else ("layout-motion.js", "advanceLayout", ["add-node", "remove-node", "reset"]))
        (root / "index.html").write_text("".join(f'<button id="{i}"></button>' for i in ids) + '<script src="src/app.js" type="module"></script>', encoding="utf-8")
        (root / "src/app.js").write_text(f"import {{ {export} }} from './{entry}'; {export}();", encoding="utf-8")
        (root / "src" / entry).write_text(f"export function {export}() {{ {code}; }}", encoding="utf-8")
        return root

    def clip(self, name="open-1", role="calibration", provenance="user_declared", seed=1, behaviors=None):
        self.count += 1
        paths = []
        texture = np.random.default_rng(seed).integers(20, 230, (12, 12, 3), dtype=np.uint8)
        for index in range(8):
            image = np.zeros((320, 320, 3), np.uint8)
            image[20:32, 10+index:22+index] = texture
            path = self.root / f"frame-{self.count}-{index}.png"
            cv2.imwrite(str(path), image)
            paths.append(path)
        package = self.root / f"package-{self.count}"
        inputs = {"provenance": provenance, "events": [] if provenance == "unknown" else [
            {"action": "click", "selector": "#open-panel", "time_s": 4.4}]}
        create_package(package, paths, [4.+index*.2 for index in range(8)], inputs)
        destination = self.root / f"single-{self.count}"
        with patch("kiroclops.local_engine.intake._qualify_automatic", return_value=(None, {"status": "needs_confirmation"})):
            prepare_intake(package, self.parent, "Transfer the panel movement", destination)
        manifest = load_package(destination / "package")
        selection = {"kind": "ObjectSelection", "schema_version": 1, "package_sha256": manifest["package_sha256"],
                     "objects": [{"id": "my-handle", "roi": [10, 20, 12, 12], "anchor": [6, 6],
                                  "body_height_px": 12, "frame_index": 0,
                                  "template_frame_sha256": manifest["frames"][0]["sha256"], "method": "template"}],
                     "background_rois": []}
        resume_selection(destination, selection)
        result = suite_intake.add_clip(self.intake, destination, name, behaviors or ["open"], {"my-handle": "handle"}, role)
        return result, manifest, selection

    def fake_analysis(self, package, selection, output, check, progress, *, contract, candidate_sha256=None):
        manifest = load_package(package)
        objects = []
        for obj in selection["objects"]:
            x, y, w, h = obj["roi"]
            objects.append({"id": obj["id"], "method": obj["method"], "frames": [
                {"index": f["index"], "time_s": f["time_s"], "valid": True,
                 "x": x+w/2+f["index"], "y": y+h/2, "width": w, "height": h}
                for f in manifest["frames"]]})
        observed = {"kind": "Observations", "package_sha256": manifest["package_sha256"], "image_size": manifest["image_size"],
                    "inputs": deepcopy(manifest["inputs"]), "selection": deepcopy(selection), "objects": objects}
        result = {"kind": "AnalysisResult", "measurement_status": "ok", "candidate_sha256": candidate_sha256,
                  "package_sha256": manifest["package_sha256"], "observations": observed,
                  "events": extract_events(observed, selection, contract)}
        write_json(Path(output) / "analysis.json", result)
        return result

    def bind(self, **kwargs):
        with patch("kiroclops.local_engine.evaluation._analysis", side_effect=self.fake_analysis) as analyze, \
             patch("kiroclops.local_engine.evaluation._capture_web", side_effect=AssertionError("no capture")):
            result = bridge.bind_intake(self.intake, self.output, original=self.original, authoring_parent=self.parent, **kwargs)
        return result, analyze

    def ready(self, provenance="user_declared", validation=False):
        self.clip(provenance=provenance)
        if validation:
            self.clip("open-distinct", "validation", provenance, seed=2)
        suite_intake.freeze(self.intake)
        return self.bind()

    def record(self):
        suite = media_suite.load_suite(self.output)
        return media_suite.record_from_suite(self.output, suite["cases"][0])

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_declared_inputs_keep_authority_mapping_source_roles_and_no_fake_validation(self):
        self.clip()
        frozen = suite_intake.freeze(self.intake)
        frozen_bytes = (self.intake / "suite.json").read_bytes()
        before = source_hashes(self.parent)
        result, analyze = self.bind()
        self.assertEqual(result["status"], "suite_bound")
        self.assertEqual(result["heldout_status"], "not_provided")
        self.assertFalse(result["behavior_accepted"])
        self.assertFalse(result["model_started"])
        self.assertEqual(analyze.call_count, 1)
        self.assertIsNone(analyze.call_args.kwargs["candidate_sha256"])
        record = self.record()
        self.assertEqual(record["intake_binding"]["object_map"], {"my-handle": "handle"})
        self.assertEqual(record["intake_binding"]["claim"], "conditional_on_user_declared_inputs")
        self.assertAlmostEqual(record["scenario"]["events"][0]["time_s"], .4)
        self.assertEqual(record["observations"]["inputs"]["events"][0]["time_s"], 4.4)
        self.assertEqual(record["intake_provenance"]["intake_suite_sha256"], digest(frozen))
        suite = media_suite.load_suite(self.output)
        self.assertEqual(suite["original_source_hashes"], source_hashes(self.original))
        self.assertEqual(suite["authoring_parent_source_hashes"], before)
        self.assertNotEqual(suite["original_source_hashes"], suite["authoring_parent_source_hashes"])
        self.assertEqual(source_hashes(self.parent), before)
        self.assertEqual((self.intake / "suite.json").read_bytes(), frozen_bytes)
        self.assertEqual(bridge.verify_reference_record(record)["status"], "verified")

    def test_verified_inputs_remain_verified_without_model_or_unseen_claim(self):
        result, _ = self.ready("recorder_verified")
        self.assertEqual(result["timeline_authority"], ["recorder_verified"])
        self.assertEqual(self.record()["intake_binding"]["claim"], "recorded_input_response")
        self.assertFalse(self.record()["scenario"]["blind_validation"])

    def test_distinct_actual_validation_is_preserved_without_adding_recipes(self):
        result, analyze = self.ready(validation=True)
        suite = media_suite.load_suite(self.output)
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(result["heldout_status"], "provided")
        self.assertEqual(result["validation_basis"], "user_designated_distinct_pixels")
        self.assertEqual([c["case_id"] for c in suite["required_cases"]], ["open-1", "open-distinct"])
        self.assertEqual(suite["cases"][1]["role"], "validation")
        self.assertFalse(suite["cases"][1]["scenario"]["blind_validation"])

    def test_unknown_timeline_never_infers_or_analyzes(self):
        added, manifest, selection = self.clip(provenance="unknown")
        self.assertEqual(added["status"], "needs_input_binding")
        from kiroclops.local_engine.intake import detect_project
        self.assert_code("needs_input_binding", bridge._binding, manifest, selection, {"my-handle": "handle"},
                         detect_project(self.parent), "unknown", "calibration", {})
        result, analyze = self.bind()
        self.assertEqual(result["status"], "needs_reference")
        self.assertEqual(analyze.call_count, 0)
        self.assertFalse(self.output.exists())

    def test_missing_or_only_validation_capability_requests_reference(self):
        result, analyze = self.bind()
        self.assertEqual(result["status"], "needs_reference")
        self.assertEqual(analyze.call_count, 0)
        self.clip(role="validation")
        suite_intake.freeze(self.intake)
        result, analyze = self.bind()
        self.assertEqual(result["missing_calibration_behaviors"], ["open"])
        self.assertEqual(analyze.call_count, 0)
        self.assertFalse(self.output.exists())

    def test_authoring_parent_cannot_be_invented_from_original_or_changed_target(self):
        self.clip()
        suite_intake.freeze(self.intake)
        self.assert_code("wrong_authoring_parent", bridge.bind_intake, self.intake, self.output,
                         original=self.original, authoring_parent=self.original)
        (self.parent / "src/panel-motion.js").write_text("export function advanceMotion() { return 99; }")
        self.assert_code("wrong_authoring_parent", self.bind)

    def test_map_mismatch_invalid_options_and_raw_pixel_tampering_fail_closed(self):
        self.ready()
        record = self.record()
        altered = deepcopy(record)
        altered["intake_binding"]["object_map"]["my-handle"] = "star"
        self.assert_code("binding_changed", bridge.verify_reference_record, altered)
        altered = deepcopy(record)
        altered["intake_binding"]["input_provenance"] = "recorder_verified"
        self.assert_code("binding_changed", bridge.verify_reference_record, altered)
        altered = deepcopy(record)
        altered["observations"]["objects"][0]["frames"][0]["x"] += 1
        self.assert_code("measurement_binding_changed", bridge.verify_reference_record, altered)
        (Path(record["package"]) / "frames/000000.png").write_bytes(b"changed")
        self.assert_code("package_integrity_failure", bridge.verify_reference_record, record)

    def test_options_cannot_create_new_case_or_change_input_authority(self):
        self.clip()
        suite_intake.freeze(self.intake)
        self.assert_code("invalid_binding", self.bind, case_options={"invented": {}})
        self.assert_code("invalid_binding", self.bind, case_options={"open-1": {"input_provenance": "recorder_verified"}})
        self.assertFalse(self.output.exists())

    def test_portable_reference_rechecks_after_copy(self):
        self.ready()
        moved = self.root / "copied-suite"
        shutil.copytree(self.output, moved)
        suite = media_suite.load_suite(moved)
        record = media_suite.record_from_suite(moved, suite["cases"][0])
        self.assertEqual(bridge.verify_reference_record(record)["status"], "verified")

    def test_capture_honors_reference_id_and_shared_policy_once_without_real_browser(self):
        self.ready()
        reference = self.record()
        manifest = load_package(reference["package"])
        target_package = self.root / "target-pixels"
        inputs = deepcopy(manifest["inputs"])
        inputs["provenance"] = "recorder_verified"
        create_package(target_package, [Path(reference["package"]) / f["path"] for f in manifest["frames"]],
                       [f["time_s"] for f in manifest["frames"]], inputs)
        selection = deepcopy(reference["intake_source_selection"])
        selection["package_sha256"] = load_package(target_package)["package_sha256"]
        selection["objects"][0]["id"] = "handle"
        raw = {"package": str(target_package), "scenario": reference["scenario"], "source_sha256": digest(source_hashes(self.parent)),
               "functional": {"status": "pass"}, "timing": {"status": "pass"}}
        with patch("kiroclops.local_engine.evaluation._capture_web", return_value=raw), \
             patch("kiroclops.web_local.selection.select_package_from_pixels", return_value=selection), \
             patch("kiroclops.local_engine.evaluation._analysis", side_effect=self.fake_analysis) as analyze:
            row = bridge.capture_record(self.parent, self.root / "target-capture", reference["scenario"],
                lambda: None, lambda *args: None, None, "candidate", reference=reference)
        self.assertEqual([o["id"] for o in row["selection"]["objects"]], ["my-handle"])
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(analyze.call_args.kwargs["contract"], reference["suite_event_contract"])
        self.assertEqual(analyze.call_args.kwargs["candidate_sha256"], raw["source_sha256"])

    def test_comparison_retains_conditional_authority_and_rejects_target_map_or_drag_mismatch(self):
        self.ready()
        reference = self.record()
        candidate = deepcopy(reference)
        candidate["observations"]["inputs"]["provenance"] = "recorder_verified"
        candidate["functional"] = {"status": "pass"}
        with patch("kiroclops.local_engine.media_session._compare", return_value={"pass": True, "score": .01}) as compare:
            result = bridge.compare_records(reference, candidate, candidate)
        self.assertTrue(result["conditional_on_user_declared_inputs"])
        self.assertEqual(result["claim"], "conditional_on_user_declared_inputs")
        self.assertEqual(result["score"], .01)
        self.assertEqual(compare.call_args.kwargs["require_improvement"], True)
        wrong = deepcopy(candidate)
        wrong["intake_binding"]["object_map"]["my-handle"] = "star"
        self.assert_code("suite_recipe_mismatch", bridge.compare_records, reference, candidate, wrong)
        wrong = deepcopy(candidate)
        wrong["observations"]["inputs"]["events"][0]["fraction"] = .5
        self.assert_code("capture_timing_failure", bridge.compare_records, reference, candidate, wrong)
        wrong = deepcopy(candidate)
        wrong["observations"]["inputs"]["provenance"] = "user_declared"
        self.assert_code("capture_timing_failure", bridge.compare_records, reference, candidate, wrong)

    def test_svg_initial_state_missing_is_explicit_input_binding(self):
        from kiroclops.local_engine.intake import detect_project
        self.clip()
        manifest = load_package(self.intake / "clips/open-1/package")
        manifest["inputs"]["events"][0]["selector"] = "#add-node"
        selection = read_json(self.intake / "clips/open-1/selection.json")
        project = detect_project(self.project("svg", "svg"))
        self.assert_code("needs_input_binding", bridge._binding, manifest, selection, {"my-handle": "a"},
                         project, "svg-1", "calibration", {})

    def test_svg_suite_binds_declared_state_complete_user_graph_and_drag_recipe(self):
        self.parent = self.project("svg-parent", "svg")
        self.original = self.project("svg-original", "svg", code="return 0")
        self.intake = self.root / "svg-intake-suite"
        suite_intake.draft(self.intake, "svg", "Transfer node dragging", ["drag"])
        paths = []
        for index in range(8):
            path = self.root / f"svg-{index}.png"
            cv2.imwrite(str(path), np.full((320, 320, 3), 20+index, np.uint8))
            paths.append(path)
        package = self.root / "svg-pixels"
        create_package(package, paths, [4.+i*.2 for i in range(8)], {"provenance": "user_declared", "events": [
            {"action": "drag_start", "selector": '[data-node-id="b"]', "time_s": 4.4,
             "destination": {"viewport_x": .64, "viewport_y": .32}},
            {"action": "drag_move", "fraction": 1., "time_s": 4.6},
            {"action": "drag_end", "time_s": 4.8}]})
        single = self.root / "svg-single"
        with patch("kiroclops.local_engine.intake._qualify_automatic", return_value=(None, {"status": "needs_confirmation"})):
            prepare_intake(package, self.parent, "Drag the node", single)
        manifest = load_package(single / "package")
        objects = [{"id": "my-"+node, "roi": [20+index*50, 20, 12, 12], "anchor": [6, 6],
                    "body_height_px": 12, "frame_index": 0, "template_frame_sha256": manifest["frames"][0]["sha256"],
                    "method": "contour"} for index, node in enumerate("abc")]
        connections = [{"id": "edge-"+str(i), "from_object": "my-"+a, "to_object": "my-"+b, "method": "line"}
                       for i, (a, b) in enumerate((("a", "b"), ("b", "c")))]
        resume_selection(single, {"kind": "ObjectSelection", "schema_version": 1,
            "package_sha256": manifest["package_sha256"], "objects": objects, "connections": connections, "background_rois": []})
        mapping = {"my-"+node: node for node in "abc"}
        suite_intake.add_clip(self.intake, single, "drag-1", ["drag"], mapping)
        suite_intake.freeze(self.intake)
        result, analyze = self.bind(case_options={"drag-1": {"initial_node_count": 3, "expected_selected_id": "b"}})
        self.assertEqual(result["status"], "suite_bound")
        self.assertEqual(analyze.call_count, 1)
        record = self.record()
        self.assertEqual(record["scenario"]["initial_node_count"], 3)
        self.assertEqual(record["scenario"]["expected_final_node_count"], 3)
        self.assertEqual(record["scenario"]["expected_selected_id"], "b")
        self.assertEqual(record["intake_binding"]["object_map"], mapping)
        self.assertEqual(record["selection"]["connections"], connections)
        self.assertEqual(bridge.verify_reference_record(record)["status"], "verified")

    def test_source_race_after_analysis_never_publishes_suite(self):
        self.clip()
        suite_intake.freeze(self.intake)
        def mutate(*args, **kwargs):
            result = self.fake_analysis(*args, **kwargs)
            (self.original / "src/panel-motion.js").write_text("export function advanceMotion() { return 42; }")
            return result
        with patch("kiroclops.local_engine.evaluation._analysis", side_effect=mutate):
            self.assert_code("source_changed", bridge.bind_intake, self.intake, self.output,
                             original=self.original, authoring_parent=self.parent)
        self.assertFalse((self.output / "suite.json").exists())

    def test_cli_requires_explicit_roles_and_rejects_duplicate_case_options(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bridge.main(["--intake", str(self.intake), "--output", str(self.output)])
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            status = bridge.main(["--intake", str(self.intake), "--output", str(self.output), "--original", str(self.original),
                                  "--authoring-parent", str(self.parent), "--svg-initial", "same=3", "--svg-initial", "same=4"])
        self.assertEqual(status, 2)
        self.assertIn("invalid_binding", text.getvalue())


if __name__ == "__main__":
    unittest.main()
