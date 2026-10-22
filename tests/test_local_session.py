"""Session protocol checks using trusted evaluator callbacks and real source snapshots."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine.backend import _atomic, _read
from kiroclops.local_engine.session import Session, copy_project, create_session, editable, run_session_job, source_hashes
from kiroclops.visual.schema import VisualError


class LocalSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="local session café ")
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "original"
        (self.baseline / "src").mkdir(parents=True)
        (self.baseline / "src/panel-motion.js").write_text("export const speed=1;", encoding="utf-8")
        (self.baseline / "index.html").write_text("<main>Protected appearance</main>", encoding="utf-8")
        self.reference = self.root / "private reference"
        self.reference.mkdir()
        (self.reference / "secret.js").write_text("private reference implementation", encoding="utf-8")
        self.directory = self.root / "new session"
        create_session(self.directory, "web_panel", self.baseline, self.reference, "Match opening behavior", experiment={"experiment_id": "test", "capture_lock": str(self.root / "capture.lock"), "calibration": ["open"]})
        self.session = Session(self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, call):
        with self.assertRaises(VisualError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def submit(self, operation, result=None, candidate_id=None, runner=None):
        with patch.object(Session, "_spawn"):
            submitted = self.session.submit(operation, candidate_id)
        if submitted["status"] == "queued":
            run_session_job(self.directory, submitted["job_id"], runner=runner or (lambda *args: result or {}))
        return self.session.get_job(submitted["job_id"])

    def prepare(self):
        prepared = self.submit("prepare", {"reference": {"media": []}, "before": {"media": []}})
        self.assertEqual("succeeded", prepared["status"], prepared)

    def candidate(self, score, eligible=True):
        self.prepare()
        path = self.directory / "target/src/panel-motion.js"
        path.write_text(f"export const speed={score};", encoding="utf-8")
        return self.submit("evaluate", {"score": score, "eligible": eligible})

    def test_create_isolated_target_and_external_reference(self):
        self.assertEqual(source_hashes(self.baseline), source_hashes(self.directory / "target"))
        self.assertEqual(source_hashes(self.baseline), source_hashes(self.directory / "baseline"))
        self.assertFalse(any(p.name == "secret.js" for p in self.directory.rglob("*")))
        inspected = self.session.inspect()
        self.assertNotIn(str(self.reference), json.dumps(inspected))
        self.assertEqual(["src/panel-motion.js", "src/motion/*.js"], inspected["editable_scope"])

    def test_source_filter_and_editable_scopes(self):
        target = self.directory / "target"
        (target / ".kiro").mkdir()
        (target / ".kiro/config.json").write_text("{}")
        self.assertEqual(source_hashes(self.baseline), source_hashes(target))
        self.assertTrue(editable("game", "motion/helper.gd"))
        self.assertTrue(editable("svg", "src/layout-motion.js"))
        self.assertFalse(editable("svg", "src/panel-motion.js"))
        self.assertFalse(editable("game", "motion/../player.gd"))
        self.assertFalse(editable("web_panel", "src/motion/nested/other.js"))

    def test_copy_does_not_overwrite_or_embed_nested_project(self):
        self.assert_code("output_exists", lambda: copy_project(self.baseline, self.directory / "target"))
        self.assert_code("unsafe_path", lambda: copy_project(self.baseline, self.baseline / "nested"))

    def test_session_binding_rejects_scope_or_budget_change(self):
        state = _read(self.directory / "session.json")
        state["budget"]["max_candidates"] = 300
        _atomic(self.directory / "session.json", state)
        self.assert_code("session_binding_changed", self.session.inspect)

    def test_clock_starts_at_prepare_and_survives_reconnect(self):
        self.assertIsNone(_read(self.directory / "session.json")["started_at"])
        self.prepare()
        before = _read(self.directory / "session.json")
        Session(self.directory).start_clock()
        after = _read(self.directory / "session.json")
        self.assertEqual(before["started_at"], after["started_at"])
        self.assertEqual(1800, after["deadline_at"] - after["started_at"])
        self.assertEqual("prepare", after["clock_origin"])

    def test_model_clock_starts_before_prepare(self):
        clock = self.session.start_clock()
        self.prepare()
        state = _read(self.directory / "session.json")
        self.assertEqual(clock["started_at"], state["started_at"])
        self.assertEqual("model_generation", state["clock_origin"])

    def test_protected_edit_consumes_candidate_and_rejects_execution(self):
        self.prepare()
        (self.directory / "target/index.html").write_text("changed protected layout")
        result = self.submit("evaluate", {"eligible": True, "score": 0})
        self.assertEqual("failed", result["status"])
        self.assertEqual("protected_source_changed", result["error"]["code"])
        self.assertEqual(1, self.session.inspect()["budget"]["used_candidates"])

    def test_protected_deletion_and_new_file_rejected(self):
        self.prepare()
        (self.directory / "target/index.html").unlink()
        first = self.submit("evaluate")
        self.assertEqual("protected_source_changed", first["error"]["code"])
        (self.directory / "target/index.html").write_text("<main>Protected appearance</main>")
        (self.directory / "target/shortcut.js").write_text("forbidden")
        second = self.submit("evaluate")
        self.assertEqual("protected_source_changed", second["error"]["code"])

    def test_snapshot_survives_later_target_edits(self):
        self.prepare()
        with patch.object(Session, "_spawn"):
            job = self.session.submit("evaluate")
        (self.directory / "target/src/panel-motion.js").write_text("next version")
        def evaluator(directory, job, check, progress, cancel):
            self.assertEqual("export const speed=1;", (Path(job["source_path"]) / "src/panel-motion.js").read_text())
            return {"eligible": True, "score": .01}
        run_session_job(self.directory, job["job_id"], runner=evaluator)
        self.assertEqual("succeeded", self.session.get_job(job["job_id"])["status"])

    def test_frozen_source_mutation_during_evaluation_fails(self):
        self.prepare()
        def mutate(directory, job, *args):
            (Path(job["source_path"]) / "src/panel-motion.js").write_text("mutated by execution")
            return {"eligible": True, "score": 0}
        result = self.submit("evaluate", runner=mutate)
        self.assertEqual("source_changed", result["error"]["code"])
        self.assertFalse(self.session.inspect()["candidates"][0]["eligible"])

    def test_best_finite_calibration_candidate_selected_and_sealed(self):
        first = self.candidate(.1)
        second = self.candidate(.04)
        self.assertEqual("succeeded", first["status"])
        with patch.object(Session, "_spawn"):
            final = self.session.submit("finalize")
        inspected = _read(self.directory / "session.json")
        self.assertEqual(second["candidate_id"], inspected["selected_candidate_id"])
        self.assertIsNotNone(inspected["sealed_at"])
        run_session_job(self.directory, final["job_id"], runner=lambda *args: {"pass": True})
        self.assertTrue(self.session.get_job(final["job_id"])["result"]["pass"])
        self.assert_code("session_sealed", lambda: self.session.submit("evaluate"))
        self.assertEqual(final["job_id"], self.session.submit("finalize")["job_id"])

    def test_requesting_worse_candidate_does_not_reveal_held_out(self):
        first = self.candidate(.2)
        self.candidate(.1)
        result = self.submit("finalize", candidate_id=first["candidate_id"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("candidate_selection_rejected", result["error"]["code"])
        self.assertIsNone(self.session.inspect()["sealed_at"])

    def test_missing_or_non_numeric_score_is_not_a_zero_error_pass(self):
        self.prepare()
        for score in (None, "0", True):
            self.submit("evaluate", {"eligible": True, "score": score})
        inspected = self.session.inspect()
        self.assertTrue(all(not c["eligible"] for c in inspected["candidates"]))
        result = self.submit("finalize")
        self.assertEqual("no_eligible_candidate", result["error"]["code"])
        self.assert_code("budget_exhausted", lambda: self.session.submit("evaluate"))

    def test_baseline_mode_selects_latest_capture_valid_without_scores(self):
        directory = self.root / "normal kiro"
        create_session(directory, "web_panel", self.baseline, self.reference, "Same goal", mode="baseline", experiment={"capture_lock": str(self.root / "capture.lock")})
        self.directory, self.session = directory, Session(directory)
        self.prepare()
        self.submit("evaluate", {"eligible": True, "score": None, "neutral_capture": True})
        second = self.submit("evaluate", {"eligible": True, "score": None, "neutral_capture": True})
        result = self.submit("finalize", {"pass": False, "score": .7})
        self.assertEqual(second["candidate_id"], result["candidate_id"])
        self.assertTrue(all("score" not in c for c in self.session.inspect()["candidates"]))

    def test_all_errors_count_and_no_fourth_candidate(self):
        self.prepare()
        def failed(*args):
            raise VisualError("capture_timing_failure", "bad capture")
        for _ in range(3):
            self.assertEqual("failed", self.submit("evaluate", runner=failed)["status"])
        self.assert_code("budget_exhausted", lambda: self.session.submit("evaluate"))

    def test_only_one_active_job(self):
        with patch.object(Session, "_spawn"):
            self.session.submit("prepare")
            self.assert_code("job_active", lambda: self.session.submit("report"))

    def test_session_deadline_is_not_reset_and_report_still_allowed(self):
        self.prepare()
        state = _read(self.directory / "session.json")
        state["deadline_at"] = time.time()-1
        _atomic(self.directory / "session.json", state)
        self.assert_code("budget_exhausted", lambda: self.session.submit("evaluate"))
        self.assertEqual("succeeded", self.submit("report", {"report_path": "index.html"})["status"])

    def test_native_report_defers_immediately_in_both_arms(self):
        for mode in ("tools", "baseline"):
            for status in ("starting", "running"):
                with self.subTest(mode=mode, status=status):
                    directory = self.directory if mode == "tools" else self.root / "baseline session"
                    if mode == "baseline" and not directory.exists():
                        create_session(directory, "web_panel", self.baseline, self.reference, "Same goal", mode=mode)
                    session = Session(directory)
                    started = time.time()
                    launch = {"status": status, "started_at": started, "prompt_sent": False}
                    if status == "running":
                        launch["prompt_started_at"] = started + 1
                    _atomic(directory / "kiro/launch.json", launch)
                    before = (directory / "session.json").read_bytes()
                    with patch.object(Session, "_reconcile_active", side_effect=AssertionError("must return before reconciliation")), \
                         patch.object(Session, "_spawn", side_effect=AssertionError("must not spawn")):
                        response = session.submit("report")
                    self.assertEqual({"job_id": None, "status": "deferred_to_runner", "candidate_id": None,
                                      "message": "The local runner builds the report after this native turn ends."}, response)
                    self.assertEqual(before, (directory / "session.json").read_bytes())
                    self.assertFalse((directory / "jobs").exists())

    def test_malformed_native_launch_fails_closed_before_report_job(self):
        launch = self.directory / "kiro/launch.json"
        launch.parent.mkdir(parents=True)
        for value in ('{invalid', '{"status":"running","started_at":"bad","prompt_sent":false}',
                      '{"status":"running","started_at":1,"prompt_sent":false,"prompt_started_at":"bad"}',
                      '{}'):
            with self.subTest(value=value):
                launch.write_text(value, encoding="utf-8")
                self.assert_code("invalid_native_launch", lambda: self.session.submit("report"))
                self.assertFalse((self.directory / "jobs").exists())

    def test_completed_native_turn_allows_manual_report_job(self):
        _atomic(self.directory / "kiro/launch.json", {"status": "completed"})
        with patch.object(Session, "_spawn") as spawn:
            result = self.session.submit("report")
        self.assertEqual("queued", result["status"])
        self.assertEqual("report", _read(self.directory / "jobs" / result["job_id"] / "job.json")["operation"])
        spawn.assert_called_once_with(result["job_id"])

    def test_deferred_response_reaches_mcp_and_baseline_cli(self):
        from kiroclops.local_engine.mcp import create_server
        from kiroclops.local_engine.__main__ import main
        started = time.time()
        _atomic(self.directory / "kiro/launch.json", {"status": "running", "started_at": started,
                                                     "prompt_started_at": started + 1, "prompt_sent": False})
        server, _ = create_server(self.directory)
        result = asyncio.run(server.call_tool("build_report", {}))
        self.assertEqual("deferred_to_runner", result.structured_content["status"])
        self.assertFalse((self.directory / "jobs").exists())
        baseline = self.root / "baseline cli"
        create_session(baseline, "web_panel", self.baseline, self.reference, "Same goal", mode="baseline")
        _atomic(baseline / "kiro/launch.json", {"status": "running", "started_at": started,
                                               "prompt_started_at": started + 1, "prompt_sent": False})
        output = io.StringIO()
        with patch("kiroclops.local_engine.environment.configure_environment"), contextlib.redirect_stdout(output):
            code = main(["trial", "report", "--session", str(baseline), "--async"])
        self.assertEqual(0, code)
        self.assertEqual("deferred_to_runner", json.loads(output.getvalue())["status"])
        self.assertFalse((baseline / "jobs").exists())

    def test_cancel_marker_interrupts_cooperative_execution(self):
        self.prepare()
        def cancelled(directory, job, check, progress, cancel):
            _atomic(directory / "jobs" / job["job_id"] / "cancel.json", {"requested_at": time.time()})
            self.assertTrue(cancel())
            check()
        result = self.submit("evaluate", runner=cancelled)
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("job_cancelled", result["error"]["code"])

    def test_result_tampering_revokes_candidate_eligibility(self):
        candidate = self.candidate(.01)
        _atomic(self.directory / "jobs" / candidate["job_id"] / "result.json", {"eligible": True, "score": 0})
        result = self.session.get_job(candidate["job_id"])
        self.assertEqual("result_integrity_failure", result["error"]["code"])
        self.assertFalse(self.session.inspect()["candidates"][0]["eligible"])

    def test_control_files_bound_separately_from_target_code(self):
        control = self.directory / "target/.kiro"
        control.mkdir()
        (control / "agent.json").write_text("{}")
        self.session.bind_controls()
        self.prepare()
        (control / "agent.json").write_text('{"changed":true}')
        result = self.submit("evaluate")
        self.assertEqual("control_changed", result["error"]["code"])

    def test_real_launcher_guard_and_preflight_logs_do_not_change_bound_controls(self):
        from kiroclops.local_engine.launcher import configure, guard
        configure(self.directory)
        decision = guard(self.directory, {"tool_name":"read_file", "tool_input":{"path":str(self.directory / "target/index.html")}})
        self.assertTrue(decision["allow"])
        self.assertTrue((self.directory / "kiro/tool-audit.jsonl").is_file())
        log = self.directory / "kiro/preflight-example/result.json"
        _atomic(log, {"status":"pass", "prompt_sent":False})
        _atomic(self.directory / "kiro/launch.json", {"status":"starting"})
        self.session._verify_sources(self.session._load())
        self.prepare()
        self.assertTrue(self.session.inspect()["prepared"])
        config = self.directory / "kiro/config.json"
        altered = _read(config); altered["effort"] = "low"; _atomic(config,altered)
        self.assert_code("control_changed", lambda:self.session._verify_sources(self.session._load()))

    def test_external_reference_change_rejects_candidate(self):
        self.prepare()
        (self.reference / "secret.js").write_text("changed reference")
        result = self.submit("evaluate")
        self.assertEqual("reference_changed", result["error"]["code"])

    def test_job_source_binding_cannot_select_another_candidate(self):
        self.prepare()
        with patch.object(Session, "_spawn"):
            submitted = self.session.submit("evaluate")
        job_path = self.directory / "jobs" / submitted["job_id"] / "job.json"
        job = _read(job_path)
        job["source_path"] = str(self.directory / "target")
        _atomic(job_path, job)
        run_session_job(self.directory, submitted["job_id"], runner=lambda *args: {"eligible": True, "score": 0})
        result = self.session.get_job(submitted["job_id"])
        self.assertEqual("job_binding_changed", result["error"]["code"])

    def test_mcp_exposes_the_eight_tool_names(self):
        from kiroclops.local_engine.mcp import create_server
        server, _ = create_server(self.directory)
        names = asyncio.run(server.list_tools())
        self.assertEqual({"inspect_session", "prepare_experiment", "evaluate_candidate", "finalize_candidate", "get_job", "cancel_job", "build_report", "inspect_window"}, {tool.name for tool in names})


if __name__ == "__main__":
    unittest.main()
