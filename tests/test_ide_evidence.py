import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zipfile

from kiroclops.integration import ide_evidence as audit
from kiroclops.integration.storage import project_hashes
from scripts.audit_ide_export import main


PLAYER = "source/characters/player/player.gd"
HELPER = "source/characters/player/new_helper.gd"
STAMP = "2026-09-18T12:00:00+00:00"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def message(payload, index=1):
    return {"id": f"message-{index}", "timestamp": STAMP, "payload": payload}


def call(tid, tool, args, *, action=None, **extra):
    return message({"type": "tool_call", "toolCallId": tid, "toolName": action or tool,
                    "actionType": action or tool, "args": args, "status": "completed", **extra})


def result(tid, content="done", success=True):
    return message({"type": "tool_result", "toolCallId": tid, "content": content, "success": success})


class IDEEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = self.root / "sessions/ide-01"
        self.target = self.session / "target"
        self.target.mkdir(parents=True)
        self.patch_root = patch.object(audit, "ROOT", self.root)
        self.patch_root.start()

    def tearDown(self):
        self.patch_root.stop()
        self.temporary.cleanup()

    def metadata(self):
        return {"id": "native-session", "workspacePaths": [str(self.target)], "agentMode": "kiroclops-tools",
                "modelId": "claude-opus-5", "effortLevel": "high", "createdAt": STAMP, "lastModifiedAt": STAMP}

    def archive(self, messages=None, metadata=None, extras=None, name="export.zip"):
        path = self.session / "ide" / name
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("native-session/session.json", json.dumps(metadata or self.metadata()))
            archive.writestr("native-session/messages.jsonl", "\n".join(json.dumps(value) for value in messages or []))
            for key, value in (extras or {}).items():
                archive.writestr(key, value)
        return path

    def candidate(self, *, final=None, baseline=None):
        baseline = baseline or {PLAYER: b"extends Node\r\nvar speed = 1\r\n"}
        final = final or {PLAYER: b"extends Node\r\nvar speed = 2\n", HELPER: b"extends RefCounted\n"}
        hashes = []
        for directory, files in ((self.session / "baseline", baseline),
                                 (self.session / "candidates/candidate-01/project", final)):
            directory.mkdir(parents=True)
            (directory / "project.godot").write_bytes(b"[application]\n")
            for name, data in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            hashes.append(project_hashes(directory))
        record = {"candidate_id": "candidate-01", "source_hashes": hashes[1]}
        write_json(self.session / "session.json", {"session_id": self.session.name,
                   "baseline_hashes": hashes[0], "selected_candidate_id": "candidate-01", "candidates": [record]})
        write_json(self.session / "candidates/candidate-01/candidate.json", record)
        (self.session / "candidates/candidate-01/patch.diff").write_bytes(b"selected source diff\n")
        return hashes

    def replay_messages(self):
        return [
            call("replace", "replace", {"path": PLAYER, "oldStr": "var speed = 1\n", "newStr": "var speed = 2\n"}, kind="edit"),
            result("replace"),
            call("helper", "fsWrite", {"path": HELPER, "text": "extends RefCounted\n"}, kind="edit"),
            result("helper"),
            call("evaluate", "@kiroclops/evaluate_candidate", {}),
            result("evaluate", json.dumps({"structuredContent": {"candidate_id": "candidate-01", "job_id": "job-1"}})),
        ]

    def test_export_is_preserved_and_final_metadata_is_not_historical_proof(self):
        messages = [message({"type": "assistant", "content": "claude-opus-5 high", "operationType": "Say"}),
                    message({"type": "assistant", "content": "reasoning", "operationType": "Reasoning",
                             "reasoningModelId": "claude-opus-5"})]
        path = self.archive(messages)
        original = path.read_bytes()
        report = audit.audit_ide_export(self.session, path)
        self.assertEqual(report["configuration"]["status"], "pass")
        self.assertEqual(report["model"]["status"], "limited")
        self.assertEqual(report["model"]["historical_effort_status"], "unverified")
        self.assertFalse(report["strict_configuration_verified"])
        self.assertEqual(report["status"], "limited")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(report["export"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertFalse((path.parent / "native-session").exists())

    def test_model_claim_in_prose_does_not_count_and_wrong_reasoning_model_fails(self):
        messages = [message({"type": "assistant", "content": "modelId claude-opus-5 high", "operationType": "Say"})]
        self.assertEqual(audit.audit_ide_export(self.session, self.archive(messages))["model"]["status"], "unverified")
        messages.append(message({"type": "assistant", "content": "", "operationType": "Reasoning", "reasoningModelId": "auto"}))
        report = audit.audit_ide_export(self.session, self.archive(messages))
        self.assertEqual(report["model"]["status"], "mismatch")
        self.assertEqual(report["status"], "failed")

    def test_native_qdev_model_prefix_is_preserved_and_only_exact_namespace_is_normalized(self):
        for raw, status in (("qdev::claude-opus-5", "limited"), ("other::claude-opus-5", "mismatch"),
                            ("qdev::auto", "mismatch"), ("qdev::qdev::claude-opus-5", "mismatch")):
            messages = [message({"type": "assistant", "content": "", "operationType": "Reasoning", "reasoningModelId": raw})]
            with self.subTest(raw=raw):
                report = audit.audit_ide_export(self.session, self.archive(messages))
                self.assertEqual(report["model"]["status"], status)
                self.assertEqual(report["model"]["raw_reasoning_models"], [raw])
                self.assertEqual(report["model"]["historical_effort_status"], "unverified")

    def test_only_complete_native_cloud_preload_is_excluded_from_experiment(self):
        preload = [call("cloud", "fetch_cloud_config", {"toolName": "fetch_cloud_config"},
                        _meta={"kiro": {"toolId": "fetch_cloud_config"}}), result("cloud", "{}")]
        user = message({"type": "user", "content": "exact prompt"})
        report = audit.audit_ide_export(self.session, self.archive(preload + [user]))
        self.assertEqual(report["timing"]["prelude"], {"status": "known_cloud_config", "message_count": 2,
                         "call_count": 1, "excluded_from_experiment": True})
        self.assertEqual(report["timing"]["user_messages"][0]["content_sha256"], hashlib.sha256(b"exact prompt").hexdigest())
        for prefix in (preload[:1], [preload[0], result("cloud", success=False)],
                       [call("cloud", "read_file", {"path": PLAYER}), result("cloud")],
                       [message({"type": "assistant", "content": "earlier generation"})]):
            with self.subTest(prefix=prefix):
                report = audit.audit_ide_export(self.session, self.archive(prefix + [user]))
                self.assertEqual(report["timing"]["prelude"]["status"], "unverified")
                self.assertFalse(report["timing"]["prelude"]["excluded_from_experiment"])

    def test_postlude_requires_exact_native_descriptor_and_cannot_hide_actions(self):
        end = message({"type": "turn_end", "stopReason": "end_turn"})
        descriptor = message({"type": "session_start", "agentType": "kiroclops-tools", "content": "instructions",
                              "forcedRole": True, "messageId": "01234567-89ab-cdef-0123-456789abcdef"})
        descriptor["id"] = "session_start_native-session_kiroclops-tools"
        report = audit.audit_ide_export(self.session, self.archive([end, descriptor]))
        self.assertEqual(report["timing"]["postlude"]["status"], "native_session_descriptor")
        variants = [dict(descriptor, id="wrong-session"),
                    dict(descriptor, payload=dict(descriptor["payload"], forcedRole=False)),
                    dict(descriptor, payload=dict(descriptor["payload"], action="execute")),
                    dict(descriptor, payload=dict(descriptor["payload"], agentType="vibe"))]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                report = audit.audit_ide_export(self.session, self.archive([end, invalid]))
                self.assertEqual(report["timing"]["postlude"]["status"], "unverified")
                self.assertFalse(report["timing"]["postlude"]["excluded_from_experiment"])

    def test_wrong_workspace_agent_model_or_effort_fails_and_missing_stays_unverified(self):
        for key, value in (("workspacePaths", [str(self.root)]), ("agentMode", "vibe"), ("modelId", "auto"), ("effortLevel", "low")):
            metadata = self.metadata()
            metadata[key] = value
            with self.subTest(key=key):
                report = audit.audit_ide_export(self.session, self.archive(metadata=metadata))
                self.assertEqual(report["configuration"]["checks"][key]["status"], "fail")
                self.assertEqual(report["status"], "failed")
        metadata = self.metadata()
        del metadata["effortLevel"]
        report = audit.audit_ide_export(self.session, self.archive(metadata=metadata))
        self.assertEqual(report["configuration"]["status"], "unverified")

    def test_structured_tools_link_results_and_missing_or_orphan_is_visible(self):
        messages = [call("read", "read_file", {"path": PLAYER}), result("read"), call("pending", "read_file", {"path": HELPER})]
        report = audit.audit_ide_export(self.session, self.archive(messages))
        self.assertEqual(report["tools"]["status"], "incomplete")
        self.assertEqual(report["tools"]["missing_results"], 1)
        self.assertEqual(report["tools"]["successful_calls"], 1)
        messages.append(result("orphan"))
        report = audit.audit_ide_export(self.session, self.archive(messages))
        self.assertEqual(report["tools"]["status"], "invalid")
        self.assertEqual(report["status"], "failed")

    def test_conflicting_tool_identity_is_rejected(self):
        messages = [call("same", "fsWrite", {"path": PLAYER, "text": "a"}), call("same", "fsWrite", {"path": PLAYER, "text": "b"})]
        with self.assertRaisesRegex(audit.IDEExportError, "identity or arguments"):
            audit.audit_ide_export(self.session, self.archive(messages))

    def test_traversal_absolute_windows_symlink_and_duplicate_members_are_rejected(self):
        for name in ("../escape", "/absolute", "C:/absolute", "native-session/../escape", "native-session\\bad"):
            with self.subTest(name=name), self.assertRaises(audit.IDEExportError):
                audit.audit_ide_export(self.session, self.archive(extras={name: "bad"}))
        link = zipfile.ZipInfo("native-session/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(audit.IDEExportError, "links"):
            audit.audit_ide_export(self.session, self.archive(extras={link: "elsewhere"}))
        with self.assertRaisesRegex(audit.IDEExportError, "duplicate"):
            audit.audit_ide_export(self.session, self.archive(extras={"NATIVE-SESSION/SESSION.JSON": "{}"}))
        self.assertFalse((self.session / "escape").exists())

    def test_multiple_sessions_malformed_json_and_archive_limits_are_rejected(self):
        with self.assertRaises(audit.IDEExportError):
            audit.audit_ide_export(self.session, self.archive(extras={"another/session.json": "{}"}))
        path = self.archive(extras={"native-session/sub-executions/sub.jsonl": '{"id":"x","id":"y"}'})
        with self.assertRaises(audit.IDEExportError):
            audit.audit_ide_export(self.session, path)
        with patch.object(audit, "MAX_MEMBER_BYTES", 10), self.assertRaisesRegex(audit.IDEExportError, "size limit"):
            audit.audit_ide_export(self.session, self.archive())

    def test_export_path_outside_selected_session_is_rejected(self):
        path = self.archive()
        outside = self.root / "outside.zip"
        outside.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(audit.IDEExportError, "inside"):
            audit.audit_ide_export(self.session, outside)

    def test_crlf_replacement_and_helper_write_reproduce_candidate_at_evaluation(self):
        hashes = self.candidate()
        messages = self.replay_messages()
        # The selected candidate may precede a later edit. Its evaluation boundary matters.
        messages += [call("later", "fsWrite", {"path": PLAYER, "text": "later abandoned edit"}, kind="edit"), result("later")]
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        authorship = report["authorship"]
        self.assertEqual(authorship["status"], "verified", authorship)
        self.assertEqual(authorship["source_hashes"], hashes[1])
        self.assertEqual(len(authorship["replay_steps"]), 2)
        self.assertEqual(authorship["candidate_boundary"]["tool_call_id"], "evaluate")
        self.assertEqual(report["status"], "limited")

    def native_editor_messages(self):
        def uri(checkpoint):
            return f"kiro-snapshot-v2://native-session:{checkpoint}/?" + quote("originalPath=" + quote(PLAYER, safe=""), safe="")
        messages = []
        for index, old, new in ((1, "1", "2"), (2, "2", "3")):
            item = call(f"editor-{index}", "str_replace", {"path": str(self.target / PLAYER),
                        "oldStr": f"var speed = {old}\n", "newStr": f"var speed = {new}\n", "replace_all": False}, kind="edit",
                        _meta={"kiro": {"toolOrigin": "default"}})
            item["payload"]["actionType"] = "replace"
            response = result(f"editor-{index}", "Replaced text")
            response["payload"]["_meta"] = {"kiro": {"checkpoint": {
                "original": uri(f"{index:08x}"), "modified": uri(f"{index + 1:08x}"), "local": (self.target / PLAYER).as_uri()}}}
            messages.extend([item, response])
        return messages + [call("evaluate", "@kiroclops/evaluate_candidate", {}),
                           result("evaluate", json.dumps({"candidate_id": "candidate-01", "job_id": "job-1"}))]

    def test_native_editor_replay_preserves_crlf_and_binds_exact_snapshot_chain(self):
        self.candidate(baseline={PLAYER: b"extends Node\r\nvar speed = 1\r\n"},
                       final={PLAYER: b"extends Node\r\nvar speed = 3\r\n"})
        report = audit.audit_ide_export(self.session, self.archive(self.native_editor_messages()), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "verified", report["authorship"].get("reason"))
        self.assertEqual(len(report["authorship"]["replay_steps"]), 2)
        self.assertEqual(report["authorship"]["replay_steps"][-1]["sha256_after"], hashlib.sha256(b"extends Node\r\nvar speed = 3\r\n").hexdigest())

    def test_native_editor_wrong_checkpoint_session_path_or_chain_cannot_verify(self):
        self.candidate(final={PLAYER: b"extends Node\r\nvar speed = 3\r\n"})
        for field, replacement in (("original", "different-session"), ("local", "wrong-file"), ("original", "broken-chain")):
            messages = self.native_editor_messages()
            checkpoint = messages[3]["payload"]["_meta"]["kiro"]["checkpoint"]
            if replacement == "different-session": checkpoint[field] = checkpoint[field].replace("native-session", "another-session")
            elif replacement == "wrong-file": checkpoint[field] = (self.target / HELPER).as_uri()
            else: checkpoint[field] = checkpoint[field].replace(":00000002/", ":00000009/")
            with self.subTest(replacement=replacement):
                report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
                self.assertEqual(report["authorship"]["status"], "failed")

    def test_native_editor_never_normalizes_final_hash_or_guesses_mixed_document_eol(self):
        self.candidate(final={PLAYER: b"extends Node\r\nvar speed = 3\n"})
        report = audit.audit_ide_export(self.session, self.archive(self.native_editor_messages()), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "failed")
        self.assertEqual(report["authorship"]["mismatched_files"], [PLAYER])
        messages = self.native_editor_messages()
        with self.assertRaisesRegex(audit.IDEExportError, "unambiguous"):
            audit._native_editor_replayer("native-session")({PLAYER: b"extends Node\r\nvar speed = 1\n"},
                self.target, messages[0]["payload"], messages[1]["payload"])

    def test_missing_write_content_or_candidate_boundary_never_verifies(self):
        self.candidate()
        messages = self.replay_messages()
        del messages[2]["payload"]["args"]["text"]
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "unverified")
        messages = self.replay_messages()[:-2]
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "unverified")
        messages = self.replay_messages()
        messages[-1]["payload"]["content"] = "candidate-01 completed successfully"
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "unverified")

    def test_failed_partial_or_ambiguous_writes_and_shell_actions_do_not_verify(self):
        self.candidate()
        cases = []
        messages = self.replay_messages()
        messages[1]["payload"]["success"] = False
        cases.append(messages)
        messages = self.replay_messages()
        messages[0]["payload"]["args"]["oldStr"] = "not present"
        cases.append(messages)
        cases.append([call("shell", "execute_pwsh", {"command": "anything"}, kind="execute"), result("shell")] + self.replay_messages())
        for messages in cases:
            report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
            self.assertEqual(report["authorship"]["status"], "unverified")

    def test_replay_or_snapshot_tampering_fails(self):
        self.candidate()
        messages = self.replay_messages()
        messages[2]["payload"]["args"]["text"] = "wrong helper bytes"
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "failed")
        self.assertEqual(report["authorship"]["mismatched_files"], [HELPER])
        (self.session / "baseline" / PLAYER).write_bytes(b"tampered")
        report = audit.audit_ide_export(self.session, self.archive(self.replay_messages()), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "failed")

    def test_preview_mismatch_and_outside_write_fail(self):
        self.candidate()
        messages = self.replay_messages()
        messages[0]["payload"]["_meta"] = {"kiro": {"preview": {"originalContent": "wrong before"}}}
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "failed")
        messages = self.replay_messages()
        messages[0]["payload"]["args"]["path"] = "../outside.gd"
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "failed")

    def test_subexecution_model_is_checked_and_source_order_not_invented(self):
        self.candidate()
        extra = [message({"type": "assistant", "content": "", "operationType": "Reasoning", "reasoningModelId": "other-model"})]
        path = self.archive(self.replay_messages(), extras={"native-session/sub-executions/sub.jsonl": "\n".join(json.dumps(item) for item in extra)})
        report = audit.audit_ide_export(self.session, path, candidate_id="candidate-01")
        self.assertEqual(report["model"]["status"], "mismatch")
        self.assertNotEqual(report["authorship"]["status"], "verified")
        extra = [call("sub-write", "fsWrite", {"path": PLAYER, "text": "anything"}, kind="edit"), result("sub-write")]
        path = self.archive(self.replay_messages(), extras={"native-session/sub-executions/sub.jsonl": "\n".join(json.dumps(item) for item in extra)})
        report = audit.audit_ide_export(self.session, path, candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "unverified")

    def test_script_writes_only_new_audit_json_and_refuses_overwrite(self):
        path = self.archive()
        original = path.read_bytes()
        with patch("builtins.print"):
            self.assertEqual(main(["--session", str(self.session), "--export", str(path)]), 0)
            output = self.session / "ide/evidence.json"
            evidence = output.read_bytes()
            self.assertEqual(main(["--session", str(self.session), "--export", str(path)]), 2)
            self.assertEqual(main(["--session", str(self.session), "--export", str(path), "--output", "../outside.json"]), 2)
        self.assertEqual(output.read_bytes(), evidence)
        self.assertEqual(path.read_bytes(), original)

    def test_native_power_actions_and_evaluation_are_visible_and_bound(self):
        self.candidate()
        messages = [call("activate", "kiro_powers", {"action": "activate", "powerName": "kiroclops"}), result("activate"),
                    call("skill", "kiro_powers", {"action": "readSkill", "powerName": "kiroclops", "skillName": "motion-calibration"}), result("skill")]
        messages += self.replay_messages()
        messages[-2] = call("evaluate", "kiro_powers", {"action": "use", "powerName": "kiroclops",
                                "serverName": "kiroclops", "toolName": "evaluate_candidate", "arguments": {}})
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "verified", report["authorship"])
        powers = [row["power"] for row in report["tools"]["calls"] if "power" in row]
        self.assertEqual([row["action"] for row in powers], ["activate", "readSkill", "use"])
        messages[-2]["payload"]["args"]["powerName"] = "other-power"
        report = audit.audit_ide_export(self.session, self.archive(messages), candidate_id="candidate-01")
        self.assertEqual(report["authorship"]["status"], "unverified")

    def test_power_outer_success_preserves_structured_logical_error(self):
        messages = [call("finalize", "kiro_powers", {"action": "use", "powerName": "kiroclops",
                    "serverName": "kiroclops", "toolName": "finalize_candidate"}),
                    result("finalize", json.dumps({"structuredContent": {"status": "error", "error": {"code": "active_job"}}}), success=True)]
        report = audit.audit_ide_export(self.session, self.archive(messages))
        native = report["tools"]["calls"][0]
        self.assertTrue(native["success"])  # Transport succeeded, the application request did not.
        self.assertEqual(native["structured_result"], {"status": "error", "error_codes": ["active_job"]})

    def test_structured_queued_job_requires_consistent_safe_identity_and_no_error(self):
        accepted = {"status": "queued", "job_id": "job-012345abcdef", "candidate_id": "candidate-01"}
        wrappers = [accepted, {"structuredContent": accepted}, {"response": json.dumps(accepted)},
                    {"content": [{"type": "text", "text": json.dumps(accepted)}]},
                    {"structuredContent": accepted, "content": [{"type": "text", "text": json.dumps(accepted)}]}]
        for value in wrappers:
            with self.subTest(value=value):
                parsed = audit._structured_job_result(json.dumps(value))
                self.assertEqual(parsed, {"status": "accepted", "job_id": accepted["job_id"], "candidate_id": "candidate-01"})
        for value in ({"structuredContent": accepted, "isError": True},
                      {"structuredContent": accepted, "result": {"status": "error", "error": {"code": "denied"}}}):
            self.assertEqual(audit._structured_job_result(json.dumps(value))["status"], "error")
        conflicting = {"structuredContent": accepted, "result": dict(accepted, job_id="job-other")}
        self.assertEqual(audit._structured_job_result(json.dumps(conflicting))["status"], "ambiguous")
        self.assertEqual(audit._structured_job_result(json.dumps(dict(accepted, job_id="../outside")))["status"], "ambiguous")
        self.assertEqual(audit._structured_job_result('job-012345abcdef candidate-01 queued')["status"], "unverified")
        self.assertEqual(audit._structured_job_result('{"status": {}, "data": "not JSON"}')["status"], "unverified")

    def test_timing_uses_native_turn_end_not_export_metadata_or_review_time(self):
        messages = [message({"type": "turn_start"}), message({"type": "turn_end", "stopReason": "end_turn"})]
        messages[1]["timestamp"] = "2026-09-18T12:05:00+00:00"
        metadata = self.metadata()
        metadata["lastModifiedAt"] = "2026-09-18T13:00:00+00:00"
        report = audit.audit_ide_export(self.session, self.archive(messages, metadata))
        self.assertEqual(report["timing"]["last_turn_end_at"], messages[1]["timestamp"])
        self.assertFalse(report["timing"]["later_turn_start_present"])
        messages.append(message({"type": "turn_start"}))
        report = audit.audit_ide_export(self.session, self.archive(messages, metadata))
        self.assertTrue(report["timing"]["later_turn_start_present"])


if __name__ == "__main__":
    unittest.main()
