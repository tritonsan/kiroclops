"""Durable startup identities and offline archives; every process is mocked."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kiroclops.local_engine import launcher, review
from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json
from scripts.prove_historical_launcher_598 import ARCHIVE, OUTPUT, build_proof
from tests.evidence import requires_evidence


class OwnedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.directory=self.root/'session';self.directory.mkdir()
        self.config={'mode':'baseline','session_dir':str(self.directory),'target_dir':str(self.directory/'target'),
                     'launcher_sha256':file_sha256(launcher.__file__),'model':launcher.MODEL,'effort':launcher.EFFORT,
                     'approval_mode':launcher.APPROVAL,'prompt_path':str(self.root/'prompt.md')}
        Path(self.config['prompt_path']).write_text('one task')
        self.ready={'config':self.config,'cli_version':'mocked'}
        self.alive=True;self.identity='original-client-creation';self.process=SimpleNamespace(pid=987654,poll=lambda:None if self.alive else 0)
        self.close=Mock(side_effect=lambda:self._close())
        self.seen=[]

    def _close(self):self.alive=False;return []
    def _identity(self,pid):return 'launcher-creation' if pid==os.getpid() else self.identity if self.alive else None

    def base_init(self,obj,ready,log_dir,timeout,cancel):
        obj.config=ready['config'];obj.log_dir=Path(log_dir);obj.log_dir.mkdir(parents=True,exist_ok=True)
        obj.process=self.process;obj.prompt_sent=False;obj.kas_version='mocked'
        obj.guard=SimpleNamespace(generation_started=False,summary=lambda:{})

    def constructor(self,log='run'):
        with patch.object(launcher.NativeClient,'__init__',lambda obj,*args:self.base_init(obj,*args)),\
             patch.object(launcher.LocalACPClient,'close',self.close),\
             patch('kiroclops.local_engine.backend._identity',side_effect=self._identity):
            return launcher.LocalACPClient(self.ready,self.directory/'kiro'/log,10,None)

    def test_client_identity_is_durable_before_any_handshake_or_prompt(self):
        client=self.constructor();p=Path(client.owned_process_receipt['path']);saved=read_json(p)
        self.assertEqual('native_transport_started_before_handshake',saved['phase']);self.assertFalse(saved['prompt_sent'])
        self.assertEqual({'pid':987654,'identity':'original-client-creation'},saved['native_client_process'])
        self.assertEqual(client.owned_process_receipt['sha256'],file_sha256(p));self.close.assert_not_called()

    def test_missing_client_identity_is_preserved_and_never_accepted(self):
        self.identity=None
        with self.assertRaises(VisualError) as caught:self.constructor()
        self.assertEqual('owned_identity_missing',caught.exception.code)
        saved=read_json(self.directory/'kiro/run/owned-processes.json')
        self.assertEqual('unconfirmed',saved['status']);self.assertEqual(987654,saved['native_client_pid'])
        self.assertNotIn('native_client_process',saved);self.close.assert_called_once()

    def test_dead_client_retains_startup_identity_but_cannot_start_prompt(self):
        client=self.constructor();self.alive=False
        with patch('kiroclops.local_engine.backend._identity',side_effect=self._identity):
            self.assertEqual('original-client-creation',launcher._verify_saved_client_process(client)['identity'])
            with self.assertRaises(VisualError) as caught:launcher._verify_saved_client_process(client,require_alive=True)
        self.assertEqual('owned_client_not_alive',caught.exception.code)

    def test_recycled_or_replaced_pid_never_recaptures_an_identity(self):
        client=self.constructor();before=Path(client.owned_process_receipt['path']).read_bytes();self.identity='recycled-creation'
        with patch('kiroclops.local_engine.backend._identity',side_effect=self._identity):
            with self.assertRaises(VisualError) as caught:launcher._verify_saved_client_process(client)
        self.assertEqual('owned_identity_changed',caught.exception.code)
        self.assertEqual(before,Path(client.owned_process_receipt['path']).read_bytes())
        self.process.pid=555
        with self.assertRaises(VisualError):launcher._saved_client_process(client)

    def test_identity_receipt_tamper_and_duplicate_are_rejected(self):
        client=self.constructor();path=Path(client.owned_process_receipt['path']);value=read_json(path)
        value['native_client_process']['identity']='other';write_json(path,value)
        with self.assertRaises(VisualError):launcher._saved_client_process(client)
        with self.assertRaises(VisualError):launcher._write_process_identity(path,value)

    def run_native(self,*,die_before_prompt=False,launcher_identity_missing=False,recycle_after_prompt=False):
        session=Mock();session._load.return_value={'schema_version':1, 'experiment':{},
            'budget':{'max_candidates':3,'max_seconds':1800,'max_validation_seconds':1800},
            'session_id':'owned-session','active_job_id':None,'final_job_id':None,'sealed_at':None}
        now=time.time();session.start_clock.return_value={'started_at':now,'deadline_at':now+1800}
        request={'session_id':'owned-session','had_pending_job':False}
        finish=Mock(return_value={'status':'pass'})
        prompt=Mock()
        def connect(ready,log,factory):
            client=factory(ready,log,10,None)
            def run(text):
                saved=read_json(self.directory/'kiro/launch.json')
                self.assertIn('launcher_process_receipt',saved);self.assertIn('native_client_process_receipt',saved)
                self.assertEqual('original-client-creation',saved['native_client_process']['identity'])
                prompt(text);client.prompt_sent=True;client.guard.generation_started=True;self.alive=recycle_after_prompt
                if recycle_after_prompt:self.identity='recycled-client-creation'
                return {'stopReason':'end_turn'}
            client.prompt=run
            if die_before_prompt:self.alive=False
            return client,{'session_id':'native-owned'},[{'status':'pass'}]
        identity=lambda pid:None if launcher_identity_missing and pid==os.getpid() else self._identity(pid)
        with patch.object(launcher,'Session',return_value=session),patch.object(launcher,'preflight',return_value=self.ready),\
             patch.object(launcher.NativeClient,'__init__',lambda obj,*args:self.base_init(obj,*args)),patch.object(launcher.LocalACPClient,'close',self.close),\
             patch('kiroclops.local_engine.backend._identity',side_effect=identity),\
             patch('kiroclops.local_engine.startup.connect_native',side_effect=connect),\
             patch('kiroclops.local_engine.trial_cleanup.begin_terminal_cleanup',return_value=request),\
             patch('kiroclops.local_engine.trial_cleanup.finish_terminal_cleanup',finish),\
             patch('kiroclops.local_engine.backend._terminate_owned') as terminate:
            result=launcher._run_native(self.directory,{'run_kind':'unit'})
            terminate.assert_not_called()
        return result,prompt,session,request

    def test_launcher_and_client_receipts_exist_before_prompt_dead_client_cleanup_reuses_saved_identity(self):
        result,prompt,session,request=self.run_native()
        prompt.assert_called_once();self.assertEqual('completed',result['status'])
        self.assertEqual({'pid':987654,'identity':'original-client-creation'},request['client_process'])
        self.assertEqual('launcher_started_before_native_transport',read_json(self.directory/'kiro/launcher-process.json')['phase'])
        self.close.assert_called_once()

    def test_preprompt_death_never_starts_clock_or_sends_prompt(self):
        result,prompt,session,request=self.run_native(die_before_prompt=True)
        self.assertEqual('owned_client_not_alive',result['status']);prompt.assert_not_called();session.start_clock.assert_not_called()
        self.assertEqual('original-client-creation',request['client_process']['identity'])

    def test_missing_launcher_identity_never_creates_client_or_sends_prompt(self):
        result,prompt,session,request=self.run_native(launcher_identity_missing=True)
        self.assertEqual('owned_identity_missing',result['status']);prompt.assert_not_called();session.start_clock.assert_not_called()
        self.close.assert_not_called();self.assertNotIn('client_process',request)

    def test_finally_rejects_recycled_pid_and_keeps_original_cleanup_identity(self):
        result,prompt,session,request=self.run_native(recycle_after_prompt=True)
        prompt.assert_called_once();self.assertEqual('terminal_cleanup_unconfirmed',result['status'])
        self.assertEqual('owned_identity_changed',result['terminal_cleanup']['error']['code'])
        self.assertEqual({'pid':987654,'identity':'original-client-creation'},request['client_process'])
        saved=read_json(self.directory/'kiro/run/owned-processes.json')
        self.assertEqual('original-client-creation',saved['native_client_process']['identity'])


class HistoricalConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.target=self.directory/'target';self.controls=self.target/'.kiro'
        self.controls.mkdir(parents=True);(self.controls/'agent.json').write_text('historical agent')
        self.config={'session_dir':str(self.directory),'target_dir':str(self.target),'model':launcher.MODEL,'effort':launcher.EFFORT,
                     'approval_mode':launcher.APPROVAL,'launcher_sha256':review._HISTORICAL_CONFIG_ONLY_598['launcher_sha256'],
                     'generated_hashes':{'agent.json':file_sha256(self.controls/'agent.json')},'guard_sources':{}}
        write_json(self.directory/'kiro/config.json',self.config)

    @requires_evidence('artifacts/phase3d/historical-launcher-59866911')
    def test_exact_archive_and_proof_are_rebuilt_and_configuration_is_readable_only(self):
        self.assertEqual(read_json(OUTPUT),build_proof(ARCHIVE.read_bytes()))
        self.assertEqual(review._HISTORICAL_CONFIG_ONLY_598['proof_sha256'],file_sha256(OUTPUT))
        with patch.object(launcher,'load_config',side_effect=VisualError('launcher_changed','new launcher')):
            self.assertEqual(self.config,review._load_native_config_for_audit(self.directory))
        proof=read_json(OUTPUT);self.assertFalse(proof['native_canary']);self.assertFalse(proof['runtime_output_policy_rebound'])
        self.assertNotEqual(self.config['launcher_sha256'],review._NATIVE_OUTPUT_POLICY_STARTUP['launcher_sha256'])

    def test_missing_or_modified_archive_or_proof_is_closed(self):
        actual=file_sha256
        for target in (ARCHIVE,OUTPUT):
            def sha(path):
                if Path(path)==target:raise FileNotFoundError('fixture missing archive')
                return actual(path)
            with patch.object(launcher,'load_config',side_effect=VisualError('launcher_changed','new launcher')),\
                 patch.object(review,'file_sha256',side_effect=sha):
                with self.assertRaises(VisualError) as caught:review._load_native_config_for_audit(self.directory)
            self.assertEqual('historical_launcher_unbound',caught.exception.code)
            with patch.object(launcher,'load_config',side_effect=VisualError('launcher_changed','new launcher')),\
                 patch.object(review,'file_sha256',side_effect=lambda p:'0'*64 if Path(p)==target else actual(p)):
                with self.assertRaises(VisualError) as caught:review._load_native_config_for_audit(self.directory)
            self.assertEqual('historical_launcher_unbound',caught.exception.code)

    def test_historical_control_model_and_guard_tamper_still_fail(self):
        with patch.object(launcher,'load_config',side_effect=VisualError('launcher_changed','new launcher')):
            (self.controls/'agent.json').write_text('tampered')
            with self.assertRaises(VisualError):review._load_native_config_for_audit(self.directory)
            (self.controls/'agent.json').write_text('historical agent')
            self.config['model']='wrong';write_json(self.directory/'kiro/config.json',self.config)
            with self.assertRaises(VisualError):review._load_native_config_for_audit(self.directory)
            self.config['model']=launcher.MODEL;self.config['guard_sources']={'unregistered.py':'a'*64};write_json(self.directory/'kiro/config.json',self.config)
            with self.assertRaises(VisualError):review._load_native_config_for_audit(self.directory)


if __name__=='__main__':unittest.main()
