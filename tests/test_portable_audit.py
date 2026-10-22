"""Independent acceptance replay tests; no native processes or manual observations invented."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.storage import write_json, read_json, sha256
from kiroclops.integration import kiro as native
from kiroclops.portable import audit
from kiroclops.portable.session import digest


class NativeAutonomyReplay(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        target = self.root/"target"
        target.mkdir()
        prompt = target/"prepared.md"
        prompt.write_text("One prepared autonomous task", encoding="utf-8")
        self.config = {"agent_name":"kiroclops-portable","prompt_path":str(prompt),"model_launcher":{"revision":"test"}}
        self.config["approval_mode"] = audit.launcher.APPROVAL_MODE
        self.messages = [{"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(target)}},
                         {"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"sid","prompt":[{"type":"text","text":prompt.read_text()}]}}]
        self.inbound = [{"jsonrpc":"2.0","id":1,"result":{"sessionId":"sid"}},
            {"method":"session/update","params":{"update":{"currentModeId":"kiroclops-portable","modelId":"gpt-5.6-sol","effortLevel":"max"}}},
            {"method":"session/update","params":{"sessionId":"sid","update":{"sessionUpdate":"agent_message_chunk","content":{"text":"Public explanation"}}}},
            {"jsonrpc":"2.0","id":2,"result":{"stopReason":"end_turn"}}]
        self.inbound[0]["result"]["configOptions"] = [{"id":"autopilot","currentValue":"on"}]
        guard = native.StreamConfigurationGuard("kiroclops-portable",model="gpt-5.6-sol",effort="max")
        for row in self.inbound:
            guard.observe(row)
        self.launch = {"status":"completed","prompt_sent":True,"model_requested":"gpt-5.6-sol","effort_requested":"max",
            "surface":"cli","transport":"acp","process_returncode_after_cleanup":0,"model_launcher":{"revision":"test"},
            "stream_configuration":guard.summary(),"started_at":"2026-09-21T00:00:00+00:00","finished_at":"2026-09-21T00:00:10+00:00",
            "elapsed_seconds":10,"prompt_sha256":sha256(prompt),"acp_preflight":{"session_id":"sid","workspace_verified":str(target)},
            "prompt_result":{"stopReason":"end_turn"}}
        self.launch.update(approval_mode=audit.launcher.APPROVAL_MODE, argv=audit.launcher.acp_argv("kiro-cli.exe"))
        self.launch["acp_preflight"].update(approval_mode=audit.launcher.APPROVAL_MODE, argv=self.launch["argv"], autopilot="on")
        self.outbound = []
        for i,message in enumerate(self.messages,1):
            common={"sequence":i,"payload_sha256":audit._canonical(message),"at":f"2026-09-21T00:00:0{i}+00:00"}
            self.outbound.extend([{**common,"phase":"attempt","message":message},{**common,"phase":"sent"}])

    def verify(self):
        return audit.verify_native(self.root,self.launch,self.config,self.inbound,self.outbound)

    def test_exact_single_prepared_task_and_native_selection_replay(self):
        self.assertEqual(self.verify()["status"],"pass")

    def test_unreceipted_task_cannot_pass(self):
        self.outbound.pop()
        with self.assertRaises(ValueError):
            self.verify()

    def test_unsupported_v3_flag_cannot_claim_native_session_consent(self):
        self.launch["argv"] = self.launch["argv"] + ["--trust-all-tools"]
        with self.assertRaises(ValueError):
            self.verify()

    def test_native_autopilot_off_cannot_pass(self):
        self.inbound[0]["result"]["configOptions"][0]["currentValue"] = "off"
        with self.assertRaises(ValueError):
            self.verify()

    def add_permission(self, option="always", scope="session"):
        self.inbound.insert(-1, {"jsonrpc":"2.0", "id":"permission-1", "method":"session/request_permission",
            "params":{"options":[{"optionId":"once","kind":"allow_once"},{"optionId":"always","kind":"allow_always"}]}})
        guard = native.StreamConfigurationGuard("kiroclops-portable",model="gpt-5.6-sol",effort="max")
        for row in self.inbound:
            guard.observe(row)
        self.launch["stream_configuration"] = guard.summary()
        message = {"jsonrpc":"2.0", "id":"permission-1", "result":{"outcome":{"outcome":"selected","optionId":option},
            "_meta":{"kiro":{"consent":{"scope":scope}}}}}
        common = {"sequence":3,"payload_sha256":audit._canonical(message),"at":"2026-09-21T00:00:03+00:00"}
        self.outbound.extend([{**common,"phase":"attempt","message":message},{**common,"phase":"sent"}])

    def test_real_native_always_allow_response_is_bound_to_session_consent(self):
        self.add_permission()
        self.assertEqual(self.verify()["status"], "pass")

    def test_allow_once_cannot_masquerade_as_available_always_allow(self):
        self.add_permission(option="once")
        with self.assertRaises(ValueError):
            self.verify()

    def test_global_consent_cannot_masquerade_as_session_consent(self):
        self.add_permission(scope="global")
        with self.assertRaises(ValueError):
            self.verify()

    def test_extra_followup_task_cannot_pass(self):
        message=deepcopy(self.messages[-1]); message["id"]=3
        common={"sequence":3,"payload_sha256":audit._canonical(message),"at":"2026-09-21T00:00:03+00:00"}
        self.outbound.extend([{**common,"phase":"attempt","message":message},{**common,"phase":"sent"}])
        with self.assertRaises(ValueError):
            self.verify()

    def test_silent_effort_change_after_generation_is_rejected(self):
        self.inbound.insert(-1,{"method":"session/update","params":{"update":{"effortLevel":"high"}}})
        with self.assertRaises(native.KiroError):
            self.verify()

    def test_changed_prepared_prompt_cannot_pass(self):
        Path(self.config["prompt_path"]).write_text("operator follow-up",encoding="utf-8")
        with self.assertRaises(ValueError):
            self.verify()

    def test_missing_native_end_turn_cannot_pass(self):
        self.inbound.pop()
        with self.assertRaises(ValueError):
            self.verify()


class NativeAuthorshipReplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.baseline=self.root/"baseline"; self.baseline.mkdir()
        (self.root/"target").mkdir()
        (self.baseline/"player.gd").write_bytes(b"before\n")
        before={"player.gd":sha256(self.baseline/"player.gd")}
        import hashlib
        after={"player.gd":hashlib.sha256(b"after\n").hexdigest()}
        self.candidate={"candidate_id":"candidate-01","source_hashes":after,"source_sha256":digest(after)}
        self.state={"session_id":"heart-test-jump","baseline":str(self.baseline),"baseline_hashes":before,"candidates":[self.candidate]}
        write_json(self.root/"kiro/launch.json",{"acp_preflight":{"session_id":"sid"}})
        write_json(self.root/"jobs/job-one.json",{"session_id":"heart-test-jump","action":"evaluate","candidate_id":"candidate-01","candidate_source_sha256":digest(after)})
        def row(update):
            return {"method":"session/update","params":{"sessionId":"sid","update":update}}
        args={"path":str(self.root/"target/player.gd"),"oldStr":"before","newStr":"after"}
        self.inbound=[row({"sessionUpdate":"tool_call","toolCallId":"edit","kind":"edit","rawInput":args,
                       "_meta":{"kiro":{"toolOrigin":"default"}}}),
            row({"sessionUpdate":"tool_call_update","toolCallId":"edit","status":"completed","rawInput":args,
                 "_meta":{"kiro":{"preview":{"originalContent":"before\n","modifiedContent":"after\n"}}}}),
            row({"sessionUpdate":"tool_call","toolCallId":"evaluate","kind":"other","title":"@kiroclops/evaluate_candidate","rawInput":{},
                 "_meta":{"kiro":{"serverName":"kiroclops"}}}),
            row({"sessionUpdate":"tool_call_update","toolCallId":"evaluate","status":"completed","rawInput":{},
                 "rawOutput":{"response":"{\"job_id\":\"job-one\",\"candidate_id\":\"candidate-01\"}"},"_meta":{"kiro":{"serverName":"kiroclops"}}})]
        self.hooks=[{"allow":True,"tool":"str_replace","paths":[str(self.root/"target/player.gd")]}]

    def verify(self):
        return audit.verify_authorship(self.root,self.state,self.candidate,self.inbound,self.hooks)

    def test_full_native_replacement_matches_real_evaluation_snapshot(self):
        self.assertEqual(self.verify()["status"],"pass")

    def test_operator_source_not_in_native_edit_is_rejected(self):
        self.candidate["source_hashes"]={"player.gd":"0"*64}
        with self.assertRaises(ValueError):
            self.verify()

    def test_misleading_native_preview_is_rejected(self):
        self.inbound[1]["params"]["update"]["_meta"]["kiro"]["preview"]["modifiedContent"]="different\n"
        with self.assertRaises(ValueError):
            self.verify()

    def test_native_write_without_hook_is_rejected(self):
        with self.assertRaises(ValueError):
            audit.verify_authorship(self.root,self.state,self.candidate,self.inbound,[])

    def test_other_job_result_cannot_claim_this_candidate(self):
        path=self.root/"jobs/job-one.json"
        job=read_json(path); job["session_id"]="heart-other"
        write_json(path,job)
        with self.assertRaises(ValueError):
            self.verify()


class IndependentReviewReceipt(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_missing_review_is_pending(self):
        self.assertEqual(audit.verify_review(self.root)["status"],"pending")

    def test_unbound_pass_flag_never_grants_acceptance(self):
        write_json(self.root/"final/independent-review.json",{"status":"pass"})
        self.assertEqual(audit.verify_review(self.root)["status"],"fail")

    def test_failed_audit_is_immutable(self):
        first=audit.audit_session(self.root,self.root/"missing-observations.json",self.root/"missing-source-review.json")
        self.assertEqual(first["status"],"fail")
        with self.assertRaises(ValueError):
            audit.audit_session(self.root,"another.json","another-source.json")

    def test_changed_evidence_fails_before_replay(self):
        evidence=self.root/"evidence.txt"; evidence.write_text("before",encoding="utf-8")
        write_json(self.root/"final/independent-review.json",{"status":"pass","audit_version":audit.VERSION,
                   "evidence_files":{str(evidence):sha256(evidence)}})
        evidence.write_text("after",encoding="utf-8")
        with patch.object(audit,"_compute",side_effect=AssertionError("Should not replay changed inputs")):
            result=audit.verify_review(self.root)
        self.assertEqual(result["status"],"fail")
        self.assertIn("changed",result["reason"])


if __name__ == "__main__":
    unittest.main()
