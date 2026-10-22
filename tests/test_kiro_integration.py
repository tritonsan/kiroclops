"""Kiro integration controls; all model invocations are mocked."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

from kiroclops.integration import kiro


class KiroIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name) / "session with spaces"
        self.target = self.session / "target"
        self.target.mkdir(parents=True)
        (self.target / "project.godot").write_text("[application]\n", encoding="utf-8")
        self.player = self.target / "source/characters/player/player.gd"
        self.player.parent.mkdir(parents=True)
        self.player.write_text("extends CharacterBody2D\n", encoding="utf-8")
        (self.session / "session.json").write_text(json.dumps({"status": "new", "started_at": None,
            "deadline_at": None, "selected_candidate_id": None, "sealed_at": None}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_skill_and_local_mcp_only(self):
        original = self.player.read_bytes()
        config = kiro.configure_kiro(self.session)
        loaded = kiro._load_config(self.session, "tools")
        agent = json.loads(Path(config["agent_path"]).read_text(encoding="utf-8"))
        skill = (kiro.server_root() / "power/kiroclops/skills/motion-calibration/SKILL.md").read_text(encoding="utf-8")
        local_skill = Path(config["power_dir"]) / "skills/motion-calibration/SKILL.md"
        self.assertEqual(agent["prompt"], skill)
        self.assertEqual(local_skill.read_text(encoding="utf-8"), skill)
        self.assertEqual(agent["model"], "claude-opus-5")
        self.assertFalse(agent["includePowers"])
        self.assertFalse(agent["includeMcpJson"])
        self.assertEqual(agent["mcpServers"]["kiroclops"]["args"][-1], str(self.session.resolve()))
        self.assertEqual(loaded["max_candidates"], 3)
        self.assertEqual(self.player.read_bytes(), original)
        self.assertEqual(kiro.configure_kiro(self.session)["generated_hashes"], config["generated_hashes"])

    def test_power_mcp_uses_agent_plugins_schema_not_custom_agent_timeout(self):
        # Official Agent Plugins 1.0 stdio schema forbids extra properties:
        # https://agent-plugins.org/schemas/1.0.0/mcp.schema.json
        config = kiro.configure_kiro(self.session)
        power = json.loads((Path(config["power_dir"]) / "mcp.json").read_text(encoding="utf-8"))
        server = power["mcpServers"]["kiroclops"]
        self.assertEqual(power["$schema"], "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(set(server), {"type", "command", "args", "env"})
        self.assertNotIn("timeout", server)
        agent = json.loads(Path(config["agent_path"]).read_text(encoding="utf-8"))
        self.assertEqual(agent["mcpServers"]["kiroclops"]["timeout"], 30000)
        self.assertNotIn("/", server["command"])
        self.assertNotIn("\\", server["command"])
        if os.name == "nt":
            self.assertEqual(server["env"]["KIROCLOPS_EXPECTED_PYTHON"], agent["mcpServers"]["kiroclops"]["command"])
        self.assertIn(str(self.session.resolve()), " ".join(server["args"]))
        for key, value in agent["mcpServers"]["kiroclops"]["env"].items():
            self.assertEqual(server["env"][key], value)

    @unittest.skipUnless(os.name == "nt", "Windows Power executable resolution")
    def test_power_windows_launcher_uses_literal_args_and_child_only_path(self):
        server = {"command": "C:/owner's folder/python.exe", "args": ["-m", "module", "--session", "D:/a $literal; name"], "env": {}}
        original = os.environ.get("PATH", "")
        result = kiro._power_server(server)
        self.assertEqual(result["command"], "python.exe")
        self.assertEqual(result["args"], server["args"])
        self.assertEqual(result["env"]["KIROCLOPS_EXPECTED_PYTHON"], server["command"])
        self.assertEqual(result["env"]["PATH"], str(Path(server["command"]).parent) + os.pathsep + original)
        self.assertEqual(os.environ.get("PATH", ""), original)

    def test_baseline_has_only_three_exact_commands_no_measurement_tools(self):
        config = kiro.configure_kiro(self.session, "baseline")
        agent = json.loads(Path(config["agent_path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(config["allowed_commands"]), {"prepare", "capture", "finish"})
        self.assertEqual(agent["mcpServers"], {})
        self.assertNotIn("@kiroclops", agent["tools"])
        self.assertIsNone(config["power_dir"])
        for command in config["allowed_commands"].values():
            allowed = kiro.check_tool_request(self.session, "baseline", {"tool_name": "execute_pwsh", "tool_input": {"command": command}})
            self.assertTrue(allowed["allow"])
            rejected = kiro.check_tool_request(self.session, "baseline", {"tool_name": "execute_pwsh", "tool_input": {"command": command + "; Get-Content ../secret"}})
            self.assertFalse(rejected["allow"])
        with self.assertRaises(kiro.KiroError):
            kiro.configure_kiro(self.session, "tools")

    def test_ide_profile_uses_only_real_selected_power_without_direct_mcp(self):
        config = kiro.configure_kiro(self.session, surface="ide")
        agent = json.loads(Path(config["agent_path"]).read_text())
        self.assertEqual(config["integration"], "ide_power")
        self.assertEqual(agent["tools"], ["read", "write", "kiro_powers"])
        self.assertFalse(agent["includePowers"])
        self.assertFalse(agent["includeMcpJson"])
        self.assertEqual(agent["mcpServers"], {})
        self.assertEqual(json.loads((self.target / ".kiro/settings/mcp.json").read_text())["mcpServers"], {})
        self.assertIn("action=activate", agent["prompt"])
        cases = [
            ("kiro_powers", {"action": "activate", "powerName": "kiroclops"}, True),
            ("kiro_powers", {"action": "activate", "powerName": "other"}, False),
            ("kiro_powers", {"action": "list"}, False),
            ("kiro_powers", {"action": "readSkill", "powerName": "kiroclops", "skillName": "motion-calibration"}, True),
            ("kiro_powers", {"action": "readSkill", "powerName": "kiroclops", "skillName": "../../other"}, False),
            ("kiro_powers", {"action": "use", "powerName": "kiroclops", "serverName": "kiroclops", "toolName": "get_job", "arguments": {}}, True),
            ("kiro_powers", {"action": "use", "powerName": "kiroclops", "serverName": "other", "toolName": "get_job"}, False),
            ("power-kiroclops-kiroclops::get_job", {}, True),
            ("kiroclops::get_job", {}, False),
            ("power-other-kiroclops::get_job", {}, False),
        ]
        for tool, args, expected in cases:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(kiro.check_tool_request(self.session, "tools", {"tool_name": tool, "tool_input": args})["allow"], expected)

    def test_native_power_parameterless_calls_accept_null_empty_or_omitted_arguments(self):
        config = kiro.configure_kiro(self.session, surface="ide")
        original_hashes = dict(config["generated_hashes"])
        # Actual IDE export: inspect_session included arguments:null, with the
        # unrelated optional native fields also null. Kiro forwards this as {}.
        for name in ("inspect_session", "prepare_experiment", "evaluate_candidate",
                     "finalize_candidate", "build_report"):
            for arguments in ({}, None, "omitted"):
                with self.subTest(tool=name, arguments=arguments):
                    args = {"action": "use", "powerName": "kiroclops",
                            "serverName": "kiroclops", "toolName": name,
                            "steeringFile": None, "skillName": None}
                    if arguments != "omitted":
                        args["arguments"] = arguments
                    decision = kiro.guard_request(self.session, "tools", {
                        "hook_event_name": "PreToolUse", "tool_name": "kiro_powers",
                        "tool_input": args})
                    self.assertTrue(decision["allow"], decision)
        self.assertEqual(kiro._load_config(self.session, "tools")["generated_hashes"], original_hashes)

    def test_native_power_null_arguments_preserve_scope_and_type_restrictions(self):
        kiro.configure_kiro(self.session, surface="ide")
        base = {"action": "use", "powerName": "kiroclops", "serverName": "kiroclops",
                "toolName": "inspect_session", "arguments": None}
        cases = [{"arguments": value} for value in (False, 0, "", "{}", [], [1])]
        cases += [{"powerName": "other"}, {"serverName": "other"},
                  {"toolName": "unknown"}, {"action": "list"}]
        for changes in cases:
            with self.subTest(changes=changes):
                decision = kiro.check_tool_request(self.session, "tools", {
                    "tool_name": "kiro_powers", "tool_input": {**base, **changes}})
                self.assertFalse(decision["allow"], decision)
        # Native Power remains unavailable to the CLI tool profile.
        cli = self.session / "other-cli-session"
        (cli / "target").mkdir(parents=True)
        (cli / "target/project.godot").write_text("[application]\n", encoding="utf-8")
        kiro.configure_kiro(cli)
        self.assertFalse(kiro.check_tool_request(cli, "tools", {
            "tool_name": "kiro_powers", "tool_input": base})["allow"])

    def test_guard_blocks_sealed_expired_or_missing_state_writes_but_allows_reports(self):
        kiro.configure_kiro(self.session)
        state_path = self.session / "session.json"
        payload = {"tool_name": "str_replace", "tool_input": {"path": str(self.player)}}
        states = [{"selected_candidate_id": "candidate-01"}, {"sealed_at": "2026-09-18T01:00:00+00:00"},
                  {"deadline_at": "2000-01-01T00:00:00+00:00"}, {"deadline_at": "bad"}, {"deadline_at": "2099-01-01"},
                  {"deadline_at": 946684800.0}, {"deadline_at": float("inf")}, {"deadline_at": True}]
        for state in states:
            state_path.write_text(json.dumps(state))
            self.assertFalse(kiro.check_tool_request(self.session, "tools", payload)["allow"])
            for tool in ("get_job", "build_report"):
                self.assertTrue(kiro.check_tool_request(self.session, "tools", {"tool_name": "kiroclops::" + tool})["allow"])
        state_path.unlink()
        self.assertFalse(kiro.check_tool_request(self.session, "tools", payload)["allow"])
        state_path.write_text(json.dumps({"deadline_at": "2099-01-01T00:00:00+00:00"}))
        self.assertTrue(kiro.check_tool_request(self.session, "tools", payload)["allow"])
        state_path.write_text(json.dumps({"deadline_at": 4070908800.0}))
        self.assertTrue(kiro.check_tool_request(self.session, "tools", payload)["allow"])

    def test_surface_cannot_change_after_experiment_starts(self):
        kiro.configure_kiro(self.session)
        (self.session / "session.json").write_text(json.dumps({"started_at": "2026-09-18T01:00:00+00:00"}))
        with self.assertRaises(kiro.KiroError) as caught:
            kiro.configure_kiro(self.session, surface="ide")
        self.assertEqual(caught.exception.code, "surface_conflict")

    def test_path_and_tool_guard_rejects_outside_reads_and_protected_writes(self):
        kiro.configure_kiro(self.session)
        cases = [
            ("read_file", {"path": "source/characters/player/player.gd"}, True),
            ("list_directory", {"path": "~"}, False),
            ("read_file", {"path": "$HOME/secrets"}, False),
            ("read_files", {"paths": ["project.godot", "../session.json"]}, False),
            ("fs_read", {"path": str(self.session / "previous-profile.json")}, False),
            ("file_search", {"query": "*.gd"}, True),
            ("str_replace", {"path": "source/characters/player/new_helper.gd"}, True),
            ("fs_write", {"path": ".kiro/settings/mcp.json"}, False),
            ("fs_write", {"path": ".kiroclops-reference/inputs.json"}, False),
            ("fs_write", {"path": "project.godot"}, False),
            ("fs_write", {"path": "source/characters/player/../../secret.gd"}, False),
            ("execute_pwsh", {"command": "Get-Content ../session.json"}, False),
            ("@kiroclops/inspect_session", {}, True),
            ("mcp__kiroclops__evaluate_candidate", {}, True),
            ("tool_load", {"tool_ids": ["kiroclops::*"]}, True),
            ("tool_call", {"tool_id": "kiroclops::get_job", "arguments": {"job_id": "abc"}}, True),
            ("tool_load", {"tool_ids": ["kiroclops::*", "other::*"]}, False),
            ("tool_call", {"tool_id": "other::get_job", "arguments": {}}, False),
            ("@other/inspect_session", {}, False),
            ("unknown", {}, False),
        ]
        for name, args, expected in cases:
            with self.subTest(tool=name, args=args):
                result = kiro.check_tool_request(self.session, "tools", {"tool_name": name, "tool_input": args})
                self.assertEqual(result["allow"], expected)

    def test_modified_controls_fail_closed_and_audit_is_outside_target(self):
        config = kiro.configure_kiro(self.session)
        Path(config["agent_path"]).write_text("{}", encoding="utf-8")
        result = kiro.guard_request(self.session, "tools", {"tool_name": "read_file", "tool_input": {"path": "project.godot"}})
        self.assertFalse(result["allow"])
        self.assertIn("changed", result["reason"])
        audit = self.session / "kiro/tool-audit.jsonl"
        self.assertFalse(json.loads(audit.read_text(encoding="utf-8"))["allow"])

    def test_hook_denial_and_malformed_input_use_blocking_exit_two(self):
        import sys
        kiro.configure_kiro(self.session)
        script = kiro.server_root() / "scripts/run_kiro.py"
        command = [sys.executable, str(script), "--guard", "--session", str(self.session)]
        for payload in ('not-json', '[]', json.dumps({"tool_name": "read_file", "tool_input": {"path": "~/.secret"}})):
            result = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)

    def test_missing_exact_model_stops_before_paid_process(self):
        kiro.configure_kiro(self.session)
        result = subprocess.CompletedProcess([], 0, '{"models":[{"model_id":"auto"}]}', "")
        with patch.object(kiro, "_python", return_value=Path(__file__)), patch.object(kiro.subprocess, "run", return_value=result), patch.object(kiro.subprocess, "Popen") as launch:
            with self.assertRaises(kiro.KiroError) as caught:
                kiro.run_kiro(self.session, executable="kiro-cli")
            self.assertEqual(caught.exception.code, "model_unavailable")
            launch.assert_not_called()

    def test_model_metadata_parser_ignores_available_models_and_message_text(self):
        self.assertEqual(kiro._reported_models({"params": {"models": {"currentModelId": kiro.MODEL,
                         "availableModels": [{"modelId": "auto"}]}}}), {kiro.MODEL})
        self.assertEqual(kiro._reported_models({"text": '{"modelId":"auto"}'}), set())
        self.assertEqual(kiro._reported_models({"params": {"update": {"configOptions": [
            {"category": "model", "currentValue": kiro.MODEL, "options": [{"value": "auto"}]}]}}}), {kiro.MODEL})

    @staticmethod
    def config_event(model=kiro.MODEL, agent="kiroclops-tools", effort="high"):
        return {"type": "sessionUpdate", "data": {"sessionId": "test", "update": {
            "sessionUpdate": "config_option_update", "configOptions": [
                {"id": "mode", "currentValue": agent}, {"id": "model", "currentValue": model},
                {"id": "effortLevel", "currentValue": effort}]}}}

    def test_stream_guard_allows_startup_defaults_then_locks_generation_config(self):
        guard = kiro.StreamConfigurationGuard("kiroclops-tools")
        guard.observe(self.config_event("auto", "vibe"))
        guard.observe(self.config_event())
        guard.observe({"data": {"update": {"sessionUpdate": "agent_thought_chunk", "content": {"text": "auto"}}}})
        self.assertTrue(guard.summary()["generation_configuration_verified"])
        self.assertEqual(guard.summary()["selected"]["model"], kiro.MODEL)
        with self.assertRaises(kiro.KiroError) as caught:
            guard.observe(self.config_event("auto"))
        self.assertEqual(caught.exception.code, "model_mismatch")

    def test_stream_guard_requires_model_agent_and_effort_before_generation(self):
        for event, code in ((self.config_event("auto"), "model_mismatch"),
                            (self.config_event(agent="vibe"), "mode_mismatch"),
                            (self.config_event(effort="low"), "effort_mismatch")):
            guard = kiro.StreamConfigurationGuard("kiroclops-tools")
            guard.observe(event)
            with self.assertRaises(kiro.KiroError) as caught:
                guard.observe({"params": {"update": {"sessionUpdate": "tool_call", "title": "List Directory"}}})
            self.assertEqual(caught.exception.code, code)

    def test_stream_guard_ignores_deterministic_config_fetch(self):
        guard = kiro.StreamConfigurationGuard("kiroclops-tools")
        guard.observe({"data": {"update": {"sessionUpdate": "tool_call", "title": "Fetching your cloud config",
            "_meta": {"kiro": {"toolId": "fetch_cloud_config"}}}}})
        self.assertFalse(guard.summary()["generation_started"])

    def test_cancelled_before_launch_and_invalid_timeout_do_not_preflight(self):
        event = threading.Event()
        event.set()
        with patch.object(kiro, "preflight_kiro") as preflight:
            for kwargs in ({"cancel": event}, {"timeout": 1801}, {"timeout": 0}):
                with self.assertRaises(kiro.KiroError):
                    kiro.run_kiro(self.session, **kwargs)
            preflight.assert_not_called()

    def test_launcher_sends_prompt_only_after_handshake_and_records_active_model(self):
        config = kiro.configure_kiro(self.session)
        ready = {"executable": "kiro-cli", "config": config, "inventory": {"models": [{"model_id": kiro.MODEL}]}, "cli_version": "2.21.0"}
        client = MagicMock()
        client.close.return_value = []
        client.process.pid = 1234
        client.process.poll.return_value = 0
        client.argv = ["kiro-cli", "acp", "--agent-engine", "v3", "--auth-method", "cli"]
        client.kas_version = "0.54.8"
        client.prompt_sent = True
        client.handshake.return_value = {"selected": {"mode": "kiroclops-tools", "model": kiro.MODEL, "effort": "high"}}
        client.guard = kiro.StreamConfigurationGuard("kiroclops-tools")
        client.guard.observe(self.config_event("auto", "vibe"))
        client.guard.observe(self.config_event())
        client.guard.observe({"params": {"update": {"sessionUpdate": "agent_message_chunk"}}})
        client.prompt.return_value = {"stopReason": "end_turn"}
        order = []
        client.handshake.side_effect = lambda: order.append("handshake") or {"verified": True}
        client.prompt.side_effect = lambda text: order.append("prompt") or {"stopReason": "end_turn"}
        with patch.object(kiro, "preflight_kiro", return_value=ready), patch.object(kiro, "_ACPClient", return_value=client), patch("kiroclops.integration.session.Session") as session:
            result = kiro.run_kiro(self.session)
            session.return_value.start_clock.assert_called_once()
        self.assertEqual(order, ["handshake", "prompt"])
        self.assertEqual(client.prompt.call_args.args[0], Path(config["prompt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transport"], "acp")
        self.assertEqual(result["model_reported"], [kiro.MODEL])
        self.assertEqual(result["model_confirmation"], "stream_confirmed")
        self.assertEqual(result["stream_configuration"]["observed_models"], ["auto", kiro.MODEL])
        client.close.assert_called_once()

    def test_failed_handshake_never_sends_paid_prompt(self):
        config = kiro.configure_kiro(self.session)
        ready = {"executable": "kiro-cli", "config": config, "inventory": {"models": []}}
        client = MagicMock()
        client.close.return_value = []
        client.process.pid = 1234
        client.process.poll.return_value = 0
        client.argv, client.kas_version, client.prompt_sent = [], "0.54.8", False
        client.guard = kiro.StreamConfigurationGuard("kiroclops-tools")
        client.handshake.side_effect = kiro.KiroError("agent_not_loaded", "Wrong workspace agent")
        with patch.object(kiro, "preflight_kiro", return_value=ready), patch.object(kiro, "_ACPClient", return_value=client), patch("kiroclops.integration.session.Session"):
            result = kiro.run_kiro(self.session)
        self.assertEqual(result["status"], "agent_not_loaded")
        self.assertFalse(result["prompt_sent"])
        client.prompt.assert_not_called()
        client.close.assert_called_once()

    def test_acp_handshake_verifies_origin_and_exact_tool_availability(self):
        config = kiro.configure_kiro(self.session)
        client = object.__new__(kiro._ACPClient)
        client.config = config
        client.deadline = __import__("time").monotonic() + 20
        client.model_choices = {kiro.MODEL}
        client.guard = kiro.StreamConfigurationGuard(config["agent_name"])
        client.guard.observe(self.config_event())
        client.tags = {"read", "write"} | {f"@kiroclops/{t}" for t in kiro.MCP_TOOLS}
        client.servers = {"kiroclops": {"status": "connected", "tools": [{"name": t} for t in kiro.MCP_TOOLS]}}
        created = {"sessionId": "test", "_meta": {"workspacePaths": [str(self.target)]},
            "modes": {"availableModes": [{"id": config["agent_name"], "_meta": {"kiro": {"resource": {
                "source": {"origin": "workspace", "root": str(self.target)}}}}}]}}
        hook_path = self.target / ".kiro/hooks/kiroclops-guard.json"
        hook_command = json.loads(hook_path.read_text())["hooks"][0]["action"]["command"]
        hooks = {"hooks": [{"_meta": {"enabled": True, "filePath": str(hook_path)},
                            "action": {"command": hook_command}}]}
        client.request = MagicMock(side_effect=[{"protocolVersion": 1}, created, {}, {}, {}, hooks])
        self.assertEqual(client.handshake()["mcp_tools"], sorted(kiro.MCP_TOOLS))
        created["modes"]["availableModes"][0]["_meta"]["kiro"]["resource"]["source"]["origin"] = "user"
        client.request = MagicMock(side_effect=[{"protocolVersion": 1}, created])
        with self.assertRaises(kiro.KiroError) as caught:
            client.handshake()
        self.assertEqual(caught.exception.code, "agent_not_loaded")

    def test_acp_permissions_require_bound_arguments_and_grant_once_only(self):
        config = kiro.configure_kiro(self.session)
        client = object.__new__(kiro._ACPClient)
        client.config = config
        client.tool_inputs = {"call-1": {}}
        client.tool_identifiers = {}
        client._send = MagicMock()
        event = {"jsonrpc": "2.0", "id": 8, "method": "session/request_permission", "params": {
            "toolCall": {"toolCallId": "call-1"}, "_meta": {"kiro": {"toolId": "kiroclops::inspect_session"}},
            "options": [{"kind": "allow_always", "optionId": "forever"}, {"kind": "allow_once", "optionId": "once"}]}}
        client._permission_reply(event)
        self.assertEqual(client._send.call_args.args[0]["result"]["outcome"], {"outcome": "selected", "optionId": "once"})
        client.tool_identifiers["call-1"] = "other/tool"
        with self.assertRaises(kiro.KiroError):
            client._permission_reply(event)
        client.tool_identifiers.clear()
        event["params"]["toolCall"]["toolCallId"] = "unknown"
        with self.assertRaises(kiro.KiroError):
            client._permission_reply(event)
        self.assertEqual(client._send.call_args.args[0]["result"]["outcome"], {"outcome": "cancelled"})
        event["params"]["toolCall"]["toolCallId"] = "call-1"
        event["params"]["_meta"]["kiro"]["toolId"] = "read_file"
        client.tool_inputs["call-1"] = {"path": "~/.secret"}
        with self.assertRaises(kiro.KiroError):
            client._permission_reply(event)

    def test_acp_prompt_uses_standard_field_and_rechecks_generated_controls(self):
        config = kiro.configure_kiro(self.session)
        client = object.__new__(kiro._ACPClient)
        client.config, client.session_id = config, "s-test"
        client.deadline = __import__("time").monotonic() + 20
        client.guard = kiro.StreamConfigurationGuard(config["agent_name"])
        client.guard.observe(self.config_event())
        client.request = MagicMock(return_value={"stopReason": "end_turn"})
        client.prompt("literal task")
        self.assertEqual(client.request.call_args.args, ("session/prompt", {"sessionId": "s-test", "prompt": [{"type": "text", "text": "literal task"}]}))
        client.request.reset_mock()
        Path(config["agent_path"]).write_text("{}")
        with self.assertRaises(kiro.KiroError):
            client.prompt("must not send")
        client.request.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process ownership")
    def test_windows_job_closes_descendant_after_parent_has_exited(self):
        import ctypes
        import sys
        from ctypes import wintypes as w
        pid_path = Path(self.tmp.name) / "child-pid.txt"
        code = ("import subprocess,sys;from pathlib import Path;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                "creationflags=subprocess.CREATE_NO_WINDOW);"
                "Path(sys.argv[1]).write_text(str(p.pid))")
        job = kiro._WindowsJob()
        parent, child = None, None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        try:
            parent = subprocess.Popen([sys.executable, "-c", code, str(pid_path)],
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            job.assign_and_resume(parent)
            self.assertEqual(parent.wait(timeout=10), 0)
            child = kernel.OpenProcess(0x100000, False, int(pid_path.read_text()))
            self.assertTrue(child)
            self.assertEqual(kernel.WaitForSingleObject(child, 0), 258)  # still running
            job.close()
            self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0)  # killed despite exited parent
        finally:
            job.close()
            if parent is not None:
                kiro._terminate_tree(parent)
            if child:
                kernel.CloseHandle(child)

    def test_taskkill_timeout_falls_back_to_owned_process_kill(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("kiro", 5), 0]
        with patch.object(kiro.os, "name", "nt"), patch.object(kiro.subprocess, "run", side_effect=subprocess.TimeoutExpired("taskkill", 15)):
            kiro._terminate_tree(process)
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
