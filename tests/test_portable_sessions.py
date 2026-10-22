"""Portable session/ACP boundary tests; no game, model or GUI is launched."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from kiroclops.integration.storage import IntegrationError, project_hashes, read_json, sha256, write_json
from kiroclops.portable import session as mod
from kiroclops.portable import launcher, cohort


class FakeProcess:
    def __init__(self):
        import os
        self.pid = os.getpid()
    def poll(self):
        return None


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="KiroClops café space ")
        self.root = Path(self.temp.name)
        self.source = self.root / "prepared"
        self.source.mkdir()
        for rel, content in {"project.godot": "[application]\n", "player.gd": "extends CharacterBody2D\n",
                             "DefaultMovementData.tres": "speed = 100\n", "PlayerMovementData.gd": "extends Resource\n",
                             "kiroclops_heart/capture.gd": "extends Node2D\n"}.items():
            path = self.source / rel
            path.parent.mkdir(exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.target = {"target_id": "heart", "editable_files": ["player.gd"], "editable_globs": ["motion/*.gd"]}
        self.experiment = {"experiment_id": "heart-jump-v1", "stage": "jump", "calibration_ids": ["h03", "h24"]}
        self.path = mod.create_session("heart-test-jump", self.source, self.target, self.experiment, root=self.root)
        self.session = mod.Session(self.path)
        self.spawn = patch.object(mod.Session, "_spawn_worker", return_value=FakeProcess())
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.addCleanup(self.temp.cleanup)

    def ready(self):
        job = self.session.submit("prepare")
        self.session.finish_job(job["job_id"], result={"reference": {"calibration": True}, "baseline_runs": {"calibration": []}})

    def candidate(self, score=.05, eligible=True, integrity="pass"):
        job = self.session.submit("evaluate")
        result = {"eligible": eligible, "mean_rmse_body": score, "automatic_integrity_status": integrity}
        self.session.finish_job(job["job_id"], result=result)
        return job


class SessionTests(Fixtures):
    def test_worker_semantic_failure_is_completed_ineligible_not_tool_error(self):
        self.ready()
        job = self.session.submit("evaluate")
        payload = {"eligible": False, "mean_rmse_body": None, "automatic_integrity_status": "fail",
                   "motion_calibration": "not_run", "failure_class": "gameplay_integrity_violation"}
        with patch("kiroclops.portable.evaluation.run_job", return_value=payload):
            mod.run_worker(self.path, job["job_id"])
        actual = self.session.get_job(job["job_id"], 0)
        self.assertEqual(actual["status"], "succeeded")
        self.assertIsNone(actual["result"]["mean_rmse_body"])
        self.assertEqual(actual["result"]["motion_calibration"], "not_run")
        with self.assertRaises(IntegrationError):
            self.session.submit("finalize")

    def test_worker_cancel_prevents_evaluator(self):
        self.ready()
        job = self.session.submit("evaluate")
        self.session.cancel_job(job["job_id"])
        with patch("kiroclops.portable.evaluation.run_job", side_effect=AssertionError("Cancelled evaluation ran")):
            mod.run_worker(self.path, job["job_id"])
        self.assertEqual(self.session.get_job(job["job_id"], 0)["status"], "cancelled")

    def test_worker_checks_snapshot_again_after_evaluator(self):
        self.ready()
        job = self.session.submit("evaluate")
        def mutate(*args, **kwargs):
            (self.path / "candidates" / job["candidate_id"] / "project/player.gd").write_text("changed during capture", encoding="utf-8")
            return {"eligible": True, "mean_rmse_body": 0, "automatic_integrity_status": "pass"}
        with patch("kiroclops.portable.evaluation.run_job", side_effect=mutate):
            mod.run_worker(self.path, job["job_id"])
        actual = self.session.get_job(job["job_id"], 0)
        self.assertEqual(actual["status"], "failed")
        self.assertEqual(actual["error"]["code"], "candidate_integrity")

    def test_source_copies_and_unicode_paths(self):
        state = self.session.verify()
        self.assertEqual(project_hashes(self.source), project_hashes(state["target"]))
        (Path(state["target"]) / "player.gd").write_text("changed", encoding="utf-8")
        self.assertNotEqual(project_hashes(state["target"]), project_hashes(state["baseline"]))

    def test_existing_session_never_replaced(self):
        with self.assertRaises(IntegrationError):
            mod.create_session("heart-test-jump", self.source, self.target, self.experiment, root=self.root)

    def test_immutable_definition_rejected(self):
        self.session.update(lambda s: s["experiment_spec"].update(stage="ground"))
        with self.assertRaises(IntegrationError):
            self.session.inspect()

    def test_baseline_mutation_rejected(self):
        (self.path / "baseline/player.gd").write_text("changed", encoding="utf-8")
        with self.assertRaises(IntegrationError):
            self.session.submit("prepare")

    def test_snapshot_does_not_follow_later_edits(self):
        self.ready()
        target = self.path / "target/player.gd"
        target.write_text("first", encoding="utf-8")
        job = self.session.submit("evaluate")
        target.write_text("later", encoding="utf-8")
        candidate = self.session.verify_candidate(job["candidate_id"])
        self.assertEqual((Path(candidate["project_path"]) / "player.gd").read_text(), "first")

    def test_protected_edit_consumes_attempt_and_is_failed_job(self):
        self.ready()
        (self.path / "target/DefaultMovementData.tres").write_text("speed = 1", encoding="utf-8")
        job = self.session.submit("evaluate")
        result = self.session.get_job(job["job_id"], 0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "source_scope")
        self.assertEqual(len(self.session.read()["candidates"]), 1)

    def test_failed_candidate_budget_cannot_be_restarted(self):
        self.ready()
        for _ in range(3):
            self.candidate(score=None, eligible=False, integrity="fail")
        with self.assertRaises(IntegrationError):
            self.session.submit("evaluate")
        with self.assertRaises(IntegrationError):
            self.session.start_clock()

    def test_best_eligible_selected_and_sealed(self):
        self.ready()
        self.candidate(.12)
        best = self.candidate(.03)
        self.candidate(.001, eligible=False, integrity="fail")
        with self.assertRaises(IntegrationError):
            self.session.submit("finalize", "candidate-01")
        chosen = self.session.submit("finalize")
        self.assertEqual(chosen["candidate_id"], best["candidate_id"])
        self.assertIsNotNone(self.session.read()["sealed_at"])
        self.session.finish_job(chosen["job_id"], result={"numerical_status": "pass", "automatic_integrity_status": "pass", "measurement_status": "pending"})
        with self.assertRaises(IntegrationError):
            self.session.submit("evaluate")

    def test_wrong_candidate_job_result_cannot_qualify(self):
        self.ready()
        result = self.candidate(.02)
        path = self.path / "jobs" / (result["job_id"] + ".json")
        job = read_json(path)
        job["candidate_source_sha256"] = "0" * 64
        write_json(path, job)
        with self.assertRaises(IntegrationError):
            self.session.submit("finalize")

    def test_changed_snapshot_cannot_finalize(self):
        self.ready()
        result = self.candidate()
        (self.path / "candidates" / result["candidate_id"] / "project/player.gd").write_text("tampered", encoding="utf-8")
        with self.assertRaises(IntegrationError):
            self.session.submit("finalize")

    def test_cancel_does_not_call_unknown_process(self):
        self.ready()
        job = self.session.submit("evaluate")
        result = self.session.cancel_job(job["job_id"])
        self.assertEqual(result["status"], "cancellation_requested")
        self.assertTrue((self.path / "jobs" / (job["job_id"] + ".cancel")).exists())
        with self.assertRaises(IntegrationError):
            self.session.cancel_job("job-not-owned")

    def test_crashed_worker_is_interrupted_not_pass(self):
        self.ready()
        job = self.session.submit("evaluate")
        with patch.object(mod, "process_identity", return_value=None):
            state = self.session.get_job(job["job_id"], 0)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(self.session.read()["candidates"][0]["status"], "interrupted")

    def test_owner_crash_before_spawn_does_not_leave_permanent_queued_job(self):
        self.ready()
        job = self.session.submit("evaluate")
        path = self.path / "jobs" / (job["job_id"] + ".json")
        value = read_json(path)
        value.pop("worker_pid")
        value.pop("worker_identity")
        write_json(path, value)
        with patch.object(mod, "process_identity", return_value=None):
            actual = self.session.get_job(job["job_id"], 0)
        self.assertEqual(actual["status"], "interrupted")
        self.assertIsNone(self.session.read()["active_job_id"])

    def test_deadline_enforced(self):
        self.ready()
        self.session.update(lambda s: s.update(deadline_at=time.time()-1))
        with self.assertRaises(IntegrationError):
            self.session.submit("evaluate")

    def test_current_jobs_never_mix_sessions(self):
        self.ready()
        job = self.session.submit("evaluate")
        path = self.path / "jobs" / (job["job_id"] + ".json")
        value = read_json(path)
        value["session_id"] = "heart-another"
        write_json(path, value)
        with self.assertRaises(IntegrationError):
            self.session.get_job(job["job_id"], 0)

    def test_pending_manual_review_is_not_full_acceptance(self):
        self.ready()
        self.candidate()
        job = self.session.submit("finalize")
        self.session.finish_job(job["job_id"], result={"numerical_status": "pass", "automatic_integrity_status": "pass", "measurement_status": "pending"})
        self.assertEqual(self.session.inspect()["final"]["measurement_status"], "pending")

    def test_snapshot_helpers_are_direct_gd_only(self):
        self.ready()
        helper = self.path / "target/motion/new.gd"
        helper.parent.mkdir()
        helper.write_text("extends RefCounted\n", encoding="utf-8")
        job = self.candidate()
        self.assertIn("motion/new.gd", self.session.verify_candidate(job["candidate_id"])["source_hashes"])
        nested = helper.parent / "nested/new.gd"
        nested.parent.mkdir()
        nested.write_text("extends Node", encoding="utf-8")
        invalid = self.session.submit("evaluate")
        self.assertEqual(invalid["status"], "failed")


class LauncherTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.config = launcher.configure_model_cli(self.path)

    def request(self, name, path):
        return launcher.check_tool_request(self.path, {"tool_name": name, "tool_input": {"path": str(path)}})

    def test_exact_model_and_new_target_permissions(self):
        loaded = launcher.load_model_config(self.path)
        agent = read_json(loaded["agent_path"])
        self.assertEqual((agent["model"], agent["effortLevel"]), ("gpt-5.6-sol", "max"))
        self.assertEqual(loaded["approval_mode"], launcher.APPROVAL_MODE)
        self.assertTrue(self.request("str_replace", "player.gd")["allow"])
        self.assertTrue(self.request("write_file", "motion/helper.gd")["allow"])
        self.assertFalse(self.request("write_file", "PlayerMovementData.gd")["allow"])
        self.assertFalse(self.request("write_file", "motion/deep/helper.gd")["allow"])

    def test_native_governance_failure_is_explicit_without_prompt_or_bypass(self):
        client = object.__new__(launcher.ModelACPClient)
        event = {"method": "_kiro/mcp/governance_disabled", "params": {"reason": "api_failure"}}
        with patch.object(launcher.SelectionClient, "_receive", return_value=event):
            with self.assertRaises(launcher.KiroError) as caught:
                client._receive(123)
        self.assertEqual(caught.exception.code, "mcp_governance_unavailable")
        self.assertIn("api_failure", str(caught.exception))

    def test_native_receive_preserves_other_events(self):
        client = object.__new__(launcher.ModelACPClient)
        event = {"method": "_kiro/mcp/status", "params": {"servers": []}}
        with patch.object(launcher.SelectionClient, "_receive", return_value=event):
            self.assertIs(client._receive(123), event)

    def test_native_constructor_passes_real_flags_and_owns_hidden_process(self):
        process, job = MagicMock(), MagicMock()
        ready = {"config":self.config, "executable":"native-kiro.exe"}
        with patch.object(launcher.subprocess, "Popen", return_value=process) as popen, \
             patch.object(launcher.native, "_WindowsJob", return_value=job), \
             patch.object(launcher.threading, "Thread") as thread, \
             patch.object(launcher.SelectionClient, "__init__", side_effect=AssertionError("frozen constructor used")):
            client = launcher.ModelACPClient(ready, self.path/"native-test", 30, None)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["native-kiro.exe", "acp", "--agent-engine", "v3", "--auth-method", "cli"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], self.config["target_dir"])
        self.assertEqual(kwargs["env"]["KIRO_DISABLE_SESSION_TITLE_LLM"], "true")
        if launcher.os.name == "nt":
            self.assertEqual(kwargs["creationflags"], launcher.subprocess.CREATE_NO_WINDOW | 0x4)
            job.assign_and_resume.assert_called_once_with(process)
        self.assertEqual(len(client.readers), 2)
        self.assertFalse(client.prompt_sent)
        self.assertEqual(client._task_prompt_count, 0)
        self.assertTrue(client.outbound_path.is_file())

    @unittest.skipUnless(launcher.os.name == "nt", "Windows process ownership contract")
    def test_failed_job_assignment_terminates_child_before_any_reader(self):
        process, job = MagicMock(), MagicMock()
        job.assign_and_resume.side_effect = RuntimeError("assignment failed")
        with patch.object(launcher.subprocess, "Popen", return_value=process), \
             patch.object(launcher.native, "_WindowsJob", return_value=job), \
             patch.object(launcher.native, "_terminate_tree") as terminate, \
             patch.object(launcher.threading, "Thread") as thread:
            with self.assertRaises(RuntimeError):
                launcher.ModelACPClient({"config":self.config,"executable":"native.exe"},self.path/"failed-init",30,None)
        job.close.assert_called_once()
        terminate.assert_called_once_with(process)
        thread.assert_not_called()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once()

    def test_always_allow_handshake_still_requires_inherited_hook_and_tools(self):
        client = object.__new__(launcher.ModelACPClient)
        client.autopilot = "on"
        client.argv = launcher.acp_argv("native.exe")
        client.guard = MagicMock()
        with patch.object(launcher.SelectionClient, "handshake", side_effect=launcher.KiroError("hook_not_loaded","missing hook")):
            with self.assertRaises(launcher.KiroError):
                client.handshake()
        with patch.object(launcher.SelectionClient, "handshake", return_value={"mcp_tools":list(launcher.MCP_TOOLS)}):
            self.assertEqual(client.handshake()["approval_mode"], launcher.APPROVAL_MODE)
            client.autopilot = "off"
            client.session_id, client.deadline = "sid", time.monotonic()+1
            client.request = MagicMock(side_effect=launcher.KiroError("autopilot_unverified","No native on response"))
            with self.assertRaises(launcher.KiroError):
                client.handshake()
            client.request.assert_called_once_with("session/set_config_option", {"sessionId":"sid","configId":"autopilot","value":"on"})

    def test_parent_and_heldout_read_denied(self):
        self.assertFalse(self.request("read_file", "../baseline/player.gd")["allow"])
        self.assertFalse(self.request("read_file", "../../heart-parent-jump/kiro/stdout.jsonl")["allow"])
        self.assertFalse(self.request("read_file", ".kiroclops-evidence/validation/analysis.json")["allow"])
        self.assertFalse(self.request("read_file", ".kiroclops-private/results.json")["allow"])
        self.assertTrue(self.request("read_image", ".kiroclops-reference/h03/frame.png")["allow"])

    def test_recursive_search_cannot_reach_heldout(self):
        private = self.path / "target/.kiroclops-private"
        private.mkdir()
        (private / "answer.json").write_text("{}", encoding="utf-8")
        self.assertFalse(self.request("grep_search", ".")["allow"])

    def test_shell_and_external_tools_denied(self):
        self.assertFalse(launcher.check_tool_request(self.path, {"tool_name": "execute_pwsh", "tool_input": {"command": "echo test"}})["allow"])
        self.assertFalse(launcher.check_tool_request(self.path, {"tool_name": "other::evaluate_candidate", "tool_input": {}})["allow"])
        for tool in launcher.MCP_TOOLS:
            self.assertTrue(launcher.check_tool_request(self.path, {"tool_name": "kiroclops::" + tool, "tool_input": {}})["allow"])

    def test_sealed_source_writes_are_denied(self):
        self.session.update(lambda s: s.update(sealed_at="2026-09-21T00:00:00+00:00", selected_candidate_id="candidate-01"))
        self.assertFalse(self.request("str_replace", "player.gd")["allow"])

    def test_modified_controls_fail_closed(self):
        path = Path(self.config["prompt_path"])
        path.write_text(path.read_text() + "changed", encoding="utf-8")
        with self.assertRaises(launcher.KiroError):
            launcher.load_model_config(self.path)

    def test_native_prompt_override_does_not_call_legacy_loader(self):
        client = object.__new__(launcher.ModelACPClient)
        client.cancel, client.deadline, client.config, client.session_id = None, time.monotonic()+10, self.config, "native-test"
        class Guard:
            def _verify(self):
                pass
        client.guard = Guard()
        client.request = lambda method, params, timeout: {"method": method, "params": params}
        with patch.object(launcher.native, "_load_config", side_effect=AssertionError("legacy configuration used")):
            result = client.prompt(Path(self.config["prompt_path"]).read_text())
        self.assertEqual(result["method"], "session/prompt")

    def test_model_cannot_launch_without_registered_qualification(self):
        with patch.object(launcher, "preflight_model_cli", side_effect=AssertionError("native process launched")):
            with self.assertRaises(IntegrationError):
                launcher.run_model_cli(self.path)

    def test_native_permission_uses_portable_guard(self):
        client = object.__new__(launcher.ModelACPClient)
        client.config = self.config
        client.tool_inputs = {"t": {"path": "player.gd", "old_str": "a", "new_str": "b"}}
        client.tool_identifiers = {"t": "str_replace"}
        messages = []
        client._send = messages.append
        event = {"id": 1, "params": {"toolCall": {"toolCallId": "t"}, "_meta": {"kiro": {"toolId": "str_replace"}},
                                     "options": [{"optionId": "once", "kind": "allow_once"}]}}
        with patch.object(launcher.native, "guard_request", side_effect=AssertionError("legacy guard used")):
            client._permission_reply(event)
        self.assertEqual(messages[0]["result"]["outcome"]["optionId"], "once")

    def test_residual_always_allow_is_guarded_and_only_session_scoped(self):
        client = object.__new__(launcher.ModelACPClient)
        client.config = self.config
        client.tool_inputs = {"t": {"path": "player.gd", "old_str": "a", "new_str": "b"}}
        client.tool_identifiers = {"t": "str_replace"}
        messages = []
        client._send = messages.append
        event = {"id":1,"params":{"toolCall":{"toolCallId":"t"},"_meta":{"kiro":{"toolId":"str_replace"}},
                 "options":[{"optionId":"once","kind":"allow_once"},{"optionId":"always","kind":"allow_always"}]}}
        client._permission_reply(event)
        self.assertEqual(messages[-1]["result"], {"outcome":{"outcome":"selected","optionId":"always"},
                                                "_meta":{"kiro":{"consent":{"scope":"session"}}}})
        client.tool_inputs["t"]["path"] = "DefaultMovementData.tres"
        with self.assertRaises(launcher.KiroError):
            client._permission_reply(event)
        self.assertEqual(messages[-1]["result"], {"outcome":{"outcome":"cancelled"}})

    def test_model_fallback_controls_rejected(self):
        control = self.path / "target/.kiro/kiroclops-session.json"
        value = read_json(control)
        value["model"] = "different-model"
        write_json(control, value)
        self.assertFalse(launcher.guard_request(self.path, {"tool_name": "read_file", "tool_input": {"path": "player.gd"}})["allow"])


class CohortTests(Fixtures):
    def setUp(self):
        super().setUp()
        from kiroclops.portable import specs
        self.specs = specs
        qualification = self.root / "config/phase2c/qualification.json"
        write_json(qualification, {"status": "pass", "gates": {name: {"status": "pass"} for name in cohort.QUALIFICATION_GATES},
                                  "files": {}, "evidence_files": {}, "reference_files": {},
                                  "protocol_sources": {"new.py": "1"*64}})
        self.qualification = qualification
        for name, value in [("verify_legacy", {"old.py": "0"*64}), ("verify_source", {"ok": True}), ("protocol_sources", {"new.py": "1"*64})]:
            p = patch.object(specs, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def register(self):
        return cohort.register_cohort("test-chain", self.source, qualification=self.qualification, root=self.root)

    def test_three_fixed_chains_no_replacements(self):
        path = self.register()
        value = cohort.verify_registry(path)
        self.assertEqual(len(value["chains"]), 3)
        self.assertEqual(value["chains"][0]["jump_session"], "heart-test-chain-01-jump")
        with self.assertRaises(FileExistsError):
            self.register()

    def test_changed_qualification_rejected(self):
        path = self.register()
        write_json(self.qualification, {"status": "fail"})
        with self.assertRaises(IntegrationError):
            cohort.verify_registry(path)

    def test_changed_baseline_rejected(self):
        path = self.register()
        (self.source / "player.gd").write_text("mutated", encoding="utf-8")
        with self.assertRaises(IntegrationError):
            cohort.verify_registry(path)

    def test_unqualified_cohort_not_registered(self):
        write_json(self.qualification, {"status": "fail"})
        with self.assertRaises(IntegrationError):
                self.register()

    def test_qualification_cannot_register_later_changed_protocol(self):
        with patch.object(self.specs, "protocol_sources", return_value={"new.py": "2"*64}):
            with self.assertRaises(IntegrationError) as caught:
                self.register()
        self.assertEqual(caught.exception.code, "qualification_binding")

    def test_overall_pass_cannot_hide_any_missing_or_failed_gate(self):
        good = read_json(self.qualification)
        for name in cohort.QUALIFICATION_GATES:
            for missing in (True, False):
                with self.subTest(gate=name, missing=missing):
                    proof = json.loads(json.dumps(good))
                    if missing:
                        proof["gates"].pop(name)
                    else:
                        proof["gates"][name]["status"] = "fail"
                    write_json(self.qualification, proof)
                    with self.assertRaises(IntegrationError):
                        self.register()
        write_json(self.qualification, good)

    def test_failure_skips_child_and_preserves_three_attempted_chains(self):
        path = self.register()
        with patch.object(launcher, "configure_model_cli"), patch.object(launcher, "run_model_cli", return_value={"status": "timeout"}) as run:
            result = cohort.run_cohort(path)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(all(c["stages"]["ground"]["status"] == "not_run" for c in result["chains"]))

    def test_promotion_pending_review_no_parent_evidence_in_inspect(self):
        self.ready()
        candidate = self.candidate()
        final_job = self.session.submit("finalize")
        self.session.finish_job(final_job["job_id"], result={"numerical_status": "pass", "automatic_integrity_status": "pass", "measurement_status": "pending"})
        write_json(self.path / "kiro/launch.json", {"status": "completed", "process_returncode_after_cleanup": 0,
                   "model_requested": "gpt-5.6-sol", "effort_requested": "max", "elapsed_seconds": 10,
                   "stream_configuration": {"generation_configuration_verified": True}})
        parent = cohort.promotion_receipt(self.path)
        source = self.path / "candidates" / candidate["candidate_id"] / "project"
        child = mod.create_session("heart-test-ground", source, self.target,
                 {"experiment_id": "heart-ground-v1", "stage": "ground"}, parent=parent, root=self.root)
        inspected = mod.Session(child).inspect()
        self.assertNotIn("parent", inspected)
        self.assertNotIn(str(self.path), json.dumps(inspected))
        self.assertEqual(project_hashes(child / "baseline"), parent["source_hashes"])
        self.assertFalse((child / "target/.kiro").exists())


if __name__ == "__main__":
    unittest.main()
