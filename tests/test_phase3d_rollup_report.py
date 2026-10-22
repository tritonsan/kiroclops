"""Small synthetic receipts only; no capture, analysis, or live-session access."""
import json
import hashlib
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase3d_rollup_report as report
from tests.evidence import requires_evidence


class RollupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "evidence"
        self.root.mkdir()
        self.output = Path(temporary.name) / "report"

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def final_fixture(self, *, folder="s/gj04", preparation="p/gj04"):
        self.write(preparation + "/result.json", {"status": "ready_for_raw_review"})
        hashes = {"main.py": "a" * 64}
        source = report._digest(hashes)
        result = {"pass": True}
        self.write(folder + "/jobs/final1/result.json", result)
        self.write(folder + "/jobs/final1/job.json", {
            "job_id": "final1", "operation": "finalize", "status": "succeeded", "candidate_id": "c1",
            "source_hashes": hashes, "source_sha256": source, "result_sha256": report._digest(result)})
        state = {"selected_candidate_id": "c1", "sealed_at": 10, "started_at": 1,
                 "final_job_id": "final1", "status": "finalized",
                 "experiment": {"suite_sha256": "suite"},
                 "candidates": [{"candidate_id": "c1", "source_sha256": source}],
                 "final": {"job_id": "final1", "status": "succeeded", "pass": True}}
        self.write(folder + "/session.json", state)
        self.write(folder + "/admission.json", {"kind": "ReferenceSuiteAdmission", "status": "pass",
                   "suite_sha256": "suite", "model_started": False, "behavior_acceptance_granted": False})
        self.write(folder + "/environment-readiness.json", {"measurement_revision_required": False})
        saved = {"kind": "LocalEvidenceReport", "selected_candidate_id": "c1", "candidate_sha256": source,
                 "numerical": state["final"], "overall_acceptance": "pass", "native_run_status": "pass",
                 "independent_review_status": "pass", "review_coverage_status": "pass", "source_review_status": "pass",
                 "acceptance_axes": {"observed_behavior": {"status": "pass"}, "application_integrity": {"status": "pass"}}}
        self.write(folder + "/report/report.json", saved)
        return state, saved

    def test_missing_and_partial_live_files_remain_pending(self):
        broken = self.root / "s/gj04/session.json"
        broken.parent.mkdir(parents=True)
        broken.write_text('{"status":', encoding="utf-8")
        data = report.collect(self.root)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["game_run"]["axes"]["numeric_behavior"]["status"], "pending")
        self.assertEqual(len(data["warnings"]), 1)

    def test_panel_admission_does_not_accept_41_original_event_failures(self):
        self.write("p/p02/result.json", {"status": "ready_for_raw_review"})
        raw = {"version": "v3", "numeric_status": "fail", "sheets": [{"path": "one.png"}],
               "failures": [{"code": "event_interval_disagreement", "record_id": "original-case"}] * 41,
               "admission": {"status": "pass", "original_event_diagnostics": [{"code": "event"}] * 41}}
        self.write("p/p02/raw-cal-03/result.json", raw)
        self.write("p/p02/raw-cal-03/admission-attestation.json", {
            "status": "pass", "admitted": True, "images_inspected": True,
            "result_sha256": report._digest(raw), "whole_raw_review_accepted": False,
            "original_event_measurements_qualified": False})
        data = report.collect(self.root)
        row = data["raw_reviews"][0]
        self.assertEqual((row["whole_raw_review"], row["scoring_admission"], row["failure_count"]), ("fail", "pass", 41))
        self.assertFalse(row["original_event_measurements_qualified"])
        self.assertEqual(data["panel_progression"]["measurement_admission"]["status"], "pass")
        self.assertEqual(data["panel_progression"]["full_acceptance"]["status"], "pending")
        self.assertEqual(data["game_run"]["axes"]["numeric_behavior"]["status"], "pending")

    def test_raw_pass_and_pool_pass_do_not_promote_behavior(self):
        raw = {"numeric_status": "pass", "failures": [], "unsupported": [], "sheets": [{"path": "one.png"}]}
        self.write("p/gj03/raw-cal-02/result.json", raw)
        self.write("p/gj03/raw-cal-02/attestation.json", {"status": "pass", "accepted": True, "images_inspected": True,
                   "result_sha256": report._digest(raw), "inspected_sheets": raw["sheets"]})
        self.write("q/pool01-qualification.json", {"status": "pass"})
        data = report.collect(self.root)
        self.assertEqual(data["raw_reviews"][0]["whole_raw_review"], "pass")
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pending")
        self.assertFalse(data["full_phase3d_acceptance_granted"])

    def test_separate_final_gates_and_current_session_binding(self):
        _, saved = self.final_fixture()
        data = report.collect(self.root)
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pass")
        self.assertEqual(data["status"], "partial")  # No panel final acceptance.
        saved["candidate_sha256"] = "other"
        self.write("s/gj04/report/report.json", saved)
        self.assertEqual(report.collect(self.root)["game_run"]["axes"]["full_acceptance"]["status"], "pending")

    def test_function_failure_blocks_full_acceptance(self):
        _, saved = self.final_fixture()
        saved["acceptance_axes"]["application_integrity"]["status"] = "fail"
        self.write("s/gj04/report/report.json", saved)
        self.assertEqual(report.collect(self.root)["game_run"]["axes"]["full_acceptance"]["status"], "fail")

    def test_interruption_overrides_stale_running_and_preserves_recovery(self):
        self.final_fixture()
        self.write("s/gj04/kiro/launch.json", {"status": "running", "prompt_sent": True})
        self.write("s/gj04/operator-interruption.json", {"status": "operator_interrupted", "candidate_count": 0})
        self.write("s/gj04/operator-terminal-cleanup.json", {"status": "pass"})
        self.final_fixture(folder="s/gj05", preparation="p/gj05")
        data = report.collect(self.root, recovery_session=self.root / "s/gj05", recovery_preparation=self.root / "p/gj05")
        self.assertEqual(data["game_run"]["axes"]["native_run"]["status"], "operator_interrupted")
        self.assertEqual(data["game_run"]["axes"]["numeric_behavior"]["status"], "not_evaluated")
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pending")
        self.assertEqual(data["recovery_run"]["axes"]["full_acceptance"]["status"], "pass")
        page = report.render(data, self.output)
        self.assertIn("operator_interrupted", page)
        self.assertIn("Separately registered recovery", page)

    def test_only_existing_explicit_media_links_and_html_escaping(self):
        _, saved = self.final_fixture()
        video = self.root / "s/gj04/report/media/one.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"fixture")
        saved["groups"] = [{"id": "<script>alert(1)</script>", "roles": {"final": {"video": "media/one.mp4"}, "before": {"video": "missing.mp4"}}}]
        self.write("s/gj04/report/report.json", saved)
        # A recursive search would fail this test; the builder uses named paths.
        with patch.object(Path, "rglob", side_effect=AssertionError("No recursive artifact scanning")), patch.object(Path, "glob", side_effect=AssertionError("No glob artifact scanning")):
            data = report.build(self.output, self.root)
        self.assertEqual(len(data["game_run"]["media"]), 1)
        page = (self.output / "index.html").read_text()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertTrue((self.output / "status.json").is_file())
        with self.assertRaises(ValueError):
            report.build(self.output, self.root)
        with self.assertRaises(ValueError):
            report.build(self.root / "s/new-report", self.root)

    def test_reads_data_report_and_only_job_ids_declared_by_session(self):
        state, saved = self.final_fixture()
        path = self.root / "s/gj04/report/report.json"
        path.unlink()
        self.write("s/gj04/report/data.json", saved)
        state["active_job_id"] = "../../escape"
        self.write("s/gj04/session.json", state)
        job_path = self.root / "s/gj04/jobs/final1/job.json"
        legacy = json.loads(job_path.read_text())
        job_path.unlink()
        self.write("s/gj04/jobs/jobs/final1/state.json", legacy)
        self.write("s/gj04/jobs/jobs/final1/result.json", {"pass": True})
        data = report.collect(self.root)
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pass")
        self.assertEqual(data["game_run"]["jobs"], [{"job_id": "final1", "status": "succeeded", "operation": "finalize"}])

    def test_timeout_without_candidate_is_execution_failure_and_not_evaluated(self):
        self.write("s/gj05/session.json", {
            "status": "prepared", "started_at": 1, "candidates": [], "final": None,
            "prepare_job_id": "prepare1", "selected_candidate_id": None})
        self.write("s/gj05/kiro/launch.json", {
            "status": "timeout", "native_turn_status": "timeout", "prompt_sent": True,
            "finished_at": 1801, "error": "Bounded session deadline.",
            "terminal_cleanup": {"status": "pass"}})
        self.write("s/gj05/jobs/prepare1/job.json", {"status": "succeeded", "operation": "prepare"})
        data = report.collect(self.root, recovery_session=self.root / "s/gj05",
                              recovery_preparation=self.root / "p/gj04")
        axes = data["recovery_run"]["axes"]
        self.assertEqual(axes["native_run"]["status"], "timeout")
        self.assertEqual(axes["native_run"]["recorded_terminal_cleanup_status"], "pass")
        self.assertEqual(axes["numeric_behavior"]["status"], "not_evaluated")
        self.assertEqual(axes["numeric_behavior"]["candidate_count"], 0)
        self.assertEqual(axes["full_acceptance"]["status"], "fail")
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["recovery_run"]["jobs"], [
            {"job_id": "prepare1", "status": "succeeded", "operation": "prepare"}])
        page = report.render(data, self.output)
        self.assertIn("timeout", page)
        self.assertIn("not_evaluated", page)
        self.assertFalse(data["full_phase3d_acceptance_granted"])

    def test_recorded_timeout_overrides_stale_native_pass_without_failing_behavior(self):
        self.final_fixture()
        self.write("s/gj04/kiro/launch.json", {
            "status": "timeout", "native_turn_status": "completed", "finished_at": 1801})
        data = report.collect(self.root)
        axes = data["game_run"]["axes"]
        self.assertEqual(axes["native_run"]["status"], "timeout")
        self.assertEqual(axes["numeric_behavior"]["status"], "pass")
        self.assertEqual(axes["full_acceptance"]["status"], "fail")

    def test_cancelled_execution_without_candidate_is_not_behavior_failure(self):
        self.write("s/gj04/session.json", {"started_at": 1, "candidates": [], "final": None})
        self.write("s/gj04/kiro/launch.json", {"status": "cancelled", "finished_at": 2})
        axes = report.collect(self.root)["game_run"]["axes"]
        self.assertEqual(axes["native_run"]["status"], "cancelled")
        self.assertEqual(axes["numeric_behavior"]["status"], "not_evaluated")
        self.assertEqual(axes["full_acceptance"]["status"], "fail")

    def test_collect_reads_actual_panel_run_and_current_job_paths(self):
        self.write("p/p04/result.json", {"status": "ready_for_raw_review"})
        self.write("s/p04/session.json", {
            "status": "prepared", "started_at": 1, "final": None, "selected_candidate_id": None,
            "prepare_job_id": "prepare1", "active_job_id": "eval1",
            "candidates": [{"candidate_id": "candidate-01", "job_id": "eval1", "status": "queued"}]})
        self.write("s/p04/kiro/launch.json", {"status": "running", "prompt_sent": False})
        self.write("s/p04/jobs/prepare1/job.json", {"status": "succeeded", "operation": "prepare"})
        self.write("s/p04/jobs/eval1/job.json", {"status": "running", "operation": "evaluate"})
        data = report.collect(self.root)
        self.assertEqual(data["panel_run"]["session"], str(self.root / "s/p04"))
        self.assertEqual(data["panel_progression"]["native_run"]["status"], "running")
        self.assertEqual(data["panel_progression"]["numeric_behavior"]["status"], "pending")
        self.assertEqual(data["panel_progression"]["numeric_behavior"]["candidate_count"], 1)
        self.assertEqual(data["panel_run"]["jobs"], [
            {"job_id": "eval1", "status": "running", "operation": "evaluate"},
            {"job_id": "prepare1", "status": "succeeded", "operation": "prepare"}])
        self.assertEqual(data["panel_progression"]["full_acceptance"]["status"], "pending")
        self.assertIn("Job eval1", report.render(data, self.output))

    def test_current_job_record_precedes_historical_layout(self):
        self.final_fixture()
        self.write("s/gj04/jobs/final1/job.json", {"status": "running", "operation": "finalize"})
        self.write("s/gj04/jobs/jobs/final1/state.json", {"status": "succeeded", "operation": "finalize"})
        jobs = report.collect(self.root)["game_run"]["jobs"]
        self.assertEqual(jobs, [{"job_id": "final1", "status": "running", "operation": "finalize"}])
        self.assertEqual(report.collect(self.root)["game_run"]["axes"]["numeric_behavior"]["status"], "pending")

    def test_completed_authoring_turn_is_separate_from_audit_and_live_finalization(self):
        self.write("s/p04/session.json", {
            "status": "sealed", "started_at": 1, "sealed_at": 10, "selected_candidate_id": "c1",
            "final_job_id": "final1", "active_job_id": "final1", "final": None,
            "candidates": [{"candidate_id": "c1", "source_sha256": "source"}]})
        self.write("s/p04/kiro/launch.json", {
            "status": "completed", "finished_at": None, "native_turn_status": None,
            "prompt_sent": False, "response": {"stopReason": "end_turn"}, "response_received_at": 20})
        self.write("s/p04/jobs/final1/job.json", {"status": "running", "operation": "finalize"})
        data = report.collect(self.root)
        axes = data["panel_run"]["axes"]
        self.assertEqual(axes["native_run"]["status"], "completed")
        self.assertEqual(axes["native_evidence_audit"]["status"], "pending")
        self.assertEqual(axes["numeric_behavior"]["status"], "pending")
        self.assertEqual(axes["full_acceptance"]["status"], "pending")
        self.assertFalse(data["full_phase3d_acceptance_granted"])
        self.assertIn("native evidence audit", report.render(data, self.output))

    def test_completed_flag_without_recorded_end_turn_does_not_claim_completed_turn(self):
        for launch in ({"status": "completed"},
                       {"status": "completed", "response": {"stopReason": "unknown"}, "response_received_at": 20},
                       {"status": "completed", "response": {"stopReason": "end_turn"}}):
            self.assertEqual(report._native_status(launch, {}, False, True), "pending")

    def test_final_pointer_and_selected_candidate_must_bind_current_finalize_job(self):
        for field, bad in (("final_job_id", "other"), ("selected_candidate_id", "other")):
            with self.subTest(field=field):
                state, _ = self.final_fixture()
                state[field] = bad
                self.write("s/gj04/session.json", state)
                axes = report.collect(self.root)["game_run"]["axes"]
                self.assertEqual(axes["numeric_behavior"]["status"], "invalid")
                self.assertNotEqual(axes["full_acceptance"]["status"], "pass")

    def test_final_job_identity_operation_and_source_mismatches_are_invalid(self):
        for field, bad in (("job_id", "other"), ("candidate_id", "other"), ("operation", "evaluate"),
                           ("source_sha256", "other"), ("source_hashes", {"main.py": "b" * 64})):
            with self.subTest(field=field):
                self.final_fixture()
                path = self.root / "s/gj04/jobs/final1/job.json"
                job = json.loads(path.read_text())
                job[field] = bad
                self.write("s/gj04/jobs/final1/job.json", job)
                axes = report.collect(self.root)["game_run"]["axes"]
                self.assertEqual(axes["numeric_behavior"]["status"], "invalid")
                self.assertEqual(axes["native_evidence_audit"]["status"], "pending")

    def test_final_result_hash_and_boolean_verdict_must_match_saved_summary(self):
        for result in ({"pass": False}, {"pass": 1}, {"pass": True, "changed": True}):
            with self.subTest(result=result):
                self.final_fixture()
                self.write("s/gj04/jobs/final1/result.json", result)
                self.assertEqual(report.collect(self.root)["game_run"]["axes"]["numeric_behavior"]["status"], "invalid")

    def test_missing_final_job_or_result_stays_pending(self):
        for name in ("job.json", "result.json"):
            with self.subTest(name=name):
                self.final_fixture()
                (self.root / "s/gj04/jobs/final1" / name).unlink()
                axes = report.collect(self.root)["game_run"]["axes"]
                self.assertEqual(axes["numeric_behavior"]["status"], "pending")
                self.assertEqual(axes["native_evidence_audit"]["status"], "pending")

    def test_invalid_current_job_never_falls_back_to_historical_success(self):
        self.final_fixture()
        path = self.root / "s/gj04/jobs/final1/job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":', encoding="utf-8")
        self.write("s/gj04/jobs/jobs/final1/state.json", {"status": "succeeded", "operation": "finalize"})
        data = report.collect(self.root)
        self.assertEqual(data["game_run"]["jobs"], [{"job_id": "final1", "status": "pending", "operation": None}])
        self.assertEqual(len(data["warnings"]), 1)

    def test_panel_failure_contributes_to_rollup_without_changing_game_verdict(self):
        self.final_fixture()
        self.write("s/p04/session.json", {"started_at": 1, "candidates": [], "final": None})
        self.write("s/p04/kiro/launch.json", {"status": "timeout", "finished_at": 1801})
        data = report.collect(self.root)
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pass")
        self.assertEqual(data["panel_progression"]["native_run"]["status"], "timeout")
        self.assertEqual(data["panel_progression"]["numeric_behavior"]["status"], "not_evaluated")
        self.assertEqual(data["status"], "fail")

    def test_even_saved_game_and_panel_passes_never_grant_full_phase3d(self):
        self.final_fixture()
        self.final_fixture(folder="s/p04", preparation="p/p04")
        data = report.collect(self.root)
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pass")
        self.assertEqual(data["panel_progression"]["full_acceptance"]["status"], "pass")
        self.assertEqual(data["status"], "partial")
        self.assertFalse(data["full_phase3d_acceptance_granted"])

    def test_nonfinite_or_changing_evidence_cannot_grant_acceptance(self):
        self.final_fixture()
        path = self.root / "q/pool01-qualification.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"status": "pass", "value": NaN}', encoding="utf-8")
        data = report.collect(self.root)
        self.assertEqual(data["prepared_pool_qualification"]["status"], "pending")
        self.assertEqual(len(data["warnings"]), 1)
        with patch.object(report.Snapshot, "stable", return_value=False):
            data = report.collect(self.root)
        self.assertEqual(data["game_run"]["axes"]["full_acceptance"]["status"], "pending")

    def source_fixture(self):
        from kiroclops.local_engine import review
        state, saved = self.final_fixture(folder="s/p04", preparation="p/p04")
        directory = self.root / "s/p04"
        source_path = directory / "candidates/c1/source/main.py"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"fixture source")
        hashes = {"main.py": hashlib.sha256(source_path.read_bytes()).hexdigest()}
        source = report._digest(hashes)
        state["candidates"][0]["source_sha256"] = source
        self.write("s/p04/session.json", state)
        saved["candidate_sha256"] = source
        saved["native_run_status"] = "fail"
        saved["source_review_status"] = "pending"
        self.write("s/p04/report/report.json", saved)
        job = json.loads((directory / "jobs/final1/job.json").read_text())
        job.update(source_sha256=source, source_hashes=hashes)
        self.write("s/p04/jobs/final1/job.json", job)
        self.write("s/p04/kiro/launch.json", {"status": "completed", "response": {"stopReason": "end_turn"}, "response_received_at": 20})
        native_path = directory / "kiro/fixture.log"
        native_path.write_bytes(b"native fixture")
        receipt = {"kind": "IndependentSourceReview", "status": "pass", "candidate_id": "c1",
                   "candidate_sha256": source, "source_hashes": hashes, "session_directory": str(directory),
                   "reviewer_engine_sha256": hashlib.sha256(Path(review.__file__).read_bytes()).hexdigest(),
                   "source_policy_sha256": review._NATIVE_BUILTIN_POLICY_227["source_proof_sha256"]}
        self.write("s/p04/source_review.json", receipt)
        actual = {**receipt, "native_validation": {"status": "pass", "model": "claude-opus-5.5", "effort": "max",
                    "tool_outcomes": {"application_call_count": 60},
                    "evidence_hashes": {"kiro/fixture.log": hashlib.sha256(native_path.read_bytes()).hexdigest()}}}
        return review, receipt, actual

    @requires_evidence('reviews/staged-native227v3')
    def test_current_replayed_source_pass_keeps_old_report_failure(self):
        review, _, actual = self.source_fixture()
        with patch.object(review, "validate_source_review", return_value=actual) as validator:
            data = report.collect(self.root)
        axes = data["panel_progression"]
        self.assertEqual(validator.call_count, 1)
        self.assertEqual(axes["native_run"]["status"], "completed")
        self.assertEqual(axes["native_evidence_audit"]["status"], "fail")
        self.assertTrue(axes["native_evidence_audit"]["historical_report_verdict"])
        self.assertEqual(axes["current_native_audit"]["status"], "pass")
        self.assertEqual(axes["current_source_review"]["status"], "pass")
        self.assertEqual(axes["source_and_function"]["status"], "pending")
        self.assertFalse(data["full_phase3d_acceptance_granted"])
        self.assertIn("current native audit", report.render(data, self.output))

    def test_declared_current_pass_without_real_native_evidence_is_invalid(self):
        # No validator mock here: a claimed receipt cannot replace real native
        # config/log/guard evidence; the production validator must reject it.
        self.source_fixture()
        axes = report.collect(self.root)["panel_progression"]
        self.assertEqual(axes["current_native_audit"]["status"], "invalid")
        self.assertEqual(axes["current_source_review"]["status"], "invalid")

    def test_foreign_or_stale_current_source_scope_never_calls_validator(self):
        for field, value in (("candidate_id", "other"), ("candidate_sha256", "other"),
                             ("session_directory", "C:/other-session"), ("reviewer_engine_sha256", "other"),
                             ("source_policy_sha256", "other")):
            with self.subTest(field=field):
                review, receipt, actual = self.source_fixture()
                receipt[field] = value
                self.write("s/p04/source_review.json", receipt)
                with patch.object(review, "validate_source_review", return_value=actual) as validator:
                    axes = report.collect(self.root)["panel_progression"]
                validator.assert_not_called()
                self.assertEqual(axes["current_source_review"]["status"], "invalid")

    def test_native_or_source_bytes_changed_after_replay_reject_current_pass(self):
        for native in (False, True):
            with self.subTest(native=native):
                review, _, actual = self.source_fixture()
                def changed(*args, **kwargs):
                    path = self.root / ("s/p04/kiro/fixture.log" if native else "s/p04/candidates/c1/source/main.py")
                    path.write_bytes(b"changed bytes")
                    return actual
                with patch.object(review, "validate_source_review", side_effect=changed):
                    axes = report.collect(self.root)["panel_progression"]
                self.assertEqual(axes["current_native_audit"]["status"], "invalid")

    def test_missing_current_receipt_stays_pending_and_never_replays(self):
        from kiroclops.local_engine import review
        with patch.object(review, "validate_source_review", side_effect=AssertionError("No receipt")):
            axes = report.collect(self.root)["panel_progression"]
        self.assertEqual(axes["current_source_review"]["status"], "pending")

    def svg_fixture(self, *, passed=True, measurement_sources=None):
        from kiroclops.local_engine import suite_initial_acceptance as initial
        from kiroclops.local_engine import suite_svg_final_acceptance as closure
        directory = self.root / "a/svg-initial-01"
        cases = [{"case_id": "case%02d" % i, "role": "calibration" if i < 4 else "regression" if i < 16 else "validation"}
                 for i in range(28)]
        hashes = {"main.js": hashlib.sha256(b"fixture SVG source").hexdigest()}
        source = report._digest(hashes)
        for project in (self.root / "parent", directory / "unchanged-source", self.root / "p/sv04/suite/original"):
            project.mkdir(parents=True, exist_ok=True)
            (project / "main.js").write_bytes(b"fixture SVG source")
        code = self.root / "svg-workflow.py"
        code.write_bytes(b"fixture frozen workflow")
        plan = {"domain": "svg", "stage": "svg", "unchanged_source_sha256": source, "suite_sha256": "suite", "suite": str(self.root / "p/sv04/suite"),
                "authoring_parent": str(self.root / "parent"), "unchanged_source": str(directory / "unchanged-source"),
                "authoring_parent_source_hashes": hashes, "original_source_hashes": hashes, "measurement_source_hashes": measurement_sources or {},
                "workflow_source_hashes": {str(code): hashlib.sha256(code.read_bytes()).hexdigest()}}
        suite = {"original": "original"}
        metric = {"measurement_pass": True, "baseline_measurement_pass": True, "pass": True}
        comparisons = [{**metric, "case_id": case["case_id"], "role": case["role"]} for case in cases]
        if not passed:
            comparisons[-1]["pass"] = False
        preserved = [{**metric, "case_id": case["case_id"], "role": case["role"],
                      "accepted_parent_reference_gates": True, "parent_reference_result": {"pass": True}, "preservation_required": True}
                     for case in cases]
        functional = {"kind": "ReferenceSuiteFunctionalChecks", "schema_version": 1, "domain": "svg",
                      "checks_source": "separate_private_browser_oracle", "source_sha256": source, "status": "pass",
                      "checks": [{"id": name, "pass": True} for name in sorted(closure.FUNCTIONAL_CHECKS)], "page_errors": []}
        groups = {role: {"required": sum(case["role"] == role for case in cases),
                         "passed": sum(row["role"] == role and row["pass"] for row in comparisons),
                         "pass": all(row["pass"] for row in comparisons if row["role"] == role)} for role in initial.media_suite.ROLES}
        result = {"kind": "NoCodeChangeInitialAcceptance", "complete": True, "measurement_qualified": True,
                  "status": "numerical_pass_pending_review" if passed else "behavior_fail_pending_review", "numerical_pass": passed,
                  "behavior_failure_observed": not passed, "repair_preregistration_allowed": not passed,
                  "comparisons": comparisons, "parent_preservation": preserved, "functional": functional, "groups": groups}
        self.write("a/svg-initial-01/initial-plan.json", plan)
        self.write("a/svg-initial-01/result.json", result)
        self.write("a/svg-initial-01/comparisons.json", comparisons)
        self.write("a/svg-initial-01/preservation.json", preserved)
        self.write("a/svg-initial-01/functional.json", functional)
        for role in closure.ROLES:
            self.write("a/svg-initial-01/indexes/" + role + ".json", [{"case_id": case["case_id"], "role": role} for case in cases])
        def compare(reference, original, candidate, stage, *, require_improvement):
            return {**metric, "pass": passed or reference["role"] == "before" or candidate["case_id"] != cases[-1]["case_id"]}
        stack = ExitStack()
        stack.enter_context(patch.object(initial, "load_result", return_value=deepcopy(result)))
        stack.enter_context(patch.object(initial, "load_plan", return_value=(deepcopy(plan), suite)))
        stack.enter_context(patch.object(initial, "_matrix", return_value=cases))
        qualifier = stack.enter_context(patch.object(initial, "_qualified", side_effect=lambda record, *args, **kwargs: record))
        stack.enter_context(patch.object(initial, "_source_expectation", return_value={"kind": "web", "sources": {}}))
        stack.enter_context(patch.object(initial.media_suite, "_compare", side_effect=compare))
        stack.enter_context(patch.object(initial.media_suite, "_parent_acceptance", return_value={"pass": True}))
        self.addCleanup(stack.close)
        return directory, initial, cases, result, qualifier

    def test_optional_svg_not_supplied_or_not_started_does_not_invent_acceptance(self):
        self.assertIsNone(report.collect(self.root)["svg_initial"])
        data = report.collect(self.root, svg_initial=self.root / "a/not-created")
        svg = data["svg_initial"]
        self.assertFalse(svg["validator_replayed"])
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "pending")
        self.assertFalse(svg["full_behavior_acceptance"])

    def test_bound_svg_numeric_pass_still_has_pending_independent_axes(self):
        directory, _, _, _, qualifier = self.svg_fixture()
        data = report.collect(self.root, svg_initial=directory)
        svg = data["svg_initial"]
        self.assertEqual(qualifier.call_count, 112)
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "pass")
        self.assertEqual(svg["axes"]["parent_preservation"]["status"], "pass")
        self.assertEqual(svg["axes"]["current_source_review"]["status"], "pending")
        self.assertEqual(svg["axes"]["independent_final_review"]["status"], "pending")
        self.assertEqual(svg["axes"]["historical_generation"]["status"], "historical_linked")
        self.assertEqual(svg["axes"]["full_acceptance"]["status"], "pending")
        self.assertEqual(svg["axes"]["executed_functional_checks"]["status"], "pending")
        self.assertFalse(data["full_phase3d_acceptance_granted"])
        self.assertIn("No fresh model generation", report.render(data, self.output))

    def test_svg_exact_replayed_failure_is_distinct_from_unknown_verification(self):
        directory, _, _, _, _ = self.svg_fixture(passed=False)
        svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "fail")
        self.assertEqual(svg["groups"]["validation"]["passed"], 11)
        self.assertEqual(svg["axes"]["parent_preservation"]["status"], "pass")
        self.assertFalse(svg["full_behavior_acceptance"])

    def test_svg_actual_loader_rejection_keeps_claimed_pass_invalid(self):
        directory, initial, _, _, _ = self.svg_fixture()
        with patch.object(initial, "load_result", side_effect=ValueError("Source or bound output changed")):
            svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertFalse(svg["validator_replayed"])
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "invalid")

    def test_svg_stored_numeric_or_group_projection_must_match_current_replay(self):
        directory, _, _, result, _ = self.svg_fixture()
        bad = deepcopy(result["comparisons"])
        bad[-1]["pass"] = False
        self.write("a/svg-initial-01/comparisons.json", bad)
        svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "invalid")

    def test_svg_missing_role_record_or_failed_qualification_never_passes(self):
        directory, initial, cases, _, _ = self.svg_fixture()
        self.write("a/svg-initial-01/indexes/unchanged.json", [{"case_id": case["case_id"]} for case in cases[:-1]])
        svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["measurement_qualification"]["status"], "invalid")
        self.write("a/svg-initial-01/indexes/unchanged.json", [{"case_id": case["case_id"]} for case in cases])
        with patch.object(initial, "_qualified", side_effect=ValueError("Worker binding differs")):
            svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "invalid")

    def test_svg_report_cannot_be_written_inside_its_evidence(self):
        directory, _, _, _, _ = self.svg_fixture()
        with self.assertRaises(ValueError):
            report.build(directory / "new-report", self.root, svg_initial=directory)

    def test_svg_source_or_code_drift_during_qualification_cannot_publish_pass(self):
        directory, initial, _, _, _ = self.svg_fixture()
        def changed(record, *args, **kwargs):
            (self.root / "svg-workflow.py").write_bytes(b"changed during qualification")
            return record
        with patch.object(initial, "_qualified", side_effect=changed):
            data = report.collect(self.root, svg_initial=directory)
        self.assertFalse(data["snapshot_stable"])
        self.assertEqual(data["svg_initial"]["axes"]["unchanged_source"]["status"], "pending")
        self.assertEqual(data["svg_initial"]["axes"]["numeric_behavior"]["status"], "pending")

    def test_svg_exact_stage_metadata_is_bound_without_becoming_a_file_path(self):
        directory, _, _, _, _ = self.svg_fixture(measurement_sources={"@stage": report._digest("svg")})
        original = report.Snapshot.file
        def no_metadata_path(snapshot, path):
            self.assertNotEqual(Path(path).name, "@stage")
            return original(snapshot, path)
        with patch.object(report.Snapshot, "file", new=no_metadata_path):
            svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["unchanged_source"]["status"], "pass")
        self.assertEqual(svg["metadata_bindings"], [{"field": "measurement_source_hashes", "key": "@stage",
                          "value": report._digest("svg"), "authoritative_loader_verified": True}])

    def test_svg_unknown_or_wrong_stage_metadata_is_not_skipped(self):
        for metadata in ({"@stage": report._digest("panel")}, {"@unknown": report._digest("svg")}, {"@stage": True}):
            with self.subTest(metadata=metadata):
                directory, _, _, _, _ = self.svg_fixture(measurement_sources=metadata)
                svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
                self.assertEqual(svg["axes"]["unchanged_source"]["status"], "invalid")
                self.assertEqual(svg["metadata_bindings"], [])

    def test_svg_missing_real_inventory_file_remains_fail_closed(self):
        missing = self.root / "missing-real-source.py"
        directory, _, _, _, _ = self.svg_fixture(measurement_sources={"@stage": report._digest("svg"), str(missing): "a" * 64})
        svg = report.collect(self.root, svg_initial=directory)["svg_initial"]
        self.assertEqual(svg["axes"]["unchanged_source"]["status"], "invalid")
        self.assertEqual(svg["axes"]["numeric_behavior"]["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
