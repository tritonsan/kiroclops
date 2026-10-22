import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.common import ROOT
from kiroclops.local_engine.intake import prepare_intake,resume_selection
from kiroclops.local_engine.media_session import binding_preset,create_media_session,load_bundle,run_media_job
from kiroclops.local_engine import game_media
from kiroclops.local_engine.session import source_hashes,copy_project,Session,run_session_job
from kiroclops.visual.package import create_package,load_package
from kiroclops.visual.schema import VisualError,read_json,write_json,digest
from tests.evidence import requires_evidence


class GameMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'game café naïve';self.root.mkdir()
        self.project=self.root/'heart';copy_project(ROOT/'vendor/heart',self.project)
        self.raw=self.root/'raw';self.raw.mkdir();sources=[]
        rng=np.random.default_rng(6);self.glyph=rng.integers(20,245,(8,10,3),dtype=np.uint8)
        background=rng.integers(20,245,(12,12,3),dtype=np.uint8)
        for i in range(240):
            image=np.zeros((100,160,3),np.uint8);phase=(i-30)/54
            height=max(0,round(30*4*phase*(1-phase))) if 0<=phase<=1 else 0
            image[60-height:68-height,55:65]=self.glyph;image[80:92,10:22]=background
            path=self.raw/f'{i:06}.png';path.write_bytes(cv2.imencode('.png',image)[1].tobytes());sources.append(path)
        self.package=self.root/'package';self.inputs={'provenance':'user_declared','events':[
            {'action':'jump','pressed':True,'time_s':.5},{'action':'jump','pressed':False,'time_s':.55}]}
        create_package(self.package,sources,[i/60 for i in range(240)],self.inputs)
        self.intake=self.root/'intake';self.session=self.root/'session'
        with patch('kiroclops.local_engine.intake._qualify_automatic',return_value=(None,{'status':'needs_confirmation'})):
            prepare_intake(self.package,self.project,'Match this observed jump',self.intake)
        manifest=load_package(self.intake/'package')
        self.selection={'schema_version':1,'kind':'ObjectSelection','package_sha256':manifest['package_sha256'],
            'objects':[{'id':'hero','roi':[55,60,10,8],'anchor':[5,4],'body_height_px':8,
                'frame_index':0,'template_frame_sha256':manifest['frames'][0]['sha256'],'method':'template','search_radius':12}],
            'background_rois':[[10,80,12,12]]}
        resume_selection(self.intake,self.selection)

    def binding(self,**kw):return binding_preset(self.intake,object_map={'hero':'character'},game_stage='jump',**kw)

    def bound(self,mode='tools'):
        from kiroclops.local_engine.session import create_session
        binding=self.binding()
        def isolated(*args,**kwargs):
            kwargs['experiment']={**kwargs['experiment'],'capture_lock':str(self.root/'unit-capture.lock')}
            return create_session(*args,**kwargs)
        with patch('kiroclops.local_engine.media_session.create_session',side_effect=isolated):
            result=create_media_session(self.intake,self.session,binding,mode=mode)
        return binding,result

    @requires_evidence('vendor/heart')
    def test_exact_heart_binding_explicit_body_and_actions_without_inference(self):
        binding=self.binding(body_height=9)
        self.assertEqual(9,binding['body_height_px']);self.assertEqual('jump',binding['game_stage'])
        self.assertEqual([30,33],[e['tick'] for e in binding['scenario']['events']])
        self.assertEqual('conditional_on_user_declared_inputs',binding['claim'])
        self.assertEqual('not_provided',binding['heldout_status'])
        with self.assertRaises(VisualError):binding_preset(self.intake,object_map={'hero':'character'})

    @requires_evidence('vendor/heart')
    def test_prepared_overlay_is_separate_controller_and_original_stay_identical(self):
        original=source_hashes(self.project);binding,result=self.bound()
        self.assertEqual(original,source_hashes(self.project))
        state=read_json(self.session/'session.json');preparation=state['experiment']['source_preparation']
        self.assertTrue(preparation['original_project_unchanged']);self.assertTrue(preparation['controller_unchanged'])
        self.assertEqual(original['player.gd'],source_hashes(self.session/'target')['player.gd'])
        self.assertTrue((self.session/'target/kiroclops_heart/capture.gd').is_file())
        self.assertEqual('game',load_bundle(result['reference_bundle'])['domain'])
        self.assertFalse(any(p.suffix=='.gd' for p in Path(result['reference_bundle']).rglob('*')))

    @requires_evidence('vendor/heart')
    def test_bound_source_schedule_and_contract_cannot_be_changed(self):
        binding,result=self.bound();bundle=Path(result['reference_bundle'])
        changed=read_json(bundle/'capture-binding.json');changed['scenario']['events'][0]['tick']=29
        write_json(bundle/'capture-binding.json',changed)
        manifest=read_json(bundle/'bundle.json')
        from kiroclops.visual.schema import file_sha256
        manifest['file_hashes']['capture-binding.json']=file_sha256(bundle/'capture-binding.json')
        write_json(bundle/'bundle.json',manifest);write_json(bundle/'binding.json',{'bundle_sha256':digest(manifest)})
        with self.assertRaises(VisualError) as caught:load_bundle(bundle)
        self.assertEqual('invalid_binding',caught.exception.code)

    @requires_evidence('vendor/heart')
    def test_nonheart_resource_and_variable_rate_not_implicitly_supported(self):
        (self.project/'player.tscn').write_text('[gd_scene]\n')
        with self.assertRaises(VisualError) as caught:game_media.validate_project(self.project)
        self.assertEqual('unsupported_media_adapter',caught.exception.code)
        manifest=load_package(self.package);manifest['frames'][20]['time_s']+=.001
        with self.assertRaises(VisualError) as caught:game_media._scenario(manifest,'jump')
        self.assertEqual('unsupported_game_media_timing',caught.exception.code)

    @requires_evidence('vendor/heart')
    def test_unknown_input_is_pending_and_offgrid_or_mixed_actions_rejected(self):
        manifest=load_package(self.package);project={'project':str(ROOT/'vendor/heart')}
        manifest['inputs']={'provenance':'unknown','events':[]}
        self.assertEqual('needs_input_binding',game_media.make_binding(project,manifest,self.selection,{'hero':'character'},'jump')['status'])
        manifest=load_package(self.package);manifest['inputs']['events'][0]['time_s']+=.001
        with self.assertRaises(VisualError):game_media._scenario(manifest,'jump')
        manifest=load_package(self.package)
        with self.assertRaises(VisualError):game_media._scenario(manifest,'ground')

    @requires_evidence('vendor/heart')
    def test_left_reverse_release_timeline_is_retained_as_supplied(self):
        manifest=load_package(self.package);manifest['inputs']['events']=[
            {'action':'left','pressed':True,'time_s':.5},
            {'action':'left','pressed':False,'time_s':.9},
            {'action':'right','pressed':True,'time_s':.9},
            {'action':'right','pressed':False,'time_s':1.3}]
        recipe=game_media._scenario(manifest,'ground')
        self.assertEqual([('left',True),('left',False),('right',True),('right',False)],[(e['action'],e['pressed']) for e in recipe['events']])
        phases=game_media._phases({'game_stage':'ground','scenario':recipe})
        self.assertEqual([(30,54,'held'),(54,78,'held'),(78,109,'coast')],phases)

    def measured(self,binding):
        from kiroclops.visual.tracking import analyze_package
        from kiroclops.visual.events import extract_events
        observed=analyze_package(self.intake/'package',self.selection,{'require_static_camera':True})
        return {'scenario':binding['scenario'],'package':str(self.intake/'package'),'body_height_px':8,
            'observations':observed,'events':extract_events(observed,self.selection),
            'measurement_status':'ok','functional':{'status':'captured_only'}}

    @requires_evidence('vendor/heart')
    def test_real_reference_worker_and_durable_seal_remain_partial(self):
        binding,result=self.bound();captured=self.measured(binding);session=Session(self.session)
        def target(*args,**kw):
            value=deepcopy(captured)
            if args[0]==str(self.session/'baseline'):
                for f in value['observations']['objects'][0]['frames']:
                    if f['valid']:f['y']=64+(f['y']-64)*.7
            return value
        def job(operation):
            with patch.object(Session,'_spawn'):value=session.submit(operation)
            run_session_job(self.session,value['job_id'],runner=lambda d,j,c,p,x:run_media_job(d,j,read_json(self.session/'session.json'),c,p,x))
            completed=session.get_job(value['job_id']);self.assertEqual('succeeded',completed['status'],completed.get('error'));return completed['result']
        with patch.object(game_media,'_target',side_effect=target),patch('kiroclops.local_engine.game._preservation',return_value={'status':'pass'}):
            self.assertIn('profile',job('prepare'))
            self.assertTrue(job('evaluate')['eligible'])
            final=job('finalize');self.assertTrue(final['calibration_pass']);self.assertFalse(final['pass'])
            self.assertEqual('partial_unobserved_holdout',final['acceptance_status'])
        with self.assertRaises(VisualError):session.submit('evaluate')

    @requires_evidence('vendor/heart')
    def test_visible_absent_jump_is_motion_failure_not_capture_or_tracking_failure(self):
        binding=self.binding();reference=self.measured(binding);flat=deepcopy(reference)
        for frame in flat['observations']['objects'][0]['frames']:frame['y']=64
        result=game_media.compare(reference,flat,flat,binding)
        self.assertFalse(result['pass']);self.assertFalse(result['metric_gates']['jump_observed'])
        self.assertEqual('expected_jump_not_observed',result['metrics']['candidate']['status'])
        self.assertGreater(result['score'],0)

    @requires_evidence('vendor/heart')
    def test_neutral_feedback_never_calls_pixel_analysis_or_preservation(self):
        binding,result=self.bound(mode='baseline');state=read_json(self.session/'session.json')
        captured={'scenario':binding['scenario'],'package':str(self.intake/'package'),'body_height_px':16,'functional':{'status':'captured_only'}}
        with patch.object(game_media,'_target',return_value=captured),patch('kiroclops.local_engine.evaluation._analysis',side_effect=AssertionError('Neutral feedback must not analyze pixels')),patch('kiroclops.local_engine.game._preservation',side_effect=AssertionError('Neutral feedback must not measure protected behavior')):
            prepared=run_media_job(self.session,{'job_id':'p','operation':'prepare'},state,lambda:None,lambda *a:None,lambda:False)
            self.assertNotIn('profile',prepared)
            result=run_media_job(self.session,{'job_id':'e','operation':'evaluate','source_path':state['target'],'candidate_id':'candidate-01'},state,lambda:None,lambda *a:None,lambda:False)
            self.assertTrue(result['eligible']);self.assertIsNone(result['score']);self.assertTrue(result['neutral_capture'])

    @requires_evidence('vendor/heart')
    def test_disposable_import_sidecars_allowed_but_controller_mutation_rejected(self):
        binding,result=self.bound();project=self.session/'target';original=source_hashes(project)
        def capture(rendered,raw,recipe,**kw):
            (raw/'frames').mkdir(parents=True)
            for path in self.raw.glob('*.png'):(raw/'frames'/path.name).write_bytes(path.read_bytes())
            write_json(raw/'manifest.json',{'status':'ok','fps':60,'game':'heart','variant':'after','scenario':recipe,
                'frames':[{'frame_index':i,'physics_tick':60+i,'time_s':i/60,'actions':['jump'] if 30<=i<33 else []} for i in range(240)]})
        for mutate in (False,True):
            def imported(rendered,*args,**kw):
                (rendered/'generated.gd.uid').write_text('uid://generated')
                if mutate:(rendered/'player.gd').write_text('# unexpected source mutation')
            with self.subTest(mutate=mutate),patch.object(game_media.runtime,'import_project',side_effect=imported),patch.object(game_media.runtime,'capture_project',side_effect=capture):
                call=lambda:game_media._target(project,self.root/f'capture-{mutate}',binding,lambda:None,lambda *a:None,lambda:False,analyze=False)
                if mutate:
                    with self.assertRaises(VisualError) as caught:call()
                    self.assertEqual('source_changed',caught.exception.code)
                else:self.assertEqual('captured_only',call()['functional']['status'])
        self.assertEqual(original,source_hashes(project))


if __name__=='__main__':unittest.main()
