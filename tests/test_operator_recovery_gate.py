"""Tiny interrupted-prefix fixtures. No process, model, capture or attestation of a real run."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import operator_recovery_gate as gate
from kiroclops.local_engine import review
from kiroclops.local_engine.session import source_hashes, JOB_BINDING_FIELDS, LIMITS
from kiroclops.visual.schema import digest, file_sha256, read_json, write_json, VisualError
from scripts import phase3d_operator_recovery as script
from tests.test_native_progressive_tools import load, deferred
from tests.test_local_native_tool_outcomes import event, decision, SID
from tests.evidence import requires_evidence


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.old=self.root/'old';self.new=self.root/'new';self.old.mkdir()
        self.target=self.old/'target';self.baseline=self.old/'baseline'
        for project in (self.target,self.baseline):
            project.mkdir()
            for name in ('player.gd','PlayerMovementData.gd','DefaultMovementData.tres','player.tscn'):(project/name).write_text('unchanged')
        hashes=source_hashes(self.target);self.suite_dir=self.root/'suite';self.suite_dir.mkdir()
        self.suite={'authoring_parent_source_hashes':hashes,'stage':'jump'};write_json(self.suite_dir/'suite.json',self.suite)
        job_id='0'*32;worker={'pid':123,'identity':'actual-fixture-creation-identity'}
        job={'schema_version':1,'job_id':job_id,'operation':'prepare','created_at':15,'candidate_id':None,
             'source_path':str(self.baseline),'source_hashes':hashes,'source_sha256':digest(hashes),
             'worker':worker,'status':'cancelled','result':None}
        self.jobdir=self.old/'jobs'/job_id;self.jobdir.mkdir(parents=True)
        write_json(self.jobdir/'job.json',job);binding=digest({k:job[k] for k in JOB_BINDING_FIELDS})
        write_json(self.jobdir/'binding.json',{'sha256':binding});write_json(self.jobdir/'cancel.json',{'reason':'authoring_turn_terminal'})
        self.state={'session_id':'fixture-session','target':str(self.target),'baseline':str(self.baseline),
                    'baseline_hashes':hashes,'reference':str(self.suite_dir),'experiment':{'suite_sha256':digest(self.suite)},
                    'capture_lock':str(self.root/'capture.lock'),'candidates':[],'sealed_at':None,'final_job_id':None,
                    'selected_candidate_id':None,'final':None,'prepared':False,'prepare_job_id':job_id,'budget':dict(LIMITS)}
        write_json(self.old/'session.json',self.state)
        request={'session_id':self.state['session_id'],'capture_lock':self.state['capture_lock'],'operation':'prepare',
                 'preserved_finalization':False,'worker':worker,'job_id':job_id,'job_binding_sha256':binding}
        cleanup={'kind':'TerminalTrialCleanup','status':'pass','worker_exit_verified':True,'capture_lease_released':True,
                 'client_exit_verified':True,'request':request,'finished_at':30}
        write_json(self.old/'operator-terminal-cleanup.json',cleanup)
        (self.old/'kiro/run').mkdir(parents=True);self.prompt=self.old/'kiro/task.md';self.prompt.write_text('Calibration-only fixture task.')
        self.config={'prompt_path':str(self.prompt),'target_dir':str(self.target),'launcher_sha256':'a'*64,'generated_hashes':{'fixture':'b'*64},
                     'agent_name':'kiroclops-local-tools','model':'claude-opus-5.5','effort':'max'}
        write_json(self.old/'kiro/config.json',self.config)
        launch={'run_kind':'multi_reference_repair','prompt_sent':False,'started_at':10,'cli_version':'kiro-cli-chat 2.27.0',
                'preflight':{'session_id':SID,'effort_options':{'config_id':'effortLevel'}}}
        write_json(self.old/'kiro/launch.json',launch)
        incident={'kind':'OperatorInterruptedTrial','status':'operator_interrupted','session_id':self.state['session_id'],
                  'behavior_outcome':'unevaluated','cleanup_sha256':file_sha256(self.old/'operator-terminal-cleanup.json'),
                  'launch_snapshot_sha256':file_sha256(self.old/'kiro/launch.json')}
        write_json(self.old/'operator-interruption.json',incident)
        write_json(self.old/'admission.json',{'raw_review':{'directory':'fixture-review'}})
        write_json(self.old/'environment-readiness.json',{'runtime':{'fixture':'runtime'}})
        self.inbound=load();self.decisions=[decision('tool_load',{'tool_ids':['kiroclops::*']})]
        for index,tool in enumerate(('inspect_session','prepare_experiment'),2):
            es=deferred(tool);self.change_id(es,'toolu_bdrk_'+str(index).zfill(24))
            if tool=='prepare_experiment':
                u=es[-1]['params']['update'];m=json.dumps({'job_id':job_id,'candidate_id':None,'status':'queued'})
                u['rawOutput']['message']=u['rawOutput']['response']=m;u['content'][0]['content']['text']=m
            self.inbound.extend(es);self.decisions.append(decision('mcp_kiroclops_'+tool,{}))
        paths=[str(self.target)]+[str(self.target/n) for n in hashes]
        for index,path in enumerate(paths,4):
            read=index>4;title='Read File' if read else 'List Directory';tool='read_file' if read else 'list_directory'
            raw={'path':path,'offset':None,'limit':None} if read else {'path':path,'depth':2,'explanation':None}
            base={'toolCallId':f'builtin-{index}','rawInput':raw,'_meta':{'kiro':{'toolOrigin':'default' if read else 'acp'}}}
            if read:base['locations']=[{'path':path}]
            message='Recorded target source' if read else 'Recorded target listing'
            es=[event({**deepcopy(base),'sessionUpdate':'tool_call','status':'pending','kind':'read' if read else 'search','title':title}),
                event({**deepcopy(base),'sessionUpdate':'tool_call_update','status':'in_progress'}),
                event({**deepcopy(base),'sessionUpdate':'tool_call_update','status':'completed','title':title,
                       'rawOutput':{'message':message},'content':[{'type':'content','content':{'type':'text','text':message}}]})]
            self.inbound.extend(es);self.decisions.append(decision(tool,raw))
        hook=str(self.target/'.kiro/hooks/kiroclops-guard.json')+'#hook-0'
        for i in range(8):self.inbound.append(event({'sessionUpdate':'session_info_update','_meta':{'kiro':{'kind':'hook_update','hook':{
            'hookId':hook,'operationId':str(i),'name':'Local trial boundary','status':'completed','actionType':'runCommand'}}}}))
        methods=['initialize','session/new','session/set_mode','session/set_config_option','session/set_config_option','_kiro/hooks/list','session/prompt']
        self.messages=[{'jsonrpc':'2.0','id':i+1,'method':m,'params':{}} for i,m in enumerate(methods)]
        self.messages[0]['params']={'protocolVersion':1,'clientCapabilities':{'_meta':{'kiro':{'hooks':{'enabled':True,'v2':True}}}},
                                   'clientInfo':{'name':'kiroclops-local','version':'1'}}
        self.messages[1]['params']={'cwd':str(self.target),'mcpServers':[]}
        self.messages[2]['params']={'sessionId':SID,'modeId':self.config['agent_name']}
        self.messages[3]['params']={'sessionId':SID,'configId':'model','value':self.config['model']}
        self.messages[4]['params']={'sessionId':SID,'configId':'effortLevel','value':self.config['effort']}
        self.messages[5]['params']={'sessionId':SID,'workspacePaths':[str(self.target)],'trigger':'PreToolUse'}
        self.messages[-1]['params']={'sessionId':SID,'prompt':[{'type':'text','text':self.prompt.read_text()}]}
        self.flush()
        self.patch=patch.object(gate.Session,'_load',side_effect=lambda obj=None:deepcopy(self.state))
        self.patch.start();self.addCleanup(self.patch.stop)
        loader=patch.object(review,'_load_native_config_for_audit',return_value=self.config);loader.start();self.addCleanup(loader.stop)

    @staticmethod
    def change_id(es,cid):
        for e in es:e['params']['update']['toolCallId']=cid

    def flush(self):
        def save(path,values):path.write_text(''.join(json.dumps(v)+'\n' for v in values),encoding='utf-8')
        save(self.old/'kiro/run/stdout.jsonl',self.inbound);save(self.old/'kiro/tool-audit.jsonl',self.decisions)
        outbound=[]
        for i,message in enumerate(self.messages,1):
            sha=__import__('hashlib').sha256(json.dumps(message,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            at=datetime.fromtimestamp(10+i/10,timezone.utc).isoformat()
            outbound.extend([{'at':at,'phase':'attempt','sequence':i,'payload_sha256':sha,'message':message},
                             {'at':at,'phase':'sent','sequence':i,'payload_sha256':sha}])
        save(self.old/'kiro/run/outbound.jsonl',outbound)
        (self.old/'kiro/run/stderr.log').write_text('[INFO] kas.server.starting {"version":"0.66.22"}\n')

    def attestation(self,scope=None):
        scope=scope or gate.inspect_eligibility(self.old)
        value={'kind':'OperatorRecoveryExposureAttestation','schema_version':1,'status':'pass',
               'method':'manual_whole_recorded_prefix_review','reviewer':'fixture reviewer','scope_sha256':digest(scope),
               'evidence_hashes':scope['evidence_hashes'],'explicit_fresh_registration_authorized':True,
               'launcher_process_identity_status':scope['launcher_process_identity_status'],'launcher_exit_proof':scope['launcher_exit_proof'],
               'residual_owned_processes':{'assessment':'none_observed','scope':'fixture worker only','evidence':'fixture observation'},
               'checks':[{'id':c,'verdict':'pass','evidence':'fixture manual review'} for c in sorted(gate.CHECKS)]}
        path=self.root/'attestation.json';write_json(path,value);return path

    @requires_evidence('reviews/staged-native227v3')
    def test_unsigned_scope_keeps_lost_launcher_identity_and_requires_real_review(self):
        scope=gate.inspect_eligibility(self.old)
        self.assertEqual('not_recorded',scope['launcher_process_identity_status'])
        self.assertEqual('operator_report_only',scope['launcher_exit_proof'])
        self.assertEqual('requires_operator_attestation',scope['exposure_status'])
        self.assertEqual(8,len(scope['native_prefix']['calls']));self.assertFalse(scope['behavior_acceptance'])
        # Stale prompt_sent=False from a killed launcher is not no-exposure proof.
        self.assertFalse(read_json(self.old/'kiro/launch.json')['prompt_sent'])
        self.assertEqual(scope,gate.validate_eligibility(self.old,self.attestation(scope))[0])

    @requires_evidence('reviews/staged-native227v3')
    def test_missing_or_stale_no_coaching_exposure_checks_fail(self):
        path=self.attestation();v=read_json(path);v['checks']=[c for c in v['checks'] if c['id']!='no_operator_coaching'];write_json(path,v)
        with self.assertRaises(VisualError):gate.validate_eligibility(self.old,path)
        path=self.attestation();(self.old/'kiro/run/stderr.log').write_text((self.old/'kiro/run/stderr.log').read_text()+'additional line\n')
        with self.assertRaises(VisualError):gate.validate_eligibility(self.old,path)

    def test_candidate_prepared_final_or_source_edit_is_never_reset(self):
        for key,value in [('candidates',[{'candidate_id':'existing'}]),('prepared',True),('final',{'pass':False}),('sealed_at',12)]:
            old=self.state[key];self.state[key]=value
            with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)
            self.state[key]=old
        (self.target/'player.gd').write_text('edited')
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)

    def test_prompt_addendum_unknown_read_envelope_and_incomplete_hook_fail(self):
        self.messages.append(deepcopy(self.messages[-1]));self.messages[-1]['id']=20;self.flush()
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)
        self.messages.pop();self.flush()
        es=next(e for e in self.inbound if e.get('params',{}).get('update',{}).get('title')=='Read File')
        es['params']['update']['extra']='unproven';self.flush()
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)
        es['params']['update'].pop('extra');self.inbound.pop();self.flush()
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)

    def test_job_result_or_worker_creation_identity_mismatch_fail(self):
        p=self.jobdir/'job.json';job=read_json(p);job['worker']['identity']='different';write_json(p,job)
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)
        job['worker']['identity']='actual-fixture-creation-identity';write_json(p,job);write_json(self.jobdir/'result.json',{'pass':False})
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)

    def test_raw_hook_argument_null_omission_is_not_silently_equated(self):
        self.decisions[4]['arguments'].pop('offset');self.flush()
        with self.assertRaises(VisualError):gate.inspect_eligibility(self.old)

    @requires_evidence('reviews/staged-native227v3')
    def test_invented_launcher_identity_and_unknown_residual_scope_fail(self):
        path=self.attestation();v=read_json(path);v['launcher_process_identity_status']='recorded';write_json(path,v)
        with self.assertRaises(VisualError):gate.validate_eligibility(self.old,path)
        path=self.attestation();v=read_json(path);v.pop('residual_owned_processes');write_json(path,v)
        with self.assertRaises(VisualError):gate.validate_eligibility(self.old,path)

    def test_cleanup_replay_never_terminates_and_does_not_invent_launcher_identity(self):
        with patch('kiroclops.local_engine.trial_cleanup.finish_terminal_cleanup',return_value={
            'status':'pass','worker_exit_verified':True,'capture_lease_released':True,'termination_requested':False}) as replay:
            result=gate.replay_cleanup(self.old)
        self.assertFalse(replay.call_args.kwargs['terminate']);self.assertEqual('not_recorded',result['launcher_process_identity_status'])

    def register(self,certify=None,budget=None):
        scope=gate.inspect_eligibility(self.old);path=self.attestation(scope)
        def create(*args,**kw):self.new.mkdir();write_json(self.new/'session.json',{'budget':budget or dict(LIMITS)})
        original=self.patch;original.stop()
        try:
            with patch.object(gate.Session,'_load',autospec=True,side_effect=lambda obj:
                              deepcopy(self.state) if obj.directory==self.old else {'budget':budget or dict(LIMITS)}),\
                 patch.object(script.media_suite,'load_suite',return_value=self.suite),\
                 patch.object(script.media_suite,'create_suite_session',side_effect=create),\
                 patch.object(script,'replay_cleanup',return_value={'status':'pass'}),\
                 patch.object(script.suite_readiness,'certify',side_effect=certify):
                return script.register(self.old,self.new,eligibility=path)
        finally:original.start()

    @requires_evidence('reviews/staged-native227v3')
    def test_registration_does_not_launch_and_uses_actual_session_budget(self):
        result=self.register()
        self.assertEqual('registered',result['status']);self.assertFalse(result['model_started'])
        self.assertFalse(result['automatic_model_retry']);self.assertEqual('not_recorded',result['launcher_process_identity_status'])
        self.assertEqual(3,read_json(self.new/'session.json')['budget']['max_candidates'])
        self.assertTrue((self.old/'operator-interruption.json').exists())

    @requires_evidence('reviews/staged-native227v3')
    def test_readiness_failure_is_retained_and_never_reported_registered(self):
        with self.assertRaises(VisualError):self.register(VisualError('not_ready','fixture admission failed'))
        r=read_json(self.new/'operator-recovery-registration.json');self.assertEqual('registration_failed',r['status']);self.assertFalse(r['model_started'])


if __name__=='__main__':unittest.main()
