"""Startup recovery must never create a second model attempt or hide a failure."""
import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.kiro import KiroError
from kiroclops.local_engine.mcp_tools import ACTIVE_MCP_TOOLS as MCP_TOOLS
from kiroclops.local_engine.startup import connect_native, probe_stdio
from tests.evidence import requires_evidence


class OutputCompatibilityTests(unittest.TestCase):
    @requires_evidence('artifacts/phase3b/startup-transport-proof-01')
    def test_startup_policy_is_bound_to_exact_reconstruction_proof(self):
        import hashlib
        from kiroclops.local_engine import review
        policy=review._NATIVE_OUTPUT_POLICY_STARTUP
        root=Path(__file__).resolve().parents[1]
        path=root/'artifacts/phase3b/startup-transport-proof-01/native-transport-proof.json'
        proof=json.loads(path.read_text('utf8'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),policy['transport_compatibility_proof_sha256'])
        self.assertEqual(proof['current_local_launcher']['sha256'],policy['launcher_sha256'])
        self.assertEqual(proof['previous_cloud_launcher']['sha256'],review._NATIVE_OUTPUT_POLICY['launcher_sha256'])
        self.assertEqual(proof['status'],'pass')
        self.assertFalse(proof['live_model_call'])

    def test_new_policy_still_requires_each_hook_decision(self):
        from copy import deepcopy
        from tests.test_local_native_described_pwsh import call, audit, CONTEXT
        from kiroclops.local_engine import review
        context={**CONTEXT, **{k:review._NATIVE_OUTPUT_POLICY_STARTUP[k]
                    for k in ('cli_version','kas_version','launcher_sha256')}}
        self.assertEqual(audit(call(),context=context)['status'],'pass')
        self.assertEqual(audit(call(),rows=[],context=context)['status'],'fail')
        unknown=deepcopy(context);unknown['launcher_sha256']='0'*64
        self.assertEqual(audit(call(),context=unknown)['status'],'fail')


class StdioTests(unittest.TestCase):
    def run_probe(self, replies, *, expected_code=None):
        with tempfile.TemporaryDirectory() as temp:
            process=SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(
                ''.join(json.dumps(r)+'\n' for r in replies)),
                wait=lambda timeout:0, poll=lambda:0)
            output=Path(temp)/'probe'
            with patch('kiroclops.local_engine.startup.subprocess.Popen', return_value=process):
                if expected_code:
                    with self.assertRaises(KiroError) as caught:
                        probe_stdio({'command':'python','args':[]},output,temp,timeout=1)
                    self.assertEqual(caught.exception.code,expected_code)
                else:
                    probe_stdio({'command':'python','args':[]},output,temp,timeout=1)
            saved=json.loads((output/'result.json').read_text())
            self.assertTrue(saved['process_exit_verified'])
            self.assertFalse(saved['prompt_sent'])
            self.assertFalse(saved['trial_tools_called'])
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            return saved

    def test_registry_verified_without_trial_execution(self):
        result=self.run_probe([{'id':1,'result':{}},
            {'id':2,'result':{'tools':[{'name':t} for t in MCP_TOOLS]}}])
        self.assertEqual(result['status'],'pass')

    def test_incomplete_registry_is_rejected_and_owned_process_closed(self):
        self.run_probe([{'id':1,'result':{}},{'id':2,'result':{'tools':[]}}],
                       expected_code='unexpected_tools')

    def test_early_eof_is_explicit_startup_failure(self):
        self.run_probe([],expected_code='mcp_startup_failed')

    def test_protocol_error_is_explicit_startup_failure(self):
        self.run_probe([{'id':1,'error':{'code':-32600}}],expected_code='mcp_startup_failed')


class StartupTests(unittest.TestCase):
    def run_case(self, outcomes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / 'run'
        clients = []
        def factory(ready, log, timeout, cancel):
            log.mkdir(parents=True)
            (log / 'stdout.jsonl').write_text('original-journal-'+str(len(clients)), encoding='utf8')
            outcome = outcomes[len(clients)]
            class Client:
                prompt_sent = outcome.get('prompted', False)
                guard = SimpleNamespace(generation_started=outcome.get('generated', False))
                closed = False
                def handshake(self):
                    if 'error' in outcome:
                        raise KiroError(outcome['error'], 'test failure')
                    return {'verified':True}
                def close(self):
                    self.closed=True
                    return outcome.get('cleanup_errors', [])
            client=Client(); clients.append(client)
            return client
        return root, factory, clients

    def test_transient_failure_preserved_before_one_successful_connection(self):
        root,factory,clients=self.run_case([{'error':'mcp_startup_failed'}, {}])
        client,ready,attempts=connect_native({},root,factory)
        self.assertEqual(len(clients),2)
        self.assertTrue(clients[0].closed)
        self.assertFalse(client.closed)
        self.assertEqual((root.parent/'run-startup-failed-01/stdout.jsonl').read_text(),'original-journal-0')
        self.assertEqual((root/'stdout.jsonl').read_text(),'original-journal-1')
        self.assertEqual([a['status'] for a in attempts],['fail','pass'])
        self.assertTrue(ready['verified'])

    def test_two_failed_connections_stop_and_keep_both_journals(self):
        root,factory,clients=self.run_case([{'error':'timeout'}, {'error':'timeout'}])
        with self.assertRaises(KiroError):connect_native({},root,factory)
        self.assertEqual(len(clients),2)
        self.assertTrue(all(c.closed for c in clients))
        self.assertTrue((root.parent/'run-startup-failed-02/stdout.jsonl').exists())

    def test_wrong_model_is_never_retried(self):
        root,factory,clients=self.run_case([{'error':'model_changed'}, {}])
        with self.assertRaises(KiroError):connect_native({},root,factory)
        self.assertEqual(len(clients),1)

    def test_after_prompt_transport_failure_is_never_retried(self):
        root,factory,clients=self.run_case([{'error':'timeout','prompted':True}, {}])
        with self.assertRaises(KiroError):connect_native({},root,factory)
        self.assertEqual(len(clients),1)

    def test_premature_generation_is_rejected(self):
        root,factory,clients=self.run_case([{'generated':True}, {}])
        with self.assertRaisesRegex(KiroError,'Startup must not generate'):connect_native({},root,factory)
        self.assertEqual(len(clients),1)

    def test_unconfirmed_cleanup_stops_recovery(self):
        root,factory,clients=self.run_case([{'error':'timeout','cleanup_errors':['still alive']}, {}])
        with self.assertRaises(KiroError):connect_native({},root,factory)
        self.assertEqual(len(clients),1)


if __name__=='__main__':unittest.main()
