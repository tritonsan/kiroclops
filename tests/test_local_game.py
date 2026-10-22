import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

import cv2
import numpy as np

from kiroclops.local_engine import game
from kiroclops.visual.schema import VisualError,read_json,write_json


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def raw_jump(self):
        run=self.root/'raw';(run/'frames').mkdir(parents=True)
        rng=np.random.default_rng(732)
        body=rng.integers(10,240,(7,6,3),dtype=np.uint8)
        background=rng.integers(10,240,(12,14,3),dtype=np.uint8)
        frames=[]
        for i in range(240):
            y=70-(min(i-30,50-i) if 30<=i<=50 else 0)
            image=np.zeros((100,140,3),np.uint8)
            image[y:y+7,60:66]=body;image[80:92,10:24]=background
            (run/'frames'/f'{i:06d}.png').write_bytes(cv2.imencode('.png',image)[1].tobytes())
            frames.append({'frame_index':i,'physics_tick':60+i,'time_s':i/60,'actions':['jump'] if 30<=i<33 else []})
        manifest={'schema_version':1,'game':'tiny','variant':'reference','status':'ok','fps':60,
            'viewport':[140,100],'frame_count':240,'frames':frames,
            'scenario':{'id':'h03','hold_ticks':3,'start_tick':30,'repeat':1,'spawn_offset':0,'split':'calibration'}}
        write_json(run/'manifest.json',manifest)
        annotation=self.root/'annotation.json'
        write_json(annotation,{'template_run':str(run),'template_frame':0,'roi':[60,70,6,7],
            'anchor_offset':[2,3],'body_height_px':7,'background_rois':[[10,80,14,12]],'min_score':.75,'min_margin':.03})
        return run,annotation

    def test_common_pixel_replay_preserves_known_jump_measurement_and_raw_source(self):
        run,annotation=self.raw_jump()
        original=(run/'manifest.json').read_bytes()
        with patch('kiroclops.analysis.analyze_run',side_effect=AssertionError('old tracker forbidden')):
            record=game.analyze_game_run(run,'jump',self.root/'new',annotation=annotation)
        measured=read_json(record['analysis'])
        self.assertEqual(record['measurement_status'],'ok')
        self.assertEqual(measured['valid_ratio'],1)
        self.assertEqual(measured['events'],{'takeoff_frame':30,'apex_frame':40,'landing_frame':50})
        self.assertAlmostEqual(measured['metrics']['height_body'],10/7)
        self.assertEqual(measured['annotation_validation']['status'],'pending')
        self.assertEqual((run/'manifest.json').read_bytes(),original)
        self.assertFalse((run/'analysis.json').exists())
        self.assertTrue(measured['provenance']['domain_math_reuse_only'])

    def test_replay_refuses_writes_into_original_raw_tree(self):
        run,annotation=self.raw_jump()
        with self.assertRaisesRegex(VisualError,'must not mutate'):
            game.analyze_game_run(run,'jump',run/'analysis',annotation=annotation)

    def test_projection_rejects_native_clock_mismatch_and_missing_camera(self):
        run,annotation=self.raw_jump()
        result=game.analyze_game_run(run,'jump',self.root/'good',annotation=annotation)
        obs=read_json(self.root/'good/observations.json');manifest=read_json(run/'manifest.json')
        bad=copy.deepcopy(manifest);bad['frames'][100]['physics_tick']=99
        with self.assertRaises(VisualError):game.observation_to_motion(obs,bad,read_json(annotation),'jump')
        obs['camera'][0]['status']='unverified'
        projection=game.observation_to_motion(obs,manifest,read_json(annotation),'jump')
        self.assertEqual(projection['status'],'rejected')

    def test_ground_projection_uses_shared_coordinates_and_keeps_invalid_gap(self):
        from kiroclops.portable_v2.specs import scenarios
        scenario=scenarios('ground','calibration')[0]
        metadata=[];frames=[];camera=[]
        for i in range(240):
            metadata.append({'frame_index':i,'physics_tick':i+60,'time_s':i/60,'actions':[]})
            frames.append({'index':i,'time_s':i/60,'valid':True,'x':10+max(0,min(i-30,6)),
                           'y':20,'score':1.,'margin':.2,'mirrored':False})
            camera.append({'status':'pass','max_shift_px':0})
        obs={'objects':[{'frames':frames}],'camera':camera,'package_sha256':'a'*64}
        manifest={'fps':60,'frames':metadata,'scenario':scenario,'game':'heart','variant':'before'}
        with patch('kiroclops.ground.analysis.analyze_run',side_effect=AssertionError('old tracker forbidden')):
            result=game.observation_to_motion(obs,manifest,{'body_height_px':16},'ground')
        self.assertEqual(result['status'],'ok')
        self.assertAlmostEqual(result['metrics']['end_position_body'],6/16)
        obs['objects'][0]['frames'][22].update(valid=False,x=None,y=None)
        self.assertEqual(game.observation_to_motion(obs,manifest,{'body_height_px':16},'ground')['status'],'rejected')

    def job_context(self,mode='tools'):
        # These tests isolate native dispatch; actual cache tamper rejection is
        # exercised by test_local_prepared_evidence with durable job receipts.
        cache_patch=patch('kiroclops.local_engine.prepared_evidence.load_prepared',
            side_effect=lambda root,state:read_json(Path(root)/'prepared-private.json'))
        cache_patch.start();self.addCleanup(cache_patch.stop)
        prepared={'stage':'jump','reference':str(self.root/'reference.json'),'before':str(self.root/'before.json'),'source_bindings':{}}
        write_json(self.root/'prepared-private.json',prepared)
        state={'domain':'game','mode':mode,'baseline':str(self.root/'baseline'),'target':str(self.root/'target'),
               'experiment':{'stage':'jump'},'selected_candidate_id':None,'sealed_at':None}
        job={'job_id':'job-example','operation':'evaluate','candidate_id':'candidate-01','source_path':str(self.root/'candidate')}
        return state,job

    def test_unsealed_job_never_opens_heldout(self):
        state,job=self.job_context();job['operation']='finalize'
        with patch.object(game,'_reference') as reference,patch.object(game,'_capture') as capture:
            with self.assertRaisesRegex(VisualError,'sealed candidate'):
                game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        reference.assert_not_called();capture.assert_not_called()

    def test_baseline_evaluation_is_raw_only_and_has_no_metric_hint(self):
        state,job=self.job_context('baseline')
        with patch.object(game,'_capture',return_value=[]) as capture,patch.object(game,'_export',return_value=[]),patch.object(game,'_preservation') as preserve:
            result=game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        self.assertTrue(result['neutral_capture']);self.assertIsNone(result['score'])
        self.assertFalse(capture.call_args.kwargs['analyze']);preserve.assert_not_called()
        self.assertNotIn('diagnostics',result)

    def test_valid_preservation_failure_skips_motion_without_fake_score(self):
        state,job=self.job_context()
        with patch.object(game,'_preservation',return_value={'status':'fail','failed_cases':1}),patch.object(game,'_capture') as capture:
            result=game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        self.assertFalse(result['eligible']);self.assertIsNone(result['score'])
        self.assertEqual(result['motion_status'],'not_run');capture.assert_not_called()

    def test_changed_prepared_source_refuses_more_work(self):
        state,job=self.job_context()
        prepared=read_json(self.root/'prepared-private.json');prepared['source_bindings']={str(self.root/'baseline'):{'player.gd':'old'}}
        write_json(self.root/'prepared-private.json',prepared)
        with patch.object(game.runtime,'source_hashes',return_value={'player.gd':'new'}),patch.object(game,'_preservation') as preserve:
            with self.assertRaisesRegex(VisualError,'starting-source binding'):
                game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        preserve.assert_not_called()

    def test_native_cancellation_accepts_session_callback_and_keeps_deadline(self):
        cancelled=[False];checks=[]
        event=game._Cancellation(lambda:cancelled[0],lambda:checks.append(True))
        self.assertFalse(event.is_set());cancelled[0]=True
        self.assertTrue(event.is_set());self.assertEqual(len(checks),2)
        with self.assertRaisesRegex(VisualError,'expired'):
            game._Cancellation(None,lambda:(_ for _ in ()).throw(VisualError('budget_exhausted','expired'))).is_set()

    def test_import_sidecars_are_created_only_in_disposable_capture_copy(self):
        project=self.root/'sealed';project.mkdir();(project/'player.gd').write_text('unchanged')
        native=[]
        def importing(path,*args,**kwargs):
            self.assertNotEqual(path,project);(path/'new.uid').write_text('generated')
        def capture(path,raw,*args,**kwargs):
            native.append(path);self.assertTrue((path/'new.uid').is_file())
        # This test isolates disposable imports; the record contract is exercised
        # with complete recipes and input receipts in test_local_game_scenario_envelope.
        with patch.object(game.runtime,'import_project',side_effect=importing),patch.object(game.runtime,'capture_project',side_effect=capture),\
             patch.object(game,'scenarios',return_value=[{'id':'h03','repeat':1,'spawn_offset':0}]),patch.object(game,'run_name',return_value='h03'),\
             patch.object(game,'analyze_game_run',return_value={'measurement_status':'ok'}),\
             patch.object(game,'_canonical_record',side_effect=lambda record,*args:record):
            rows=game._capture(project,'jump','validation',self.root/'capture','after',lambda:None,lambda *a:None,None)
        self.assertEqual(len(rows),1);self.assertEqual(native,[self.root/'capture/project'])
        self.assertEqual([p.name for p in project.iterdir()],['player.gd'])

    def test_selected_visual_wall_runs_eight_cases_and_binds_readable_records(self):
        from kiroclops.local_engine import visual_boundary_v4 as boundary
        project=self.root/'selected';project.mkdir();(project/'player.gd').write_text('selected source')
        originals=game.runtime.source_hashes(project);rendered=[]
        observations={'status':'ok','objects':[{'id':'character','frames':[]}]}
        selection={'objects':[{'roi':[100,140,10,6],'anchor':[4.5,4]}]}
        def render(path,output,scenario,cancel):
            self.assertEqual((path/'player.gd').read_text(),'selected source')
            self.assertNotEqual(path,project);rendered.append(scenario['id'])
        expected={'approach':'visible_approach','parallel':'visible_parallel_motion','rebound':'visible_departure_after_approach','near_miss':'distant_motion'}
        with ExitStack() as stack:
            stack.enter_context(patch.object(game.runtime,'import_project'))
            stack.enter_context(patch.object(boundary,'_render',side_effect=render))
            stack.enter_context(patch.object(boundary,'fixture_validity',return_value={'status':'pass'}))
            lease=stack.enter_context(patch.object(boundary,'_lock',side_effect=AssertionError('session already holds lease')))
            stack.enter_context(patch.object(game,'import_legacy_run'))
            stack.enter_context(patch.object(game,'selection_from_legacy',side_effect=lambda *a:copy.deepcopy(selection)))
            stack.enter_context(patch('kiroclops.visual.package.read_image',return_value=np.zeros((180,320,3),np.uint8)))
            stack.enter_context(patch.object(boundary,'visible_wall',return_value={'id':'wall','points':[[79,60],[79,158]]}))
            stack.enter_context(patch.object(game,'analyze_package',return_value=observations))
            stack.enter_context(patch('kiroclops.visual.events.extract_events',return_value={'events':[],'relations':[]}))
            stack.enter_context(patch.object(boundary,'classify',side_effect=[{'status':'observed','classification':expected[s['kind']]} for s in boundary.cases()]))
            result=game._visual_wall(project,self.root/'boundary','candidate-02',lambda:None,lambda *a:None,game._Cancellation(None,lambda:None))
        self.assertEqual(result['status'],'pass');self.assertEqual(result['passed'],8)
        self.assertEqual(rendered,[s['id'] for s in boundary.cases()]);lease.assert_not_called()
        self.assertEqual(game.runtime.source_hashes(project),originals)
        records=read_json(result['records_path']);self.assertEqual(len(records),8)
        for record in records:
            self.assertEqual(record['source_hashes'],originals);self.assertEqual(record['candidate_id'],'candidate-02')
            self.assertTrue(Path(record['observations']).is_file());self.assertTrue(Path(record['events']).is_file())
        self.assertFalse(result['physics_collision_inferred']);self.assertEqual(result['independent_review_status'],'pending')

    def test_final_motion_pass_cannot_hide_failed_visual_wall(self):
        state,job=self.job_context();state.update(sealed_at=1,selected_candidate_id='candidate-01');job['operation']='finalize'
        comparison={'pass':True,'passed':12,'required':12}
        with patch.object(game,'_reference',return_value=[]),patch.object(game,'_capture',return_value=[]),\
             patch.object(game.measurement,'compare_motion',return_value=comparison),patch.object(game,'_preservation',return_value={'status':'pass'}),\
             patch.object(game,'_visual_wall',return_value={'status':'fail','passed':7,'required':8}) as wall:
            result=game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        self.assertFalse(result['pass']);self.assertEqual(result['motion_status'],'pass')
        self.assertEqual(result['visual_wall_status'],'fail');self.assertEqual(result['overall_acceptance'],'pending')
        self.assertEqual(wall.call_args.args[0],job['source_path'])

    def test_final_wall_pass_still_does_not_claim_manual_or_source_acceptance(self):
        state,job=self.job_context();state.update(sealed_at=1,selected_candidate_id='candidate-01');job['operation']='finalize'
        with patch.object(game,'_reference',return_value=[]),patch.object(game,'_capture',return_value=[]),\
             patch.object(game.measurement,'compare_motion',return_value={'pass':True,'passed':12,'required':12}),\
             patch.object(game,'_preservation',return_value={'status':'pass'}),patch.object(game,'_visual_wall',return_value={'status':'pass','passed':8,'required':8}):
            result=game.run_game_job(self.root,job,state,lambda:None,lambda *a:None,None)
        self.assertTrue(result['pass']);self.assertEqual(result['source_review_status'],'pending')
        self.assertEqual(result['independent_review_status'],'pending')


if __name__=='__main__':unittest.main()
