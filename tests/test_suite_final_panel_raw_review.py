"""Small in-memory metric/receipt tests; no capture or real attestation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import suite_final_panel_raw_review as final
from kiroclops.visual.schema import VisualError, digest, write_json


class FinalPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cases = [{"case_id": role, "role": role, "purpose": "preservation" if role == "regression" else "primary", "stage": "panel"}
                      for role in ("calibration", "regression", "validation")]
        self.context = {"kind": "SealedSessionFinalRawMatrix", "domain": "panel", "cases": self.cases,
                        "directory": str(self.root), "selected_candidate_id": "chosen", "suite_sha256": "suite", "heldout_status": "provided"}
        self.rows = []
        for case in self.cases:
            for role in final.ROLES:
                package = self.root / (case["case_id"] + "-" + role)
                package.mkdir()
                self.rows.append({"id": package.name, "case_id": case["case_id"], "role": role,
                                  "domain": "panel", "stage": "panel", "split_role": case["role"], "purpose": case["purpose"],
                                  "package": package, "observations": {"package_sha256": package.name, "objects": [],
                                      "inputs": {"events": [{"time_s": .5, "action": "click"}]}},
                                  "events": {"events": [{"type": "motion_start", "time_interval_s": [.5, .51]}]}})

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_exact_three_split_four_role_matrix_cannot_omit_or_duplicate(self):
        final._matrix(self.context, self.rows)
        self.assert_error("incomplete_review", final._matrix, self.context, self.rows[:-1])
        self.assert_error("incomplete_review", final._matrix, self.context, self.rows[:-1] + [self.rows[0]])
        bad = deepcopy(self.rows)
        bad[0]["split_role"] = "validation"
        self.assert_error("incomplete_review", final._matrix, self.context, bad)

    def test_public_v3_point20_definition_and_first_palette_are_unchanged(self):
        contract = final._contract({"event_options": {"motion_policy": final.v3.legacy.raw_event_review.CONFIRMED, "rest_duration_s": .2}})
        self.assertEqual(contract["rest_duration_s"], .2)
        self.assertEqual(contract["max_event_uncertainty_s"], .1)
        self.assertEqual(final.ANCHOR_POLICY, "first_palette_mixture_v1")
        self.assert_error("unsupported_event_definition", final._contract, {"event_options": {"rest_duration_s": .25}})

    def test_only_proven_original_event_diagnostics_are_discounted(self):
        bindings = [{"record_id": role, "role": role} for role in final.ROLES]
        errors = [{"record_id": role, "code": "event_interval_disagreement"} for role in final.ROLES]
        errors += [{"record_id": "original", "code": "raw_body_geometry_unobservable"}, {"role": "original", "code": "position_review_failed"}]
        mandatory, diagnostics = final._partition_failures(errors, bindings, {"status": "pass"})
        self.assertEqual(diagnostics, [errors[1]])
        self.assertEqual(len(mandatory), 5)
        self.assertTrue(any(row.get("record_id") == "before" for row in mandatory))
        mandatory, diagnostics = final._partition_failures(errors, bindings, {"status": "incomplete"})
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(mandatory), 7)

    def test_projection_retains_complete_position_function_preservation_and_decisions(self):
        saved = {"pass": True, "score": 1., "comparisons": [{"new_gate": True, "original_pixel_comparison": {
            "event_checks": [1], "errors": [1], "success": True, "object_windows": [{"position_rmse": 3.}], "measurement_status": "pass"}}],
                 "parent_preservation": [{"pass": True}], "preservation": {"status": "pass"}}
        diagnostic = deepcopy(saved)
        diagnostic["comparisons"][0]["original_pixel_comparison"]["event_checks"] = [2]
        self.assertEqual(final._diagnostic_projection(saved), final._diagnostic_projection(diagnostic))
        for field in ("pass", "score", "parent_preservation", "preservation"):
            changed = deepcopy(saved)
            changed[field] = "changed"
            self.assertNotEqual(final._diagnostic_projection(saved), final._diagnostic_projection(changed))
        diagnostic["comparisons"][0]["original_pixel_comparison"]["object_windows"][0]["position_rmse"] = 4.
        self.assertNotEqual(final._diagnostic_projection(saved), final._diagnostic_projection(diagnostic))

    def proof(self, *, consumes=False, saved_mutation=None):
        def extract(observed, *args, **kwargs):
            return {"events": [{"type": "motion_start", "time_interval_s": [.5, .51]}], "relations": []}

        def compare(reference, original, selected, *args, **kwargs):
            old = final.comparison.extract_events(original["observations"])
            new = final.comparison.extract_events(selected["observations"])
            ref = final.comparison.extract_events(reference["observations"])
            # Expose a deliberate original-event consumer to the complete proof.
            score = len(old["events"]) if consumes else 1.
            return {"pass": True, "score": score, "functional": {"status": "pass"},
                    "pixel_comparison": {"event_checks": new["events"], "reference_events": ref["events"]},
                    "original_pixel_comparison": {"event_checks": old["events"], "errors": [], "success": True,
                                                  "object_windows": [{"position_rmse": 5.}], "measurement_status": "pass"}}

        state = {"candidates": [{"candidate_id": "chosen", "job_id": "caljob"}], "final_job_id": "finaljob"}
        grouped = {}
        for row in self.rows:
            grouped.setdefault(row["case_id"], {})[row["role"]] = row
        functional = {"status": "pass"}
        for job in ("caljob", "finaljob"):
            write_json(self.root / "measurements" / job / "functional.json", functional)
        with patch.object(final.comparison, "extract_events", side_effect=extract), \
             patch.object(final.media_suite, "_compare", side_effect=compare), \
             patch.object(final.media_suite, "_parent_acceptance", side_effect=lambda a, b, c, stage: compare(a, b, c)), \
             patch.object(final.Session, "_load", return_value=state), \
             patch.object(final, "_code_sources", return_value={"unit": "unit"}):
            metrics = {case["case_id"]: final._case_metrics(grouped[case["case_id"]], case) for case in self.cases}
            saved = {"caljob": final._aggregate(self.context, self.cases[:1], metrics, functional, final_job=False),
                     "finaljob": final._aggregate(self.context, self.cases[1:], metrics, functional, final_job=True)}
            if saved_mutation:
                saved_mutation(saved)
            with patch.object(final.evidence, "_completed_job", side_effect=lambda directory, state, job, operation, selected: saved[job]):
                return final.prove_metric_dependencies(self.context, self.rows)

    def test_complete_job_dependency_replay_and_every_original_interception(self):
        proof = self.proof()
        self.assertEqual(proof["status"], "pass")
        self.assertEqual(len(proof["trials"]), 6)
        self.assertEqual(proof["case_ids"], ["calibration", "regression", "validation"])
        self.assertTrue(all(len(row["intercepted_identities"]) == 3 for row in proof["trials"][3:]))
        self.assertFalse(proof["before_events_diagnostic_only"])

    def test_any_actual_original_event_consumer_blocks_exclusion(self):
        self.assertEqual(self.proof(consumes=True)["status"], "fail")

    def test_unreplayed_new_acceptance_field_cannot_be_silently_excluded(self):
        self.assert_error("metric_dependency_unproven", self.proof,
                          saved_mutation=lambda saved: saved["finaljob"].update(new_acceptance_field=True))

    def test_geometry_requires20distinct_images_each_role_object_using_map(self):
        bundles, controls = [], []
        for role in final.ROLES:
            name = "r-" + role
            bundles.append({"record": {"id": name, "role": role}, "mapping": {"logical": "handle"},
                            "raw": {"rows": [{"points": {"handle": {"geometry_bounds": [0, 0, 2, 2]}}} for _ in range(20)]}})
            controls.extend({"record_id": name, "role": role, "object_id": "logical", "index": index,
                             "image_sha256": str(index)} for index in range(20))
        values, errors = final._geometry_controls(bundles, {"controls": controls})
        self.assertEqual(len(values), 4)
        self.assertEqual(errors, [])
        controls[-1]["image_sha256"] = "0"
        self.assertEqual(len(final._geometry_controls(bundles, {"controls": controls})[1]), 1)

    def test_actual_all_sheet_inspection_required_and_proof_failure_cannot_waive(self):
        self.assert_error("invalid_review", final.attest_review, self.root / "absent", reviewer="unit",
                          inspected_sheets=[], accepted=True, images_inspected=False)
        result = {"sheets": [{"path": "one.png"}, {"path": "two.png"}], "numeric_status": "incomplete",
                  "failures": [{"code": "complete_metric_dependency_unproven"}], "unsupported": [], "metric_dependency_proof": {"status": "incomplete"}}
        with patch.object(final, "_verified_proposal", return_value=(self.root, result, {})):
            self.assert_error("incomplete_visual_review", final.attest_review, self.root, reviewer="unit",
                              inspected_sheets=["one.png"], accepted=True, images_inspected=True)
            self.assert_error("review_not_qualified", final.attest_review, self.root, reviewer="unit",
                              inspected_sheets=["one.png", "two.png"], accepted=True, images_inspected=True)
        self.assertFalse((self.root / "attestation.json").exists())


if __name__ == "__main__":
    unittest.main()
