"""Explicit-model adapter checks; no native executable or capture is started."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from kiroclops.integration import kiro
from kiroclops.integration import model_cli_v1 as model_cli
from tests.evidence import requires_evidence


class ModelCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name) / "fresh-sol-session"
        self.target = self.session / "target"
        self.target.mkdir(parents=True)
        (self.target / "project.godot").write_text("[application]\n", encoding="utf-8")
        self.player = self.target / "source/characters/player/player.gd"
        self.player.parent.mkdir(parents=True)
        self.player.write_text("extends CharacterBody2D\n", encoding="utf-8")
        self.state = {"schema_version": 3, "experiment_id": "ground-motion-v1",
            "session_id": self.session.name, "mode": "tools", "status": "new",
            "started_at": None, "deadline_at": None, "candidates": [],
            "selected_candidate_id": None, "sealed_at": None, "active_job_id": None}
        self.write_state()

    def write_state(self):
        (self.session / "session.json").write_text(json.dumps(self.state), encoding="utf-8")

    def configure(self, **kwargs):
        return model_cli.configure_model_cli(self.session, **kwargs)

    def assert_code(self, code, callback):
        with self.assertRaises(kiro.KiroError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_fresh_configuration_binds_explicit_model_without_changing_engine_or_state(self):
        state_before = (self.session / "session.json").read_bytes()
        source_before = self.player.read_bytes()
        config = self.configure()
        self.assertEqual(config, model_cli.load_model_config(self.session))
        self.assertEqual((config["model"], config["effort"]), ("gpt-5.6-sol", "high"))
        agent = json.loads(Path(config["agent_path"]).read_text(encoding="utf-8"))
        self.assertEqual((agent["model"], agent["effortLevel"]), ("gpt-5.6-sol", "high"))
        self.assertFalse(agent["includePowers"])
        self.assertFalse(agent["includeMcpJson"])
        self.assertEqual(agent["tools"], ["read", "write", "@kiroclops"])
        self.assertIn("Model: gpt-5.6-sol. Effort: high.", Path(config["prompt_path"]).read_text())
        self.assertEqual(kiro.MODEL, "claude-opus-5")
        self.assertEqual(kiro.StreamConfigurationGuard("agent").expected["model"], "claude-opus-5")
        self.assertEqual((self.session / "session.json").read_bytes(), state_before)
        self.assertEqual(self.player.read_bytes(), source_before)
        self.assertEqual(config["model_launcher"], model_cli.launcher_binding())
        self.assertEqual(set(config["model_launcher"]["source_hashes"]), {
            "kiroclops/integration/model_cli_v1.py", "scripts/run_model_cli.py",
            "kiroclops/integration/kiro.py", "scripts/run_kiro.py"})

    def test_supplemental_instruction_is_explicit_hashed_text_not_an_enforcement_claim(self):
        text = "Preserve airborne behavior on Idle→Fall and Run→Fall."
        config = self.configure(supplemental_instructions=text)
        agent = json.loads(Path(config["agent_path"]).read_text(encoding="utf-8"))
        self.assertIn(text, agent["prompt"])
        for path in (Path(config["prompt_path"]), self.target / ".kiro/kiroclops-instructions.md",
                     Path(config["power_dir"]) / "skills/motion-calibration/SKILL.md"):
            self.assertIn(text, path.read_text(encoding="utf-8"))
        self.assertEqual(config["supplemental_instructions"], {"kind": "cohort_instruction",
            "enforced_by_mcp": False, "sha256": hashlib.sha256(text.encode()).hexdigest()})
        self.assertEqual(config["instruction_sha256"], hashlib.sha256(agent["prompt"].encode()).hexdigest())
        self.assertEqual(model_cli.load_model_config(self.session), config)
        self.assertNotIn(text, (kiro.server_root() / "power/kiroclops/ground-motion.md").read_text())

    def test_invalid_supplement_before_writes(self):
        for text in ("", "  ", "a\x00b", 12):
            with self.subTest(text=text):
                self.assert_code("invalid_instructions", lambda: self.configure(supplemental_instructions=text))
                self.assertFalse((self.target / ".kiro").exists())

    def test_existing_session_controls_never_replaced(self):
        self.configure()
        marker = self.target / ".kiro/kiroclops-session.json"
        before = marker.read_bytes()
        self.assert_code("existing_config", self.configure)
        self.assertEqual(marker.read_bytes(), before)

    def test_started_sealed_wrong_experiment_and_candidate_states_rejected(self):
        mutations = [{"started_at": 0}, {"deadline_at": 0}, {"candidates": [{"candidate_id": "candidate-01"}]},
            {"selected_candidate_id": "candidate-01"}, {"sealed_at": 1}, {"active_job_id": "job-01"},
            {"status": "ready"}, {"mode": "baseline"}, {"schema_version": 2}, {"experiment_id": "legacy-jump-v1"}]
        initial = dict(self.state)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.state = {**initial, **mutation}
                self.write_state()
                self.assert_code("not_fresh", self.configure)
                self.assertFalse((self.target / ".kiro").exists())

    def test_unavailable_selection_does_not_fallback_or_write(self):
        for model, effort in (("gpt-6-astra", "high"), ("claude-opus-5", "high"),
                              ("gpt-5.6-sol", "max"), (["gpt-5.6-sol"], "high")):
            with self.subTest(model=model, effort=effort):
                self.assert_code("unsupported_selection", lambda: self.configure(model=model, effort=effort))
                self.assertFalse((self.target / ".kiro").exists())

    def test_existing_frozen_guard_accepts_new_marker_but_retains_all_boundaries(self):
        config = self.configure()
        hook = json.loads((self.target / ".kiro/hooks/kiroclops-guard.json").read_text())
        self.assertIn("run_kiro.py", hook["hooks"][0]["action"]["command"])
        self.assertIn("--guard", hook["hooks"][0]["action"]["command"])
        request = lambda name, args: kiro.check_tool_request(self.session, "tools", {"tool_name": name, "tool_input": args})
        self.assertTrue(request("@kiroclops/inspect_session", {})["allow"])
        self.assertTrue(request("str_replace", {"path": str(self.player), "oldStr": "old", "newStr": "new"})["allow"])
        self.assertFalse(request("read_file", {"path": str(self.session / "reference/code.gd")})["allow"])
        self.assertFalse(request("@kiroclops/unknown_tool", {})["allow"])
        self.assertFalse(request("execute_pwsh", {"command": "whoami"})["allow"])
        self.state.update(selected_candidate_id="candidate-01", sealed_at=time.time())
        self.write_state()
        self.assertFalse(request("str_replace", {"path": str(self.player)})["allow"])
        self.assertTrue(request("@kiroclops/get_job", {"job_id": "job-01"})["allow"])
        self.assertEqual(kiro._load_config(self.session, "tools"), config)

    def test_changed_generated_controls_or_launcher_are_rejected(self):
        config = self.configure()
        with patch.object(model_cli, "launcher_binding", return_value={"revision": "changed"}):
            self.assert_code("launcher_changed", lambda: model_cli.load_model_config(self.session))
        Path(config["prompt_path"]).write_text("different prompt")
        self.assert_code("config_changed", lambda: model_cli.load_model_config(self.session))

    def configured_client(self, *, effort_choices=("low", "high"), effort_echo="high", model_echo=True):
        config = self.configure()
        client = object.__new__(model_cli.ModelACPClient)
        client.config = config
        client.deadline = time.monotonic() + 60
        client.cancel = None
        client.guard = kiro.StreamConfigurationGuard(config["agent_name"], config["model"], config["effort"])
        client.model_echo = client.effort_echo = 0
        client.effort_options = None
        client.model_choices = {"gpt-5.6-sol"}
        client.servers = {"kiroclops": {"status": "connected", "tools": [{"name": x} for x in kiro.MCP_TOOLS]}}
        client.tags = {"read", "write"} | {f"@kiroclops/{x}" for x in kiro.MCP_TOOLS}

        def emit(options):
            event = {"jsonrpc": "2.0", "method": "session/update", "params": {
                "update": {"sessionUpdate": "config_option_update", "configOptions": options}}}
            client.guard.observe(event)
            client._observe_options(event)

        def request(method, params, **kwargs):
            if method == "initialize":
                return {"protocolVersion": 1}
            if method == "session/new":
                emit([{"id": "model", "currentValue": "auto"},
                      {"id": "effortLevel", "currentValue": "high", "options": [{"value": "high"}]}])
                return {"sessionId": "native-test", "_meta": {"workspacePaths": [str(self.target)]},
                    "modes": {"availableModes": [{"id": config["agent_name"], "_meta": {"kiro": {
                        "resource": {"source": {"origin": "workspace", "root": str(self.target)}}}}}]}}
            if method == "session/set_mode":
                emit([{"id": "mode", "currentValue": params["modeId"]}])
            if method == "session/set_config_option" and params["configId"] == "model" and model_echo:
                emit([{"id": "model", "currentValue": params["value"]},
                      {"id": "effortLevel", "category": "thought_level", "currentValue": "low",
                       "options": [{"value": x} for x in effort_choices]}])
            if method == "session/set_config_option" and params["configId"] == "effortLevel":
                if effort_echo is not None:
                    emit([{"id": "effortLevel", "category": "thought_level", "currentValue": effort_echo}])
            if method == "_kiro/hooks/list":
                path = self.target / ".kiro/hooks/kiroclops-guard.json"
                hook = json.loads(path.read_text())["hooks"][0]
                return {"hooks": [{**hook, "_meta": {"enabled": True, "filePath": str(path)}}]}
            return {}

        client.request = MagicMock(side_effect=request)
        client._receive = MagicMock(side_effect=kiro.KiroError("timeout", "No further echoed configuration."))
        return client

    def test_handshake_discovers_selected_model_effort_and_requires_echoes(self):
        client = self.configured_client()
        result = client.handshake()
        self.assertTrue(result["selection_echo_verified"])
        self.assertEqual(result["selected"], {"mode": "kiroclops-tools", "model": "gpt-5.6-sol", "effort": "high"})
        self.assertEqual(result["effort_selection"], {"config_id": "effortLevel", "observed_model": "gpt-5.6-sol",
            "values": ["high", "low"], "selected": "high"})
        self.assertFalse(result["prompt_sent"])
        self.assertFalse(client.guard.generation_started)
        self.assertNotIn("session/prompt", [c.args[0] for c in client.request.call_args_list])
        client._receive.assert_not_called()

    def test_default_models_effort_options_cannot_stand_in_for_sol_support(self):
        client = self.configured_client(effort_choices=("low", "medium"))
        self.assert_code("effort_unavailable", client.handshake)
        self.assertNotIn("session/prompt", [c.args[0] for c in client.request.call_args_list])

    def test_missing_model_echo_cannot_use_command_argument_as_evidence(self):
        client = self.configured_client(model_echo=False)
        self.assert_code("timeout", client.handshake)

    def test_missing_effort_echo_cannot_use_old_selected_high(self):
        client = self.configured_client(effort_echo=None)
        self.assert_code("timeout", client.handshake)

    def test_wrong_effort_echo_fails_closed(self):
        client = self.configured_client(effort_echo="low")
        self.assert_code("effort_mismatch", client.handshake)

    def test_unexpected_mcp_or_native_tool_fails_pre_prompt(self):
        client = self.configured_client()
        client.tags.add("shell")
        self.assert_code("unexpected_tools", client.handshake)

    def test_explicit_guard_rejects_model_or_effort_change_after_generation(self):
        for key, value in (("model", "claude-opus-5"), ("effortLevel", "low")):
            guard = kiro.StreamConfigurationGuard("kiroclops-tools", "gpt-5.6-sol", "high")
            guard.observe({"configOptions": [{"id": "mode", "currentValue": "kiroclops-tools"},
                {"id": "model", "currentValue": "gpt-5.6-sol"}, {"id": "effortLevel", "currentValue": "high"}]})
            guard.observe({"sessionUpdate": "agent_message_chunk"})
            self.assertTrue(guard.summary()["generation_configuration_verified"])
            self.assert_code("model_mismatch" if key == "model" else "effort_mismatch",
                             lambda: guard.observe({"configOptions": [{"id": key, "currentValue": value}]}))
            self.assertFalse(guard.summary()["generation_configuration_verified"])

    def test_constructor_replaces_default_guard_with_explicit_model(self):
        config = self.configure()
        def base_init(client, ready, *args):
            client.config = ready["config"]
            client.guard = kiro.StreamConfigurationGuard(config["agent_name"])
        with patch.object(kiro._ACPClient, "__init__", new=base_init):
            client = model_cli.ModelACPClient({"config": config}, self.session / "kiro", 1800, None)
        self.assertEqual(client.guard.expected["model"], "gpt-5.6-sol")
        self.assertEqual(client.guard.expected["effort"], "high")

    def test_prompt_rechecks_config_cancellation_and_deadline_before_send(self):
        client = self.configured_client()
        client.handshake()
        client.cancel = threading.Event()
        client.cancel.set()
        self.assert_code("cancelled", lambda: client.prompt("prepared"))
        client.cancel.clear()
        client.deadline = 0
        self.assert_code("timeout", lambda: client.prompt("prepared"))
        client.deadline = time.monotonic() + 60
        Path(client.config["prompt_path"]).write_text("tampered")
        self.assert_code("config_changed", lambda: client.prompt("prepared"))
        self.assertNotIn("session/prompt", [c.args[0] for c in client.request.call_args_list])

    def test_successful_prompt_uses_existing_native_request_with_explicit_guard(self):
        client = self.configured_client()
        client.handshake()
        client.prompt("prepared")
        call = client.request.call_args
        self.assertEqual(call.args, ("session/prompt", {"sessionId": "native-test",
            "prompt": [{"type": "text", "text": "prepared"}]}))
        self.assertTrue(client.prompt_sent)
        self.assertEqual(client.guard.expected["model"], "gpt-5.6-sol")

    def mock_inventory(self, available=True):
        models = [{"model_id": "gpt-5.6-sol" if available else "claude-opus-5"}]
        return [subprocess.CompletedProcess([], 0, json.dumps({"models": models}), ""),
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 0, "kiro-cli test", "")]

    @requires_evidence('.venv-kiro')
    def test_inventory_absence_fails_before_native_client_or_prompt(self):
        self.configure()
        with patch.object(model_cli.subprocess, "run", side_effect=self.mock_inventory(False)) as run, \
             patch.object(model_cli, "ModelACPClient") as client:
            self.assert_code("model_unavailable", lambda: model_cli.preflight_model_cli(self.session, executable="fake"))
        self.assertEqual(run.call_count, 1)
        client.assert_not_called()
        self.assertFalse((self.session / "kiro/launch.json").exists())

    @requires_evidence('.venv-kiro')
    def test_preflight_uses_native_handshake_but_no_prompt_or_session_clock(self):
        config = self.configure()
        original = (self.session / "session.json").read_bytes()
        with patch.object(model_cli.subprocess, "run", side_effect=self.mock_inventory()), \
             patch.object(model_cli, "ModelACPClient") as client_type:
            client = client_type.return_value
            client.handshake.return_value = {"selection_echo_verified": True, "prompt_sent": False}
            client.kas_version = "kas-test"
            client.close.return_value = []
            ready = model_cli.preflight_model_cli(self.session, executable="fake")
        client.prompt.assert_not_called()
        client.close.assert_called_once()
        self.assertEqual(ready["config"], config)
        self.assertEqual((self.session / "session.json").read_bytes(), original)
        self.assertFalse((self.session / "kiro/launch.json").exists())
        receipt = json.loads((self.session / "kiro/preflight/result.json").read_text())
        self.assertFalse(receipt["prompt_sent"])
        self.assertEqual(receipt["model_launcher"], config["model_launcher"])

    @requires_evidence('.venv-kiro')
    def test_preflight_cleanup_error_is_explicit_failure(self):
        self.configure()
        with patch.object(model_cli.subprocess, "run", side_effect=self.mock_inventory()), \
             patch.object(model_cli, "ModelACPClient") as client_type:
            client = client_type.return_value
            client.handshake.return_value = {"selection_echo_verified": True, "prompt_sent": False}
            client.kas_version = "kas-test"
            client.close.return_value = ["owned reader did not exit"]
            self.assert_code("cleanup_failed", lambda: model_cli.preflight_model_cli(self.session, executable="fake"))
            client.prompt.assert_not_called()

    def test_cli_requires_explicit_action_and_binds_supplement_only_during_configuration(self):
        from scripts import run_model_cli as entry
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            entry.main(["--session", str(self.session)])
        instructions = Path(self.tmp.name) / "cohort.txt"
        instructions.write_text("Preserve first airborne update.", encoding="utf-8")
        with patch("sys.stdout", new_callable=io.StringIO):
            code = entry.main(["--session", str(self.session), "--configure",
                               "--supplemental-instructions-file", str(instructions)])
        self.assertEqual(code, 0)
        self.assertIn("Preserve first airborne update.", Path(model_cli.load_model_config(self.session)["prompt_path"]).read_text())
        with patch.object(entry, "preflight_model_cli") as preflight, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(entry.main(["--session", str(self.session), "--preflight",
                                        "--supplemental-instructions-file", str(instructions)]), 1)
            preflight.assert_not_called()

    def test_cli_selection_conflict_never_changes_controls_or_calls_model(self):
        from scripts import run_model_cli as entry
        config = self.configure()
        with patch.object(entry, "run_model_cli") as run, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(entry.main(["--session", str(self.session), "--run", "--model", "gpt-6-astra"]), 1)
            run.assert_not_called()
        self.assertEqual(config, model_cli.load_model_config(self.session))

    def run_mock(self, *, handshake_error=None, prompt_error=None, generation=True, stop="end_turn", cleanup=None):
        config = self.configure()
        ready = {"executable": "fake", "config": config, "inventory": {"models": []}, "cli_version": "test"}
        client = MagicMock()
        client.process.pid = 123456
        client.process.poll.return_value = None
        client.argv = ["fake", "acp"]
        client.kas_version = "kas-test"
        client.guard = kiro.StreamConfigurationGuard(config["agent_name"], config["model"], config["effort"])
        client.guard.selected = dict(client.guard.expected)
        client.prompt_sent = False
        client.handshake.side_effect = handshake_error
        client.handshake.return_value = {"selection_echo_verified": True}
        def prompt(text):
            self.assertIn("Model: gpt-5.6-sol. Effort: high.", text)
            client.prompt_sent = True
            if generation:
                client.guard.observe({"sessionUpdate": "agent_message_chunk"})
            if prompt_error:
                raise prompt_error
            return {"stopReason": stop}
        client.prompt.side_effect = prompt
        client.close.return_value = cleanup or []
        with patch.object(model_cli, "preflight_model_cli", return_value=ready), \
             patch.object(model_cli, "ModelACPClient", return_value=client) as constructor, \
             patch("kiroclops.integration.session.Session") as session_type:
            record = model_cli.run_model_cli(self.session, timeout=1800)
        session_type.return_value.start_clock.assert_called_once()
        self.assertEqual(constructor.call_args.args[2], 1800)
        client.close.assert_called_once()
        self.assertEqual(record, json.loads((self.session / "kiro/launch.json").read_text()))
        return record, client

    def test_completed_run_records_actual_sol_stream_and_cleanup_without_exitcode_requirement(self):
        record, client = self.run_mock()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["model_requested"], "gpt-5.6-sol")
        self.assertEqual(record["effort_requested"], "high")
        self.assertEqual(record["model_reported"], ["gpt-5.6-sol"])
        self.assertEqual(record["model_confirmation"], "stream_confirmed")
        self.assertTrue(record["prompt_sent"])
        self.assertIsNone(record["process_returncode_after_cleanup"])
        self.assertEqual(record["model_launcher"], model_cli.launcher_binding())

    def test_handshake_failure_closes_owned_client_without_prompt(self):
        record, client = self.run_mock(handshake_error=kiro.KiroError("effort_unavailable", "No high effort"))
        self.assertEqual(record["status"], "effort_unavailable")
        self.assertFalse(record["prompt_sent"])
        client.prompt.assert_not_called()

    def test_end_turn_without_generation_evidence_is_not_success(self):
        record, _ = self.run_mock(generation=False)
        self.assertEqual(record["status"], "generation_unverified")
        self.assertEqual(record["model_reported"], [])

    def test_timeout_preserves_stop_cause_and_cleans_owned_process(self):
        record, _ = self.run_mock(prompt_error=kiro.KiroError("timeout", "Reached deadline"))
        self.assertEqual(record["status"], "timeout")
        self.assertNotIn("prompt_result", record)
        self.assertIn("finished_at", record)

    def test_non_end_turn_and_cleanup_failure_never_mark_complete(self):
        record, _ = self.run_mock(stop="cancelled", cleanup=["reader still active"])
        self.assertEqual(record["status"], "cleanup_failed")
        self.assertEqual(record["prompt_result"]["stopReason"], "cancelled")
        self.assertEqual(record["cleanup_errors"], ["reader still active"])

    def test_invalid_or_cancelled_budget_rejects_before_any_preflight(self):
        with patch.object(model_cli, "preflight_model_cli") as preflight:
            for timeout in (0, -1, 1801, float("nan"), float("inf"), True, "1800"):
                with self.subTest(timeout=timeout):
                    self.assert_code("invalid_timeout", lambda: model_cli.run_model_cli(self.session, timeout=timeout))
            cancelled = threading.Event()
            cancelled.set()
            self.assert_code("cancelled", lambda: model_cli.run_model_cli(self.session, cancel=cancelled))
            preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
