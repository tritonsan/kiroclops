"""Sol cohort proof and packaging regressions; no engine/model is invoked."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import model_ground_benchmark_v1 as mb
from kiroclops.integration import model_cli_v1 as launcher
from kiroclops.integration.kiro import StreamConfigurationGuard
from kiroclops.integration.storage import sha256, write_json


class ModelGroundBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module in (mb, mb.ground, mb.proof):
            mocked = patch.object(module, "ROOT", self.root)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.session = self.root / "sessions" / mb.SESSION_IDS[0]
        self.session.mkdir(parents=True)
        self.locks = {"tracking_lock_sha256": "1" * 64, "reference_lock_sha256": "2" * 64}
        self.cohort = {"schema_version": 1, "cohort_id": mb.COHORT_ID, "experiment_id": "ground-motion-v1",
            "model": "gpt-5.6-sol", "effort": "high", "cli_session_ids": list(mb.SESSION_IDS),
            "max_candidates": 3, "max_seconds": 1800, "cli_version": "kiro-cli-chat 2.21.0", "kas_version": "0.54.8",
            "registered_at": "2026-09-19T08:00:00+00:00", "launcher": launcher.launcher_binding(),
            "reporter_source_hashes": mb.reporter_source_hashes(), "experiment_locks": self.locks,
            "supplemental_instructions_sha256": "3" * 64, "background_implementation_sha256": "4" * 64}
        self.write_cohort()

    def write_cohort(self):
        write_json(self.root / mb.COHORT_PATH, self.cohort)

    def launch(self):
        expected = {"mode": "kiroclops-tools", "model": "gpt-5.6-sol", "effort": "high"}
        event = {"method": "session/update", "params": {"update": {"sessionUpdate": "config_option_update",
            "configOptions": [{"id": "mode", "currentValue": expected["mode"]},
                              {"id": "model", "currentValue": expected["model"]},
                              {"id": "effortLevel", "currentValue": expected["effort"],
                               "options": [{"value": "low"}, {"value": "high"}]}]}}}
        events = [{"configOptions": [{"id": "model", "currentValue": "auto"}]}, event,
                  {"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk"}}},
                  {"id": 9, "result": {"stopReason": "end_turn"}}]
        guard = StreamConfigurationGuard("kiroclops-tools", "gpt-5.6-sol", "high")
        for e in events:
            guard.observe(e)
        launch = {"surface": "cli", "transport": "acp", "mode": "tools", "status": "completed",
            "model_requested": "gpt-5.6-sol", "effort_requested": "high", "model_reported": ["gpt-5.6-sol"],
            "model_confirmation": "stream_confirmed", "model_launcher": self.cohort["launcher"],
            "cli_version": self.cohort["cli_version"], "kas_version": self.cohort["kas_version"],
            "prompt_sent": True, "prompt_result": {"stopReason": "end_turn"},
            "stream_configuration": guard.summary(), "process_returncode_after_cleanup": -15,
            "started_at": "2026-09-19T09:00:00+00:00", "finished_at": "2026-09-19T09:01:00+00:00",
            "elapsed_seconds": 60, "timeout_seconds": 1800,
            "acp_preflight": {"selected": expected, "selection_echo_verified": True,
                "effort_selection": {"observed_model": "gpt-5.6-sol", "selected": "high", "values": ["low", "high"]}}}
        return launch, events

    def state(self):
        candidate = {"candidate_id": "candidate-01", "status": "finalized", "source_hashes": {"player.gd": "source"},
                     "feedback": {"eligible": True}}
        comparison = self.session / "final/comparison.json"
        write_json(comparison, {"saved": True})
        final = {"candidate_id": candidate["candidate_id"], "comparison_path": str(comparison), "numerical_status": "pass",
                 "ground_status": "pass", "jump_status": "pass", "smoke_status": "passed", "measurement_status": "pass"}
        state = {"schema_version": 3, "session_id": self.session.name, "experiment_id": "ground-motion-v1", "mode": "tools",
            "status": "finished", "started_at": "2026-09-19T09:00:00.010000+00:00",
            "deadline_at": datetime.fromisoformat("2026-09-19T09:30:00.010000+00:00").timestamp(),
            "candidates": [candidate], "selected_candidate_id": candidate["candidate_id"], "sealed_at": "2026-09-19T09:00:50+00:00",
            "final": final, "baseline_hashes": {"baseline.gd": "before"}, "experiment_locks": self.locks}
        write_json(self.session / "session.json", state)
        job = {"job_id": "job-final", "session_id": self.session.name, "action": "finalize", "candidate_id": candidate["candidate_id"],
            "status": "succeeded", "started_at": "2026-09-19T09:00:50+00:00", "finished_at": "2026-09-19T09:00:55+00:00",
            "result": {**final, "status": "finished"}}
        write_json(self.session / "jobs/job-final.json", job)
        return state, candidate, job

    def test_only_registered_sol_cohort_and_no_ide_or_replacements(self):
        self.assertEqual(mb._cohort()[0], self.cohort)
        original = deepcopy(self.cohort)
        for changes in ({"model": "claude-opus-5"}, {"effort": "max"}, {"cli_session_ids": ["cli-ground-01"] * 3},
                        {"ide_session_ids": ["ide-sol-01"]}, {"max_seconds": 3600}, {"replacement_runs": True},
                        {"reporter_source_hashes": {}}, {"registered_at": None}):
            with self.subTest(changes=changes):
                self.cohort = {**original, **changes}
                self.write_cohort()
                with self.assertRaises(ValueError):
                    mb._cohort()
        self.cohort = original
        self.write_cohort()
        for ids in (["cli-ground-01"], ["../escape"], [mb.SESSION_IDS[0]] * 2):
            with self.assertRaises(ValueError):
                mb.build_benchmark(ids)

    def test_unrun_cohort_has_no_invented_error_success_or_ide_requirement(self):
        with patch.object(mb.ground, "verify_ground_lock", side_effect=FileNotFoundError()):
            report = mb.build_benchmark()
        self.assertEqual([r["status"] for r in report["sessions"]], ["not_run"] * 3)
        self.assertEqual(report["cli"]["successes"], 0)
        self.assertIsNone(report["cli"]["mean_final_rmse_body"])
        self.assertEqual(report["acceptance"], "pending_or_failed")
        self.assertEqual(report["ide"], "not_part_of_this_cohort")

    def test_explicit_sol_replay_ignores_startup_auto_and_never_uses_opus_default(self):
        launch, events = self.launch()
        result = mb._configuration(launch, events, "complete", self.cohort)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["expected"]["model"], "gpt-5.6-sol")
        self.assertIn("auto", result["observed_models"])
        for changed in ({"id": "model", "currentValue": "claude-opus-5"}, {"id": "effortLevel", "currentValue": "low"}):
            with self.subTest(changed=changed):
                bad = events + [{"configOptions": [changed]}]
                self.assertEqual(mb._configuration(launch, bad, "complete", self.cohort)["status"], "fail")

    def test_recorded_success_or_requested_argv_without_stream_proof_is_pending(self):
        launch, events = self.launch()
        self.assertEqual(mb._configuration(launch, [], "complete", self.cohort)["status"], "pending")
        self.assertEqual(mb._configuration(launch, events, "truncated", self.cohort)["status"], "pending")
        launch["acp_preflight"]["selection_echo_verified"] = False
        self.assertEqual(mb._configuration(launch, events, "complete", self.cohort)["status"], "pending")

    def test_effort_advertisement_for_startup_model_is_not_sol_capability_proof(self):
        launch, events = self.launch()
        events[0]["configOptions"].append({"id": "effortLevel", "options": [{"value": "high"}]})
        del events[1]["params"]["update"]["configOptions"][2]["options"]
        self.assertEqual(mb._configuration(launch, events, "complete", self.cohort)["status"], "pending")

    def test_new_workflow_extends_hash_locked_base_and_binds_exact_supplement(self):
        target = self.session / "target"
        target.mkdir()
        (target / "project.godot").write_text("[application]\n")
        write_json(self.session / "session.json", {"schema_version": 3, "session_id": self.session.name,
            "experiment_id": "ground-motion-v1", "mode": "tools", "status": "new", "candidates": []})
        extra = "Preserve the first airborne update."
        config = launcher.configure_model_cli(self.session, supplemental_instructions=extra)
        src = Path(launcher.frozen.server_root()) / "power/kiroclops/ground-motion.md"
        dest = self.root / "power/kiroclops/ground-motion.md"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(src.read_bytes())
        lock = {"tracking": {"files": {"power/kiroclops/ground-motion.md": sha256(dest)}}}
        self.cohort["supplemental_instructions_sha256"] = hashlib.sha256(extra.encode()).hexdigest()
        gate = mb._configuration_binding(self.session, self.cohort, lock)
        self.assertEqual(gate["status"], "pass")
        self.assertFalse(gate["supplemental_instructions"]["enforced_by_mcp"])
        self.cohort["supplemental_instructions_sha256"] = "a" * 64
        self.assertEqual(mb._configuration_binding(self.session, self.cohort, lock)["status"], "fail")
        self.assertEqual(config["model"], "gpt-5.6-sol")

    def test_finalization_job_must_succeed_and_bind_exact_result_not_only_state_finished(self):
        state, candidate, job = self.state()
        self.assertEqual(mb._finalization_job(self.session, state, candidate)["status"], "pass")
        for change in ({"status": "interrupted"}, {"session_id": "other"}, {"job_id": "wrong"}, {"error": "failed"}):
            with self.subTest(change=change):
                write_json(self.session / "jobs/job-final.json", {**job, **change})
                self.assertEqual(mb._finalization_job(self.session, state, candidate)["status"], "fail")
        write_json(self.session / "jobs/job-final.json", job)
        write_json(self.session / "jobs/job-duplicate.json", {**job, "job_id": "job-duplicate"})
        self.assertEqual(mb._finalization_job(self.session, state, candidate)["status"], "fail")

    def test_timing_preserves_registered_deadline_and_never_calls_recovery_time_runtime(self):
        state, _, job = self.state()
        launch, _ = self.launch()
        good = mb._timing(self.session, state, launch, self.cohort)
        self.assertEqual(good["status"], "pass")
        self.assertEqual(good["jobs"][0]["duration_seconds"], 5)
        for changes in ({"elapsed_seconds": 1800.01}, {"timeout_seconds": 3600}, {"elapsed_seconds": 0}, {"elapsed_seconds": 30}):
            with self.subTest(changes=changes):
                self.assertEqual(mb._timing(self.session, state, {**launch, **changes}, self.cohort)["status"], "fail")
        state["deadline_at"] += 3600
        self.assertEqual(mb._timing(self.session, state, launch, self.cohort)["status"], "fail")
        job.update(status="interrupted", finished_at="2026-09-19T12:00:00+00:00")
        write_json(self.session / "jobs/job-final.json", job)
        self.assertIsNone(mb._timing(self.session, state, launch, self.cohort)["jobs"][0]["duration_seconds"])

    def runtime_receipt(self):
        _, candidate, _ = self.state()
        checks = {}
        for name, normal in mb.PROBES.items():
            folder = self.root / "artifacts/model-trials/reviews" / self.session.name / name
            project, output = folder / "project", folder / "capture"
            project.mkdir(parents=True)
            write_json(output / "probe.json", {"probe": name, "normal_level": normal})
            checks[name] = {"project": str(project), "output": str(output), "status": "pass",
                            "receipt_sha256": sha256(output / "probe.json")}
        receipt = {"schema_version": 1, "cohort_id": mb.COHORT_ID, "session_id": self.session.name,
            "candidate_id": candidate["candidate_id"], "source_hashes": candidate["source_hashes"],
            "reviewed_by": "assistant_independent_review", "reviewed_at": "2026-09-19T10:00:00+00:00", "checks": checks}
        write_json(self.session / "review/model-trial-runtime.json", receipt)
        return candidate, receipt

    def test_runtime_replays_selected_source_and_two_exact_probe_identities(self):
        candidate, receipt = self.runtime_receipt()
        def verifier(project, output, *, expected_source_hashes):
            self.assertEqual(expected_source_hashes, candidate["source_hashes"])
            return {"status": "pass", "receipt_sha256": sha256(output / "probe.json")}
        with patch("kiroclops.integration.movement_probe.verify_probe", side_effect=verifier) as verify:
            self.assertEqual(mb._runtime(self.session, candidate, self.cohort)["status"], "pass")
            self.assertEqual(verify.call_count, 2)
        path = Path(receipt["checks"]["air_idle_transition"]["output"]) / "probe.json"
        write_json(path, {"probe": "run_jump_land_brake", "normal_level": True})
        with patch("kiroclops.integration.movement_probe.verify_probe") as verify:
            self.assertEqual(mb._runtime(self.session, candidate, self.cohort)["status"], "fail")
            verify.assert_not_called()

    def test_runtime_outer_pass_cannot_override_replayed_failure_or_missing_evidence(self):
        _, candidate, _ = self.state()
        self.assertEqual(mb._runtime(self.session, candidate, self.cohort)["status"], "pending")
        candidate, receipt = self.runtime_receipt()
        def failed(project, output, **kwargs):
            return {"status": "fail", "receipt_sha256": sha256(output / "probe.json")}
        with patch("kiroclops.integration.movement_probe.verify_probe", side_effect=failed):
            self.assertEqual(mb._runtime(self.session, candidate, self.cohort)["status"], "fail")
        receipt["source_hashes"] = {"other": "source"}
        write_json(self.session / "review/model-trial-runtime.json", receipt)
        with patch("kiroclops.integration.movement_probe.verify_probe") as verify:
            self.assertEqual(mb._runtime(self.session, candidate, self.cohort)["status"], "fail")
            verify.assert_not_called()

    def test_positive_negative_runtime_qualification_replays_actual_predicates(self):
        folder = self.root / "artifacts/model-trials/preflight/probes"
        script = self.root / "scripts/qualify_movement_probes.py"
        script.parent.mkdir()
        script.write_text("qualification implementation")
        cases = {"parent-air": ("air_idle_transition", False, "pass"),
                 "known-bad-air": ("air_idle_transition", False, "fail"),
                 "parent-combined-normal": ("run_jump_land_brake", True, "pass")}
        checks = {}
        for key, (name, normal, status) in cases.items():
            source = self.root / "sources" / ("bad" if key == "known-bad-air" else "parent")
            source.mkdir(parents=True, exist_ok=True)
            project, output = folder / key / "project", folder / key / "capture"
            project.mkdir(parents=True)
            write_json(output / "probe.json", {"probe": name, "normal_level": normal})
            actual = {"status": status, "receipt_sha256": sha256(output / "probe.json"),
                "checks": {"airborne_idle_precondition": True, "natural_idle_to_fall": True,
                           "horizontal_velocity_unchanged": status == "pass"}}
            checks[key] = {"project": str(project), "output": str(output), "source": str(source), "status": "pass",
                "original_source_hashes": {"source": source.name}, "source_preserved": True, "expected_status": status, "actual": actual}
        record = {"status": "pass", "script_sha256": sha256(script), "checks": checks}
        write_json(folder / "result.json", record)
        self.cohort["control_preflight"] = {"path": str(folder / "result.json"), "sha256": sha256(folder / "result.json")}
        def verify(project, output, **kwargs):
            return deepcopy(checks[output.parent.name]["actual"])
        with patch.object(mb, "project_hashes", side_effect=lambda p: {"source": p.name}), \
             patch("kiroclops.integration.movement_probe.verify_probe", side_effect=verify):
            self.assertEqual(mb._control_preflight(self.cohort)["status"], "pass")
            checks["known-bad-air"]["actual"] = {"status": "fail", "reason": "Broken engine"}
            write_json(folder / "result.json", record)
            self.cohort["control_preflight"]["sha256"] = sha256(folder / "result.json")
            self.assertEqual(mb._control_preflight(self.cohort)["status"], "fail")

    def test_background_replays_native_pixels_and_negative_offscreen_coordinates(self):
        folder = self.root / "artifacts/model-trials/preflight/background"
        source, reference, project, run = (folder / name for name in ("source", "reference", "project", "capture"))
        for path in (source, reference / "frames", project, run / "frames"):
            path.mkdir(parents=True)
        script = self.root / "scripts/check_background_capture.py"
        script.parent.mkdir()
        script.write_text("background preflight")
        base = {"game": "slay", "variant": "before", "fps": 60, "frame_count": 240, "viewport": [640, 360],
                "status": "ok", "scenario": {"id": "r06"}, "frames": [{"frame_index": i} for i in range(240)]}
        write_json(reference / "manifest.json", {**base, "provenance": {"full_source_hashes_before": {"p": "old"}}})
        write_json(run / "manifest.json", {**base, "provenance": {
            "full_source_hashes_before": {"p": "capture"}, "full_source_hashes_after": {"p": "capture"}}})
        images = {}
        for i in range(240):
            name = f"frames/{i:06d}.png"
            for path in (reference / name, run / name):
                path.write_bytes(f"native image {i}".encode())
            images[name] = {"reference": sha256(reference / name), "offscreen": sha256(run / name)}
        windows = [{"no_focus": True, "focused": False, "mode": 0, "position": [-10000, -10000], "display_server": "Windows"}] * 302
        write_json(run / "window-observer.json", {"samples": windows})
        record = {"status": "pass", "frame_count": 240, "different_frames": [], "viewport": [640, 360],
            "source": str(source), "reference": str(reference), "source_hashes": {"p": "old"},
            "preflight_script_sha256": sha256(script), "reference_manifest_sha256": sha256(reference / "manifest.json"),
            "capture_manifest_sha256": sha256(run / "manifest.json"), "frame_hashes": images,
            "window_observer_sha256": sha256(run / "window-observer.json"), "window_samples": 302, "window_status": "pass"}
        write_json(folder / "result.json", record)
        self.cohort["background_preflight"] = {"path": str(folder / "result.json"), "sha256": sha256(folder / "result.json")}
        with patch.object(mb, "project_hashes", side_effect=lambda p: {"p": "old" if p == source else "capture"}):
            self.assertEqual(mb._background_preflight(self.cohort)["status"], "pass")
            (run / "frames/000100.png").write_bytes(b"changed native image")
            self.assertEqual(mb._background_preflight(self.cohort)["status"], "fail")
            (run / "frames/000100.png").write_bytes((reference / "frames/000100.png").read_bytes())
            windows[0] = {**windows[0], "focused": True}
            write_json(run / "window-observer.json", {"samples": windows})
            record["window_observer_sha256"] = sha256(run / "window-observer.json")
            write_json(folder / "result.json", record)
            self.cohort["background_preflight"]["sha256"] = sha256(folder / "result.json")
            self.assertEqual(mb._background_preflight(self.cohort)["status"], "fail")

    def mocked_collect(self, change=None):
        state, candidate, _ = self.state()
        launch, events = self.launch()
        write_json(self.session / "kiro/launch.json", launch)
        (self.session / "kiro/stdout.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))
        (self.session / "kiro/tool-audit.jsonl").write_text("")
        fresh = {"ground": {"numerical_status": "pass"}, "jump_regression": {"numerical_status": "pass"},
                 "manual_validation_status": "pass", "mean_rmse_body": .05}
        with ExitStack() as stack:
            for name in ("_state_integrity", "_capture_identity", "_source_scope", "_smoke", "_evaluation_job"):
                stack.enter_context(patch.object(mb.ground, name, return_value={"status": "pass"}))
            for name in ("_configuration_binding", "_background_session", "_runtime"):
                stack.enter_context(patch.object(mb, name, return_value={"status": "pass"}))
            stack.enter_context(patch.object(mb.ground, "_manual", return_value={"status": "pass", "manual_validation_status": "pass"}))
            stack.enter_context(patch.object(mb.ground, "_quality", return_value=({"status": "pass"}, fresh)))
            stack.enter_context(patch("kiroclops.integration.authorship.audit_kiro_authorship", return_value={
                "status": "verified", "candidate_id": candidate["candidate_id"], "source_hashes": candidate["source_hashes"]}))
            if change:
                change(stack, state, candidate, launch, fresh)
            return mb._collect(self.session, self.cohort, {"mock": True})

    def test_all_gates_compose_and_each_missing_independent_proof_blocks_success(self):
        row = self.mocked_collect()
        self.assertTrue(row["success"])
        self.assertEqual(row["evidence"]["model_configuration"]["expected"]["model"], "gpt-5.6-sol")
        for module, name, status in ((mb, "_runtime", "pending"), (mb.ground, "_manual", "pending"),
                (mb.ground, "_source_scope", "fail"), (mb.ground, "_smoke", "fail"), (mb.ground, "_evaluation_job", "fail")):
            with self.subTest(name=name):
                def change(stack, *args):
                    stack.enter_context(patch.object(module, name, return_value={"status": status}))
                self.assertFalse(self.mocked_collect(change)["success"])

    def test_saved_numerical_pass_or_authorship_of_another_candidate_never_gives_full_pass(self):
        def changed_author(stack, *args):
            stack.enter_context(patch("kiroclops.integration.authorship.audit_kiro_authorship", return_value={
                "status": "verified", "candidate_id": "candidate-02", "source_hashes": {"other": "source"}}))
        self.assertEqual(self.mocked_collect(changed_author)["gates"]["authorship"], "fail")
        def changed_comparison(stack, state, candidate, launch, fresh):
            stack.enter_context(patch.object(mb.ground, "_quality", return_value=({"status": "fail"}, fresh)))
        row = self.mocked_collect(changed_comparison)
        self.assertFalse(row["success"])
        self.assertEqual(row["final_error"]["status"], "unavailable")

    def test_cohort_needs_all_three_same_baselines_and_both_independent_qualifications(self):
        identity = {"baseline_hashes": {"p": "x"}}
        def collect(session, *args):
            return {"session_id": session.name, "success": True, "cohort_identity": deepcopy(identity),
                    "final_error": {"status": "available", "mean_rmse_body": .05}}
        with patch.object(mb.ground, "verify_ground_lock", return_value=self.locks), \
             patch.object(mb, "_collect", side_effect=collect), \
             patch.object(mb, "_background_preflight", return_value={"status": "pass"}), \
             patch.object(mb, "_control_preflight", return_value={"status": "pass"}):
            self.assertEqual(mb.build_benchmark()["acceptance"], "pass")
            self.assertEqual(mb.build_benchmark([mb.SESSION_IDS[0]])["acceptance"], "pending_or_failed")
            with patch.object(mb, "_control_preflight", return_value={"status": "pending"}):
                self.assertFalse(mb.build_benchmark()["cli"]["all_pass"])
            def different(session, *args):
                row = collect(session)
                if session.name == mb.SESSION_IDS[1]:
                    row["cohort_identity"] = {"baseline_hashes": {"different": "source"}}
                return row
            with patch.object(mb, "_collect", side_effect=different):
                self.assertFalse(mb.build_benchmark()["cli"]["all_pass"])

    def test_package_copies_existing_local_assets_without_editing_source(self):
        report = self.session / "report"
        (report / "assets").mkdir(parents=True)
        (report / "assets/video.mp4").write_bytes(b"fake local video")
        (report / "index.html").write_text('<video src="assets/video.mp4"></video>')
        before = {str(p): sha256(p) for p in report.rglob("*") if p.is_file()}
        with patch.object(mb.ground, "verify_ground_lock", side_effect=FileNotFoundError()):
            path = mb.generate_report()
        self.assertEqual(path, self.root / mb.OUTPUT_PATH / "index.html")
        self.assertIn(f'reports/{self.session.name}/index.html', path.read_text(encoding="utf-8"))
        self.assertEqual((path.parent / "reports" / self.session.name / "assets/video.mp4").read_bytes(), b"fake local video")
        self.assertEqual({str(p): sha256(p) for p in report.rglob("*") if p.is_file()}, before)
        result = read_json(path.parent / "benchmark.json")
        self.assertEqual(result["visual_reports"][mb.SESSION_IDS[1]]["status"], "unavailable")
        self.assertFalse((self.session / "session.json").exists())

    def test_package_rejects_links_outside_report_instead_of_copying_private_files(self):
        report = self.session / "report"
        report.mkdir()
        (self.session / "outside.txt").write_text("do not include")
        (report / "index.html").write_text('<a href="../outside.txt">outside</a>')
        with patch.object(mb.ground, "verify_ground_lock", side_effect=FileNotFoundError()):
            path = mb.generate_report()
        result = read_json(path.parent / "benchmark.json")
        self.assertEqual(result["visual_reports"][self.session.name]["status"], "unavailable")
        self.assertFalse((path.parent / "reports" / self.session.name / "outside.txt").exists())

    def test_package_rejects_shared_report_inode(self):
        report = self.session / "report"
        report.mkdir()
        external = self.session / "original.html"
        external.write_text("original")
        try:
            os.link(external, report / "index.html")
        except OSError:
            self.skipTest("Hardlinks unavailable")
        with patch.object(mb.ground, "verify_ground_lock", side_effect=FileNotFoundError()):
            path = mb.generate_report()
        self.assertEqual(read_json(path.parent / "benchmark.json")["visual_reports"][self.session.name]["status"], "unavailable")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
