"""IDE launch receipts bind explicit UI evidence to native turn timestamps."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kiroclops.integration import ide_evidence, ide_run, kiro, session, storage
from scripts import record_ide_run


class IDERunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "café IDE"
        self.directory = self.root / "sessions/ide-demo"
        self.target = self.directory / "target"
        for project in (self.target, self.directory / "baseline"):
            project.mkdir(parents=True)
            (project / "project.godot").write_text('[application]\nconfig/name="Slay"\n')
            script = project / "source/characters/player/player.gd"
            script.parent.mkdir(parents=True)
            script.write_text("extends Node\n")
        for module in (storage, ide_evidence):
            mocked = patch.object(module, "ROOT", self.root)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.config = kiro.configure_kiro(self.directory, mode="tools", surface="ide")
        self.state = {"session_id": "ide-demo", "mode": "tools", "status": "new",
                      "target": str(self.target), "baseline": str(self.directory / "baseline"),
                      "baseline_hashes": storage.project_hashes(self.target), "started_at": None, "deadline_at": None,
                      "candidates": [], "selected_candidate_id": None, "active_job_id": None,
                      "reference": {}, "baseline_runs": {}, "final": None}
        storage.write_json(self.directory / "session.json", self.state)
        self.screenshot = self.directory / "ide/settings.png"
        self.screenshot.parent.mkdir()
        self.screenshot.write_bytes(b"test UI screenshot fixture")
        self.start_time = datetime.now(timezone.utc) - timedelta(minutes=3)
        self.end_time = self.start_time + timedelta(seconds=62.5)
        self.clock = patch.object(session, "utc_now", return_value=self.start_time.isoformat())
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def start(self):
        return ide_run.start_ide_run(self.directory, screenshots=["ide/settings.png"],
                                     agent="kiroclops-tools", model="claude-opus-5", effort="high")

    def archive(self, *, later_start=False, stop="end_turn", model="claude-opus-5", prelude=None, user_text=None, postlude=None):
        def msg(kind, seconds, **extra):
            return {"id": f"m{seconds}", "timestamp": (self.start_time + timedelta(seconds=seconds)).isoformat(),
                    "payload": {"type": kind, **extra}}
        messages = [msg("turn_start", 1),
                    msg("user", 2, content=user_text if user_text is not None else (self.target / ".kiro/kiroclops-prompt.md").read_text(encoding="utf-8")),
                    msg("tool_call", 3, toolCallId="inspect", toolName="inspect_session", args={}, status="completed"),
                    msg("tool_result", 4, toolCallId="inspect", success=True, content='{"status":"ready"}'),
                    msg("turn_end", 62.5, stopReason=stop)]
        if later_start:
            messages.append(msg("turn_start", 63))
        if prelude == "cloud":
            messages = [msg("tool_call", -100, toolCallId="cloud", toolName="fetch_cloud_config",
                            actionType="fetch_cloud_config", args={"toolName": "fetch_cloud_config"}, status="completed",
                            _meta={"kiro": {"toolId": "fetch_cloud_config"}}),
                        msg("tool_result", -99, toolCallId="cloud", success=True, content="{}")] + messages
        elif prelude == "assistant":
            messages.insert(0, msg("assistant", -100, content="Earlier generation", operationType="Say"))
        if postlude == "descriptor":
            entry = msg("session_start", 62.545, agentType="kiroclops-tools", content="Agent instructions",
                        forcedRole=True, messageId="01234567-89ab-cdef-0123-456789abcdef")
            entry["id"] = "session_start_native-demo_kiroclops-tools"
            messages.append(entry)
        elif postlude == "assistant":
            messages.append(msg("assistant", 62.545, content="Later model output", operationType="Say"))
        metadata = {"id": "native-demo", "workspacePaths": [str(self.target)], "agentMode": "kiroclops-tools",
                    "modelId": model, "effortLevel": "high", "createdAt": messages[0]["timestamp"],
                    "lastModifiedAt": messages[-1]["timestamp"]}
        path = self.directory / "ide/native.zip"
        with zipfile.ZipFile(path, "w") as output:
            output.writestr("native-demo/session.json", json.dumps(metadata))
            output.writestr("native-demo/messages.jsonl", "\n".join(json.dumps(value) for value in messages))
        return path

    def test_start_preserves_exact_prompt_ui_and_calls_clock_only_once(self):
        target_before = storage.project_hashes(self.target)
        controls_before = {str(path): path.read_bytes() for path in (self.target / ".kiro").rglob("*") if path.is_file()}
        original_clock = session.Session.start_clock
        with patch.object(session.Session, "start_clock", autospec=True, side_effect=original_clock) as clock:
            record = self.start()
            with self.assertRaises(storage.IntegrationError):
                self.start()
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(record["started_at"], self.start_time.isoformat())
        self.assertEqual(record["prompt_sha256"], storage.sha256(self.target / ".kiro/kiroclops-prompt.md"))
        self.assertEqual(record["ui_observed"]["evidence"], [{"path": "ide/settings.png", "sha256": storage.sha256(self.screenshot)}])
        self.assertFalse(record["strict_configuration_verified"])
        self.assertNotIn("model_confirmation", record)
        self.assertEqual(storage.project_hashes(self.target), target_before)
        self.assertEqual(controls_before, {str(path): path.read_bytes() for path in (self.target / ".kiro").rglob("*") if path.is_file()})

    def test_nonfresh_session_or_existing_launch_rejected_without_clock(self):
        for update in ({"mode": "baseline"}, {"started_at": "already"}, {"deadline_at": 1},
                       {"active_job_id": "job"}, {"reference": {"calibration": ["old"]}}, {"status": "ready"}):
            storage.write_json(self.directory / "session.json", dict(self.state, **update))
            with self.subTest(update=update), patch.object(session.Session, "start_clock") as clock:
                with self.assertRaises(storage.IntegrationError):
                    self.start()
                clock.assert_not_called()
        storage.write_json(self.directory / "session.json", self.state)
        storage.write_json(self.directory / "kiro/launch.json", {"status": "completed"})
        with self.assertRaises(storage.IntegrationError):
            self.start()
        self.assertIsNone(storage.read_json(self.directory / "session.json")["started_at"])

    def test_explicit_ui_evidence_paths_and_settings_are_required(self):
        for paths in ([], [str(self.root / "outside.png")], ["ide/missing.png"], ["ide/settings.png", "ide/settings.png"]):
            with self.subTest(paths=paths), self.assertRaises((storage.IntegrationError, ide_evidence.IDEExportError)):
                ide_run.start_ide_run(self.directory, screenshots=paths, agent="kiroclops-tools", model="claude-opus-5", effort="high")
        with self.assertRaises(storage.IntegrationError):
            ide_run.start_ide_run(self.directory, screenshots=["ide/settings.png"], agent="kiroclops-tools", model="auto", effort="high")
        self.assertIsNone(storage.read_json(self.directory / "session.json")["started_at"])

    def test_changed_prompt_or_target_is_rejected_before_start(self):
        prompt = self.target / ".kiro/kiroclops-prompt.md"
        original = prompt.read_bytes()
        prompt.write_text("different task")
        with self.assertRaises(kiro.KiroError):
            self.start()
        prompt.write_bytes(original)
        (self.target / "source/characters/player/player.gd").write_text("pre-solved source")
        with self.assertRaises(storage.IntegrationError):
            self.start()
        self.assertIsNone(storage.read_json(self.directory / "session.json")["started_at"])

    def test_finish_uses_native_turn_end_and_preserves_original_start(self):
        self.start()
        start_bytes = (self.directory / "kiro/launch-start.json").read_bytes()
        export = self.archive()
        record = ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["elapsed_seconds"], 62.5)
        self.assertEqual(record["finished_at"], self.end_time.isoformat())
        self.assertNotEqual(record["finish_recorded_at"], record["finished_at"])
        self.assertEqual(record["export"]["sha256"], storage.sha256(export))
        self.assertEqual(record["prompt_result"], {"stopReason": "end_turn"})
        self.assertEqual(record["historical_effort_status"], "unverified")
        self.assertNotIn("stream_configuration", record)
        self.assertNotIn("returncode", record)
        self.assertEqual((self.directory / "kiro/launch-start.json").read_bytes(), start_bytes)
        self.assertEqual(storage.read_json(self.directory / "ide/evidence.json")["surface"], "ide")
        completed_bytes = (self.directory / "kiro/launch.json").read_bytes()
        with self.assertRaises(storage.IntegrationError):
            ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual((self.directory / "kiro/launch.json").read_bytes(), completed_bytes)

    def test_pending_turn_nonterminal_reason_or_model_mismatch_cannot_finish(self):
        self.start()
        for options in ({"later_start": True}, {"stop": "cancelled"}, {"model": "other-model"}):
            export = self.archive(**options)
            with self.subTest(options=options), self.assertRaises(storage.IntegrationError):
                ide_run.finish_ide_run(self.directory, export=export)
            self.assertEqual(storage.read_json(self.directory / "kiro/launch.json")["status"], "running")
        self.assertFalse((self.directory / "ide/finish-receipt.json").exists())

    def test_export_older_than_start_cannot_backfill_a_clock(self):
        self.start()
        self.start_time -= timedelta(minutes=10)
        export = self.archive()
        with self.assertRaises(storage.IntegrationError) as ctx:
            ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual(ctx.exception.code, "ide_timing")

    def test_native_cloud_preload_does_not_backdate_experiment_and_prompt_is_bound(self):
        self.start()
        original_start = (self.directory / "kiro/launch-start.json").read_bytes()
        record = ide_run.finish_ide_run(self.directory, export=self.archive(prelude="cloud"))
        self.assertEqual(record["elapsed_seconds"], 62.5)
        self.assertEqual(record["timing"]["prelude"]["status"], "known_cloud_config")
        self.assertEqual(record["prompt_input"]["status"], "exact")
        self.assertEqual(record["experiment_outcome"]["status"], "not_started")
        self.assertEqual((self.directory / "kiro/launch-start.json").read_bytes(), original_start)

    def test_prior_assistant_generation_is_not_treated_as_preload(self):
        self.start()
        with self.assertRaises(storage.IntegrationError) as ctx:
            ide_run.finish_ide_run(self.directory, export=self.archive(prelude="assistant"))
        self.assertEqual(ctx.exception.code, "ide_timing")
        self.assertFalse((self.directory / "ide/finish-receipt.json").exists())

    def test_whitespace_only_gui_prompt_difference_is_not_claimed_exact(self):
        self.start()
        prompt = (self.target / ".kiro/kiroclops-prompt.md").read_text(encoding="utf-8")
        export = self.archive(user_text=prompt.replace("\n", "\n\n").rstrip())
        record = ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual(record["prompt_input"]["status"], "whitespace_only_difference")
        self.assertFalse(record["strict_configuration_verified"])

    def test_postturn_native_descriptor_keeps_original_end_time(self):
        self.start()
        record = ide_run.finish_ide_run(self.directory, export=self.archive(postlude="descriptor"))
        self.assertEqual(record["elapsed_seconds"], 62.5)
        self.assertEqual(record["timing"]["postlude"]["status"], "native_session_descriptor")
        self.assertNotEqual(record["timing"]["last_message_at"], record["finished_at"])

    def test_later_assistant_output_is_never_hidden_as_postlude(self):
        self.start()
        with self.assertRaises(storage.IntegrationError) as ctx:
            ide_run.finish_ide_run(self.directory, export=self.archive(postlude="assistant"))
        self.assertEqual(ctx.exception.code, "ide_timing")
        self.assertFalse((self.directory / "ide/finish-receipt.json").exists())

    def test_finish_rejects_modified_ui_and_active_job(self):
        self.start()
        export = self.archive()
        original = self.screenshot.read_bytes()
        self.screenshot.write_bytes(b"changed UI")
        with self.assertRaises(storage.IntegrationError) as ctx:
            ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual(ctx.exception.code, "ui_evidence_changed")
        self.screenshot.write_bytes(original)
        session.Session(self.directory).update(lambda state: state.update(active_job_id="active"))
        with self.assertRaises(storage.IntegrationError):
            ide_run.finish_ide_run(self.directory, export=export)

    def test_prior_independent_audit_is_preserved_byte_exact(self):
        self.start()
        export = self.archive()
        previous = b'{"previous":"independent audit"}\n'
        (self.directory / "ide/evidence.json").write_bytes(previous)
        ide_run.finish_ide_run(self.directory, export=export)
        self.assertEqual((self.directory / "ide/evidence-before-finish.json").read_bytes(), previous)

    def test_optional_post_ui_observation_has_separate_exact_evidence(self):
        self.start()
        export = self.archive()
        after = self.directory / "ide/settings-after.png"
        after.write_bytes(b"explicit post-run UI screenshot")
        record = ide_run.finish_ide_run(self.directory, export=export, screenshots=["ide/settings-after.png"],
                                       agent="kiroclops-tools", model="claude-opus-5", effort="high")
        self.assertEqual(record["ui_observed_after"], {"agent": "kiroclops-tools", "model": "claude-opus-5", "effort": "high",
                         "evidence": [{"path": "ide/settings-after.png", "sha256": storage.sha256(after)}]})
        self.assertEqual(record["ui_observed"]["evidence"][0]["path"], "ide/settings.png")
        self.assertFalse(record["strict_configuration_verified"])
        self.assertEqual(record["historical_effort_status"], "unverified")

    def test_partial_changed_or_relabelled_post_ui_observation_is_rejected(self):
        self.start()
        export = self.archive()
        cases = [{"screenshots": ["ide/settings.png"]},
                 {"screenshots": ["ide/settings.png"], "agent": "kiroclops-tools", "model": "auto", "effort": "high"},
                 {"screenshots": ["ide/settings.png"], "agent": "kiroclops-tools", "model": "claude-opus-5", "effort": "high"}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(storage.IntegrationError):
                ide_run.finish_ide_run(self.directory, export=export, **kwargs)
        self.assertEqual(storage.read_json(self.directory / "kiro/launch.json")["status"], "running")
        self.assertFalse((self.directory / "ide/finish-receipt.json").exists())

    def test_cli_requires_observed_settings_and_propagates_finish_export(self):
        with patch.object(record_ide_run, "finish_ide_run", return_value={"status": "completed", "surface": "ide",
                "transport": "ide_gui", "started_at": "start", "strict_configuration_verified": False,
                "elapsed_seconds": 12}) as finish, patch("sys.stdout", new_callable=StringIO):
            code = record_ide_run.main(["finish", "--session", "ide-demo", "--export", "ide/native.zip"])
        self.assertEqual(code, 0)
        finish.assert_called_once_with("ide-demo", export="ide/native.zip")
        with patch.object(record_ide_run, "finish_ide_run", return_value={"status": "completed", "surface": "ide",
                "transport": "ide_gui", "started_at": "start", "strict_configuration_verified": False}) as finish, \
                patch("sys.stdout", new_callable=StringIO):
            code = record_ide_run.main(["finish", "--session", "ide-demo", "--export", "ide/native.zip",
                                        "--screenshot", "ide/after.png", "--agent", "kiroclops-tools", "--model", "claude-opus-5", "--effort", "high"])
        self.assertEqual(code, 0)
        finish.assert_called_once_with("ide-demo", export="ide/native.zip", screenshots=["ide/after.png"],
                                       agent="kiroclops-tools", model="claude-opus-5", effort="high")


if __name__ == "__main__":
    unittest.main()
