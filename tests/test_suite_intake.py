"""Multi-reference preparation binds real pixels and never starts a model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_intake as suite
from kiroclops.local_engine.intake import prepare_intake, resume_selection
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, read_json, write_json


class SuiteIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suite intake ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        (self.target / "src").mkdir(parents=True)
        (self.target / "index.html").write_text('<button id="open-panel">Go</button><script src="src/app.js" type="module"></script>')
        (self.target / "src/app.js").write_text("import { advanceMotion } from './panel-motion.js'; advanceMotion();")
        (self.target / "src/panel-motion.js").write_text("export function advanceMotion() { return 1; }")
        self.output = self.root / "suite"
        self.counter = 0

    def intake(self, seed=1, *, provenance="user_declared", object_id="thing", original_package=None):
        self.counter += 1
        source = original_package
        if source is None:
            directory = self.root / f"frames-{self.counter}"
            directory.mkdir()
            body = np.random.default_rng(seed).integers(30, 230, (12, 12, 3), dtype=np.uint8)
            paths = []
            for index in range(8):
                image = np.zeros((64, 64, 3), dtype=np.uint8)
                image[20:32, 10 + index:22 + index] = body
                path = directory / f"{index}.png"
                path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
                paths.append(path)
            source = self.root / f"package-{self.counter}"
            create_package(source, paths, [index * .05 for index in range(8)])
        destination = self.root / f"intake-{self.counter}"
        inputs = None if provenance == "unknown" else {
            "provenance": provenance, "events": [{"action": "click", "selector": "#open-panel", "time_s": .1}]}
        with patch("kiroclops.local_engine.intake._qualify_automatic", return_value=(None, {"status": "needs_confirmation"})):
            prepare_intake(source, self.target, "Transfer the panel movement", destination, inputs=inputs)
        manifest = load_package(destination / "package")
        selection = {"schema_version": 1, "kind": "ObjectSelection", "package_sha256": manifest["package_sha256"],
                     "objects": [{"id": object_id, "roi": [10, 20, 12, 12], "anchor": [6, 6],
                                  "body_height_px": 12, "frame_index": 0,
                                  "template_frame_sha256": manifest["frames"][0]["sha256"], "method": "template"}],
                     "background_rois": []}
        resume_selection(destination, selection)
        return destination, source

    def draft(self, required=("open", "interrupt")):
        return suite.draft(self.output, "panel", "Transfer the panel's movement", list(required))

    def add(self, intake, scenario, behaviors, role="calibration", object_map=None):
        return suite.add_clip(self.output, intake, scenario, list(behaviors),
                              object_map or {"thing": "handle"}, role=role)

    def assert_code(self, expected, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)

    def test_draft_requires_supported_domain_and_versioned_plain_language_goal(self):
        result = self.draft()
        self.assertEqual(result["draft"]["schema_version"], 1)
        self.assertFalse(result["draft"]["model_started"])
        self.assertEqual(read_json(self.output / "state.json"), read_json(self.output / "revisions/000000.json"))
        self.assert_code("output_exists", self.draft)
        self.assert_code("unsupported_domain", suite.draft, self.root / "bad", "unknown", "Goal", ["open"])
        self.assert_code("unsupported_behavior", suite.draft, self.root / "bad2", "panel", "Goal", ["long_jump"])
        self.assert_code("invalid_goal", suite.draft, self.root / "bad3", "panel", " ", ["open"])

    def test_coverage_requests_long_jump_interrupt_reopen_and_drag_in_english(self):
        game = self.root / "game-suite"
        suite.draft(game, "game", "Transfer the jump", ["short_jump", "long_jump"])
        self.assertIn("long key press", " ".join(suite.coverage(game)["requests"]))
        self.draft(["interrupt", "reopen", "drag"])
        result = suite.coverage(self.output)
        self.assertEqual(result["missing_behaviors"], ["interrupt", "reopen", "drag"])
        self.assertIn("closed early", result["requests"][0])
        self.assertIn("reopened", result["requests"][1])
        self.assertIn("dragged and released", result["requests"][2])
        self.assertFalse(result["behavior_accepted"])

    def test_copies_pixels_selection_and_binding_but_never_target_source(self):
        self.draft(["open"])
        intake, _ = self.intake()
        before = source_hashes(self.target)
        result = self.add(intake, "open-01", ["open"])
        copied = self.output / "clips/open-01"
        self.assertTrue((copied / "package/frames/000000.png").is_file())
        self.assertTrue((copied / "selection.json").is_file())
        self.assertFalse(list(copied.rglob("*.js")))
        self.assertFalse(list(copied.rglob("*.gd")))
        self.assertFalse((copied / "intake.json").exists())
        self.assertEqual(source_hashes(self.target), before)
        self.assertFalse(result["clip"]["target_source_included"])
        self.assertEqual(result["coverage"]["missing_behaviors"], [])
        self.assertEqual(read_json(self.output / "state.json")["revision"], 1)
        self.assertEqual(read_json(self.output / "revisions/000000.json")["clips"], [])

    def test_unknown_inputs_request_explicit_binding_without_adding_clip(self):
        self.draft(["open"])
        intake, _ = self.intake(provenance="unknown")
        result = self.add(intake, "unknown-01", ["open"])
        self.assertEqual(result["status"], "needs_input_binding")
        self.assertFalse(result["clip_added"])
        self.assertFalse(result["inferred_events"])
        self.assertFalse(result["model_started"])
        self.assertFalse((self.output / "clips").exists())
        self.assertEqual(suite.coverage(self.output)["missing_behaviors"], ["open"])

    def test_same_pixels_cannot_be_relabelled_as_a_different_or_heldout_source(self):
        self.draft()
        first, package = self.intake()
        self.add(first, "open-01", ["open"])
        second, _ = self.intake(original_package=package)
        self.assert_code("duplicate_source", self.add, second, "interrupt-01", ["interrupt"], role="validation")
        self.assert_code("duplicate_scenario", self.add, second, "open-01", ["open"])
        self.assertEqual(len(suite.coverage(self.output)["clips"]), 1)

    def test_new_clip_requires_identical_mapping(self):
        self.draft()
        first, _ = self.intake()
        self.add(first, "open-01", ["open"])
        second, _ = self.intake(seed=2)
        self.assert_code("mapping_changed", self.add, second, "interrupt-01", ["interrupt"], object_map={"thing": "star"})
        self.assert_code("invalid_binding", self.add, second, "interrupt-02", ["interrupt"], object_map={"other": "handle"})

    def test_new_clip_requires_same_target_source_revision(self):
        self.draft()
        first, _ = self.intake()
        self.add(first, "open-01", ["open"])
        (self.target / "src/panel-motion.js").write_text("export function advanceMotion() { return 2; }")
        second, _ = self.intake(seed=2)
        self.assert_code("target_changed", self.add, second, "interrupt-01", ["interrupt"])

    def test_original_source_changed_after_intake_is_rejected_before_copy(self):
        self.draft(["open"])
        intake, _ = self.intake()
        (self.target / "src/panel-motion.js").write_text("export function advanceMotion() { return 2; }")
        self.assert_code("source_changed", self.add, intake, "open-01", ["open"])
        self.assertFalse((self.output / "clips/open-01").exists())

    def test_original_pixels_and_selection_binding_are_verified(self):
        self.draft(["open"])
        intake, _ = self.intake()
        selection = read_json(intake / "selection.json")
        selection["objects"][0]["template_frame_sha256"] = "0" * 64
        write_json(intake / "selection.json", selection)
        self.assert_code("selection_binding_mismatch", self.add, intake, "bad-selection", ["open"])
        selection["objects"][0]["template_frame_sha256"] = load_package(intake / "package")["frames"][0]["sha256"]
        write_json(intake / "selection.json", selection)
        (intake / "package/frames/000000.png").write_bytes(b"broken PNG")
        self.assert_code("package_integrity_failure", self.add, intake, "bad-pixels", ["open"])

    def test_partial_source_copy_is_preserved_but_never_registered_after_race(self):
        self.draft(["open"])
        intake, _ = self.intake()
        copier = suite.copy_package
        def change_source(source, destination):
            result = copier(source, destination)
            (self.target / "src/panel-motion.js").write_text("export function advanceMotion() { return 2; }")
            return result
        with patch.object(suite, "copy_package", side_effect=change_source):
            self.assert_code("source_changed", self.add, intake, "raced", ["open"])
        self.assertTrue((self.output / "clips/raced/package/frames/000000.png").is_file())
        self.assertEqual(read_json(self.output / "state.json")["clips"], [])

    def test_incomplete_coverage_cannot_freeze_or_invent_missing_clips(self):
        self.draft()
        result = suite.freeze(self.output)
        self.assertEqual(result["status"], "needs_reference_clips")
        self.assertFalse(result["frozen"])
        self.assertFalse((self.output / "suite.json").exists())

    def test_freeze_is_immutable_and_role_declarations_do_not_claim_behavior_success(self):
        self.draft()
        first, _ = self.intake()
        second, _ = self.intake(seed=2)
        self.add(first, "open-01", ["open"])
        self.add(second, "interrupt-01", ["interrupt"], role="validation")
        result = suite.freeze(self.output)
        before = (self.output / "suite.json").read_bytes()
        self.assertEqual(result["status"], "suite_ready_intake")
        self.assertTrue(result["frozen"])
        self.assertFalse(result["execution_ready"])
        self.assertFalse(result["behavior_accepted"])
        self.assertEqual(suite.freeze(self.output), result)
        self.assertEqual((self.output / "suite.json").read_bytes(), before)
        self.assert_code("suite_frozen", self.add, second, "extra", ["drag"])
        json.dumps(result, allow_nan=False)

    def test_model_start_marker_blocks_future_add_and_freeze_without_launching_anything(self):
        self.draft(["open"])
        intake, _ = self.intake()
        marker = suite.mark_model_started(self.output)
        self.assertTrue(marker["caller_reported"])
        self.assertEqual(suite.mark_model_started(self.output), marker)
        self.assert_code("model_already_started", self.add, intake, "open-01", ["open"])
        self.assert_code("model_already_started", suite.freeze, self.output)
        self.assertTrue(suite.coverage(self.output)["model_started"])

    def test_marking_started_keeps_frozen_suite_bytes_and_hash_unchanged(self):
        self.draft(["open"])
        intake, _ = self.intake()
        self.add(intake, "open-01", ["open"])
        suite.freeze(self.output)
        before = (self.output / "suite.json").read_bytes()
        marker = suite.mark_model_started(self.output)
        self.assertIsNotNone(marker["suite_sha256"])
        self.assertEqual((self.output / "suite.json").read_bytes(), before)

    def test_stored_clip_tampering_and_unexpected_source_code_are_detected(self):
        self.draft(["open"])
        intake, _ = self.intake()
        self.add(intake, "open-01", ["open"])
        directory = self.output / "clips/open-01"
        (directory / "source.js").write_text("unrequested target source")
        self.assert_code("clip_changed", suite.coverage, self.output)
        (directory / "source.js").unlink()
        (directory / "selection.json").write_text("{}")
        self.assert_code("clip_changed", suite.freeze, self.output)

    def test_path_escape_roles_and_state_edits_fail_closed(self):
        self.draft(["open"])
        intake, _ = self.intake()
        for name in ("../escape", "C:escape", "folder/name", ""):
            self.assert_code("unsafe_path", self.add, intake, name, ["open"])
        self.assert_code("invalid_role", self.add, intake, "bad-role", ["open"], role="hidden")
        self.assert_code("unsafe_path", self.add, self.output, "nested", ["open"])
        state = read_json(self.output / "state.json")
        state["object_map"] = {"thing": "star"}
        write_json(self.output / "state.json", state)
        self.assert_code("suite_changed", suite.coverage, self.output)


if __name__ == "__main__":
    unittest.main()
