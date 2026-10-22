import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import autonomy_evidence_v3 as evidence


class OutboundEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.session = self.root / "sessions/trial"
        self.target = self.session / "target"
        (self.target / ".kiro").mkdir(parents=True)
        (self.session / "kiro").mkdir()
        self.prompt = self.target / ".kiro/task.md"
        self.prompt.write_text("Single prepared task.\n", encoding="utf-8")
        self.config = {"target_dir": str(self.target), "prompt_path": str(self.prompt),
                       "model_launcher": {"id": "max-test"}}
        self.launch = {"model_requested": "gpt-5.6-sol", "effort_requested": "max",
                       "surface": "cli", "transport": "acp", "mode": "tools",
                       "outbound_journal": str(self.session / "kiro/outbound.jsonl"),
                       "stdout": str(self.session / "kiro/stdout.jsonl"),
                       "prompt_path": str(self.prompt),
                       "prompt_sha256": evidence.hashlib.sha256(b"Single prepared task.\n").hexdigest(),
                       "model_launcher": self.config["model_launcher"],
                       "started_at": "2026-09-19T00:00:00+00:00",
                       "finished_at": "2026-09-19T00:01:00+00:00",
                       "acp_preflight": {"session_id": "native-1"}, "prompt_sent": True,
                       "status": "completed", "prompt_result": {"stopReason": "end_turn"}}
        self.messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(self.target), "mcpServers": []}},
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": "native-1", "prompt": [
                {"type": "text", "text": "Single prepared task.\n"}]}}]
        self.inbound = [{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "native-1"}},
                        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}]
        self.addCleanup(patch.stopall)
        patch.object(evidence, "ROOT", self.root).start()
        patch.object(evidence.launcher, "load_model_config", return_value=self.config).start()

    def write(self, *, missing_sent=False):
        rows = []
        for sequence, message in enumerate(self.messages, 1):
            common = {"sequence": sequence, "at": "2026-09-19T00:00:01+00:00", "payload_sha256": evidence._digest(message)}
            rows.append({**common, "phase": "attempt", "message": message})
            rows.append({**common, "phase": "sent"})
        if missing_sent: rows.pop()
        self.rows = rows
        for name, value in (("outbound.jsonl", rows), ("stdout.jsonl", self.inbound)):
            (self.session / "kiro" / name).write_text("".join(json.dumps(x) + "\n" for x in value), encoding="utf-8")
        (self.session / "kiro/launch.json").write_text(json.dumps(self.launch), encoding="utf-8")

    def verify(self):
        return evidence.verify_outbound(self.session)

    def test_one_exact_sent_task_and_native_response_pass(self):
        self.write()
        result = self.verify()
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["prompt_count"], 1)
        self.assertNotIn("Single prepared task", json.dumps(result))

    def test_second_prompt_fails_even_if_identical(self):
        self.messages.append({**self.messages[-1], "id": 4})
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_changed_or_multi_part_prompt_fails(self):
        self.messages[-1]["params"]["prompt"].append({"type": "text", "text": "operator hint"})
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_attempt_without_flush_is_unverified(self):
        self.write(missing_sent=True)
        self.assertEqual(self.verify()["status"], "unverified")

    def test_tampered_payload_digest_fails(self):
        self.write()
        self.rows[-2]["payload_sha256"] = "0" * 64
        (self.session / "kiro/outbound.jsonl").write_text("".join(json.dumps(x) + "\n" for x in self.rows), encoding="utf-8")
        self.assertEqual(self.verify()["status"], "fail")

    def test_native_error_never_inherits_launch_success(self):
        self.inbound[-1] = {"jsonrpc": "2.0", "id": 3, "error": {"code": -1}}
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_missing_or_cancelled_completion_does_not_pass(self):
        self.inbound.pop()
        self.write()
        self.assertEqual(self.verify()["status"], "unverified")
        self.inbound.append({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}})
        self.launch["prompt_result"] = self.inbound[-1]["result"]
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_post_task_control_or_foreign_session_fails(self):
        self.messages.append({"jsonrpc": "2.0", "id": 4, "method": "session/set_mode", "params": {"sessionId": "native-1"}})
        self.write()
        self.assertEqual(self.verify()["status"], "fail")
        self.messages.pop()
        self.messages[-1]["params"]["sessionId"] = "other"
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_permission_reply_needs_native_request(self):
        reply = {"jsonrpc": "2.0", "id": "permission-1", "result": {"outcome": {"outcome": "selected", "optionId": "once"}}}
        self.messages.append(reply)
        self.write()
        self.assertEqual(self.verify()["status"], "fail")
        self.inbound.append({"jsonrpc": "2.0", "id": "permission-1", "method": "session/request_permission", "params": {}})
        self.write()
        self.assertEqual(self.verify()["status"], "pass")

    def test_duplicate_native_response_fails(self):
        self.inbound.append(self.inbound[-1])
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_missing_evidence_stays_unverified_and_escape_fails(self):
        self.assertEqual(self.verify()["status"], "unverified")
        self.write()
        self.launch["outbound_journal"] = str(self.root / "outside.jsonl")
        self.write()
        self.assertEqual(self.verify()["status"], "fail")

    def test_reversed_chronology_and_stale_prompt_hash_fail(self):
        self.launch["finished_at"] = "2026-09-18T23:59:59+00:00"
        self.write()
        self.assertEqual(self.verify()["status"], "fail")
        self.launch["finished_at"] = "2026-09-19T00:01:00+00:00"
        self.launch["prompt_sha256"] = "0" * 64
        self.write()
        self.assertEqual(self.verify()["status"], "fail")


if __name__ == "__main__":
    unittest.main()
