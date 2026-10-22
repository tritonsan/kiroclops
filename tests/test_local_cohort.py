from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.cohort import register, validate_qualification, verify_registration, summarize, _final_error
from kiroclops.visual.schema import VisualError, read_json, write_json


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.baseline=self.root/'game';self.baseline.mkdir();(self.baseline/'player.gd').write_text('extends CharacterBody2D')
        rows=[]
        for domain in ('game','panel','svg'):
            for i in range(6):rows.append({'id':f'{domain}-{i}','domain':domain,'expected_class':'correct' if i<2 else 'behavioral_deviation',
                'classification_pass':True,'independent_review_status':'pass'})
        rows.extend({'id':f'error-{i}','domain':'fault','expected_class':'tracking_failure','classification_pass':True,'independent_review_status':'pass'} for i in range(6))
        self.q=self.root/'q.json';write_json(self.q,{'status':'pass','independent_review_status':'pass','samples':rows,'source_hashes':{}})
        self.p1=patch('kiroclops.local_engine.cohort.protocol_sources',return_value={'a.py':'hash'});self.p1.start()
        self.p2=patch('kiroclops.local_engine.cohort.reference_sources',return_value={'ref.png':'hash'});self.p2.start()
        # These tests exercise registration mechanics. Actual evidence validation
        # has its own pixel-bound fixtures; fake status rows are never qualified.
        self.p3=patch('kiroclops.local_engine.cohort.validate_qualification',side_effect=lambda p:read_json(p));self.p3.start()
        self.p4=patch('kiroclops.local_engine.cohort.web_reference_binding',return_value={'manifest':'shared-reference.json','manifest_sha256':'shared-pixels'});self.p4.start()

    def tearDown(self):self.p4.stop();self.p3.stop();self.p2.stop();self.p1.stop();self.temp.cleanup()

    def test_eighteen_trials_twentyfour_stage_slots_no_generation(self):
        folder=self.root/'registered';result=register(folder,self.q,game_baseline=self.baseline)
        self.assertEqual(18,len(result['rows']));self.assertEqual(24,len(result['sessions']))
        self.assertFalse((folder/'sessions').exists())
        for d in ('game','panel','svg'):
            for m in ('tools','baseline'):self.assertEqual(3,len([r for r in result['rows'] if r['domain']==d and r['mode']==m]))
        verified=verify_registration(folder/'registration.json');self.assertEqual('max',verified['effort'])
        result=summarize(folder/'registration.json');self.assertTrue(all(g['pending_trials']==3 for g in result['groups']))

    def test_numerical_qualification_never_bypasses_independent_review(self):
        q=read_json(self.q);q['samples'][0]['independent_review_status']='pending';write_json(self.q,q)
        with self.assertRaises(VisualError) as c:validate_qualification(self.q)
        self.assertEqual('not_qualified',c.exception.code)

    def test_six_fault_controls_required_in_addition_to_behavior(self):
        q=read_json(self.q)
        for row in q['samples'][-6:]:row['expected_class']='correct'
        write_json(self.q,q)
        with self.assertRaises(VisualError):validate_qualification(self.q)

    def test_source_and_qualification_drift_reject_launch(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        with patch('kiroclops.local_engine.cohort.protocol_sources',return_value={'a.py':'changed'}):
            with self.assertRaises(VisualError) as c:verify_registration(folder/'registration.json')
            self.assertEqual('protocol_changed',c.exception.code)
        q=read_json(self.q);q['new']='changed';write_json(self.q,q)
        with self.assertRaises(VisualError) as c:verify_registration(folder/'registration.json')
        self.assertEqual('qualification_changed',c.exception.code)

    def test_structural_entry_cannot_hide_mutation_before_native_prompt(self):
        from kiroclops.local_engine import launcher
        folder=self.root/'registered';registry=register(folder,self.q,game_baseline=self.baseline)
        registration=folder/'registration.json'
        self.assertEqual(registry,verify_registration(registration,full=False))
        q=read_json(self.q);q['new']='tampered after structural check';write_json(self.q,q)
        with patch('kiroclops.local_engine.launcher._run_native') as native:
            with self.assertRaises(VisualError) as error:
                launcher.run(registry['sessions'][0],registration)
        self.assertEqual('qualification_changed',error.exception.code)
        native.assert_not_called()

    def test_structural_entry_rejects_modified_progress_and_trial_denominator(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        registration=folder/'registration.json'
        progress=folder/'progress.json';value=read_json(progress)
        value['rows'].pop();write_json(progress,value)
        with self.assertRaises(VisualError) as error:
            verify_registration(registration,full=False)
        self.assertEqual('invalid_registration',error.exception.code)

    def test_registration_is_not_overwritten(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        with self.assertRaises(VisualError):register(folder,self.q,game_baseline=self.baseline)

    def test_status_only_qualification_never_qualifies(self):
        with self.assertRaises(VisualError) as c:validate_qualification(self.q)
        self.assertEqual('not_qualified',c.exception.code)

    def test_direct_verify_detects_registration_edit(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        path=folder/'registration.json';value=read_json(path);value['goals']['panel']='coached';write_json(path,value)
        with self.assertRaises(VisualError) as c:verify_registration(path)
        self.assertEqual('invalid_registration',c.exception.code)

    def test_shared_web_recordings_are_bound_for_both_arms_and_cannot_change(self):
        folder=self.root/'registered';registry=register(folder,self.q,game_baseline=self.baseline)
        self.assertEqual('shared-pixels',registry['web_reference_bundle']['manifest_sha256'])
        with patch('kiroclops.local_engine.cohort.web_reference_binding',return_value={'manifest':'shared-reference.json','manifest_sha256':'other-pixels'}):
            with self.assertRaises(VisualError) as error:verify_registration(folder/'registration.json')
        self.assertEqual('reference_changed',error.exception.code)

    def test_both_browser_arms_prepare_the_same_frozen_reference_binding(self):
        from kiroclops.local_engine.cohort import _prepare_stage
        registry=register(self.root/'registered',self.q,game_baseline=self.baseline)
        for domain in ('panel','svg'):
            captured=[]
            with patch('kiroclops.local_engine.cohort.create_session',side_effect=lambda *a,**kw:captured.append((a,kw))),patch('kiroclops.local_engine.cohort.configure'):
                for mode in ('tools','baseline'):
                    row=next(r for r in registry['rows'] if r['domain']==domain and r['mode']==mode)
                    _prepare_stage(registry,row,row['stages'][0])
            self.assertEqual(captured[0][0][2:5],captured[1][0][2:5])
            self.assertEqual(captured[0][1]['experiment'],captured[1][1]['experiment'])
            experiment=captured[0][1]['experiment']
            self.assertEqual('shared-reference.json',experiment['web_reference_bundle'])
            self.assertEqual('shared-pixels',experiment['web_reference_bundle_sha256'])

    def test_progress_cannot_drop_a_failed_trial_from_denominator(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        path=folder/'progress.json';value=read_json(path);value['rows'].pop();write_json(path,value)
        with self.assertRaises(VisualError):summarize(folder/'registration.json')

    def test_completed_boolean_without_actual_native_session_does_not_count(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        path=folder/'progress.json';value=read_json(path);row=value['rows'][0];row['status']='completed'
        for stage in row['stages']:stage.update(status='completed',numerical_pass=True,native_validation_status='pass')
        write_json(path,value);summary=summarize(folder/'registration.json')
        group=next(g for g in summary['groups'] if g['domain']==row['domain'] and g['mode']==row['mode'])
        self.assertEqual(0,group['numerical_successes']);self.assertEqual(1,group['failed_trials'])

    def test_missing_errors_are_not_fabricated_as_zero_or_verified_success(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        summary=summarize(folder/'registration.json')
        self.assertTrue(all(g['median_final_error'] is None and g['evaluated_stage_count']==0 for g in summary['groups']))
        self.assertTrue(all(g['independently_verified_successes']==0 and g['observed_operator_coaching_count'] is None for g in summary['groups']))
        self.assertEqual('pending',summary['integration_status']);self.assertEqual('pending',summary['incremental_benefit_status'])
        self.assertTrue(all(c['status']=='pending' for c in summary['domain_comparisons']))

    def test_final_error_keeps_actual_game_and_web_values(self):
        self.assertEqual(.12,_final_error({'comparison':{'mean_rmse_body':.12}}))
        self.assertAlmostEqual(.025,_final_error({'comparisons':[{'score':.01},{'score':.04}]}))
        self.assertIsNone(_final_error({'pass':False,'error':'capture failed'}))

    def test_summary_rejects_registered_source_drift(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        with patch('kiroclops.local_engine.cohort.protocol_sources',return_value={'a.py':'changed'}):
            with self.assertRaises(VisualError) as exc:summarize(folder/'registration.json')
        self.assertEqual('protocol_changed',exc.exception.code)

    def test_result_source_mismatch_cannot_count_as_numerical_success(self):
        folder=self.root/'registered';register(folder,self.q,game_baseline=self.baseline)
        path=folder/'progress.json';value=read_json(path)
        row=next(r for r in value['rows'] if r['domain']=='panel' and r['mode']=='tools')
        row['status']='completed';stage=row['stages'][0]
        stage.update(status='completed',numerical_pass=True,native_validation_status='pass')
        session=Path(stage['session']);session.mkdir(parents=True);write_json(session/'session.json',{})
        write_json(path,value)
        with patch('kiroclops.local_engine.cohort._stage_result',side_effect=VisualError('wrong_result','Selected source mismatch')):
            with patch('kiroclops.local_engine.cohort.Session') as fake:
                fake.return_value.inspect.return_value={'final':{'pass':True,'job_id':'forged'}}
                fake.return_value.get_job.return_value={'status':'succeeded','result':{'pass':True}}
                with patch('kiroclops.local_engine.review.audit_native_run',return_value={'status':'pass'}):
                    result=summarize(folder/'registration.json')
        group=next(g for g in result['groups'] if g['domain']=='panel' and g['mode']=='tools')
        self.assertEqual(0,group['numerical_successes']);self.assertEqual(1,group['failed_trials'])
        self.assertTrue(any(e['code']=='wrong_result' for e in result['evidence_replay_errors']))


if __name__=='__main__':unittest.main()
