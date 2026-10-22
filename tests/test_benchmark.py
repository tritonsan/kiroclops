import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiroclops.integration import benchmark
REAL_MANUAL_EVIDENCE = benchmark._manual_evidence
REAL_WALL_EVIDENCE = benchmark._wall_evidence


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def update(kind, **kwargs):
    return {"type": "sessionUpdate", "data": {"sessionId": "stream-session", "update": {"sessionUpdate": kind, **kwargs}}}


def model_event(model):
    return update("config_option_update", configOptions=[{"id": "model", "currentValue": model}])


def launch(mode="tools", surface="cli"):
    return {"surface": surface, "mode": mode, "status": "completed", "returncode": 0, "model_requested": "claude-opus-5",
            "effort_requested": "high", "model_reported": ["claude-opus-5"], "model_confirmation": "stream_confirmed", "elapsed_seconds": 120,
            "transport": "headless", "cli_version": "test-cli", "kas_version": "test-kas"}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(benchmark, "ROOT", self.root)
        self.root_patch.start()
        # Most cases isolate aggregation, not the separately exercised raw
        # independent-review receipt verifier (which reads 54 real captures).
        self.manual_patch = patch.object(benchmark, "_manual_evidence", return_value={"status": "pass"})
        self.manual_patch.start()
        self.wall_patch = patch.object(benchmark, "_wall_evidence", return_value={"status": "pass"})
        self.wall_patch.start()

    def tearDown(self):
        self.wall_patch.stop()
        self.manual_patch.stop()
        self.root_patch.stop()
        self.temporary.cleanup()

    def fixture(self, name, *, mode="tools", surface="cli", error=.08):
        session = self.root / "sessions" / name
        cid = "candidate-01"
        hashes = []
        for path, text in ((session / "baseline", "before"), (session / f"candidates/{cid}/project", "after")):
            path.mkdir(parents=True)
            (path / "player.gd").write_text(text, encoding="utf-8")
            hashes.append({"player.gd": hashlib.sha256(text.encode()).hexdigest()})
        candidate = {"candidate_id": cid, "source_hashes": hashes[1], "status": "finalized", "smoke": {"status": "passed"}, "validation_runs": []}
        before = []
        scenarios = [{"hold_ticks": hold, "spawn_offset": offset, "repeat": repeat, "start_tick": 30, "split": "validation"}
                     for hold in (8, 14) for offset in (0, 24) for repeat in (1, 2, 3)]
        for role, expected, collection in (("before", hashes[0], before), ("after", hashes[1], candidate["validation_runs"])):
            for index in range(12):
                run = session / f"runs/{role}-{index:02d}"
                write(run / "manifest.json", {"game": "slay", "variant": role, "status": "ok", "fps": 60, "frame_count": 240,
                      "scenario": scenarios[index], "provenance": {"full_source_hashes_before": expected, "full_source_hashes_after": expected}})
                collection.append(str(run))
        state = {"session_id": name, "mode": mode, "status": "finished", "baseline_hashes": hashes[0],
                 "selected_candidate_id": cid, "candidates": [candidate], "baseline_runs": {"validation": before},
                 "final": {"comparison_path": "final/comparison.json", "candidate_id": cid}, "source_commit": "fixed-commit",
                 "phase1_frozen_files": {"analysis.py": "fixed-hash"}, "budget": {"max_candidates": 3, "max_seconds": 1800}}
        write(session / "session.json", state)
        write(session / f"candidates/{cid}/candidate.json", candidate)
        comparison = {"status": "pass", "numerical_status": "pass", "manual_validation_status": "pass",
                      "coverage": {"complete_triplets": 12, "required_triplets": 12},
                      "validation_scenarios": [{"scenario": scenario, "after_rmse_body": error, "pass": True, "gates": {
                          key: True for key in ("curve_rmse", "improvement", "height", "rise", "fall")}} for scenario in scenarios]}
        write(session / "final/comparison.json", comparison)
        write(session / "kiro/launch.json", launch(mode, surface))
        selected = update("config_option_update", configOptions=[{"id": "model", "currentValue": "claude-opus-5"},
                          {"id": "mode", "currentValue": f"kiroclops-{mode}"}, {"id": "effortLevel", "currentValue": "high"}])
        events = [model_event("auto"), selected, update("agent_thought_chunk", content={"type": "text", "text": "planning"})]
        (session / "kiro/stdout.jsonl").write_text("\n".join(json.dumps(event) for event in events)+"\n", encoding="utf-8")
        (session / "kiro/tool-audit.jsonl").write_text(json.dumps({"tool": "fs_write", "allow": True, "paths": ["source/characters/player/player.gd"]})+"\n", encoding="utf-8")
        (session / f"candidates/{cid}/patch.diff").write_text("-before\n+after\n", encoding="utf-8")
        evidence = [{"path": path, "sha256": benchmark._sha(session / path)} for path in
                    ("kiro/stdout.jsonl", "kiro/tool-audit.jsonl", f"candidates/{cid}/patch.diff")]
        write(session / "kiro/authorship.json", {"status": "verified", "candidate_id": cid, "source_hashes": hashes[1],
              "reviewed_by": "independent-reviewer", "reviewed_at": "2026-09-18T10:00:00Z", "evidence": evidence})
        return session

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_sparse_not_run_session_never_invents_baseline_error_or_success(self):
        session = self.root / "sessions/baseline-not-run"
        write(session / "session.json", {"session_id": session.name, "mode": "baseline", "status": "new"})
        report = benchmark.build_benchmark([session.name])
        row = report["sessions"][0]
        self.assertEqual(row["status"], "not_run")
        self.assertFalse(row["success"])
        self.assertEqual(row["final_error"]["status"], "unavailable")
        self.assertIsNone(row["final_error"]["mean_rmse_body"])
        self.assertEqual(row["model"]["status"], "unverified")
        self.assertEqual(report["cli"]["arms"]["baseline"]["selected_runs"], 0)  # No inferred CLI surface.

    def test_only_explicit_sessions_are_included_and_duplicates_rejected(self):
        self.fixture("selected")
        self.fixture("unselected", mode="baseline")
        report = benchmark.build_benchmark(["selected"])
        self.assertEqual(report["selected_sessions"], ["selected"])
        self.assertFalse(report["cli"]["matrix_complete"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            benchmark.build_benchmark(["selected", self.root / "sessions/selected"])
        with self.assertRaisesRegex(ValueError, "explicit"):
            benchmark.build_benchmark([])

    def test_six_verified_cli_runs_are_descriptive_and_ide_is_separate(self):
        names = []
        for mode, error in (("tools", .08), ("baseline", .13)):
            for index in range(3):
                name = f"{mode}-{index}"
                self.fixture(name, mode=mode, error=error)
                names.append(name)
        self.fixture("ide-demo", surface="ide")
        report = benchmark.build_benchmark(names+["ide-demo"])
        self.assertEqual(report["status"], "complete_descriptive_comparison")
        self.assertEqual(report["cli"]["arms"]["tools"]["successes"], 3)
        self.assertEqual(report["cli"]["arms"]["baseline"]["selected_runs"], 3)
        self.assertEqual(report["ide"]["session_ids"], ["ide-demo"])
        self.assertAlmostEqual(report["cli"]["observed_difference"]["mean_final_rmse_reduction_body"], .05)

    def test_initial_auto_is_ignored_only_before_confirmed_generation(self):
        events = [model_event("auto"), model_event("claude-opus-5"), update("agent_thought_chunk")]
        result = benchmark._model(launch(), events, "complete")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["stream_reported"], ["claude-opus-5"])
        self.assertEqual(result["model_at_first_generation"], "claude-opus-5")
        includes_initialization = launch()
        includes_initialization["model_reported"] = ["auto", "claude-opus-5"]
        self.assertEqual(benchmark._model(includes_initialization, events, "complete")["status"], "verified")
        switched = benchmark._model(launch(), events+[model_event("auto")], "complete")
        self.assertEqual(switched["status"], "mismatch")
        wrong_start = benchmark._model(launch(), [model_event("auto"), update("agent_message_chunk"), model_event("claude-opus-5")], "complete")
        self.assertEqual(wrong_start["status"], "mismatch")

    def test_model_argument_inventory_or_plain_text_never_proves_runtime_model(self):
        for events in ([], [{"models": [{"model_id": "claude-opus-5"}]}],
                       [update("agent_message_chunk", content={"text": "I am using claude-opus-5"})],
                       [model_event("claude-opus-5")]):
            with self.subTest(events=events):
                self.assertEqual(benchmark._model(launch(), events, "complete")["status"], "unverified")

    def test_shared_stream_guard_rejects_wrong_agent_even_with_requested_model(self):
        selected = update("config_option_update", configOptions=[{"id": "model", "currentValue": "claude-opus-5"},
                          {"id": "mode", "currentValue": "vibe"}, {"id": "effortLevel", "currentValue": "high"}])
        result = benchmark._configuration(launch(), [selected, update("agent_thought_chunk")], "complete")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["error"]["code"], "mode_mismatch")

    def test_acp_completion_uses_raw_prompt_result_and_cleanup_not_headless_returncode(self):
        selected = {"mode": "kiroclops-tools", "model": "claude-opus-5", "effort": "high"}
        config = {"status": "pass", "expected": selected}
        record = {**launch(), "transport": "acp", "returncode": None, "finished_at": "2026-09-18T10:00:00Z",
                  "prompt_sent": True, "prompt_result": {"stopReason": "end_turn"}, "process_returncode_after_cleanup": 0,
                  "stream_configuration": {"selected": selected, "generation_started": True, "generation_configuration_verified": True}}
        response = {"jsonrpc": "2.0", "id": 42, "result": {"stopReason": "end_turn"}}
        self.assertEqual(benchmark._launch_completion(record, [response], config)["status"], "pass")
        terminated = {**record, "process_returncode_after_cleanup": -15}
        self.assertEqual(benchmark._launch_completion(terminated, [response], config)["status"], "pass")
        for changes in ({"prompt_sent": False}, {"prompt_result": {"stopReason": "cancelled"}},
                        {"process_returncode_after_cleanup": None}, {"stream_configuration": {}}, {"finished_at": None}):
            with self.subTest(changes=changes):
                self.assertEqual(benchmark._launch_completion({**record, **changes}, [response], config)["status"], "unverified")
        self.assertEqual(benchmark._launch_completion(record, [], config)["status"], "unverified")
        self.assertEqual(benchmark._launch_completion(record, [response], {**config, "status": "fail"})["status"], "unverified")
        self.assertEqual(benchmark._launch_completion({**record, "cleanup_errors": ["reader did not exit"]}, [response], config)["status"], "fail")

    def test_authorship_is_required_and_bound_to_actual_evidence(self):
        session = self.fixture("authorship")
        self.assertTrue(benchmark.build_benchmark([session.name])["sessions"][0]["success"])
        record = self.read(session / "kiro/authorship.json")
        record["status"] = "unverified"
        write(session / "kiro/authorship.json", record)
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["gates"]["authorship"], "unverified")
        self.assertFalse(row["success"])
        record["status"] = "verified"
        write(session / "kiro/authorship.json", record)
        (session / "candidates/candidate-01/patch.diff").write_text("changed after review", encoding="utf-8")
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["gates"]["authorship"], "fail")

    def test_changed_sources_or_capture_hashes_fail(self):
        session = self.fixture("source")
        (session / "candidates/candidate-01/project/player.gd").write_text("tampered", encoding="utf-8")
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["gates"]["source_hashes"], "fail")
        session = self.fixture("capture")
        path = session / "runs/after-00/manifest.json"
        manifest = self.read(path)
        manifest["provenance"]["full_source_hashes_after"] = {"player.gd": "wrong"}
        write(path, manifest)
        self.assertEqual(benchmark.build_benchmark([session.name])["sessions"][0]["gates"]["source_hashes"], "fail")

    def test_missing_baseline_final_result_does_not_use_good_calibration_feedback(self):
        session = self.fixture("baseline", mode="baseline")
        state = self.read(session / "session.json")
        state["candidates"][0]["feedback"] = {"mean_rmse_body": .001, "eligible": True}
        write(session / "session.json", state)
        write(session / "final/comparison.json", {"status": "pending", "validation_scenarios": []})
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["final_error"]["status"], "unavailable")
        self.assertIsNone(row["final_error"]["mean_rmse_body"])

    def test_false_pass_label_cannot_override_failing_row_or_pending_manual(self):
        session = self.fixture("gate")
        comparison = self.read(session / "final/comparison.json")
        comparison["validation_scenarios"][3]["gates"]["height"] = False
        write(session / "final/comparison.json", comparison)
        self.assertEqual(benchmark.build_benchmark([session.name])["sessions"][0]["status"], "fail")
        comparison["validation_scenarios"][3]["gates"]["height"] = True
        comparison["manual_validation_status"] = "pending"
        write(session / "final/comparison.json", comparison)
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertFalse(row["success"])
        self.assertEqual(row["gates"]["manual"], "unverified")

    def test_denied_outside_requests_are_separate_from_successful_access(self):
        session = self.root / "sessions/audit"
        session.mkdir(parents=True)
        events = [{"tool": "fs_read", "allow": False, "paths": ["../secret"], "reason": "outside target"},
                  {"tool": "execute_pwsh", "allow": False, "paths": [], "reason": "shell disabled"},
                  {"tool": "fs_read", "allow": True, "paths": ["source/player.gd"]}]
        result = benchmark._tool_audit(session, events, "complete")
        self.assertEqual(result["denied_count"], 2)
        self.assertEqual(result["denied_outside_workspace_count"], 1)
        self.assertEqual(result["other_denied_count"], 1)
        self.assertIn("not evidence", result["note"])

    def test_path_escape_is_rejected_for_input_and_embedded_evidence(self):
        for value in ("../outside", self.root / "outside", "deep/nested"):
            with self.subTest(path=str(value)), self.assertRaisesRegex(ValueError, "ROOT/sessions"):
                benchmark.build_benchmark([value])
        session = self.fixture("badpath")
        state = self.read(session / "session.json")
        state["final"]["comparison_path"] = "../outside.json"
        write(session / "session.json", state)
        with self.assertRaisesRegex(ValueError, "escapes"):
            benchmark.build_benchmark([session.name])

    def test_output_is_offline_escaped_and_preserves_source_metadata(self):
        session = self.fixture("report")
        state = self.read(session / "session.json")
        state["status"] = "<script>bad()</script>"
        write(session / "session.json", state)
        before = (session / "session.json").read_bytes()
        result = benchmark.generate_benchmark_report([session.name, "missing-selected"])
        self.assertEqual(Path(result["json_path"]), self.root / "artifacts/phase2-report/benchmark.json")
        page = Path(result["html_path"]).read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", page)
        self.assertNotIn("<script>bad()", page)
        self.assertIn("unavailable", page)
        self.assertEqual((session / "session.json").read_bytes(), before)
        report = self.read(Path(result["json_path"]))
        self.assertEqual(len(report["sessions"]), 2)
        self.assertFalse(report["cli"]["matrix_complete"])

    def test_delivery_bundles_existing_video_diff_history_with_relative_links_without_changing_evidence(self):
        session = self.fixture("portable-session")
        source = session / "report"
        (source / "assets").mkdir(parents=True)
        (source / "session-assets").mkdir()
        (source / "index.html").write_text('<html><body><video src="assets/clip.mp4" controls></video>'
            '<a href="session-assets/patch.diff">Diff</a><a href="session-assets/history.json">Attempts</a></body></html>')
        (source / "assets/clip.mp4").write_bytes(b"test video bytes")
        (source / "session-assets/patch.diff").write_text("-before\n+after\n")
        write(source / "session-assets/history.json", {"attempts": ["candidate-01"]})
        (session / "not-report-secret.txt").write_text("must not be packaged")
        unselected = self.fixture("unselected-report") / "report"
        unselected.mkdir()
        (unselected / "index.html").write_text("must not be discovered")
        original = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in session.rglob("*") if path.is_file()}
        expected_evidence = benchmark.build_benchmark([session.name, "missing-visual"])
        with patch.object(benchmark, "build_benchmark", return_value=copy.deepcopy(expected_evidence)):
            result = benchmark.generate_benchmark_report([session.name, "missing-visual"])
        destination = Path(result["html_path"]).parent
        self.assertEqual(self.read(Path(result["json_path"])), expected_evidence)
        page = Path(result["html_path"]).read_text()
        self.assertIn('href="reports/portable-session/index.html">portable-session</a>', page)
        self.assertIn("Visual report unavailable", page)
        copied = destination / "reports/portable-session"
        self.assertEqual({path.relative_to(copied) for path in copied.rglob("*") if path.is_file()},
                         {path.relative_to(source) for path in source.rglob("*") if path.is_file()})
        for path in source.rglob("*"):
            if path.is_file():
                self.assertEqual((copied / path.relative_to(source)).read_bytes(), path.read_bytes())
        self.assertFalse((destination / "reports/unselected-report").exists())
        self.assertFalse((copied / "not-report-secret.txt").exists())
        self.assertEqual(original, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in session.rglob("*") if path.is_file()})
        # The delivery remains usable after it is moved away from the workspace.
        parser = benchmark._ReportLinks()
        parser.feed((copied / "index.html").read_text())
        for link in parser.links:
            self.assertTrue((copied / link).is_file())
            self.assertTrue((copied / link).resolve().is_relative_to(destination))

    def test_packaging_rejects_external_or_missing_asset_links_and_source_hardlinks(self):
        session = self.fixture("unsafe-report")
        source = session / "report"
        source.mkdir()
        outside = session / "private.txt"
        outside.write_text("private outside report")
        output = self.root / "artifacts/phase2-report"
        output.mkdir(parents=True)
        for link in ("../private.txt", "%2e%2e/private.txt", outside.as_uri(), "https://example.test/clip.mp4", "missing.mp4"):
            with self.subTest(link=link):
                (source / "index.html").write_text(f'<video src="{link}"></video>')
                packaged = benchmark._package_reports([session.name], output)
                self.assertEqual(packaged[session.name]["status"], "unavailable")
                self.assertFalse((output / "reports" / session.name).exists())
                self.assertEqual(outside.read_text(), "private outside report")
        (source / "index.html").write_text("local report")
        os.link(outside, source / "linked-private.txt")
        self.assertEqual(benchmark._package_reports([session.name], output)[session.name]["status"], "unavailable")
        self.assertFalse((output / "reports" / session.name).exists())

    def test_packaging_rejects_reparse_points_and_removes_stale_unselected_copies(self):
        session = self.fixture("regular-report")
        source = session / "report"
        source.mkdir()
        (source / "index.html").write_text("current local report")
        output = self.root / "artifacts/phase2-report"
        output.mkdir(parents=True)
        stale = output / "reports/old-unselected"
        stale.mkdir(parents=True)
        (stale / "index.html").write_text("stale")
        self.assertEqual(benchmark._package_reports([session.name], output)[session.name]["status"], "available")
        self.assertFalse(stale.exists())
        original_lstat = Path.lstat

        def linked_metadata(path):
            if path == source:
                return SimpleNamespace(st_mode=original_lstat(path).st_mode, st_file_attributes=0x400)
            return original_lstat(path)

        with patch.object(Path, "lstat", linked_metadata):
            result = benchmark._package_reports([session.name], output)
        self.assertEqual(result[session.name]["status"], "unavailable")
        self.assertIn("linked or external", result[session.name]["reason"])
        self.assertFalse((output / "reports" / session.name).exists())

    def test_delivery_refuses_linked_output_before_overwriting_source_or_existing_bundle(self):
        session = self.fixture("output-boundary")
        original = (session / "session.json").read_bytes()
        output = self.root / "artifacts/phase2-report"
        output.mkdir(parents=True)
        os.link(session / "session.json", output / "benchmark.json")
        with self.assertRaisesRegex(ValueError, "independent regular"):
            benchmark.generate_benchmark_report([session.name])
        self.assertEqual((session / "session.json").read_bytes(), original)
        self.assertFalse((output / "reports").exists())

    def test_same_model_is_not_enough_when_cohort_source_or_runtime_differs(self):
        names = []
        for mode in ("tools", "baseline"):
            for index in range(3):
                name = f"cohort-{mode}-{index}"
                self.fixture(name, mode=mode)
                names.append(name)
        path = self.root / "sessions" / names[-1] / "session.json"
        state = self.read(path)
        state["source_commit"] = "different-reference-revision"
        write(path, state)
        report = benchmark.build_benchmark(names)
        self.assertFalse(report["cli"]["comparable"])
        self.assertFalse(report["cli"]["outcome_comparable"])
        self.assertEqual(report["cli"]["cohort_identity"]["mismatched_components"], ["source_commit"])
        self.assertIsNone(report["cli"]["observed_difference"])

    def test_duplicate_scenarios_and_nonfinite_error_cannot_pass(self):
        session = self.fixture("duplicate-scenarios")
        comparison = self.read(session / "final/comparison.json")
        comparison["validation_scenarios"][1]["scenario"] = comparison["validation_scenarios"][0]["scenario"]
        write(session / "final/comparison.json", comparison)
        self.assertFalse(benchmark.build_benchmark([session.name])["sessions"][0]["success"])
        session = self.fixture("not-a-number")
        comparison = self.read(session / "final/comparison.json")
        comparison["validation_scenarios"][0]["after_rmse_body"] = float("nan")
        write(session / "final/comparison.json", comparison)
        row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["gates"]["numerical"], "fail")
        self.assertEqual(row["final_error"]["status"], "unavailable")

    def test_capture_matrix_and_selected_metadata_are_not_just_hash_labels(self):
        session = self.fixture("manifest-slots")
        path = session / "runs/after-01/manifest.json"
        manifest = self.read(path)
        manifest["scenario"] = self.read(session / "runs/after-00/manifest.json")["scenario"]
        write(path, manifest)
        self.assertEqual(benchmark.build_benchmark([session.name])["sessions"][0]["gates"]["source_hashes"], "fail")
        session = self.fixture("candidate-final")
        state = self.read(session / "session.json")
        state["final"]["candidate_id"] = "candidate-02"
        write(session / "session.json", state)
        self.assertEqual(benchmark.build_benchmark([session.name])["sessions"][0]["gates"]["source_hashes"], "fail")

    def test_native_ide_export_is_reaudited_but_never_becomes_cli_configuration_proof(self):
        session = self.fixture("native-ide", surface="ide")
        (session / "kiro/launch.json").unlink()
        exported = session / "ide/native.zip"
        exported.parent.mkdir()
        exported.write_bytes(b"bounded native ZIP fixture parsed by mocked auditor")
        record = {"surface": "ide", "session_id": session.name, "export": {"path": "ide/native.zip", "sha256": benchmark._sha(exported)}}
        write(session / "ide/evidence.json", record)
        fresh = {**record, "status": "limited", "strict_configuration_verified": False,
                 "configuration": {"status": "pass"}, "model": {"status": "limited", "historical_effort_status": "unverified"},
                 "tools": {"status": "valid"}, "authorship": {"status": "verified", "candidate_id": "candidate-01"}}
        with patch.object(benchmark, "audit_ide_export", return_value=fresh) as auditor:
            report = benchmark.build_benchmark([session.name])
        auditor.assert_called_once_with(session, exported, expected_agent="kiroclops-tools", expected_model="claude-opus-5",
                                       expected_effort="high", candidate_id="candidate-01")
        row = report["sessions"][0]
        self.assertEqual(row["surface"], "ide")
        self.assertEqual(row["authorship"]["status"], "pass")
        self.assertEqual(row["model"]["status"], "unverified")
        self.assertFalse(row["success"])
        self.assertEqual(report["cli"]["arms"]["tools"]["selected_runs"], 0)
        exported.write_bytes(b"changed native export")
        with patch.object(benchmark, "audit_ide_export") as auditor:
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        auditor.assert_not_called()
        self.assertEqual(row["ide_evidence"]["status"], "failed")

    def test_early_completed_ineligible_attempts_are_known_failure_without_invented_final_rmse(self):
        session = self.fixture("ended-ineligible", mode="baseline")
        state = self.read(session / "session.json")
        state["selected_candidate_id"] = None
        state["candidates"] = [{"candidate_id": f"candidate-{index:02d}", "status": "evaluated",
                                "feedback": {"status": "ok", "eligible": False}, "smoke": {"status": "passed"}} for index in (1, 2)]
        write(session / "session.json", state)
        with patch.object(benchmark, "_no_eligible_finish", return_value={"status": "pass"}):
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["experiment_outcome"]["status"], "ended_no_eligible_candidate")
        self.assertEqual(row["experiment_outcome"]["accountability_status"], "unverified")
        self.assertEqual(row["final_error"]["status"], "unavailable")
        self.assertIsNone(row["final_error"]["mean_rmse_body"])
        self.assertEqual(row["budget"]["unused_candidate_slots"], 1)
        self.assertEqual(row["budget"]["unused_seconds"], 1680)
        state["candidates"][1] = {"candidate_id": "candidate-02", "status": "failed", "error": {"code": "worker_interrupted"}}
        write(session / "session.json", state)
        with patch.object(benchmark, "_no_eligible_finish", return_value={"status": "pass"}):
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["experiment_outcome"]["status"], "unverified")

    def test_one_to_three_attempts_need_actual_finished_turn_and_bound_rejection(self):
        candidate = {"status": "evaluated", "feedback": {"status": "ok", "eligible": False}, "smoke": {"status": "passed"}}
        for count in (1, 2, 3):
            for completion, rejection, expected in (("pass", "pass", "ended_no_eligible_candidate"),
                    ("unverified", "pass", "unverified"), ("pass", "unverified", "unverified")):
                result = benchmark._experiment_outcome([candidate] * count, None, "unverified",
                    completion={"status": completion}, finish_rejection={"status": rejection})
                self.assertEqual(result["status"], expected)
        candidate["feedback"]["status"] = "invalid"
        result = benchmark._experiment_outcome([candidate], None, "unverified",
            completion={"status": "pass"}, finish_rejection={"status": "pass"})
        self.assertEqual(result["status"], "unverified")

    def test_accounted_early_failure_counts_as_outcome_but_never_invents_numeric_comparison(self):
        names = []
        for mode in ("tools", "baseline"):
            for index in range(3):
                name = f"cohort-{mode}-{index}"
                self.fixture(name, mode=mode)
                names.append(name)
        session = self.root / "sessions/cohort-baseline-0"
        state = self.read(session / "session.json")
        state["selected_candidate_id"] = None
        candidate = state["candidates"][0]
        candidate.update(status="evaluated", feedback={"status": "ok", "eligible": False})
        write(session / "session.json", state)
        write(session / "candidates/candidate-01/candidate.json", candidate)
        with patch.object(benchmark, "_no_eligible_finish", return_value={"status": "pass"}), \
                patch.object(benchmark, "_attempt_integrity", return_value={"status": "pass"}):
            report = benchmark.build_benchmark(names)
        self.assertTrue(report["cli"]["outcome_comparable"])
        self.assertFalse(report["cli"]["comparable"])
        self.assertEqual(report["cli"]["outcome_difference"]["success_count_difference"], 1)
        self.assertIsNone(report["cli"]["observed_difference"])
        self.assertIsNone(report["cli"]["arms"]["baseline"]["mean_final_rmse_body"])
        self.assertEqual(report["cli"]["arms"]["baseline"]["ended_without_eligible_candidate"], 1)

    def test_finish_rejection_requires_fresh_native_replay_after_last_capture(self):
        from kiroclops.integration import authorship
        session = self.fixture("bound-finish", mode="baseline")
        candidates = [{"candidate_id": "candidate-01"}, {"candidate_id": "candidate-02"}]
        record = {"status": "verified", "candidate_id": "candidate-02",
                  "candidate_boundary": {"tool_call_id": "capture-2", "result_line": 40},
                  "normalization": {"controlled_commands": [
                      {"action": "capture", "tool_call_id": "capture-1"},
                      {"action": "capture", "tool_call_id": "capture-2"},
                      {"action": "finish", "tool_call_id": "finish"}],
                      "controlled_rejections": [{"action": "finish", "code": "no_eligible_candidate", "exit_code": 1,
                                                  "tool_call_id": "finish", "stdout_line": 50}]}}
        with patch.object(authorship, "audit_kiro_authorship", return_value=record) as replay:
            self.assertEqual(benchmark._no_eligible_finish(session, candidates)["status"], "pass")
            replay.assert_called_with(session, candidate_id="candidate-02")
            record["normalization"]["controlled_rejections"][0]["stdout_line"] = 30
            self.assertEqual(benchmark._no_eligible_finish(session, candidates)["status"], "unverified")
            record["normalization"]["controlled_rejections"][0]["stdout_line"] = 50
            record["status"] = "unverified"  # Prose or incomplete tool output cannot replace the strict adapter.
            self.assertEqual(benchmark._no_eligible_finish(session, candidates)["status"], "unverified")

    def ide_completion_fixture(self, name):
        session = self.fixture(name, surface="ide")
        state = self.read(session / "session.json")
        state.update(started_at="2026-09-18T10:00:00+00:00", deadline_at=1789731000, active_job_id=None)
        state["final"]["numerical_status"] = "pass"
        write(session / "session.json", state)
        prompt = session / "target/.kiro/kiroclops-prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("prepared experiment prompt", encoding="utf-8")
        config_path = session / "target/.kiro/kiroclops-session.json"
        write(config_path, {"surface": "ide", "integration": "ide_power", "mode": "tools",
              "agent_name": "kiroclops-tools", "model": benchmark.EXPECTED_MODEL, "effort": "high",
              "target_dir": str(session / "target"), "generated_hashes": {prompt.name: benchmark._sha(prompt)}})
        screenshot = session / "ide/settings.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"test-only observed settings image")
        observation = {"agent": "kiroclops-tools", "model": benchmark.EXPECTED_MODEL, "effort": "high",
                       "evidence": [{"path": "ide/settings.png", "sha256": benchmark._sha(screenshot)}]}
        record = {"schema_version": 2, "surface": "ide", "transport": "ide_gui", "mode": "tools", "status": "running",
                  "session_id": name, "started_at": state["started_at"], "deadline_at": state["deadline_at"],
                  "model_requested": benchmark.EXPECTED_MODEL, "effort_requested": "high",
                  "configuration_path": str(config_path), "configuration_sha256": benchmark._sha(config_path),
                  "prompt_path": str(prompt), "prompt_sha256": benchmark._sha(prompt), "ui_observed": observation}
        write(session / "kiro/launch-start.json", record)
        export = {"path": "ide/native.zip", "sha256": "freshly-reaudited-export"}
        timing = {"first_message_at": "2026-09-18T10:00:01+00:00", "last_message_at": "2026-09-18T10:02:00+00:00",
                  "last_turn_end_at": "2026-09-18T10:02:00+00:00", "last_turn_stop_reason": "end_turn",
                  "turn_start_count": 1, "turn_end_count": 1, "later_turn_start_present": False}
        record.update(status="completed", finished_at=timing["last_turn_end_at"], elapsed_seconds=120,
                      export=export, timing=timing, ui_observed_after=observation,
                      prompt_result={"stopReason": "end_turn"},
                      start_receipt={"path": "kiro/launch-start.json", "sha256": benchmark._sha(session / "kiro/launch-start.json")})
        write(session / "kiro/launch.json", record)
        write(session / "ide/finish-receipt.json", record)
        powers = [{"action": "activate"}, {"action": "readSkill", "skillName": "motion-calibration"},
                  {"action": "use", "serverName": "kiroclops", "toolName": "evaluate_candidate"},
                  {"action": "use", "serverName": "kiroclops", "toolName": "finalize_candidate"}]
        calls = [{"success": True, "power": dict(power, powerName="kiroclops")} for power in powers]
        for index, action, status in ((2, "evaluate", "evaluated"), (3, "finalize", "finished")):
            jid = f"job-{action}"
            calls[index].update(tool_call_id=action, structured_result={"status": "accepted", "job_id": jid, "candidate_id": "candidate-01"})
            result = {"candidate_id": "candidate-01", "status": status}
            if action == "finalize":
                result.update(comparison_path="final/comparison.json", numerical_status="pass")
            write(session / f"jobs/{jid}.json", {"schema_version": 2, "job_id": jid, "session_id": session.name,
                  "action": action, "status": "succeeded", "candidate_id": "candidate-01", "result": result,
                  "created_at": f"2026-09-18T10:00:{index * 10:02d}+00:00",
                  "finished_at": f"2026-09-18T10:00:{index * 10 + 5:02d}+00:00"})
        native = {"status": "limited", "recomputed_from_original_zip": True, "export": export, "timing": timing,
                  "configuration": {"status": "pass"}, "authorship": {"status": "verified", "candidate_boundary": {"tool_call_id": "evaluate"}},
                  "model": {"status": "limited", "reasoning_models": [benchmark.EXPECTED_MODEL]},
                  "tools": {"status": "complete", "calls": calls}}
        return session, state, record, native

    def test_native_ide_demo_can_be_demonstrated_while_strict_effort_is_unverified(self):
        session, state, record, native = self.ide_completion_fixture("ide-two-axes")
        result = benchmark._ide_completion(session, state, record, native, [])
        self.assertEqual(result["status"], "pass")
        self.assertNotIn("returncode", record)
        with patch.object(benchmark, "_ide_evidence", return_value=native):
            report = benchmark.build_benchmark([session.name])
        row = report["sessions"][0]
        self.assertEqual(row["demo_status"], "demonstrated")
        self.assertEqual(row["ide_demonstration"]["phase_acceptance"], "demonstrated_with_configuration_limit")
        self.assertEqual(row["strict_model_configuration"], "unverified")
        self.assertFalse(row["success"])
        self.assertEqual(report["ide"]["demonstrated_runs"], 1)
        self.assertEqual(report["cli"]["arms"]["tools"]["selected_runs"], 0)

    def test_native_ide_completion_rechecks_receipt_timing_ui_and_profile(self):
        session, state, record, native = self.ide_completion_fixture("ide-receipts")
        original = copy.deepcopy(record)
        record["elapsed_seconds"] = 1
        write(session / "ide/finish-receipt.json", record)
        self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")
        record = original
        write(session / "ide/finish-receipt.json", record)
        path = session / "ide/settings.png"
        raw = path.read_bytes()
        path.write_bytes(b"changed settings evidence")
        self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")
        path.write_bytes(raw)
        (session / "target/.kiro/kiroclops-prompt.md").write_text("changed prompt")
        self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")

    def test_native_ide_completion_uses_fresh_audited_experiment_bounds(self):
        session, state, record, native = self.ide_completion_fixture("ide-experiment-timing")
        native["timing"] = dict(native["timing"],
            first_message_at="2026-09-18T09:55:00+00:00",
            last_message_at="2026-09-18T10:02:00.045000+00:00",
            first_experiment_message_at="2026-09-18T10:00:01+00:00",
            last_experiment_message_at="2026-09-18T10:02:00+00:00")
        original = copy.deepcopy(native)
        result = benchmark._ide_completion(session, state, record, native, [])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["message_timing_basis"], "audited_experiment")
        self.assertEqual(result["elapsed_seconds"], 120)
        self.assertEqual(native, original)  # Raw timestamps remain evidence.
        # A saved receipt's favorable timing cannot override the fresh auditor.
        native["timing"]["first_experiment_message_at"] = "2026-09-18T09:59:59+00:00"
        self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")
        native["timing"] = original["timing"]
        native["timing"]["last_experiment_message_at"] = "2026-09-18T10:02:00.001000+00:00"
        self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")

    def test_native_ide_old_timing_fallback_never_masks_partial_or_invalid_new_fields(self):
        session, state, record, native = self.ide_completion_fixture("ide-old-timing")
        result = benchmark._ide_completion(session, state, record, native, [])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["message_timing_basis"], "legacy_raw_messages")
        old = copy.deepcopy(native["timing"])
        for replacement in ({"first_experiment_message_at": None},
                            {"last_experiment_message_at": "2026-09-18T10:02:00+00:00"},
                            {"first_experiment_message_at": "invalid", "last_experiment_message_at": None}):
            with self.subTest(replacement=replacement):
                native["timing"] = dict(old, **replacement)
                self.assertEqual(benchmark._ide_completion(session, state, record, native, [])["status"], "fail")

    def test_native_ide_demo_requires_post_ui_power_use_and_each_quality_gate(self):
        session, state, record, native = self.ide_completion_fixture("ide-missing-proof")
        completion = benchmark._ide_completion(session, state, record, native, [])
        gates = {key: "pass" for key in ("numerical", "manual", "smoke", "wall_behavior", "source_hashes", "authorship")}
        jobs = benchmark._ide_job_bindings(session, state, native, [])
        self.assertEqual(benchmark._ide_demo(native, completion, gates, True, jobs)["status"], "demonstrated")
        completion["ui_after"] = {"status": "unverified"}
        self.assertEqual(benchmark._ide_demo(native, completion, gates, True, jobs)["status"], "unverified")
        completion["ui_after"] = {"status": "pass"}
        native["tools"]["calls"].pop()
        jobs = benchmark._ide_job_bindings(session, state, native, [])
        self.assertEqual(benchmark._ide_demo(native, completion, gates, True, jobs)["status"], "unverified")
        gates["wall_behavior"] = "fail"
        self.assertEqual(benchmark._ide_demo(native, completion, gates, True, jobs)["status"], "failed")

    def test_native_logical_error_cannot_count_as_finalization_despite_transport_success(self):
        from kiroclops.integration.ide_evidence import _structured_job_result
        session, state, record, native = self.ide_completion_fixture("ide-logical-error")
        native["tools"]["calls"][-1]["structured_result"] = _structured_job_result(json.dumps(
            {"structuredContent": {"status": "error", "error": {"code": "active_job"}}}))
        self.assertTrue(native["tools"]["calls"][-1]["success"])
        with patch.object(benchmark, "_ide_evidence", return_value=native):
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["demo_status"], "unverified")
        self.assertFalse(row["ide_demonstration"]["checks"]["native_finalization"])
        self.assertEqual(row["ide_demonstration"]["native_jobs"]["status"], "unverified")

    def test_native_accepted_jobs_must_finish_in_this_session_for_the_audited_candidate(self):
        session, state, record, native = self.ide_completion_fixture("ide-job-bindings")
        path = session / "jobs/job-finalize.json"
        original = self.read(path)
        binding = benchmark._ide_job_bindings(session, state, native, [])
        self.assertEqual(binding["status"], "pass")
        self.assertEqual(binding["jobs"]["finalize"]["sha256"], benchmark._sha(path))
        for replacement in ({"session_id": "other-session"}, {"candidate_id": "candidate-02"},
                            {"status": "failed"}, {"action": "evaluate"},
                            {"finished_at": "2026-09-18T10:05:00+00:00"},
                            {"result": {"status": "finished", "candidate_id": "candidate-01", "comparison_path": "other.json"}}):
            with self.subTest(replacement=replacement):
                write(path, dict(original, **replacement))
                self.assertEqual(benchmark._ide_job_bindings(session, state, native, [])["status"], "fail")
        write(path, original)
        native["authorship"]["candidate_boundary"]["tool_call_id"] = "different-evaluation"
        self.assertEqual(benchmark._ide_job_bindings(session, state, native, [])["status"], "fail")
        native["authorship"]["candidate_boundary"]["tool_call_id"] = "evaluate"
        native["tools"]["calls"][-1]["structured_result"]["job_id"] = "job-missing"
        self.assertEqual(benchmark._ide_job_bindings(session, state, native, [])["status"], "unverified")

    def test_exhausted_attempt_source_attestation_requires_six_actual_calibration_slots(self):
        session = self.fixture("attempt-integrity")
        state = self.read(session / "session.json")
        candidate = state["candidates"][0]
        state["baseline_runs"]["calibration"] = []
        candidate["calibration_runs"] = []
        for role, expected, destination in (("before", state["baseline_hashes"], state["baseline_runs"]["calibration"]),
                                            ("after", candidate["source_hashes"], candidate["calibration_runs"])):
            for index, (hold, repeat) in enumerate((hold, repeat) for hold in (3, 24) for repeat in (1, 2, 3)):
                run = session / f"calibration/{role}-{index}"
                write(run / "manifest.json", {"game": "slay", "variant": role, "status": "ok", "fps": 60, "frame_count": 240,
                      "scenario": {"hold_ticks": hold, "repeat": repeat, "spawn_offset": 0, "start_tick": 30, "split": "calibration"},
                      "provenance": {"full_source_hashes_before": expected, "full_source_hashes_after": expected}})
                destination.append(str(run))
        write(session / "candidates/candidate-01/candidate.json", candidate)
        self.assertEqual(benchmark._attempt_integrity(session, state, [candidate], [])["status"], "pass")
        candidate["calibration_runs"].pop()
        write(session / "candidates/candidate-01/candidate.json", candidate)
        self.assertEqual(benchmark._attempt_integrity(session, state, [candidate], [])["status"], "unverified")

    def test_manual_pass_needs_current_hash_bound_review_evidence(self):
        session = self.fixture("manual-receipt")
        state = self.read(session / "session.json")
        comparison = self.read(session / "final/comparison.json")
        self.assertEqual(REAL_MANUAL_EVIDENCE(session, state, state["candidates"][0], comparison, [])["status"], "unverified")
        with patch.object(benchmark, "_manual_evidence", return_value={"status": "unverified"}):
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertFalse(row["success"])
        self.assertEqual(row["gates"]["manual"], "unverified")

    def test_review_receipt_detects_raw_analysis_and_annotation_changes(self):
        from kiroclops.integration import evaluation, review
        session = self.fixture("review-binding")
        state = self.read(session / "session.json")
        candidate = state["candidates"][0]
        run = session / "reviewed/run"
        annotation = session / "annotations/manual.json"
        write(annotation, {"independent": "point"})
        report = {"status": "ok", "game": "slay", "variant": "after", "scenario": {}, "valid_ratio": 1,
                  "events": {}, "metrics": {}, "errors": [], "annotation_validation": {key: "pass" for key in
                      ("status", "point_status", "event_status", "control_point_count", "median_error_px", "tolerance_px")},
                  "provenance": {"annotation_sha256": benchmark._sha(annotation)}}
        write(run / "analysis.json", report)
        write(run / "manifest.json", {"frame_count": 240, "fps": 60})
        saved = {key: report[key] for key in ("status", "game", "variant", "scenario", "valid_ratio", "events", "metrics", "errors")}
        saved.update(analysis_path=str(run / "analysis.json"), annotation_path=str(annotation), manual_validation=report["annotation_validation"])
        receipt = {"candidate_id": candidate["candidate_id"], "measurements_unchanged": True, "raw_evidence_unchanged": True,
                   "sources_unchanged": True, "measurement_sha256": {str(run): "measurement-hash"},
                   "raw_evidence_sha256": {str(run): "raw-hash"}, "run_reviews": [saved]}
        write(session / "final/review.json", receipt)
        state["final"]["manual_review_path"] = "final/review.json"
        groups = {"reference": [], "baseline": [], "candidate": [run]}
        comparison = {"numerical_status": "pass", "manual_validation_status": "pass"}
        with patch.object(review, "_runs", return_value=groups), patch.object(review, "_measurements", return_value="measurement-hash"), \
                patch.object(review, "_raw_fingerprint", return_value="raw-hash") as raw, \
                patch.object(evaluation, "final_comparison", return_value=comparison):
            self.assertEqual(REAL_MANUAL_EVIDENCE(session, state, candidate, comparison, [])["status"], "pass")
            raw.return_value = "changed-pixel"
            self.assertEqual(REAL_MANUAL_EVIDENCE(session, state, candidate, comparison, [])["status"], "fail")
            raw.return_value = "raw-hash"
            write(annotation, {"independent": "changed"})
            self.assertEqual(REAL_MANUAL_EVIDENCE(session, state, candidate, comparison, [])["status"], "fail")

    def test_original_smoke_pass_cannot_replace_supplemental_wall_proof(self):
        session = self.fixture("wall-evidence")
        self.assertEqual(REAL_WALL_EVIDENCE(session)["status"], "unverified")
        with patch.object(benchmark, "_wall_evidence", side_effect=REAL_WALL_EVIDENCE):
            row = benchmark.build_benchmark([session.name])["sessions"][0]
        self.assertEqual(row["gates"]["smoke"], "pass")
        self.assertEqual(row["gates"]["wall_behavior"], "unverified")
        self.assertFalse(row["success"])
        write(session / "final/gameplay-review.json", {"status": "passed"})
        for result, expected in (({"status": "passed", "replayed": True}, "pass"), ({"status": "failed"}, "fail")):
            module = SimpleNamespace(verify_wall_review=lambda path, result=result: result)
            with patch.dict("sys.modules", {"kiroclops.integration.gameplay_review": module}):
                self.assertEqual(REAL_WALL_EVIDENCE(session)["status"], expected)


if __name__ == "__main__":
    unittest.main()
