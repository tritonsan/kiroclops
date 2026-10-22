"""Focused small closure seams; mocked gates do not claim real acceptance."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_web_final_closure as closure


class WebClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="kc web closure café ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session, self.suite, self.aid = (self.root / name for name in ("session", "suite", "aid"))
        for folder in (self.session, self.suite, self.aid):
            folder.mkdir()
        self.bound = self.root / "one-bound-input.txt"
        self.bound.write_text("immutable fixture pixels identity", encoding="utf-8")
        self.suite_value = {"source_epoch": "e" * 64, "acceptance_profile": "realistic-behavior-v1"}
        write_json(self.suite / "suite.json", self.suite_value)
        write_json(self.aid / "attestation.json", {"fixture": True})
        self.state = {"status": "finalized", "active_job_id": None, "reference": str(self.suite),
            "selected_candidate_id": "candidate-01", "final_job_id": "final", "clock_origin": "unchanged_initial_measurement",
            "candidates": [{"candidate_id": "candidate-01", "source_sha256": "a" * 64, "job_id": "evaluation"}]}
        self.row = {"stage": "svg", "status": "pending", "complete_matrix_verified": True, "errors": [],
            "selected_candidate": {"candidate_id": "candidate-01", "source_sha256": "a" * 64},
            "axes": {key: {"status": "pass"} for key in closure.MANDATORY}}
        self.row["axes"].update(improvement={"status": "diagnostic"}, independent_raw_review={"status": "pending"})
        self.row["axes"]["source"]["new_code_authored"] = False
        self.row["axes"]["native_origin"]["historical_only"] = True
        self.required = {("b" * 64, "c" * 64)}
        files = {str(self.bound): file_sha256(self.bound)}
        self.proof = {"kind": "IndependentFinalWebEnvelopeRawVerification", "schema_version": 1,
            "version": closure.raw.VERSION, "status": "pass", "numeric_status": "pass", "raw_only": True,
            "ordinary_reader_compatibility_claimed": False, "full_behavior_acceptance": False, "new_model_generation": False,
            "source_code_review_status": "pending", "native_model_generation_review_status": "pending",
            "verified_scopes": {"position": "pass", "events": "pass", "relationships": "pass"},
            "attestation_sha256": "f" * 64, "candidate_sha256": "a" * 64, "suite_sha256": digest(self.suite_value),
            "selected_candidate_id": "candidate-01", "evaluation_job_id": "evaluation", "final_job_id": "final",
            "record_count": 112, "session_directory": str(self.session), "raw_review_directory": str(self.aid), "domain": "svg",
            "required_observations": [list(pair) for pair in self.required], "consumed_files": files,
            "consumed_files_sha256": digest(files)}
        self.config = {"kind": "Phase3DV2WebFinalClosureInputs", "schema_version": 1,
            "stage": {"stage": "svg", "session": str(self.session), "source_axis": str(self.root / "source-axis"),
                      "unchanged_result": str(self.root / "unchanged")},
            "raw_review": str(self.aid), "raw_proof": str(self.root / "proof.json")}
        self.config_path = self.root / "inputs.json"
        self.write_inputs()
        self.output = self.root / "closure"

    def write_inputs(self):
        write_json(self.config_path, self.config)
        write_json(self.root / "proof.json", self.proof)

    def fixtures(self, fresh=None):
        stack = []
        for target, name, kwargs in ((closure.base, "_stage", {"side_effect": lambda *args: deepcopy(self.row)}),
                (closure.Session, "_load", {"side_effect": lambda *args: deepcopy(self.state)}),
                (closure.suite_evidence, "required_bindings", {"return_value": self.required}),
                (closure.raw, "validate", {"side_effect": lambda *args, **kwargs: deepcopy(self.proof if fresh is None else fresh)})):
            current = patch.object(target, name, **kwargs)
            stack.append(current)
            current.start()
            self.addCleanup(current.stop)
        return stack

    def panel(self):
        self.config["stage"] = {"stage": "panel", "session": str(self.session)}
        self.proof.update(domain="panel", record_count=128)
        self.row["stage"] = "panel"
        self.state["clock_origin"] = "model_generation"
        self.row["axes"]["source"] = {"status": "pending"}
        self.row["axes"]["native_origin"] = {"status": "pending"}
        self.write_inputs()

    def test_verified_svg_domain_closes_without_new_generation_or_global_claim(self):
        self.fixtures()
        summary = closure.build(self.config_path, self.output)
        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["domain_behavior_acceptance"])
        self.assertTrue(summary["historical_native_only"])
        for key in ("new_model_generation", "ordinary_reader_compatibility_claimed", "full_behavior_acceptance", "full_phase_acceptance"):
            self.assertFalse(summary[key])
        self.assertEqual(closure.validate(self.output), summary)
        self.assertTrue(read_json(self.output / "result.json")["code_snapshot"])

    def test_missing_full_proof_is_rejected_before_any_canonical_work(self):
        (self.root / "proof.json").unlink()
        with patch.object(closure.base, "_stage", side_effect=AssertionError("Must not enter canonical stage")):
            with self.assertRaises(VisualError) as caught:
                closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "incomplete_review")
        self.assertFalse(self.output.exists())

    def test_actual_attestation_is_required_before_replay(self):
        (self.aid / "attestation.json").unlink()
        self.fixtures()
        with self.assertRaises(VisualError):
            closure.build(self.config_path, self.output)
        self.assertFalse(self.output.exists())

    def test_fresh_validator_always_requires_attestation(self):
        self.fixtures()
        with patch.object(closure.raw, "validate", side_effect=VisualError("invalid_review", "Missing passing attestation")) as validator:
            with self.assertRaises(VisualError) as caught:
                closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "invalid_review")
        self.assertIs(validator.call_args.kwargs["require_attested"], True)

    def test_saved_proof_tamper_cannot_be_hidden_by_unchanged_fresh_proof(self):
        good = deepcopy(self.proof)
        self.proof["record_count"] = 113
        self.write_inputs()
        self.fixtures(fresh=good)
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "stale_raw_proof")

    def test_foreign_source_suite_jobs_or_incomplete_record_count_are_rejected(self):
        good = deepcopy(self.proof)
        for key, value in (("candidate_sha256", "d" * 64), ("suite_sha256", "d" * 64),
                           ("evaluation_job_id", "other"), ("final_job_id", "other"), ("record_count", 111)):
            with self.subTest(key=key):
                self.proof = {**good, key: value}
                self.write_inputs()
                stack = self.fixtures()
                try:
                    with self.assertRaises(VisualError) as caught:
                        closure.build(self.config_path, self.output)
                    self.assertEqual(caught.exception.code, "wrong_raw_binding")
                finally:
                    for current in reversed(stack):
                        current.stop()

    def test_wrong_observation_matrix_fails_even_with_same_record_count(self):
        self.proof["required_observations"] = [["d" * 64, "c" * 64]]
        self.write_inputs()
        self.fixtures()
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "incomplete_review")

    def test_duplicated_observation_identity_is_not_coverage(self):
        self.proof["required_observations"] *= 2
        self.write_inputs()
        self.fixtures()
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "incomplete_review")

    def test_raw_only_proof_cannot_transfer_source_or_native_verdict(self):
        self.proof["native_model_generation_review_status"] = "pass"
        self.write_inputs()
        self.fixtures()
        with self.assertRaises(VisualError):
            closure.build(self.config_path, self.output)

    def test_bound_input_tamper_is_rejected(self):
        self.bound.write_text("changed identity", encoding="utf-8")
        self.fixtures()
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "source_changed")

    def test_numeric_and_native_failure_are_not_promoted_by_raw_pass(self):
        self.panel()
        self.row["axes"]["behavior"] = {"status": "fail"}
        self.row["axes"]["native_origin"] = {"status": "fail", "canonical_audit": {"status": "fail", "code": "invalid_native_evidence"}}
        self.fixtures()
        summary = closure.build(self.config_path, self.output)
        actual = read_json(self.output / "result.json")
        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["domain_behavior_acceptance"])
        self.assertEqual(actual["stage"]["axes"]["native_origin"], self.row["axes"]["native_origin"])
        self.assertEqual(actual["stage"]["axes"]["independent_raw_review"]["status"], "pass")

    def test_pending_source_and_native_remain_pending(self):
        self.panel()
        self.fixtures()
        summary = closure.build(self.config_path, self.output)
        self.assertEqual(summary["status"], "pending")
        self.assertFalse(summary["domain_behavior_acceptance"])

    def test_svg_current_generation_is_out_of_scope(self):
        self.state["clock_origin"] = "model_generation"
        self.fixtures()
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "wrong_origin")

    def test_existing_output_refused_and_receipt_tamper_detected(self):
        self.fixtures()
        closure.build(self.config_path, self.output)
        with self.assertRaises(VisualError) as caught:
            closure.build(self.config_path, self.output)
        self.assertEqual(caught.exception.code, "output_exists")
        receipt = read_json(self.output / "receipt.json")
        receipt["domain_behavior_acceptance"] = False
        write_json(self.output / "receipt.json", receipt)
        with self.assertRaises(VisualError):
            closure.validate(self.output)

    def test_ordinary_raw_path_in_canonical_spec_is_refused(self):
        self.config["stage"]["final_raw_review"] = str(self.root / "fake-ordinary-review.json")
        self.write_inputs()
        self.fixtures()
        with self.assertRaises(VisualError):
            closure.build(self.config_path, self.output)


if __name__ == "__main__":
    unittest.main()
