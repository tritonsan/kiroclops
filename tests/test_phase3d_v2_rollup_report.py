"""Named-input offline snapshots cannot promote incomplete or historical results."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.session import JOB_BINDING_FIELDS
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_rollup_report as rollup


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='kc-v2-rollup-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.config=self.root/'inputs.json'
        write_json(self.config,{'kind':'Phase3DV2RollupInputs','schema_version':1,'domains':{}})
        self.output=self.root/'report'

    def test_missing_domains_are_pending_and_require_game_chain(self):
        with (patch.object(Path,'rglob',side_effect=AssertionError('No artifact discovery permitted')),
              patch.object(Path,'glob',side_effect=AssertionError('No artifact discovery permitted'))):
            value=rollup.build(self.config,self.output)
        self.assertEqual('pending',value['overall_status'])
        self.assertFalse(value['full_success'])
        self.assertEqual(['jump','ground'],value['domains']['game']['required_stages'])
        self.assertTrue(all(s['axes']['behavior']['status']=='pending' for d in value['domains'].values() for s in d['stages']))
        self.assertFalse(value['recording_or_model_started'])
        self.assertFalse(value['comparison_thresholds_recomputed'])
        self.assertEqual(value,rollup.validate_report(self.output))

    def test_historical_pass_is_retained_without_new_acceptance(self):
        past=self.root/'historical.json'
        write_json(past,{'status':'pass','acceptance_profile':'old-profile','full_success':True})
        config=read_json(self.config);config['historical']=[{'label':'old p04/gj05/SVG decisions','result':str(past)}]
        write_json(self.config,config)
        original=past.read_bytes();value=rollup.build(self.config,self.output)
        self.assertEqual(original,past.read_bytes())
        self.assertEqual('pass',value['historical'][0]['status_as_recorded'])
        self.assertFalse(value['historical'][0]['promoted_to_new_acceptance'])
        self.assertFalse(value['full_success'])

    def test_bound_result_and_consumed_input_tampering_are_detected(self):
        rollup.build(self.config,self.output)
        value=read_json(self.output/'report.json');value['overall_status']='pass'
        write_json(self.output/'report.json',value)
        with self.assertRaises(VisualError) as caught:rollup.validate_report(self.output)
        self.assertEqual('result_changed',caught.exception.code)
        second=self.root/'second';rollup.build(self.config,second)
        config=read_json(self.config);config['domains']={'panel':{'stages':[]}}
        write_json(self.config,config)
        with self.assertRaises(VisualError) as caught:rollup.validate_report(second)
        self.assertEqual('source_changed',caught.exception.code)

    def test_rehashing_status_flags_cannot_hide_missing_axes(self):
        rollup.build(self.config,self.output)
        value=read_json(self.output/'report.json')
        for domain in value['domains'].values():domain['status']='pass'
        value['full_success']=True;value['overall_status']='pass'
        write_json(self.output/'report.json',value)
        receipt=read_json(self.output/'receipt.json');receipt['report_sha256']=digest(value)
        receipt['full_success']=True;receipt['output_file_sha256']['report.json']=file_sha256(self.output/'report.json')
        write_json(self.output/'receipt.json',receipt)
        with self.assertRaises(VisualError) as caught:rollup.validate_report(self.output)
        self.assertEqual('incomplete_matrix',caught.exception.code)

    def test_existing_outputs_and_unknown_input_authority_are_rejected(self):
        rollup.build(self.config,self.output);original=(self.output/'report.json').read_bytes()
        with self.assertRaises(VisualError) as caught:rollup.build(self.config,self.output)
        self.assertEqual('output_exists',caught.exception.code)
        self.assertEqual(original,(self.output/'report.json').read_bytes())
        config=read_json(self.config);config['full_success']=True;write_json(self.config,config)
        with self.assertRaises(VisualError):rollup.build(self.config,self.root/'fake')
        self.assertFalse((self.root/'fake').exists())

    def test_named_curve_is_embedded_without_server_or_interpolating_bad_points(self):
        curve=self.root/'observations.json'
        write_json(curve,{'kind':'ObservationSet','objects':[{'id':'panel','frames':[
            {'time_s':.4,'x':10.,'y':20.,'valid':True},
            {'time_s':.5,'x':999.,'y':999.,'valid':False},
            {'time_s':.6,'x':30.,'y':15.,'valid':True}]}]})
        config=read_json(self.config);config['assets']=[{'kind':'curve','path':str(curve),'label':'Actual measured panel'}]
        write_json(self.config,config);value=rollup.build(self.config,self.output)
        self.assertEqual([[.4,0.,0.],[.6,20.,-5.]],value['assets'][0]['series'][0]['points'])
        self.assertEqual([[[.4,0.,0.]],[[.6,20.,-5.]]],value['assets'][0]['series'][0]['segments'])
        rendered=(self.output/'index.html').read_text(encoding='utf-8')
        self.assertIn('Measured x displacement against actual time',rendered)
        self.assertIn('Measured y displacement against actual time',rendered)
        self.assertNotIn('fetch(',rendered)
        self.assertNotIn('<polyline',rendered)
        self.assertEqual(curve.read_bytes(),(self.output/value['assets'][0]['relative_path']).read_bytes())
        rollup.validate_report(self.output)

    def test_real_named_job_receipt_rejects_result_tampering_and_incomplete_matrix(self):
        source_hashes={'src/player.gd':digest('source')}
        candidate={'candidate_id':'candidate-01','source_hashes':source_hashes,'source_sha256':digest(source_hashes)}
        suite={'cases':[{'case_id':'new-heldout','role':'validation'}],'source_epoch':digest('epoch'),'acceptance_profile':'realistic-behavior-v1'}
        state={'started_at':10.,'sealed_at':12.,'deadline_at':40.,'budget':{'max_validation_seconds':20.}}
        job={'schema_version':1,'job_id':'f01','operation':'finalize','created_at':13.,'started_at':14.,'finished_at':15.,
            'candidate_id':candidate['candidate_id'],'source_path':str(self.root/'candidates/candidate-01/source'),
            'source_hashes':source_hashes,'source_sha256':candidate['source_sha256'],'status':'succeeded'}
        result={'suite_sha256':digest(suite),'source_epoch':suite['source_epoch'],'acceptance_profile':suite['acceptance_profile'],
            'comparisons':[{'case_id':'new-heldout','role':'validation','pass':True}]}
        folder=self.root/'jobs/f01'
        write_json(folder/'result.json',result);job['result_sha256']=digest(result);write_json(folder/'job.json',job)
        write_json(folder/'binding.json',{'sha256':digest({k:job[k] for k in JOB_BINDING_FIELDS})})
        _,actual=rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,suite)
        self.assertEqual(result,actual)
        write_json(folder/'result.json',{**result,'comparisons':[]})
        with self.assertRaises(VisualError) as caught:rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,suite)
        self.assertEqual('wrong_result',caught.exception.code)
        changed={**result,'comparisons':[]};job['result_sha256']=digest(changed);write_json(folder/'job.json',job)
        with self.assertRaises(VisualError) as caught:rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,suite)
        self.assertEqual('incomplete_matrix',caught.exception.code)

    def test_offline_links_encode_file_names_and_support_windows_drives(self):
        path=self.root/'other'/'a b#c.html'
        self.assertIn('a%20b%23c.html',rollup._href(path,self.output))
        if os.name=='nt':
            self.assertEqual('file:///C:/kc3d/p/a%20b%23c.html',
                rollup._href(Path('C:/kc3d/p/a b#c.html'),Path('D:/report')))

    def test_archived_implementation_change_does_not_erase_old_snapshot(self):
        code=self.root/'engine.py';code.write_text('VERSION=1\n',encoding='utf-8')
        legacy_origin=rollup._code_origin
        is_code=lambda p:Path(p).absolute()==code or legacy_origin(p)
        original_stage=rollup._stage
        def stage(inputs,spec):
            inputs.code(code)
            return original_stage(inputs,spec)
        with patch.object(rollup,'_code_origin',side_effect=is_code),patch.object(rollup,'_stage',side_effect=stage):
            value=rollup.build(self.config,self.output)
        self.assertIn(str(code),value['code_snapshot'])
        code.write_text('VERSION=2\n',encoding='utf-8')
        with patch.object(rollup,'_code_origin',side_effect=is_code):
            self.assertEqual(value,rollup.validate_report(self.output))
            archived=self.output/value['code_snapshot'][str(code)]['relative_path']
            archived.write_text('VERSION=2\n',encoding='utf-8')
            with self.assertRaises(VisualError) as caught:rollup.validate_report(self.output)
            self.assertEqual('source_changed',caught.exception.code)
            archived.unlink()
            with self.assertRaises((VisualError,OSError)):rollup.validate_report(self.output)

    def test_python_candidate_evidence_cannot_be_substituted_with_code_snapshot(self):
        evidence=self.root/'candidate.py';evidence.write_text('movement=1\n',encoding='utf-8')
        config=read_json(self.config);config['assets']=[{'label':'Candidate evidence stays live','kind':'artifact','path':str(evidence)}]
        write_json(self.config,config);value=rollup.build(self.config,self.output)
        self.assertNotIn(str(evidence),value['code_snapshot'])
        evidence.write_text('movement=2\n',encoding='utf-8')
        with self.assertRaises(VisualError) as caught:rollup.validate_report(self.output)
        self.assertEqual('source_changed',caught.exception.code)

    def test_final_job_can_be_created_before_seal_but_execution_cannot(self):
        source_hashes={'player.gd':digest('source')}
        candidate={'candidate_id':'candidate-01','source_hashes':source_hashes,'source_sha256':digest(source_hashes)}
        suite={'cases':[],'source_epoch':digest('epoch'),'acceptance_profile':'legacy-game-v1'}
        state={'started_at':10.,'sealed_at':14.,'deadline_at':40.,'budget':{'max_validation_seconds':20.}}
        result={'suite_sha256':digest(suite),'source_epoch':suite['source_epoch'],
            'acceptance_profile':suite['acceptance_profile'],'comparisons':[]}
        job={'schema_version':1,'job_id':'f01','operation':'finalize','created_at':13.,'started_at':14.1,'finished_at':15.,
            'candidate_id':candidate['candidate_id'],'source_path':str(self.root/'candidates/candidate-01/source'),
            'source_hashes':source_hashes,'source_sha256':candidate['source_sha256'],'status':'succeeded','result_sha256':digest(result)}
        folder=self.root/'jobs/f01';write_json(folder/'result.json',result);write_json(folder/'job.json',job)
        write_json(folder/'binding.json',{'sha256':digest({k:job[k] for k in JOB_BINDING_FIELDS})})
        self.assertEqual(result,rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,suite)[1])
        job['started_at']=13.5;write_json(folder/'job.json',job)
        with self.assertRaises(VisualError):rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,suite)

    def test_bound_execution_error_is_failed_operation_without_usable_result(self):
        source_hashes={'player.gd':digest('source')}
        candidate={'candidate_id':'candidate-01','source_hashes':source_hashes,'source_sha256':digest(source_hashes)}
        state={'started_at':10.,'sealed_at':14.,'deadline_at':40.,'budget':{'max_validation_seconds':20.}}
        job={'schema_version':1,'job_id':'f01','operation':'finalize','created_at':13.,'started_at':14.1,'finished_at':15.,
            'candidate_id':candidate['candidate_id'],'source_path':str(self.root/'candidates/candidate-01/source'),
            'source_hashes':source_hashes,'source_sha256':candidate['source_sha256'],'status':'failed','result_sha256':None,
            'error':{'code':'execution_error','message':"'function' object has no attribute 'is_set'"}}
        folder=self.root/'jobs/f01';write_json(folder/'job.json',job)
        write_json(folder/'binding.json',{'sha256':digest({k:job[k] for k in JOB_BINDING_FIELDS})})
        actual,result=rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,{},allow_execution_failure=True)
        self.assertEqual('failed',actual['status']);self.assertIsNone(result)
        write_json(folder/'result.json',{'pass':True})
        with self.assertRaises(VisualError) as caught:
            rollup._job(rollup.Inputs(),self.root,'f01','finalize',candidate,state,{},allow_execution_failure=True)
        self.assertEqual('wrong_result',caught.exception.code)

    def test_failed_native_diagnostic_replays_negative_verdict_without_authorship(self):
        evidence=self.root/'kiro/run/stdout.jsonl';evidence.parent.mkdir(parents=True)
        evidence.write_text('actual native bytes\n',encoding='utf-8')
        copied=self.root/'diagnostic/evidence/stdout.jsonl';copied.parent.mkdir(parents=True);copied.write_bytes(evidence.read_bytes())
        target=self.root/'target';controls=target/'.kiro';controls.mkdir(parents=True)
        prompt=controls/'prompt.md';prompt.write_text('the task',encoding='utf-8')
        write_json(self.root/'kiro/config.json',{'target_dir':str(target),'prompt_path':str(prompt),
            'generated_hashes':{'prompt.md':file_sha256(prompt)}})
        diagnostic={'kind':'ReadOnlyNativePostResponseDiagnostic','schema_version':1,'session':str(self.root),
            'acceptance_override':False,'canonical_native_audit':{'status':'fail','exception_type':'VisualError',
                'code':'invalid_native_evidence','message':'Unexpected post-response event'},
            'evidence_copies':[{'source':str(evidence),'copy':str(copied),'sha256':file_sha256(evidence)}],
            'source_files':[],'model_deviation_evidenced_by_this_tail':False,'classification':'Typed metadata tail'}
        path=self.root/'diagnostic/result.json';write_json(path,diagnostic)
        row={'axes':{name:rollup.axis() for name in rollup.AXES}}
        with patch('kiroclops.local_engine.review.audit_native_run',side_effect=VisualError('invalid_native_evidence','Unexpected post-response event')):
            rollup._failed_native_diagnostic(rollup.Inputs(),path,self.root,row)
        self.assertEqual('fail',row['axes']['native_origin']['status']);self.assertFalse(rollup._verified_current_authorship(row))
        with patch('kiroclops.local_engine.review.audit_native_run',return_value={'status':'pass'}):
            with self.assertRaises(VisualError):rollup._failed_native_diagnostic(rollup.Inputs(),path,self.root,row)

    def test_frozen_partial_numerics_remain_nonacceptance_and_bind_failed_job(self):
        path=self.root/'partial';state={'session_id':'session-01'};candidate={'candidate_id':'candidate-02','source_sha256':digest('candidate')}
        job={'job_id':'failed01','status':'failed','error':{'code':'execution_error','message':'callback interface'}}
        suite={'cases':[{'case_id':'new01','role':'validation'}],'source_epoch':digest('epoch')}
        value={'kind':'FailedFinalNumericalDiagnostic','schema_version':1,'status':'partial_numerical_diagnostic',
            'session':str(self.root),'session_id':state['session_id'],'original_job_id':job['job_id'],'original_job_status':'failed',
            'original_job_error':job['error'],'candidate_id':candidate['candidate_id'],'source_sha256':candidate['source_sha256'],
            'suite_sha256':digest(suite),'source_epoch':suite['source_epoch'],'required':1,'passed':1,'groups':{'validation':{'required':1,'passed':1}},
            'mean_curve_error_body':.05,'retained_parent_behavior':True,'recorded_functional_status':'pass',
            'visual_boundary_status':'not_evaluated','normal_level_status':'not_evaluated','full_behavior_acceptance':False,
            'canonical_result_created':False,'original_job_rewritten':False,'new_model_generation':False,
            'new_capture_started':False,'failed_trial_replaced':False}
        write_json(path/'diagnostic.json',value);write_json(path/'comparisons.json',[{'case_id':'new01','role':'validation','pass':True}])
        for name in ('parent-preservation.json','record-bindings.json','evidence-bindings.json'):write_json(path/name,{})
        producer=Path(rollup.__file__).with_name('phase3d_v2_partial_final.py').resolve()
        receipt={'kind':'FailedFinalNumericalDiagnosticReceipt','schema_version':1,'diagnostic_sha256':file_sha256(path/'diagnostic.json'),
            'producer':{'path':str(producer),'sha256':rollup.PARTIAL_FINAL_PRODUCER_SHA256},'full_behavior_acceptance':False,
            'output_file_hashes':{name:file_sha256(path/name) for name in ('diagnostic.json','comparisons.json',
                'parent-preservation.json','record-bindings.json','evidence-bindings.json')}}
        write_json(path/'receipt.json',receipt);row={'status':'fail','axes':{name:rollup.axis() for name in rollup.AXES}}
        rollup._partial_final_diagnostic(rollup.Inputs(),path,self.root,state,suite,candidate,job,row)
        self.assertEqual(1,row['partial_final_diagnostic']['passed']);self.assertEqual('fail',row['status'])
        self.assertFalse(row['partial_final_diagnostic']['full_behavior_acceptance'])
        with self.assertRaises(VisualError) as caught:
            rollup._partial_final_diagnostic(rollup.Inputs(),path,self.root,state,suite,candidate,{**job,'job_id':'foreign'},row)
        self.assertEqual('wrong_partial_diagnostic',caught.exception.code)
        value['full_behavior_acceptance']=True;write_json(path/'diagnostic.json',value)
        receipt['diagnostic_sha256']=file_sha256(path/'diagnostic.json');receipt['output_file_hashes']['diagnostic.json']=receipt['diagnostic_sha256'];write_json(path/'receipt.json',receipt)
        with self.assertRaises(VisualError):rollup._partial_final_diagnostic(rollup.Inputs(),path,self.root,state,suite,candidate,job,row)

    def test_actual_prompt_clock_is_shown_without_completed_authorship_claim(self):
        state={'clock_origin':'model_generation','started_at':10.,'deadline_at':3610.}
        write_json(self.root/'kiro/launch.json',{'status':'running','prompt_started_at':10.1,
            'model':'claude-opus-5.5','effort':'max','prompt_sent':False})
        task=rollup._native_task(rollup.Inputs(),self.root,state)
        self.assertTrue(task['prompt_clock_started']);self.assertEqual('running',task['status_as_recorded'])
        self.assertEqual(10.1,task['prompt_started_at_as_recorded']);self.assertFalse(task['completed_authorship_verified'])
        self.assertFalse(task['prompt_sent_flag_as_recorded'])  # A stale running flag is not proof no task exists.
        self.assertEqual('max',task['effort_as_recorded'])
        write_json(self.root/'kiro/launch.json',{'status':'starting','prompt_started_at':None})
        task=rollup._native_task(rollup.Inputs(),self.root,{'clock_origin':'preparation','started_at':10.,'deadline_at':1810.})
        self.assertFalse(task['prompt_clock_started']);self.assertFalse(task['completed_authorship_verified'])

    def test_new_authorship_requires_both_current_source_and_native_gates(self):
        row={'axes':{'source':{'status':'pass','new_code_authored':True},
            'native_origin':{'status':'pending','historical_only':False}}}
        self.assertFalse(rollup._verified_current_authorship(row))
        row['axes']['native_origin']['status']='pass';self.assertTrue(rollup._verified_current_authorship(row))
        row['axes']['native_origin']['historical_only']=True;self.assertFalse(rollup._verified_current_authorship(row))
        row['axes']['native_origin']['historical_only']=False;row['axes']['source']['new_code_authored']=False
        self.assertFalse(rollup._verified_current_authorship(row))
        write_json(self.root/'kiro/launch.json',{'status':'running','prompt_started_at':9.})
        with self.assertRaises(VisualError) as caught:
            rollup._native_task(rollup.Inputs(),self.root,{'clock_origin':'model_generation','started_at':10.,'deadline_at':3610.})
        self.assertEqual('wrong_native_clock',caught.exception.code)

    def test_report_producer_does_not_start_model_even_if_bound_task_is_active(self):
        original=rollup._stage
        def active(inputs,spec):
            row=original(inputs,spec)
            if spec['stage']=='jump':row['native_task']={'prompt_clock_started':True,'completed_authorship_verified':False}
            return row
        with patch.object(rollup,'_stage',side_effect=active):value=rollup.build(self.config,self.output)
        self.assertTrue(value['bound_model_task_started']);self.assertFalse(value['new_model_generation_claimed'])
        self.assertEqual({'capture_started':False,'model_started':False},value['report_producer'])
        self.assertFalse(value['recording_or_model_started']);self.assertFalse(value['full_success'])
        rollup.validate_report(self.output)


class FailedPreparationReportTests(unittest.TestCase):
    """Use the real frozen metadata producer on a tiny canonical fixture."""
    def setUp(self):
        from scripts.phase3d_v2_prepare import recipes
        from scripts.phase3d_v2_failed_preparation import MESSAGE,run
        self.temporary=tempfile.TemporaryDirectory(prefix='kc-failed-rollup-');self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.previous=self.root/'p/calibration';self.failed=self.root/'p/failed'
        self.closed=self.root/'a/closure';self.config=self.root/'inputs.json';self.output=self.root/'report'
        cases=recipes('svg');private=[c for c in cases if c['role']!='calibration'];self.private=private
        write_json(self.previous/'calibration.json',{'stage':'svg','model_started':False})
        write_json(self.previous/'recipes.json',cases)
        write_json(self.previous/'start-binding.json',{'stage':'svg','model_started':False,'recipes_sha256':digest(cases),
            'parent_source_sha256':digest('parent'),'original_source_sha256':digest('original')})
        write_json(self.failed/'failure.json',{'code':'capture_timing_failure','message':MESSAGE,
            'model_started':False,'previous_attempts_preserved':True})
        write_json(self.failed/'progress.json',{'phase':private[18]['case_id']+': input 11/11','model_started':False})
        for case in private[:18]:
            for role in ('reference','original','before'):
                write_json(self.failed/role/case['case_id']/'record.json',{'measurement_status':'ok',
                    'scenario':case['scenario'],'source_sha256':digest('original' if role=='original' else 'parent')})
        attempt=self.failed/'reference'/private[18]['case_id']/'attempt-01'
        write_json(attempt/'capture-error.json',{'code':'capture_timing_failure','message':MESSAGE})
        write_json(attempt/'capture-start.json',{'scenario':private[18]['scenario'],'variant':'reference',
            'source_hashes':{},'adapter_hashes':{}})
        for name in ('received-evidence.json','frame-receipts.json','clock-runtime.json','progress-runtime.json',
            'transport-context.json','transport-order.json','stream-baseline.json','preflight.json'):
            write_json(attempt/'private'/name,{})
        run(self.failed,self.previous,self.closed)
        write_json(self.config,{'kind':'Phase3DV2RollupInputs','schema_version':1,'domains':{'svg':{'stages':[
            {'stage':'svg','failed_preparation':str(self.closed),'calibration_preparation':str(self.previous)}]}}})

    def resign(self):
        closure=read_json(self.closed/'closure.json');receipt=read_json(self.closed/'receipt.json')
        receipt['closure_sha256']=digest(closure);receipt['output_file_hashes']['closure.json']=file_sha256(self.closed/'closure.json')
        write_json(self.closed/'receipt.json',receipt)

    def svg(self):return rollup.build(self.config,self.output)['domains']['svg']['stages'][0]

    def test_actual_closure_reports_measurement_failure_without_behavior_or_model(self):
        with (patch.object(Path,'rglob',side_effect=AssertionError('No global scan')),
              patch.object(Path,'glob',side_effect=AssertionError('Only canonical named paths'))):
            value=rollup.build(self.config,self.output)
            rollup.validate_report(self.output)
        stage=value['domains']['svg']['stages'][0]
        self.assertEqual(('fail','fail','not_evaluated'),
            (stage['status'],stage['axes']['measurement']['status'],stage['axes']['behavior']['status']))
        self.assertEqual((18,6,54),tuple(stage['failed_preparation'][k] for k in
            ('completed_case_count','missing_case_count','completed_role_record_count')))
        self.assertFalse(stage['model_started']);self.assertFalse(value['full_success'])
        self.assertEqual('fail',value['overall_status']);self.assertFalse(stage['complete_matrix_verified'])
        self.assertTrue(all(not p.endswith('.png') for p in value['consumed_file_sha256']))
        self.assertFalse((self.root/'s/failed').exists())

    def test_changed_failure_or_producer_cannot_be_accepted(self):
        closure=read_json(self.closed/'closure.json');closure['producer']['sha256']=digest('unknown producer')
        write_json(self.closed/'closure.json',closure);self.resign()
        receipt=read_json(self.closed/'receipt.json');receipt['producer']=closure['producer'];write_json(self.closed/'receipt.json',receipt)
        stage=self.svg();self.assertEqual('invalid',stage['status'])
        self.assertEqual('producer_changed',stage['errors'][0]['code'])

    def test_rehashed_count_or_model_claim_cannot_promote_failure(self):
        closure=read_json(self.closed/'closure.json');closure['completed_case_count']=24
        write_json(self.closed/'closure.json',closure);self.resign()
        stage=self.svg();self.assertEqual('invalid',stage['status']);self.assertEqual('incomplete_matrix',stage['errors'][0]['code'])
        closure['completed_case_count']=18;closure['model_started']=True
        write_json(self.closed/'closure.json',closure);self.resign();self.output=self.root/'another-report'
        self.assertEqual('invalid',self.svg()['status'])

    def test_later_suite_or_session_and_foreign_calibration_are_rejected(self):
        (self.failed/'suite').mkdir();self.assertEqual('invalid',self.svg()['status']);(self.failed/'suite').rmdir()
        session=self.root/'s/failed';session.mkdir(parents=True);self.output=self.root/'second'
        self.assertEqual('invalid',self.svg()['status']);session.rmdir()
        config=read_json(self.config);config['domains']['svg']['stages'][0]['calibration_preparation']=str(self.root/'foreign')
        write_json(self.config,config);self.output=self.root/'third';stage=self.svg()
        self.assertEqual('invalid',stage['status']);self.assertEqual('wrong_preparation',stage['errors'][0]['code'])

    def test_named_record_mutation_and_missing_completion_are_detected(self):
        path=self.failed/'original'/self.private[0]['case_id']/'record.json'
        record=read_json(path);record['source_sha256']=digest('foreign starting source');write_json(path,record)
        stage=self.svg();self.assertEqual('invalid',stage['status']);self.assertEqual('source_changed',stage['errors'][0]['code'])
        path.unlink();self.output=self.root/'second';stage=self.svg()
        self.assertEqual('invalid',stage['status']);self.assertEqual('incomplete_matrix',stage['errors'][0]['code'])

    def test_report_receipt_binds_failure_inputs_and_never_claims_complete_matrix(self):
        value=rollup.build(self.config,self.output);failure=read_json(self.failed/'failure.json')
        failure['message']='different failure';write_json(self.failed/'failure.json',failure)
        with self.assertRaises(VisualError) as caught:rollup.validate_report(self.output)
        self.assertEqual('source_changed',caught.exception.code)
        self.assertFalse(value['full_success']);self.assertFalse(value['new_model_generation_claimed'])


class FailedReplayReportTests(unittest.TestCase):
    def setUp(self):
        from scripts.phase3d_v2_prepare import recipes
        from scripts.phase3d_v2_failed_replay_preparation import MESSAGE,run
        from kiroclops.local_engine.realistic_policy import input_history_check,replay_scenario
        self.tmp=tempfile.TemporaryDirectory(prefix='kc-replay-report-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.previous=self.root/'p/calibration';self.failed=self.root/'p/failed'
        self.closed=self.root/'a/closure';self.config=self.root/'inputs.json';self.output=self.root/'report'
        cases=recipes('panel');private=[c for c in cases if c['role']!='calibration'];self.private=private
        self.sources={'src/panel-motion.js':digest('parent')};self.source_sha=digest(self.sources)
        write_json(self.previous/'calibration.json',{'stage':'panel','model_started':False})
        write_json(self.previous/'recipes.json',cases)
        write_json(self.previous/'start-binding.json',{'stage':'panel','model_started':False,'recipes_sha256':digest(cases),
            'parent_source_sha256':self.source_sha,'original_source_sha256':digest('original')})
        write_json(self.failed/'failure.json',{'code':'input_history_mismatch','message':MESSAGE,
            'model_started':False,'previous_attempts_preserved':True})
        write_json(self.failed/'progress.json',{'phase':'analysis_tracking','model_started':False})
        for case in private[:8]:
            for role in ('reference','original','before') if case in private[:7] else ('reference','original'):
                record={'scenario':case['scenario'],'measurement_status':'ok',
                    'source_sha256':digest('original') if role=='original' else self.source_sha}
                if case==private[7]:
                    record['observations']={'inputs':{'provenance':'recorder_verified','events':[
                        {**event,'client_x':100.,'client_y':100.} for event in case['scenario']['events']]},
                        'objects':[{'frames':[{'time_s':0.},{'time_s':.0167},{'time_s':.0334}]}]}
                write_json(self.failed/role/case['case_id']/'record.json',record)
        scenario=private[7]['scenario'];reference=read_json(self.failed/'reference'/private[7]['case_id']/'record.json')
        execution,proof=replay_scenario(scenario,reference['observations']);self.case_root=self.failed/'before'/private[7]['case_id']
        attempts=[]
        from copy import deepcopy
        for index in (1,2):
            directory=self.case_root/f'attempt-{index:02d}';record=deepcopy(reference)
            record['observations']['inputs']['events'][-2]['time_s']-=.014
            record['observations']['inputs']['events'][-1]['time_s']+=.02
            record.update(execution_scenario=execution,input_replay=proof)
            history=input_history_check(reference['observations'],record['observations'])
            receipt={'attempt':index,'directory':str(directory),'record':record,'input_history':history,'status':'input_history_mismatch'}
            write_json(directory/'capture-attempt.json',receipt)
            write_json(directory/'capture-start.json',{'scenario':execution,'source_hashes':self.sources,'adapter_hashes':{}})
            attempts.append({'attempt':index,'directory':str(directory),'receipt_sha256':digest(receipt),
                'status':'input_history_mismatch','input_history':history})
        write_json(self.case_root/'capture-replay.json',{'status':'input_history_mismatch','attempts':attempts,
            'selected_attempt':None,'duration_rescaled':False})
        run(self.failed,self.previous,self.closed)
        write_json(self.config,{'kind':'Phase3DV2RollupInputs','schema_version':1,'domains':{'panel':{'stages':[
            {'stage':'panel','failed_preparation':str(self.closed),'calibration_preparation':str(self.previous)}]}}})

    def stage(self):return rollup.build(self.config,self.output)['domains']['panel']['stages'][0]

    def test_verified_two_actual_attempts_show_failed_comparability_not_behavior(self):
        with (patch.object(Path,'rglob',side_effect=AssertionError('No global scan')),
              patch.object(Path,'glob',side_effect=AssertionError('No discovery'))):
            value=rollup.build(self.config,self.output);rollup.validate_report(self.output)
        stage=value['domains']['panel']['stages'][0];failure=stage['failed_preparation']
        self.assertEqual(('fail','not_evaluated'),(stage['axes']['measurement']['status'],stage['axes']['behavior']['status']))
        self.assertEqual((7,17,23),tuple(failure[k] for k in ('completed_case_count','missing_case_count','completed_role_record_count')))
        self.assertEqual(2,len(failure['input_attempts']));self.assertFalse(stage['model_started'])
        self.assertFalse(value['full_success']);self.assertTrue(all(not p.endswith('.png') for p in value['consumed_file_sha256']))

    def test_changed_actual_input_is_rejected_even_when_attempt_receipt_rehashed(self):
        path=self.case_root/'attempt-02/capture-attempt.json';receipt=read_json(path)
        receipt['record']['observations']['inputs']['events'][-1]['time_s']+=.012;write_json(path,receipt)
        replay=read_json(self.case_root/'capture-replay.json');replay['attempts'][1]['receipt_sha256']=digest(receipt)
        write_json(self.case_root/'capture-replay.json',replay)
        stage=self.stage();self.assertEqual('invalid',stage['status']);self.assertEqual('wrong_input_history',stage['errors'][0]['code'])

    def test_missing_second_attempt_and_selected_attempt_do_not_form_closed_failure(self):
        path=self.case_root/'capture-replay.json';saved=read_json(path);changed=deepcopy(saved);changed['attempts'].pop()
        write_json(path,changed);self.assertEqual('invalid',self.stage()['status'])
        write_json(path,{**saved,'selected_attempt':1});self.output=self.root/'second'
        self.assertEqual('invalid',self.stage()['status'])

    def test_forged_physical_pointer_proof_is_not_verified_replay(self):
        path=self.case_root/'attempt-01/capture-attempt.json';receipt=read_json(path)
        receipt['record']['execution_scenario']['events'][-1]['replay_client_point']=[0.,0.];write_json(path,receipt)
        replay=read_json(self.case_root/'capture-replay.json');replay['attempts'][0]['receipt_sha256']=digest(receipt)
        write_json(self.case_root/'capture-replay.json',replay)
        stage=self.stage();self.assertEqual('invalid',stage['status']);self.assertEqual('source_changed',stage['errors'][0]['code'])


if __name__=='__main__':unittest.main()
