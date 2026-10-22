"""Receipt routing with a mocked actual inspector; no real accepted review."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class FinalRawBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="final bridge unit ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"; self.session.mkdir()
        self.initial = self.root / "initial"; self.initial.mkdir()
        self.raw = self.root / "raw"; self.raw.mkdir()
        write_json(self.session / "session.json", {"unit_only": True})
        self.source = "a"*64
        self.state = {"selected_candidate_id": "chosen", "candidates": [{"candidate_id": "chosen", "source_sha256": self.source}]}
        self.result = {"sealed_source_sha256": self.source, "selected_candidate_id": "chosen", "seal_kind": "SealedSessionFinalRawMatrix"}
        # These opaque synthetic files stand in for a verifier's accepted evidence
        # only in this unit test. No attestation API or real image review is run.
        write_json(self.raw / "attestation.json", {"unit_only_mock": True})
        write_json(self.raw / "evidence-receipt.json", {"unit_only_mock": True})
        write_json(self.raw / "result.json", self.result)
        self.proof = {"kind": "VerifiedFinalRawMeasurementInspection", "status": "pass", "directory": str(self.raw),
            "attestation_sha256": file_sha256(self.raw / "attestation.json"), "result_sha256": digest(self.result),
            "matrix_sha256": "matrix-unit", "sealed_source_sha256": self.source,
            "required_observations": [["package-unit", "observations-unit"]], "record_count": 4,
            "source_code_review_status": "pending", "full_behavior_acceptance": False}
        self.session_patch = patch("kiroclops.local_engine.session.Session._load", side_effect=lambda: deepcopy(self.state))
        self.session_patch.start(); self.addCleanup(self.session_patch.stop)
        from kiroclops.local_engine import suite_final_raw_review
        self.real_final_validator = suite_final_raw_review.validate_review
        self.replay_patch = patch("kiroclops.local_engine.suite_final_raw_review.validate_review", side_effect=lambda *args, **kwargs: deepcopy(self.proof))
        self.replay = self.replay_patch.start(); self.addCleanup(self.replay_patch.stop)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def attach(self):
        path = self.session / "independent_review.json"
        value = review.attach_final_raw_review(self.raw, path, session_directory=self.session)
        return path, value

    def standalone(self):
        self.result.update(selected_candidate_id=None, seal_kind="UnchangedHistoricalSVGFinalRawMatrix")
        write_json(self.raw / "result.json", self.result)
        self.proof["result_sha256"] = digest(self.result)
        plan = {"unchanged_source_sha256": self.source}
        path = self.root / "initial-visual-review.json"
        with patch("kiroclops.local_engine.suite_initial_acceptance.load_plan", return_value=(plan, {})):
            review.attach_final_raw_review(self.raw, path, initial_acceptance_directory=self.initial)
        return path, plan

    def test_session_receipt_replays_exact_authority_without_legacy_tasks(self):
        path, value = self.attach()
        receipt = read_json(path)
        self.replay.assert_called_once_with(self.raw, session_directory=self.session)
        self.assertEqual(receipt["kind"], "IndependentFinalRawVisualReview")
        self.assertEqual(receipt["selected_candidate_id"], "chosen")
        self.assertEqual(receipt["candidate_sha256"], self.source)
        self.assertEqual(receipt["required_observations"], self.proof["required_observations"])
        self.assertFalse(any(key in receipt for key in ("position_reviews", "events_reviews", "relationships_reviews")))
        self.assertEqual(value["source_code_review_status"], "pending")
        self.assertFalse(value["full_behavior_acceptance"])
        replayed = review.validate_independent_review(path, candidate_sha256=self.source)
        self.assertEqual(replayed["verified_scopes"], {"position": "pass", "events": "pass", "relationships": "pass"})
        self.assertEqual(replayed["raw_validation"], self.proof)
        self.assertEqual(self.replay.call_count, 2)
        self.assertFalse(replayed["new_model_generation_claimed_by_bridge"])

    def panel_aid(self):
        from kiroclops.local_engine import suite_final_panel_raw_review as panel
        self.state['domain']='panel'
        self.result.update(kind='FinalOnlyUnsignedPanelReviewAid',schema_version=1,version=panel.VERSION,domain='panel')
        write_json(self.raw/'result.json',self.result);self.proof['result_sha256']=digest(self.result)

    def test_panel_uses_exact_helper_and_keeps_authoritative_session_binding(self):
        self.panel_aid()
        with patch('kiroclops.local_engine.suite_final_panel_raw_review.validate_review',return_value=self.proof) as inspector:
            path,value=self.attach();self.replay.assert_not_called()
            inspector.assert_called_once_with(self.raw,session_directory=self.session)
            self.assertEqual('pending',value['source_code_review_status']);self.assertFalse(value['full_behavior_acceptance'])
            review.validate_independent_review(path)
            receipt=read_json(path);receipt['session_directory']=str(self.root/'other');write_json(path,receipt)
            self.assert_code('wrong_final_seal',review.validate_independent_review,path)
            self.assertEqual(2,inspector.call_count)

    def test_unknown_panel_version_domain_or_game_session_cannot_fall_through(self):
        self.panel_aid()
        original=deepcopy(self.result)
        with patch('kiroclops.local_engine.suite_final_panel_raw_review.validate_review') as inspector:
            for mutation in ({'version':'future'}, {'domain':'svg'}, {'kind':'unknown-panel'}):
                write_json(self.raw/'result.json',{**original,**mutation})
                self.assert_code('wrong_final_seal',review.attach_final_raw_review,self.raw,self.session/'independent_review.json',session_directory=self.session)
            write_json(self.raw/'result.json',original);self.state['domain']='game'
            self.assert_code('wrong_final_seal',review.attach_final_raw_review,self.raw,self.session/'independent_review.json',session_directory=self.session)
            inspector.assert_not_called();self.replay.assert_not_called()

    def test_receipt_inside_session_automatically_rejects_another_session_same_source(self):
        path, _ = self.attach()
        other = self.root / "other-session"; other.mkdir()
        write_json(other / "session.json", {"unit_only": True})
        receipt = read_json(path); receipt["session_directory"] = str(other); write_json(path, receipt)
        self.replay.reset_mock()
        self.assert_code("wrong_final_seal", review.validate_independent_review, path, candidate_sha256=self.source)
        self.replay.assert_not_called()

    def test_explicit_session_cannot_override_receipt_containing_session(self):
        path, _ = self.attach()
        other = self.root / "other"; other.mkdir()
        self.replay.reset_mock()
        self.assert_code("wrong_final_seal", review.validate_independent_review, path, session_directory=other)
        self.replay.assert_not_called()

    def test_source_or_selected_id_drift_rejects_before_raw_replay(self):
        path, _ = self.attach()
        self.replay.reset_mock()
        self.assert_code("stale_review", review.validate_independent_review, path, candidate_sha256="b"*64)
        self.state["candidates"][0]["source_sha256"] = "b"*64
        self.assert_code("stale_review", review.validate_independent_review, path)
        self.state["candidates"][0]["source_sha256"] = self.source
        self.state["selected_candidate_id"] = "other"
        self.assert_code("stale_review", review.validate_independent_review, path)
        self.replay.assert_not_called()

    def test_replayed_source_matrix_result_attestation_and_coverage_bindings_must_match(self):
        path, _ = self.attach()
        original = read_json(path)
        mutations = {"matrix_sha256": "other", "raw_result_sha256": "other", "raw_attestation_sha256": "other",
                     "raw_evidence_receipt_sha256": "other", "required_observations": [["other", "obs"]], "record_count": 3}
        for key, value in mutations.items():
            with self.subTest(field=key):
                write_json(path, {**original, key: value})
                self.assert_code("stale_review", review.validate_independent_review, path)
        write_json(path, original)
        self.proof["sealed_source_sha256"] = "other"
        self.assert_code("stale_review", review.validate_independent_review, path)

    def test_pending_inspection_failure_cannot_create_a_receipt(self):
        self.replay.side_effect = VisualError("incomplete_visual_review", "unit inspection missing")
        path = self.session / "independent_review.json"
        self.assert_code("incomplete_visual_review", review.attach_final_raw_review, self.raw, path, session_directory=self.session)
        self.assertFalse(path.exists())

    def test_real_final_validator_rejects_missing_attestation_without_writing(self):
        self.replay.side_effect = self.real_final_validator
        (self.raw / "attestation.json").unlink()
        path = self.session / "independent_review.json"
        with patch("kiroclops.local_engine.suite_final_raw_review._context_session", return_value=({}, [])):
            self.assert_code("incomplete_visual_review", review.attach_final_raw_review, self.raw, path, session_directory=self.session)
        self.assertFalse(path.exists())

    def test_accepted_parent_still_requires_separate_source_review(self):
        from kiroclops.local_engine import media_suite
        source = self.session / "candidates/chosen/source"; source.mkdir(parents=True)
        self.source = digest({})
        self.state.update(experiment={"reference_mode": "media_suite", "stage": "jump"},
                          sealed_at="unit-seal", final={"status": "succeeded", "pass": True})
        self.state["candidates"][0]["source_sha256"] = self.source
        self.result["sealed_source_sha256"] = self.source
        write_json(self.raw / "result.json", self.result)
        self.proof.update(sealed_source_sha256=self.source, result_sha256=digest(self.result))
        self.attach()
        with self.assertRaises(OSError):
            media_suite.accepted_parent(self.session, "jump")
        self.assertEqual(self.replay.call_count, 2)
        self.assertFalse((self.session / "source_review.json").exists())

    def test_no_source_acceptance_or_fabricated_legacy_claim_can_be_attached(self):
        path, _ = self.attach(); original = read_json(path)
        for mutation in ({"source_code_review_status": "pass"}, {"full_behavior_acceptance": True}, {"position_reviews": []}):
            write_json(path, {**original, **mutation})
            self.assert_code("invalid_review", review.validate_independent_review, path)

    def test_attachment_is_exclusive_and_existing_receipt_is_preserved(self):
        path, _ = self.attach(); before = path.read_bytes()
        self.assert_code("output_exists", review.attach_final_raw_review, self.raw, path, session_directory=self.session)
        self.assertEqual(path.read_bytes(), before)

    def test_standalone_unchanged_svg_retains_no_new_candidate_and_requires_exact_run(self):
        path, plan = self.standalone()
        with patch("kiroclops.local_engine.suite_initial_acceptance.load_plan", return_value=(plan, {})):
            value = review.validate_independent_review(path, candidate_sha256=self.source, initial_acceptance_directory=self.initial)
            self.assertIsNone(value["selected_candidate_id"])
            self.assertEqual(value["seal_kind"], "UnchangedHistoricalSVGFinalRawMatrix")
            self.assertFalse(value["full_behavior_acceptance"])
            self.replay.assert_called_with(self.raw, initial_acceptance_directory=self.initial)
            other = self.root / "other-initial"; other.mkdir()
            self.assert_code("wrong_final_seal", review.validate_independent_review, path, initial_acceptance_directory=other)

    def test_standalone_receipt_cannot_be_consumed_as_selected_session_review(self):
        path, plan = self.standalone()
        inside = self.session / "independent_review.json"; write_json(inside, read_json(path))
        self.replay.reset_mock()
        self.assert_code("wrong_final_seal", review.validate_independent_review, inside, candidate_sha256=self.source)
        self.replay.assert_not_called()

    def test_standalone_attachment_cannot_modify_immutable_initial_evidence(self):
        self.assert_code("unsafe_path", review.attach_final_raw_review, self.raw, self.initial / "independent_review.json",
                         initial_acceptance_directory=self.initial)
        self.assertFalse((self.initial / "independent_review.json").exists())

    def test_minimal_new_style_pass_receipt_never_accepts(self):
        path = self.session / "independent_review.json"
        write_json(path, {"kind": "IndependentFinalRawVisualReview", "status": "pass", "candidate_sha256": self.source})
        self.assert_code("invalid_review", review.validate_independent_review, path)
        self.replay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
