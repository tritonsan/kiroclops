"""Small PNG/unit seals only; no capture, model, or accepted attestation."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from kiroclops.local_engine import suite_final_raw_review as final
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from tests.evidence import requires_evidence


class FinalRawReviewTests(unittest.TestCase):
    def setUp(self):
        # Reuse real small PNG artwork, without exposing its TestCase to discovery.
        from tests.test_suite_generic_raw_review import GenericRawReviewTests
        self.fixture = GenericRawReviewTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        for name in ("suite", "parent", "original", "sealed", "initial"):
            (self.root / name).mkdir()

    def context(self, cases, *, unchanged=True):
        return {"kind": "UnchangedHistoricalSVGFinalRawMatrix" if unchanged else "SealedSessionFinalRawMatrix",
            "version": final.VERSION, "directory": str(self.root / "initial"), "suite": str(self.root / "suite"),
            "suite_sha256": digest(cases), "domain": "svg", "stage": "svg",
            "sealed_source": str(self.root / "sealed"), "sealed_source_hashes": {}, "sealed_source_sha256": digest({}),
            "authoring_parent": str(self.root / "parent"), "authoring_parent_source_hashes": {},
            "original": str(self.root / "original"), "original_source_hashes": {},
            "selected_candidate_id": None if unchanged else "selected-01", "boundary_receipt": None,
            "cases": cases, "event_contract": {"event_options": {"rest_duration_s": .25}},
            "expected_roles": list(final.UNCHANGED_ROLES if unchanged else final.FINAL_ROLES),
            "behavior_numerical_pass": False, "heldout_status": "provided"}

    def matrix(self, *, varying=False, unchanged=True, bad_edge=False):
        cases = [{"case_id": "small-cal", "role": "calibration", "stage": "svg"}]
        if varying:
            cases += [{"case_id": "small-reg", "role": "regression", "stage": "svg"},
                      {"case_id": "small-val", "role": "validation", "stage": "svg"}]
        context = self.context(cases, unchanged=unchanged)
        rows = []
        for case in cases:
            for role in context["expected_roles"]:
                row = self.fixture.svg(role, bad_edge=bad_edge and role in ("selected", "unchanged"))
                if case["role"] == "regression":
                    # Object sets can differ by canonical case; each source role
                    # must still contain the same public objects for that case.
                    row["selection"]["objects"] = row["selection"]["objects"][:1]
                    row["selection"]["connections"] = []
                    row["observations"]["objects"] = row["observations"]["objects"][:1]
                    row["observations"]["connections"] = []
                    row["events"] = extract_events(row["observations"], row["selection"], context["event_contract"])
                    write_json(row["analysis"], {"observations": row["observations"], "events": row["events"]})
                row.update(case_id=case["case_id"], stage="svg", split_role=case["role"], capture_role=role)
                rows.append(row)
        return context, rows

    def run_review(self, context, rows, *, output=None, after=None):
        output = output or self.root / "review"
        def producer(context, records, inventory):
            return {"required_observations": sorted({
                (load_package(r["package"])["package_sha256"], digest(r["observations"])) for r in records}),
                "record_count": len(records), "source_binding_status": "pass", "source_code_review_status": "pending"}
        # This tests image-only review, not process receipt qualification. The
        # explicit producer seal mock cannot produce real acceptance evidence.
        with patch.object(final, "_after_raw", side_effect=after or producer), \
             patch.object(final, "_code_sources", side_effect=lambda: {str(Path(final.__file__)): file_sha256(final.__file__)}), \
             patch.object(final.evidence, "_record_observation", return_value=("unit", "unit")):
            return final._review(context, rows, output), output

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    @requires_evidence('artifacts/phase3a/tools')
    def test_all_splits_four_roles_and_varying_case_objects_remain_unsigned(self):
        context, rows = self.matrix(varying=True)
        result, output = self.run_review(context, rows)
        self.assertEqual(result["numeric_status"], "pass", result["failures"][:4])
        self.assertEqual(len(result["records"]), 12)
        self.assertEqual({r["role"] for r in result["records"]}, set(final.UNCHANGED_ROLES))
        self.assertEqual({r["case_id"] for r in result["records"]}, {c["case_id"] for c in context["cases"]})
        self.assertTrue(all(p["raw_distinct_images"] >= 20 for p in result["positions"]))
        for key in ("acceptance_granted", "full_behavior_acceptance", "visual_inspection_claimed", "new_model_generation_claimed_by_aid"):
            self.assertFalse(result[key])
        self.assertEqual(result["source_code_review_status"], "pending")
        self.assertFalse(result["behavior_numerical_pass"])
        self.assertIsNone(result["selected_candidate_id"])
        self.assertFalse((output / "attestation.json").exists())

    @requires_evidence('artifacts/phase3a/tools')
    def test_selected_final_matrix_retains_candidate_identity_and_numeric_failures(self):
        context, rows = self.matrix(unchanged=False, bad_edge=True)
        result, output = self.run_review(context, rows)
        self.assertEqual(result["numeric_status"], "fail")
        self.assertEqual(result["selected_candidate_id"], "selected-01")
        self.assertEqual({r["role"] for r in result["records"]}, set(final.FINAL_ROLES))
        self.assertTrue(any(f["code"] == "relation_unobservable_or_disagreement" for f in result["failures"]))
        self.assertFalse((output / "attestation.json").exists())

    def test_missing_role_case_or_game_wall_matrix_fails_before_pixels(self):
        context, rows = self.matrix()
        with patch.object(final.raw, "_raw_image") as marking:
            self.assert_code("incomplete_review", final._review, context, rows[:-1], self.root / "missing")
            duplicate = rows + [deepcopy(rows[0])]
            self.assert_code("incomplete_review", final._review, context, duplicate, self.root / "duplicate")
            game = {**context, "domain": "game", "stage": "jump"}
            self.assert_code("incomplete_review", final._review, game, rows, self.root / "wall-missing")
        marking.assert_not_called()

    @requires_evidence('artifacts/phase3a/tools')
    def test_all_raw_files_exist_before_any_producer_comparison(self):
        context, rows = self.matrix()
        output = self.root / "order"
        marking = final.raw._raw_image
        def after(context, records, inventory):
            self.assertEqual(spy.call_count, 96)
            self.assertTrue((output / "extraction-complete.json").is_file())
            self.assertTrue(all((output / "raw" / row["id"] / "points.json").is_file() for row in rows))
            return {"required_observations": [], "record_count": len(rows)}
        with patch.object(final.raw, "_raw_image", wraps=marking) as spy:
            self.run_review(context, rows, output=output, after=after)
            self.assertTrue(all(len(call.args) == 2 and call.args[1] == "svg" for call in spy.call_args_list))

    @requires_evidence('artifacts/phase3a/tools')
    def test_public_mapping_cannot_change_object_identity_across_sources(self):
        context, rows = self.matrix()
        rows[-1]["object_map"] = {"a": "b", "b": "a"}
        self.assert_code("incomplete_review", self.run_review, context, rows)

    def test_explicit_records_and_paths_must_equal_the_authoritative_seal(self):
        context, rows = self.matrix()
        options = {"suite_directory": context["suite"], "authoring_parent": context["authoring_parent"],
                   "sealed_source": context["sealed_source"], "initial_acceptance_directory": context["directory"]}
        with patch.object(final, "_context_unchanged", return_value=(context, rows)), \
             patch.object(final, "_review", return_value={"unit": True}) as running:
            self.assertEqual(final.review_final_records(rows, self.root / "explicit", **options), {"unit": True})
            self.assert_code("wrong_final_seal", final.review_final_records, rows, self.root / "wrong", **{**options, "sealed_source": context["original"]})
            changed = deepcopy(rows); changed[-1]["source_sha256"] = "other-source"
            self.assert_code("wrong_final_seal", final.review_final_records, changed, self.root / "changed", **options)
            collected = final.collect_records(initial_acceptance_directory=context["directory"])
            self.assertEqual(collected["records"], rows)
            self.assertEqual(collected["seal_kind"], context["kind"])
            running.assert_called_once()
        self.assert_code("incomplete_review", final.collect_records)

    def test_session_collector_preserves_candidate_vs_final_capture_roles(self):
        directory = self.root / "session"; directory.mkdir()
        sealed = directory / "candidates/chosen/source"; sealed.mkdir(parents=True)
        cases = [{"case_id": "case-"+split, "role": split, "stage": "svg",
                  "scenario": {"id": "case-"+split}} for split in ("calibration", "regression", "validation")]
        suite = {"domain": "svg", "stage": "svg", "original": "original", "original_source_hashes": {},
                 "authoring_parent_source_hashes": {}, "heldout_status": "provided", "cases": cases}
        state = {"domain": "svg", "reference": str(self.root / "suite"), "baseline": str(self.root / "parent"),
            "experiment": {"reference_mode": "media_suite", "suite_sha256": digest(suite)},
            "selected_candidate_id": "chosen", "prepare_job_id": "prepare-1", "final_job_id": "final-1",
            "sealed_at": "unit-seal", "final": {"status": "succeeded", "pass": False},
            "candidates": [{"candidate_id": "chosen", "job_id": "evaluate-1", "eligible": True,
                            "status": "succeeded", "source_sha256": digest({})}]}
        def indexed(path, cases, *, complete):
            return [({"package": str(self.root / "unused-package"), "scenario": case["scenario"]}, case) for case in cases]
        with patch.object(final.Session, "_load", return_value=state), \
             patch.object(final.media_suite, "load_suite", return_value=suite), \
             patch.object(final.suite_evidence, "_source_expectation", return_value={"kind": "web", "sources": {}}), \
             patch.object(final.suite_evidence, "_records", side_effect=indexed):
            context, rows = final._context_session(directory)
        self.assertEqual(len(rows), 12)
        self.assertEqual({(r["role"], r["case_id"]) for r in rows}, {(role, case["case_id"]) for role in final.FINAL_ROLES for case in cases})
        selected = [r for r in rows if r["role"] == "selected"]
        self.assertEqual([r["capture_role"] for r in selected], ["candidate", "final", "final"])
        self.assertEqual(context["sealed_source"], str(sealed))
        self.assertFalse(context["behavior_numerical_pass"])

    def test_after_raw_binds_actual_job_filenames_and_native_transport_inventory(self):
        context, rows = self.matrix(unchanged=False)
        state = {"prepare_job_id": "prepare-1", "final_job_id": "final-1", "candidates": [
            {"candidate_id": context["selected_candidate_id"], "job_id": "evaluate-1"}]}
        directory = Path(context["directory"])
        for name in ("session.json", "binding.json", "kiro/stdout.jsonl"):
            path = directory / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("unit-only")
        for job in ("prepare-1", "evaluate-1", "final-1"):
            for name in ("job.json", "binding.json", "result.json"):
                path = directory / "jobs" / job / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("unit-only")
        for name in ("suite.json", "suite-binding.json"):
            (self.root / "suite" / name).write_text("unit-only")
        required = {(load_package(r["package"])["package_sha256"], digest(r["observations"])) for r in rows}
        inventory = {}
        # Only producer/seal verification is mocked; all files and source bytes
        # added to the inventory are read for real in the post-extraction stage.
        with patch.object(final.Session, "_load", return_value=state), \
             patch.object(final.suite_evidence, "required_bindings", return_value=required), \
             patch("kiroclops.local_engine.review.audit_native_run", return_value={"status": "pass", "evidence_hashes": {
                 "kiro/stdout.jsonl": file_sha256(directory / "kiro/stdout.jsonl")}}), \
             patch.object(final.media_suite, "load_suite", return_value=context["cases"]):
            value = final._after_raw(context, [{"record": row} for row in rows], inventory)
        self.assertEqual(value["record_count"], 4)
        for relative in ("binding.json", "jobs/prepare-1/binding.json", "jobs/evaluate-1/binding.json", "jobs/final-1/binding.json", "kiro/stdout.jsonl"):
            self.assertEqual(inventory[str(directory / relative)], file_sha256(directory / relative))
        self.assertEqual(value["source_code_review_status"], "pending")

    def test_attestation_requires_explicit_actual_inspection(self):
        self.assert_code("invalid_review", final.attest_review, self.root / "no-review", reviewer="unit", inspected_sheets=[], accepted=True)
        self.assertFalse((self.root / "no-review/attestation.json").exists())

    def test_read_only_validation_requires_a_real_separate_attestation(self):
        context, rows = self.matrix()
        with patch.object(final, "_context_unchanged", return_value=(context, rows)):
            self.assert_code("incomplete_visual_review", final.validate_review, self.root / "absent",
                             initial_acceptance_directory=context["directory"])

    def test_current_binding_reconstruction_retains_role_case_events_and_sources(self):
        context, rows = self.matrix()
        actual = final._current_record_bindings(rows)
        self.assertEqual({r["role"] for r in actual}, set(final.UNCHANGED_ROLES))
        self.assertEqual({r["case_id"] for r in actual}, {"small-cal"})
        self.assertEqual(actual[-1]["source_sha256"], rows[-1]["source_sha256"])
        self.assertEqual(actual[-1]["events_sha256"], digest(rows[-1]["events"]))
        self.assertEqual(actual[-1]["object_map"], {"a": "a", "b": "b"})
        changed = deepcopy(rows); changed[-1]["capture_role"] = "candidate"
        self.assertNotEqual(actual, final._current_record_bindings(changed))
        self.assertTrue(all(isinstance(row["package"], str) for row in rows))

    def test_wall_detector_uses_only_independent_vertical_pixel_contrast(self):
        image = np.full((180, 320, 3), 30, np.uint8)
        image[20:151, 250:256] = 220
        result = final._wall(image, "right")
        self.assertEqual((result["x"], result["top"], result["bottom"]), (249.5, 20, 150))
        self.assertEqual(result["method"], "vertical_rgb_contrast_runs")
        left = final._wall(np.flip(image, axis=1), "left")
        self.assertEqual(left["x"], 69.5)
        self.assertEqual(final._wall(np.full_like(image, 30), "right")["status"], "unobservable")
        short = np.full_like(image, 30); short[20:74, 250:256] = 220
        self.assertEqual(final._wall(short, "right")["status"], "unobservable")
        short[74, 250:256] = 220
        self.assertEqual(final._wall(short, "right")["status"], "unsigned")

    def boundary(self, *, uncertainty=0):
        rows, frames, metadata = [], [], []
        for i in range(85):
            x = 200 if i < 30 else 200+3*(i-30) if i < 45 else 242 if i < 55 else 242-3*(i-55) if i < 71 else 197
            rows.append({"index": i, "time_s": i/60, "points": {"character": {"x": float(x), "y": 120.}},
                         "wall": {"x": 249.5, "top": 20., "bottom": 159.}})
            frames.append({"index": i, "time_s": i/60, "valid": True, "x": float(x), "y": 120.,
                           "left": x-3.5, "top": 116., "width": 8., "height": 6.})
            metadata.append({"index": i, "time_s": i/60, "time_interval_s": [i/60-uncertainty, i/60+uncertainty]})
        selection = {"objects": [{"id": "character", "roi": [0, 0, 8, 6], "anchor": [3.5, 4], "body_height_px": 16}],
                     "boundaries": [{"id": "wall", "points": [[249.5, 20.], [249.5, 159.]]}]}
        observed = {"package_sha256": "unit", "objects": [{"id": "character", "frames": frames}],
                    "inputs": {"provenance": "recorder_verified", "events": []}}
        produced = extract_events(observed, selection, {"event_options": {"boundary_tolerance_px": 12.}})
        analysis = self.root / "boundary-class.json"
        write_json(analysis, {"status": "observed", "classification": "visible_departure_after_approach"})
        return {"record": {"id": "wall-unit", "analysis": str(analysis)}, "mapping": {"character": "character"},
                "selection": selection, "rows": rows, "event_set": produced, "manifest": {"frames": metadata}}

    def test_boundary_raw_geometry_and_existing_classification_gates_agree(self):
        bundle = self.boundary()
        result, failures = final._boundary_events(bundle, final.raw._public_contract({}, "game"))
        self.assertEqual(failures, [])
        self.assertEqual(result["classification"], "visible_departure_after_approach")
        self.assertEqual(len(result["relation_checks"]), 85)
        self.assertFalse(result["physics_collision_inferred"])
        self.assertEqual({c["type"] for c in result["event_checks"]}, {"boundary_enter", "boundary_leave"})

    def test_boundary_two_pixel_geometry_and_100ms_uncertainty_are_not_loosened(self):
        bundle = self.boundary()
        bundle["event_set"]["relations"][0]["distance_px"] += 3.
        _, failures = final._boundary_events(bundle, final.raw._public_contract({}, "game"))
        self.assertTrue(any(f["code"] == "raw_boundary_distance_disagreement" for f in failures))
        uncertain = self.boundary(uncertainty=.08)
        _, failures = final._boundary_events(uncertain, final.raw._public_contract({}, "game"))
        self.assertTrue(any(f["code"] == "raw_boundary_event_interval_disagreement" for f in failures))

    def test_boundary_missing_relation_or_unobservable_pixels_fail(self):
        bundle = self.boundary()
        bundle["event_set"]["relations"] = bundle["event_set"]["relations"][:-1]
        _, failures = final._boundary_events(bundle, final.raw._public_contract({}, "game"))
        self.assertTrue(any(f["code"] == "raw_boundary_required_relation_missing" for f in failures))
        duplicate = self.boundary()
        duplicate["event_set"]["relations"][-1] = deepcopy(duplicate["event_set"]["relations"][0])
        _, failures = final._boundary_events(duplicate, final.raw._public_contract({}, "game"))
        self.assertTrue(any(f["code"] == "raw_boundary_required_relation_missing" for f in failures))
        bundle = self.boundary(); bundle["rows"][40]["wall"] = {"status": "unobservable"}
        _, failures = final._boundary_events(bundle, final.raw._public_contract({}, "game"))
        self.assertTrue(any(f["code"] == "raw_boundary_unobservable" for f in failures))


if __name__ == "__main__":
    unittest.main()
