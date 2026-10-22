import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import authorship, ide_evidence
from kiroclops.integration.kiro import _baseline_commands
from kiroclops.integration.storage import project_hashes
from scripts.audit_kiro_authorship import main


PLAYER = "source/characters/player/player.gd"
OLD = "extends Node\r\nvar speed = 1\r\n"
NEW = "extends Node\r\nvar speed = 2\n"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def event(update, sid="native-1"):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}}


def permission(tid, tool):
    return {"jsonrpc": "2.0", "id": 12, "method": "session/request_permission", "params": {
        "sessionId": "native-1", "toolCall": {"toolCallId": tid}, "_meta": {"kiro": {"toolId": tool}}}}


class AuthorshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "sessions/cli-test"
        self.target = self.session / "target"
        self.target.mkdir(parents=True)
        self.root_patch = patch.object(ide_evidence, "ROOT", self.root)
        self.root_patch.start()
        hashes = []
        for relative, content in (("baseline", OLD), ("candidates/candidate-01/project", NEW)):
            project = self.session / relative
            player = project / PLAYER
            player.parent.mkdir(parents=True)
            player.write_bytes(content.encode())
            (project / "project.godot").write_bytes(b"[application]\n")
            hashes.append(project_hashes(project))
        record = {"candidate_id": "candidate-01", "source_hashes": hashes[1]}
        write(self.session / "session.json", {"session_id": self.session.name, "baseline_hashes": hashes[0],
              "selected_candidate_id": "candidate-01", "candidates": [record]})
        write(self.session / "candidates/candidate-01/candidate.json", record)
        (self.session / "candidates/candidate-01/patch.diff").write_text("diff", encoding="utf-8")
        self.launch = {"surface": "cli", "transport": "acp", "status": "completed", "mode": "tools",
                       "prompt_result": {"stopReason": "end_turn"},
                       "acp_preflight": {"session_id": "native-1", "workspace_verified": str(self.target)}}
        self.input = {"path": str(self.target / PLAYER), "oldStr": "var speed = 1\n", "newStr": "var speed = 2\n"}
        self.rows = [event({"sessionUpdate": "tool_call", "toolCallId": "write-1", "title": "Arbitrary display text",
                           "kind": "edit", "status": "in_progress", "rawInput": self.input,
                           "_meta": {"kiro": {"toolOrigin": "default", "preview": {
                               "originalContent": self.input["oldStr"], "modifiedContent": self.input["newStr"]}}}}),
                     event({"sessionUpdate": "tool_call_update", "toolCallId": "write-1", "status": "completed",
                            "rawInput": {**self.input, "replace_all": False}, "rawOutput": {"message": "replaced"},
                            "_meta": {"kiro": {"toolOrigin": "default", "preview": {
                                "originalContent": OLD, "modifiedContent": NEW}}}}),
                     event({"sessionUpdate": "tool_call", "toolCallId": "evaluate-1", "title": "Arbitrary MCP title",
                            "kind": "other", "status": "pending", "rawInput": {},
                            "_meta": {"kiro": {"serverName": "kiroclops", "toolOrigin": "client"}}}),
                     permission("evaluate-1", "mcp_kiroclops_evaluate_candidate"),
                     event({"sessionUpdate": "tool_call_update", "toolCallId": "evaluate-1", "status": "completed",
                            "rawOutput": {"response": json.dumps({"candidate_id": "candidate-01", "status": "queued"})}})]
        self.hooks = [{"tool": "str_replace", "allow": True, "paths": [str(self.target / PLAYER)]},
                      {"tool": "mcp_kiroclops_evaluate_candidate", "allow": True, "paths": []}]
        self.save()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def save(self):
        write(self.session / "kiro/launch.json", self.launch)
        for relative, rows in (("kiro/stdout.jsonl", self.rows), ("kiro/tool-audit.jsonl", self.hooks)):
            (self.session / relative).write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def audit(self):
        self.save()
        return authorship.audit_kiro_authorship(self.session)

    def test_native_args_hook_identity_and_completed_preview_reconstruct_exact_bytes(self):
        record = self.audit()
        self.assertEqual(record["status"], "verified", record)
        self.assertEqual(record["candidate_boundary"]["result_line"], 5)
        self.assertEqual(record["normalization"]["completed_write_checkpoints"], 1)
        self.assertEqual(record["normalization"]["native_write_bindings"][0]["native_tool"], "str_replace")
        self.assertEqual(record["changed_files"], [PLAYER])
        self.assertFalse((self.session / "kiro/authorship.json").exists())

    def test_missing_or_wrong_hook_does_not_infer_identity_from_title(self):
        self.hooks[0]["tool"] = "fs_write"
        self.assertEqual(self.audit()["error_code"], "hook_binding")
        self.hooks = []
        self.assertEqual(self.audit()["error_code"], "hook_binding")

    def test_unlogged_extra_allowed_write_is_rejected(self):
        self.hooks.append({"tool": "fs_write", "allow": True, "paths": [str(self.target / PLAYER)]})
        self.assertEqual(self.audit()["error_code"], "hook_binding")

    def test_missing_or_wrong_completed_checkpoint_cannot_verify(self):
        del self.rows[1]["params"]["update"]["_meta"]["kiro"]["preview"]
        self.assertEqual(self.audit()["error_code"], "missing_checkpoint")
        self.rows[1]["params"]["update"]["_meta"]["kiro"]["preview"] = {"originalContent": OLD, "modifiedContent": "wrong"}
        self.assertEqual(self.audit()["status"], "failed")

    def test_permission_identity_and_candidate_json_boundary_are_required(self):
        self.rows[3] = permission("evaluate-1", "mcp_other_evaluate_candidate")
        self.assertEqual(self.audit()["status"], "unverified")
        self.rows[3] = permission("evaluate-1", "mcp_kiroclops_evaluate_candidate")
        self.rows[-1]["params"]["update"]["rawOutput"] = {"response": "candidate-01 queued"}
        self.assertEqual(self.audit()["status"], "unverified")

    def test_changed_inputs_or_wrong_acp_session_rejected(self):
        self.rows[1]["params"]["update"]["rawInput"]["newStr"] = "different"
        self.assertEqual(self.audit()["error_code"], "changed_input")
        self.rows[1]["params"]["update"]["rawInput"]["newStr"] = self.input["newStr"]
        self.rows[0]["params"]["sessionId"] = "some-other-session"
        self.assertEqual(self.audit()["error_code"], "session_mismatch")

    def test_raw_trace_parse_failure_cannot_be_silently_skipped(self):
        path = self.session / "kiro/stdout.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
        self.assertEqual(authorship.audit_kiro_authorship(self.session)["status"], "unverified")

    def test_running_launch_is_not_a_finished_authorship_review(self):
        self.launch["status"] = "running"
        self.assertEqual(self.audit()["status"], "unverified")

    def test_write_is_explicit_and_existing_review_is_never_replaced(self):
        with patch("builtins.print"):
            self.assertEqual(main(["--session", str(self.session)]), 0)
        path = self.session / "kiro/authorship.json"
        self.assertFalse(path.exists())
        with patch("builtins.print"):
            self.assertEqual(main(["--session", str(self.session), "--write"]), 0)
            original = path.read_bytes()
            self.assertEqual(main(["--session", str(self.session), "--write"]), 2)
        self.assertEqual(path.read_bytes(), original)

    def test_changed_evidence_between_audit_and_write_is_rejected(self):
        record = self.audit()
        path = self.session / "kiro/tool-audit.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ide_evidence.IDEExportError, "changed"):
            authorship.write_authorship(self.session, record)
        self.assertFalse((self.session / "kiro/authorship.json").exists())

    def test_changed_candidate_bytes_between_audit_and_write_are_rejected(self):
        record = self.audit()
        (self.session / "candidates/candidate-01/project" / PLAYER).write_bytes(b"edited after audit")
        with self.assertRaisesRegex(ide_evidence.IDEExportError, "source bytes changed"):
            authorship.write_authorship(self.session, record)
        self.assertFalse((self.session / "kiro/authorship.json").exists())

    def test_write_completion_during_evaluation_cannot_be_credited(self):
        # The same final bytes are insufficient if their write overlaps the
        # snapshot request. Completion order alone must not manufacture proof.
        self.rows = [self.rows[0], self.rows[2], self.rows[3], self.rows[1], self.rows[4]]
        record = self.audit()
        self.assertEqual(record["status"], "unverified")
        self.assertIn("overlaps", record["reason"])
        self.assertEqual(record["candidate_boundary"]["call_line"], 2)

    def test_new_write_called_while_evaluation_pending_is_not_credited(self):
        self.rows = [self.rows[2], self.rows[3], self.rows[0], self.rows[1], self.rows[4]]
        record = self.audit()
        self.assertEqual(record["status"], "unverified")
        self.assertIn("overlaps", record["reason"])

    def baseline(self):
        self.launch["mode"] = "baseline"
        commands = _baseline_commands(self.session)
        write(self.target / ".kiro/kiroclops-session.json", {"mode": "baseline", "allowed_commands": commands})
        self.rows = self.rows[:2] + [
            event({"sessionUpdate": "tool_call", "toolCallId": "capture-1", "title": "Run command", "kind": "execute",
                   "rawInput": {"command": commands["capture"], "timeout": 900000, "run_in_background": False},
                   "_meta": {"kiro": {"toolOrigin": "default"}}}),
            event({"sessionUpdate": "tool_call_update", "toolCallId": "capture-1", "status": "completed",
                   "rawOutput": {"exitCode": 0, "output": json.dumps({"status": "completed", "candidate_id": "candidate-01"})}})]
        self.hooks = self.hooks[:1] + [{"tool": "execute_pwsh", "allow": True, "paths": []}]

    def test_exact_controlled_baseline_capture_can_bind_candidate_without_inventing_mcp(self):
        self.baseline()
        record = self.audit()
        self.assertEqual(record["status"], "verified", record)
        self.assertEqual(record["candidate_boundary"]["tool_call_id"], "capture-1")

    def test_baseline_background_arbitrary_command_or_nonzero_exit_never_verifies(self):
        self.baseline()
        self.rows[-1]["params"]["update"]["rawOutput"]["exitCode"] = 1
        self.assertEqual(self.audit()["error_code"], "shell_exit")
        self.rows[-1]["params"]["update"]["rawOutput"]["exitCode"] = 0
        self.rows[2]["params"]["update"]["rawInput"]["run_in_background"] = True
        self.assertEqual(self.audit()["error_code"], "unsupported_shell")
        self.rows[2]["params"]["update"]["rawInput"]["run_in_background"] = False
        self.rows[2]["params"]["update"]["rawInput"]["command"] += "; anything_else"
        self.assertEqual(self.audit()["error_code"], "unsupported_shell")

    def test_no_eligible_candidate_finish_is_recorded_as_nonmutating_rejection(self):
        self.baseline()
        command = _baseline_commands(self.session)["finish"]
        rejected = [event({"sessionUpdate": "tool_call", "toolCallId": "rejected-finish", "kind": "execute",
                           "rawInput": {"command": command, "timeout": 900000, "run_in_background": False},
                           "_meta": {"kiro": {"toolOrigin": "default"}}}),
                    event({"sessionUpdate": "tool_call_update", "toolCallId": "rejected-finish", "status": "failed",
                           "rawOutput": {"exitCode": 1, "output": json.dumps({"status": "error", "error": {
                               "code": "no_eligible_candidate", "message": "No candidate passed calibration and gameplay integrity"}})}})]
        # A legitimate rejected finalization does not invalidate subsequent
        # native edits and the next controlled candidate capture.
        self.rows = rejected + self.rows
        self.hooks.insert(0, {"tool": "execute_pwsh", "allow": True, "paths": []})
        record = self.audit()
        self.assertEqual(record["status"], "verified", record)
        rejections = record["normalization"]["controlled_rejections"]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["code"], "no_eligible_candidate")
        self.rows[0]["params"]["update"]["rawInput"]["command"] = _baseline_commands(self.session)["capture"]
        self.assertEqual(self.audit()["error_code"], "shell_exit")

    def test_shell_native_hook_and_acp_permission_are_bound_as_one_command(self):
        self.baseline()
        self.rows.insert(3, permission("capture-1", "run_command"))
        self.hooks.append({"tool": "run_command", "allow": True, "paths": []})
        record = self.audit()
        self.assertEqual(record["status"], "verified", record)
        command = record["normalization"]["controlled_commands"][0]
        self.assertEqual(command["hook_lines"], [2, 3])
        self.assertEqual(command["permission_tool"], "run_command")
        self.hooks.pop()
        self.assertEqual(self.audit()["error_code"], "hook_binding")


if __name__ == "__main__":
    unittest.main()
