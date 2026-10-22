import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.ground_reporting import generate_ground_report, _ground_summary, _workflow_summary, _unpublished_calibration


class GroundReportTests(unittest.TestCase):
    def test_pending_report_is_offline_escaped_and_does_not_change_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            record = {'experiment_id':'ground-motion-v1','session_id':'</script><b>test</b>',
                      'candidates':[], 'reference':{},'baseline_runs':{},'final':None}
            path = session / 'session.json'
            path.write_text(json.dumps(record),encoding='utf-8')
            before = path.read_bytes()
            page = generate_ground_report(session).read_text(encoding='utf-8')
            self.assertEqual(path.read_bytes(), before)
            self.assertIn('Horizontal numerical: pending', page)
            self.assertIn('&lt;/script&gt;', page)
            self.assertNotIn('https://', page)
            self.assertNotIn('__SESSION__', page)
            self.assertTrue((session/'report/evidence/session.json').is_file())

    def test_outside_run_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)/'session'
            session.mkdir()
            record={'experiment_id':'ground-motion-v1','reference':{'calibration':[str(session.parent)]}}
            (session/'session.json').write_text(json.dumps(record),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'escaped'):
                generate_ground_report(session)

    def test_selected_missing_candidate_never_substitutes_another_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            record={'experiment_id':'ground-motion-v1','selected_candidate_id':'candidate-02',
                    'candidates':[{'candidate_id':'candidate-01','status':'evaluated'}]}
            (session/'session.json').write_text(json.dumps(record),encoding='utf-8')
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertIn('Candidate shown: none',page)

    def test_source_scope_verification_and_receipt_are_visible_and_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            (session/'session.json').write_text(json.dumps({'experiment_id':'ground-motion-v1'}),encoding='utf-8')
            receipt=session/'final/source-review.json'
            receipt.parent.mkdir()
            receipt.write_text('{"synthetic":"operator decision"}',encoding='utf-8')
            raw=receipt.read_bytes()
            with patch('kiroclops.integration.ground_reporting.verify_source_review',
                       return_value={'status':'pending','reason':'<independent review required>'}):
                page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertIn('Source scope: pending',page)
            self.assertIn('evidence/final/source-review.json',page)
            self.assertIn('&lt;independent review required&gt;',page)
            self.assertEqual((session/'report/evidence/final/source-review.json').read_bytes(),raw)
            self.assertEqual(receipt.read_bytes(),raw)

    def test_ground_summary_shows_all_18_saved_rows_and_six_reverse_failures(self):
        rows=[]
        for sid in ('r18','l30','reverse24'):
            for offset in (0,24):
                for repeat in (1,2,3):
                    passed=sid!='reverse24'
                    rows.append({'scenario':{'id':sid,'spawn_offset':offset,'repeat':repeat},
                        'pass':passed,'after_rmse_body':.12345,'improvement_fraction':.75,
                        'speed_eligible':True,'speed_relative_error':.105,'stop_distance_error_body':.0333,
                        'end_position_error_body':.1444,'gates':{'speed':passed,'end_position':True}})
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            (session/'session.json').write_text(json.dumps({'experiment_id':'ground-motion-v1'}),encoding='utf-8')
            path=session/'final/comparison.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'ground':{'scenario_checks':rows}}),encoding='utf-8')
            original=path.read_bytes()
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(page.count('<tr data-ground-status="pass">'),12)
            self.assertEqual(page.count('<tr data-ground-status="fail">'),6)
            self.assertIn('<b>12 / 18</b> passed · <b>6</b> did not pass · <b>0</b> pending',page)
            for value in ('0.1235','75.0%','10.5%','0.0333','0.1444','Did not pass · speed','reverse24 · x24 · repeat 3'):
                self.assertIn(value,page)

    def test_missing_ground_rows_and_verdicts_remain_pending(self):
        page=_ground_summary({})
        self.assertEqual(page.count('<tr data-ground-status="pending">'),18)
        self.assertIn('<b>0 / 18</b> passed · <b>0</b> did not pass · <b>18</b> pending',page)
        entry={'scenario':{'id':'r18','spawn_offset':0,'repeat':1},'after_rmse_body':0,
               'speed_eligible':False,'gates':{'curve_rmse':True}}
        page=_ground_summary({'scenario_checks':[entry]})
        self.assertEqual(page.count('<tr data-ground-status="pending">'),18)
        self.assertIn('Not applicable',page)
        self.assertIn('0.0000',page)

    def test_ground_summary_does_not_recompute_verdict_and_escapes_gate_labels(self):
        row={'scenario':{'id':'reverse24','spawn_offset':0,'repeat':1},'pass':False,
             'after_rmse_body':0,'gates':{'<script>unexpected</script>':False}}
        page=_ground_summary({'scenario_checks':[row]})
        self.assertEqual(page.count('<tr data-ground-status="fail">'),1)
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;unexpected&lt;/script&gt;',page)
        duplicate=_ground_summary({'scenario_checks':[row,row]})
        self.assertIn('Pending · duplicate entry',duplicate)
        self.assertEqual(duplicate.count('<tr data-ground-status="pending">'),18)

    def test_timeout_with_published_numeric_pass_stays_interrupted_and_diagnostics_are_not_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            records={'session.json':{'experiment_id':'ground-motion-v1','status':'closed_failed',
                     'selected_candidate_id':'candidate-02','active_job_id':None,
                     'final':{'numerical_status':'pass','measurement_status':'pending'}},
                     'kiro/launch.json':{'surface':'cli','status':'timeout','elapsed_seconds':1800.187},
                     'jobs/job-final.json':{'job_id':'job-final','candidate_id':'candidate-02','action':'finalize','status':'interrupted'},
                     'final/comparison.json':{'ground':{'numerical_status':'pass'}},
                     'review/source-review-draft.json':{'checks':{'ground_only':{'status':'fail'}}},
                     'review/raw-observations.json':{'source':'assistant_visual','points':[]}}
            for name,record in records.items():
                path=session/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(record),encoding='utf-8')
            before={name:(session/name).read_bytes() for name in records}
            with patch('kiroclops.integration.ground_reporting.verify_source_review',return_value={'status':'pending'}):
                page=generate_ground_report(session).read_text(encoding='utf-8')
            for text in ('Horizontal numerical: pass','Workflow interrupted','closed_failed','timeout','1800.187 seconds',
                         'Absent; the normal end of the model turn was not recorded','job-final: interrupted',
                         'Diagnostic review documents · not an acceptance receipt','Source scope: pending'):
                self.assertIn(text,page)
            for name in ('source-review-draft.json','raw-observations.json'):
                self.assertIn('evidence/review/'+name,page)
                self.assertEqual((session/'report/evidence/review'/name).read_bytes(),before['review/'+name])
            self.assertEqual({name:(session/name).read_bytes() for name in records},before)

    def test_finished_publication_with_running_final_job_is_not_completed_workflow(self):
        state={'status':'finished','selected_candidate_id':'candidate-01','active_job_id':'job-final'}
        launch={'surface':'cli','status':'timeout'}
        jobs=[{'job_id':'job-final','action':'finalize','candidate_id':'candidate-01','status':'running'}]
        page=_workflow_summary(state,launch,jobs)
        self.assertIn('Workflow interrupted',page)
        self.assertIn('job-final: running',page)
        self.assertIn('numerical PASS',page)
        normal=_workflow_summary({**state,'active_job_id':None},
            {'surface':'cli','status':'completed','prompt_result':{'stopReason':'end_turn'}},
            [{**jobs[0],'status':'succeeded'}])
        self.assertIn('The closing of the candidate finalization job was recorded.',normal)
        self.assertIn('CLI end_turn record:</b> Present',normal)

    def test_nested_diagnostic_receipts_preserve_relative_paths_without_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            (session/'session.json').write_text(json.dumps({'experiment_id':'ground-motion-v1'}),encoding='utf-8')
            contents={'ground-independent/observations-receipt.json':{'source':'ground visual'},
                      'jump-independent/observations-receipt.json':{'source':'jump visual'},
                      'ground-independent/diagnostic-visual-comparison.json':{'status':'diagnostic_only'}}
            for relative,record in contents.items():
                path=session/'review'/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(record),encoding='utf-8')
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertIn('Diagnostic review documents · not an acceptance receipt',page)
            for relative in contents:
                self.assertIn(f'href="evidence/review/{relative}"',page)
                self.assertIn(f'>{relative}</a>',page)
                self.assertEqual((session/'report/evidence/review'/relative).read_bytes(),
                                 (session/'review'/relative).read_bytes())

    def test_existing_ide_operator_observations_are_copied_verbatim_without_acceptance_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            (session/'session.json').write_text(json.dumps({'experiment_id':'ground-motion-v1','final':None}),encoding='utf-8')
            names=['ide/settings.png','ide/settings-after.png','ide/permission-power.png',
                   'ide/permission-power-allowed.png','ide/permission-resolution.json',
                   'ide/power-install.json','ide/permission-wait.json','ide/operator-stop.json','ide/stopped.png','kiro/launch-start.json']
            content={name:('synthetic operator fixture: '+name).encode('utf-8') for name in names}
            for name,raw in content.items():
                path=session/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
            with patch('kiroclops.integration.ground_reporting.verify_source_review',return_value={'status':'pending'}):
                page=generate_ground_report(session).read_text(encoding='utf-8')
            for name,raw in content.items():
                self.assertIn(f'href="evidence/{name}"',page)
                self.assertEqual((session/'report/evidence'/name).read_bytes(),raw)
                self.assertEqual((session/name).read_bytes(),raw)
            self.assertIn('Operator settings and permission observations',page)
            self.assertIn('do not count as acceptance evidence on their own',page)
            self.assertIn('Horizontal numerical: pending',page)
            self.assertIn('Source scope: pending',page)

    def test_absent_optional_ide_observations_do_not_create_missing_evidence_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            (session/'session.json').write_text(json.dumps({'experiment_id':'ground-motion-v1','final':None}),encoding='utf-8')
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertNotIn('Missing evidence:',page)
            self.assertNotIn('Operator settings and permission observations',page)
            self.assertNotIn('__OPERATOR_OBSERVATIONS__',page)
            self.assertFalse((session/'report/evidence/ide').exists())

    def unpublished_fixture(self, session):
        import cv2
        import numpy as np
        folder=session/'candidates/candidate-01/runs/calibration/r06-x0-r1'
        (folder/'frames').mkdir(parents=True)
        scenario={'id':'r06','split':'calibration','repeat':1,'spawn_offset':0,
                  'frame_count':240,'warmup_ticks':60,'events':[
                      {'tick':30,'action':'right','pressed':True},{'tick':36,'action':'right','pressed':False}]}
        frames=[{'frame_index':i,'physics_tick':60+i,'time_s':i/60,
                 'actions':['right'] if 30<=i<36 else []} for i in range(240)]
        hashes={'source/characters/player/player.gd':'synthetic source digest'}
        candidate={'candidate_id':'candidate-01','status':'failed','calibration_runs':[], 'source_hashes':hashes}
        manifest={'schema_version':2,'mechanic':'ground_horizontal','run_id':folder.name,'game':'slay',
                  'variant':'after','status':'ok','fps':60,'viewport':[640,360],'frame_count':240,
                  'warmup_ticks':60,'normal_level':False,'settled':True,'scenario':scenario,'frames':frames,
                  'provenance':{'full_source_hashes_before':hashes,'full_source_hashes_after':hashes,
                                'project':str(session/'candidates/candidate-01/project')}}
        (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        analysis={'mechanic':'ground','status':'ok','run_id':str(folder),'game':'slay','variant':'after',
                  'scenario':scenario,'fps':60,'frames':frames,'provenance':{
                      'manifest_sha256':hashlib.sha256((folder/'manifest.json').read_bytes()).hexdigest()}}
        (folder/'analysis.json').write_text(json.dumps(analysis),encoding='utf-8')
        (folder/'video.mp4').write_bytes(b'synthetic test video')
        image=cv2.imencode('.png',np.zeros((360,640,3),dtype=np.uint8))[1].tobytes()
        for i in range(240):
            (folder/f'frames/{i:06d}.png').write_bytes(image)
        state={'experiment_id':'ground-motion-v1','status':'closed_failed','selected_candidate_id':None,
               'active_job_id':None,'final':None,'candidates':[candidate]}
        (session/'session.json').write_text(json.dumps(state),encoding='utf-8')
        return folder,candidate,state

    def test_complete_unpublished_calibration_is_separate_diagnostic_media_without_result_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            folder,candidate,state=self.unpublished_fixture(session)
            before={p:p.read_bytes() for p in session.rglob('*') if p.is_file()}
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertEqual(page.count('data-unpublished-calibration="true"'),1)
            self.assertIn('Unpublished calibration · not acceptance evidence',page)
            self.assertIn('Horizontal numerical: pending',page)
            self.assertEqual(page.count('<tr data-ground-status="pending">'),18)
            data=json.loads(page.split('<script id="data" type="application/json">')[1].split('</script>')[0])
            self.assertEqual(data,{'runs':[],'comparison':{}})
            for name in ('video.mp4','manifest.json','analysis.json'):
                dest=session/'report/assets/unpublished/candidate-01/r06-x0-r1'/name
                self.assertEqual(dest.read_bytes(),(folder/name).read_bytes())
            self.assertEqual({p:p.read_bytes() for p in before},before)
            self.assertFalse((session/'final').exists())
            self.assertFalse((session/'report/assets/unpublished/candidate-01/r06-x0-r1/frames').exists())

    def test_malformed_incomplete_or_unbound_unpublished_run_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            folder,candidate,_=self.unpublished_fixture(session)
            self.assertIsNotNone(_unpublished_calibration(folder,candidate,session))
            changes=[('manifest.json',b'not JSON'),('analysis.json',b'[]'),('video.mp4',b''),
                     ('frames/000239.png',None),('frames/000100.png',b'corrupt PNG')]
            for relative,replacement in changes:
                with self.subTest(relative=relative):
                    path=folder/relative; original=path.read_bytes()
                    if replacement is None: path.unlink()
                    else: path.write_bytes(replacement)
                    self.assertIsNone(_unpublished_calibration(folder,candidate,session))
                    path.write_bytes(original)
            original=(folder/'manifest.json').read_bytes()
            manifest=json.loads(original); manifest['frames'][45]['physics_tick']+=1
            (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            self.assertIsNone(_unpublished_calibration(folder,candidate,session))
            (folder/'manifest.json').write_bytes(original)
            malformed=json.loads(original); malformed['provenance']=['invalid object']
            (folder/'manifest.json').write_text(json.dumps(malformed),encoding='utf-8')
            self.assertIsNone(_unpublished_calibration(folder,candidate,session))
            (folder/'manifest.json').write_bytes(original)
            self.assertIsNone(_unpublished_calibration(folder,{**candidate,'source_hashes':{'wrong':'source'}},session))
            analysis_path=folder/'analysis.json'
            analysis=json.loads(analysis_path.read_text(encoding='utf-8'))
            analysis['provenance']['manifest_sha256']='changed'
            analysis_path.write_text(json.dumps(analysis),encoding='utf-8')
            page=generate_ground_report(session).read_text(encoding='utf-8')
            self.assertNotIn('data-unpublished-calibration="true"',page)
            self.assertIn('Horizontal numerical: pending',page)

    def test_unpublished_fallback_never_discovers_validation_active_or_selected_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=Path(tmp)
            folder,candidate,state=self.unpublished_fixture(session)
            for changed in ({**state,'active_job_id':'job-running'},
                            {**state,'selected_candidate_id':'candidate-01'},
                            {**state,'candidates':[{**candidate,'status':'evaluating'}]}):
                (session/'session.json').write_text(json.dumps(changed),encoding='utf-8')
                with patch('kiroclops.integration.ground_reporting._unpublished_calibration') as reader:
                    page=generate_ground_report(session).read_text(encoding='utf-8')
                    reader.assert_not_called()
                self.assertNotIn('data-unpublished-calibration="true"',page)
            (session/'session.json').write_text(json.dumps(state),encoding='utf-8')
            with patch('kiroclops.integration.ground_reporting._unpublished_calibration',return_value=None) as reader:
                generate_ground_report(session)
            self.assertEqual(reader.call_count,6)
            self.assertTrue(all(call.args[0].parent==folder.parent for call in reader.call_args_list))
            self.assertEqual({call.args[0].name for call in reader.call_args_list},
                             {f'{sid}-x0-r{repeat}' for sid in ('r06','r48') for repeat in (1,2,3)})
