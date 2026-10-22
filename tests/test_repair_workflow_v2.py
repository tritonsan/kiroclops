"""Repair workflow boundaries with mocked launch/import/model operations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import repair_workflow_v2 as workflow
from kiroclops.integration import session as session_module
from kiroclops.integration.storage import IntegrationError, sha256, project_hashes


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "sessions/fresh-v2"
        self.directory.mkdir(parents=True)
        engine = self.root / ".tools/godot4/Godot_v4.5.1-stable_win64.exe"
        engine.parent.mkdir(parents=True); engine.write_bytes(b"never execute")
        self.binding = {"new.py": "bound-source"}
        baseline=self.directory/'baseline';baseline.mkdir()
        (baseline/'player.gd').write_bytes(b'original source')
        target=self.directory/'target';target.mkdir();(target/'.kiro').mkdir()
        from kiroclops.integration.experiments import get_experiment, GROUND_ID
        from kiroclops.integration.ground_speed_v2 import LONG_HOLD_POLICY
        self.state = {"session_id":self.directory.name, "baseline_hashes":project_hashes(baseline),
                      "prepared_baseline_hashes":project_hashes(baseline),"baseline":str(baseline),
                      "target":str(target),"mode":"tools","schema_version":3,"experiment_id":GROUND_ID,
                      "experiment_spec":get_experiment(GROUND_ID).record()}
        self.fake = Mock(directory=self.directory)
        self.fake.read.side_effect = lambda: self.state
        self.record = {"protocol_id":workflow.PROTOCOL_ID,"session_id":self.directory.name,
                       "measurement_policy":"ground-speed-long-hold-v2", "source_hashes":self.binding,
                       "speed_policy":LONG_HOLD_POLICY.record(),"limits":{"max_candidates":3,"max_seconds":1800},
                       "baseline_hashes":self.state["baseline_hashes"], "engine_sha256":sha256(engine)}
        for target,value in [("ROOT",self.root),("Session",Mock(return_value=self.fake))]:
            p=patch.object(workflow,target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(workflow,"_source_binding",return_value=self.binding);p.start();self.addCleanup(p.stop)
        self.publish()

    def publish(self):
        path=self.directory/'execution-protocol.json'
        path.write_text(json.dumps(self.record),encoding='utf-8')
        self.state['execution_protocol']={'id':workflow.PROTOCOL_ID,'sha256':sha256(path)}
        controls=Path(self.state['target'])/'.kiro'
        generated=controls/'server.json';generated.write_text('{"fixed":"control"}')
        hashes={'server.json':sha256(generated)}
        (controls/'kiroclops-session.json').write_text(json.dumps({'execution_protocol':self.state['execution_protocol'],'generated_hashes':hashes}))
        registered=self.directory/'execution-controls.json'
        registered.write_text(json.dumps({'protocol':self.state['execution_protocol'],'generated_hashes':hashes}))
        self.state['execution_controls_sha256']=sha256(registered)

    def test_exact_protocol_accepted_without_writes(self):
        before={p.name:p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        self.assertEqual(workflow.verify_protocol(self.directory),self.record)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.directory.iterdir() if p.is_file()})
        self.fake.update.assert_not_called()

    def test_old_unregistered_session_rejected(self):
        self.state.pop('execution_protocol')
        with self.assertRaises(IntegrationError) as error:workflow.verify_protocol(self.directory)
        self.assertEqual(error.exception.code,'execution_protocol_changed')
        self.fake.update.assert_not_called()

    def test_changed_protocol_bytes_are_rejected(self):
        (self.directory/'execution-protocol.json').write_text('{}')
        with self.assertRaises(IntegrationError):workflow.verify_protocol(self.directory)

    def test_wrong_session_or_code_binding_rejected(self):
        for key,value in [('session_id','old-session'),('source_hashes',{}),('baseline_hashes',{})]:
            saved=self.record[key];self.record[key]=value;self.publish()
            with self.subTest(field=key),self.assertRaises(IntegrationError):workflow.verify_protocol(self.directory)
            self.record[key]=saved
        self.publish()

    def test_changed_live_baseline_blocks_before_model_or_worker(self):
        (Path(self.state['baseline'])/'player.gd').write_bytes(b'changed live baseline')
        with self.assertRaises(IntegrationError) as error:workflow.verify_protocol(self.directory)
        self.assertEqual(error.exception.code,'execution_protocol_changed')

    def test_controls_registration_cannot_be_removed_to_skip_hash_checks(self):
        self.state.pop('execution_controls_sha256')
        with self.assertRaises(IntegrationError):workflow.verify_protocol(self.directory)

    def test_generated_control_tamper_is_rejected(self):
        (Path(self.state['target'])/'.kiro/server.json').write_bytes(b'changed server')
        with self.assertRaises(IntegrationError) as error:workflow.verify_protocol(self.directory)
        self.assertEqual(error.exception.code,'execution_controls_changed')

    def test_copied_old_baseline_path_cannot_satisfy_fresh_identity(self):
        alternate=self.root/'old-baseline';alternate.mkdir()
        (alternate/'player.gd').write_bytes(b'original source')
        self.state['baseline']=str(alternate)
        with self.assertRaises(IntegrationError):workflow.verify_protocol(self.directory)

    def test_changed_engine_bytes_rejected(self):
        (self.root/'.tools/godot4/Godot_v4.5.1-stable_win64.exe').write_bytes(b'changed')
        with self.assertRaises(IntegrationError):workflow.verify_protocol(self.directory)


class RewriteAndSpawnTests(unittest.TestCase):
    def setUp(self):self.directory=Path('C:/sessions/fresh-v2')

    def test_exact_generated_mcp_args_only_rewritten(self):
        exact=['-m','kiroclops.integration.mcp','--session',str(self.directory)]
        old=['-m','kiroclops.integration.mcp','--session','C:/sessions/old']
        value={'mcpServers':{'ours':{'command':sys.executable,'args':exact.copy()},
                            'old':{'command':sys.executable,'args':old.copy()}},
               'nested':[{'args':exact.copy()}], 'unrelated':['unchanged']}
        self.assertEqual(workflow._rewrite_server(value,self.directory),2)
        self.assertEqual(value['mcpServers']['old']['args'],old)
        self.assertEqual(value['mcpServers']['ours']['args'],['-m',workflow.MODULE,'mcp','--session',str(self.directory)])
        self.assertEqual(workflow._rewrite_server(value,self.directory),0)

    def test_exact_owned_worker_is_rewritten_preserving_process_options(self):
        original=Mock();proxy=workflow._SpawnProxy(original,self.directory)
        args=[sys.executable,'-m','kiroclops.integration.worker','--session',str(self.directory),'--job','job-123']
        env={'PYTHONUTF8':'1'};log=object()
        self.assertIs(proxy.Popen(args,cwd='workspace',env=env,stdout=log),original.Popen.return_value)
        self.assertEqual(original.Popen.call_args.args[0],[sys.executable,'-m',workflow.MODULE,'worker','--session',str(self.directory),'--job','job-123'])
        self.assertIs(original.Popen.call_args.kwargs['env'],env)
        self.assertIs(original.Popen.call_args.kwargs['stdout'],log)

    def test_wrong_worker_session_runtime_or_shell_never_spawned(self):
        original=Mock();proxy=workflow._SpawnProxy(original,self.directory)
        args=[sys.executable,'-m','kiroclops.integration.worker','--session',str(self.directory),'--job','job-123']
        variants=[['cmd.exe','/c','something'], 'shell string', [*args,'extra']]
        for index,value in [(0,'C:/other/python.exe'),(2,'unrelated.module'),(4,'C:/sessions/old')]:
            candidate=args.copy();candidate[index]=value;variants.append(candidate)
        for candidate in variants:
            with self.subTest(args=candidate),self.assertRaises(IntegrationError):proxy.Popen(candidate)
        original.Popen.assert_not_called()

    def test_submit_proxy_is_module_local_and_restored_after_failure(self):
        repair=object.__new__(workflow.RepairSession);repair.directory=self.directory
        original=session_module.subprocess
        def submit(obj,action,candidate):
            self.assertIsInstance(session_module.subprocess,workflow._SpawnProxy)
            import subprocess
            self.assertIs(subprocess,original)
            raise RuntimeError('simulated reservation failure')
        with patch.object(workflow,'verify_protocol'),patch.object(session_module.Session,'submit',submit):
            with self.assertRaises(RuntimeError):repair.submit('evaluate')
        self.assertIs(session_module.subprocess,original)


class McpAndImportTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.directory=self.root/'fresh';self.directory.mkdir()
        self.project=self.directory/'candidates/candidate-01/project';self.project.mkdir(parents=True)
        (self.project/'project.godot').write_bytes(b'original')
        self.fake=Mock(directory=self.directory)

    def test_mcp_wrapper_preserves_parent_watch_and_closes_session_on_error(self):
        from kiroclops.integration import mcp
        previous=mcp.Session;server=Mock();session=Mock();stop=Mock()
        server.run.side_effect=RuntimeError('stdio disconnected')
        with patch.object(mcp,'create_server',return_value=(server,session)),patch('kiroclops.integration.power_lifetime.start_launcher_watch',return_value=stop) as watch:
            with self.assertRaises(RuntimeError):workflow.run_mcp(self.directory)
        self.assertIs(mcp.Session,previous)
        watch.assert_called_once_with(session)
        stop.set.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_seed_is_followed_by_real_editor_import_and_context_restores(self):
        from kiroclops.integration import ground_runner
        original=Mock();calls=[];event=threading.Event()
        def seed(*args,**kwargs):
            calls.append('seed');(self.project/'.godot').mkdir();self.assertIs(kwargs['cancel'],event)
            return {'status':'seeded'}
        def imported(*args,**kwargs):calls.append('editor');self.assertIs(kwargs['cancel'],event)
        original.side_effect=imported
        record={'asset_cache':{'cache_dir':str(self.root/'cache'),'manifest_sha256':'bound'}}
        with patch.object(workflow,'Session',return_value=self.fake),patch.object(ground_runner,'import_project',original),patch('kiroclops.integration.import_cache_v1.seed_import_cache',side_effect=seed) as seed_mock:
            with workflow.repaired_imports(self.directory,record):
                ground_runner.import_project(self.project,cancel=event)
                ground_runner.import_project(self.project,cancel=event)
            self.assertIs(ground_runner.import_project,original)
        self.assertEqual(calls,['seed','editor','editor']);self.assertEqual(seed_mock.call_count,1)
        self.assertEqual((self.project/'project.godot').read_bytes(),b'original')

    def test_import_outside_session_rejected_without_cache_or_editor(self):
        from kiroclops.integration import ground_runner
        original=Mock()
        with patch.object(workflow,'Session',return_value=self.fake),patch.object(ground_runner,'import_project',original),patch('kiroclops.integration.import_cache_v1.seed_import_cache') as seed:
            with workflow.repaired_imports(self.directory,{}),self.assertRaises(IntegrationError):ground_runner.import_project(self.root)
            self.assertIs(ground_runner.import_project,original)
        original.assert_not_called();seed.assert_not_called()

    def test_import_failure_cannot_claim_editor_success_and_restores(self):
        from kiroclops.integration import ground_runner
        (self.project/'.godot').mkdir();runtime=self.directory/'runtime';runtime.mkdir()
        original=Mock(side_effect=RuntimeError('engine error'))
        with patch.object(workflow,'Session',return_value=self.fake),patch.object(ground_runner,'import_project',original):
            with workflow.repaired_imports(self.directory,{}),self.assertRaises(RuntimeError):ground_runner.import_project(self.project,runtime_dir=runtime)
            self.assertIs(ground_runner.import_project,original)
        self.assertFalse((runtime/'repair-import.json').exists())

    def test_import_source_mutation_detected(self):
        from kiroclops.integration import ground_runner
        (self.project/'.godot').mkdir()
        original=Mock(side_effect=lambda *a,**k:(self.project/'project.godot').write_bytes(b'changed'))
        with patch.object(workflow,'Session',return_value=self.fake),patch.object(ground_runner,'import_project',original):
            with workflow.repaired_imports(self.directory,{}),self.assertRaises(IntegrationError) as error:ground_runner.import_project(self.project)
        self.assertEqual(error.exception.code,'source_changed')

    def test_foreign_job_rejected_before_worker_context(self):
        from kiroclops.integration import isolated_runtime_v2
        jobs=self.directory/'jobs';jobs.mkdir()
        (jobs/'job-123.json').write_text(json.dumps({'job_id':'job-123','session_id':'old-session'}))
        with patch.object(workflow,'Session',return_value=self.fake),patch.object(workflow,'verify_protocol',return_value={}),patch.object(isolated_runtime_v2,'isolated_runtime') as context,patch('kiroclops.integration.worker.run_job') as worker:
            with self.assertRaises(IntegrationError):workflow.run_worker(self.directory,'job-123')
        context.assert_not_called();worker.assert_not_called()


if __name__=='__main__':unittest.main()
