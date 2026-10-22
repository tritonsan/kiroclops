"""User media never needs reference source and never manufactures held-out proof."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np

from kiroclops.visual.package import create_package,load_package,copy_package
from kiroclops.visual.schema import VisualError,read_json,write_json,digest,file_sha256
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.events import extract_events
from kiroclops.local_engine.intake import prepare_intake,resume_selection
from kiroclops.local_engine.media_session import binding_preset,create_media_session,load_bundle,_compare,run_media_job
from kiroclops.local_engine.session import Session,run_session_job


class MediaSessionTests(unittest.TestCase):
    def test_qualified_reference_driver_survives_binding_and_session_setup(self):
        native=self.root/'native-package'
        create_package(native,[self.root/f'frame-{i}.png' for i in range(90)],
            [i/60 for i in range(90)],metadata={'scenario':{'frame_driver':'natural_browser_raf_v1'}})
        self.package=native;self.prepare()
        binding=binding_preset(self.intake,object_map={'thing':'handle'})
        self.assertEqual(binding['scenario']['frame_driver'],'natural_browser_raf_v1')
        create_media_session(self.intake,self.session,binding,mode='tools')
        saved=read_json(self.session/'session.json')
        self.assertEqual(read_json(Path(saved['reference'])/'capture-binding.json')['scenario']['frame_driver'],'natural_browser_raf_v1')

    def test_driver_downgrade_cannot_mix_reference_and_target_recorders(self):
        native=self.root/'native-package'
        create_package(native,[self.root/f'frame-{i}.png' for i in range(90)],
            [i/60 for i in range(90)],metadata={'scenario':{'frame_driver':'natural_browser_raf_v1'}})
        self.package=native;self.prepare()
        binding=binding_preset(self.intake,object_map={'thing':'handle'})
        binding['scenario'].pop('frame_driver')
        with self.assertRaisesRegex(VisualError,'capture driver'):
            create_media_session(self.intake,self.session,binding,mode='tools')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='media café naïve ');self.root=Path(self.tmp.name)
        self.project=self.root/'project';(self.project/'src').mkdir(parents=True)
        self._project('panel')
        rng=np.random.default_rng(9);body=rng.integers(15,240,(12,12,3),dtype=np.uint8);background=rng.integers(10,250,(16,16,3),dtype=np.uint8)
        paths=[]
        for i in range(90):
            image=np.zeros((320,320,3),np.uint8);image[2:18,2:18]=background;x=50+min(max(i-18,0),40);image[100:112,x:x+12]=body
            path=self.root/f'frame-{i}.png';path.write_bytes(cv2.imencode('.png',image)[1].tobytes());paths.append(path)
        self.package=self.root/'package';create_package(self.package,paths,[i/60 for i in range(90)])
        self.intake=self.root/'intake';self.session=self.root/'session'

    def tearDown(self):self.tmp.cleanup()

    def _project(self,domain):
        symbol,controller=('advanceMotion','panel-motion.js') if domain=='panel' else ('advanceLayout','layout-motion.js')
        button='open-panel' if domain=='panel' else 'add-node'
        (self.project/'index.html').write_text(f'<button id="{button}">Go</button><script src="src/app.js" type="module"></script>')
        (self.project/'src/app.js').write_text(f"import {{ {symbol} }} from './{controller}'; {symbol}();")
        (self.project/'src'/controller).write_text(f'export function {symbol}() {{return 1;}}')

    def prepare(self,provenance='user_declared',domain='panel'):
        timeline=None if provenance=='unknown' else {'provenance':provenance,'events':[{'action':'click','selector':'#open-panel' if domain=='panel' else '#add-node','time_s':.3}]}
        with patch('kiroclops.local_engine.intake._qualify_automatic',return_value=(None,{'status':'needs_confirmation'})):
            prepare_intake(self.package,self.project,'Match the supplied visual motion',self.intake,inputs=timeline)
        manifest=load_package(self.intake/'package')
        selection={'schema_version':1,'kind':'ObjectSelection','package_sha256':manifest['package_sha256'],
            'objects':[{'id':'thing','roi':[50,100,12,12],'anchor':[6,6],'body_height_px':12,'frame_index':0,
                        'template_frame_sha256':manifest['frames'][0]['sha256'],'method':'template'}],
            'background_rois':[[2,2,16,16]]}
        resume_selection(self.intake,selection)
        return selection

    def bound(self,mode='tools'):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'})
        from kiroclops.local_engine.session import create_session
        def isolated(*args,**kwargs):
            kwargs['experiment']={**kwargs['experiment'],'capture_lock':str(self.root/'unit-capture.lock')}
            return create_session(*args,**kwargs)
        # Every target renderer in this module is a trusted synthetic fixture;
        # its service-contract tests must not wait on real native capture work.
        with patch('kiroclops.local_engine.media_session.create_session',side_effect=isolated):
            result=create_media_session(self.intake,self.session,binding,mode=mode)
        return binding,result

    def test_unknown_inputs_request_binding_without_inventing_times_or_starting_model(self):
        self.prepare('unknown');value=binding_preset(self.intake,object_map={'thing':'handle'})
        self.assertEqual('needs_input_binding',value['status']);self.assertFalse(value['inferred_events'])
        self.assertEqual(value,create_media_session(self.intake,self.session,value));self.assertFalse(self.session.exists())

    def test_real_session_uses_only_portable_reference_pixels_and_normal_code_copy(self):
        binding,result=self.bound();state=read_json(self.session/'session.json');bundle=Path(state['reference'])
        self.assertEqual('media_bundle',state['experiment']['reference_mode']);self.assertFalse(result['model_started'])
        self.assertTrue((self.session/'target/src/panel-motion.js').is_file());self.assertFalse(bundle.is_relative_to(self.session))
        manifest=load_bundle(bundle);self.assertNotIn(str(self.root),json.dumps(manifest))
        self.assertFalse(list(bundle.rglob('*.js')));self.assertEqual('user_declared',load_package(bundle/'package')['inputs']['provenance'])
        self.assertEqual('not_provided',Session(self.session).inspect()['experiment']['heldout_status'])

    def test_bundle_can_move_but_selected_pixels_or_binding_cannot_change(self):
        binding,result=self.bound();bundle=Path(result['reference_bundle']);moved=self.root/'moved';shutil.copytree(bundle,moved)
        self.assertEqual(load_bundle(bundle),load_bundle(moved))
        choice=read_json(moved/'selection.json');choice['objects'][0]['roi'][0]+=1;write_json(moved/'selection.json',choice)
        with self.assertRaises(VisualError) as c:load_bundle(moved)
        self.assertEqual('bundle_changed',c.exception.code)

    def test_changed_input_time_or_heldout_scope_rejected_before_session_creation(self):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'})
        bad=deepcopy(binding);bad['scenario']['events'][0]['time_s']+=.1
        with self.assertRaises(VisualError):create_media_session(self.intake,self.session,bad)
        bad=deepcopy(binding);bad['scenario']['split']='validation'
        with self.assertRaises(VisualError):create_media_session(self.intake,self.session,bad)
        bad=deepcopy(binding);bad['claim']='recorded_input_response'
        with self.assertRaises(VisualError):create_media_session(self.intake,self.session,bad)
        self.assertFalse(self.session.exists())

    def test_project_change_and_ambiguous_object_mapping_reject(self):
        self.prepare()
        with self.assertRaises(VisualError):binding_preset(self.intake,object_map={})
        (self.project/'src/panel-motion.js').write_text('export function advanceMotion(){return 2;}')
        with self.assertRaises(VisualError) as c:binding_preset(self.intake,object_map={'thing':'handle'})
        self.assertEqual('source_changed',c.exception.code)

    def test_svg_requires_explicit_initial_state_and_uses_actual_timeline(self):
        self._project('svg');original=self.prepare(domain='svg')
        with self.assertRaises(VisualError) as c:binding_preset(self.intake,object_map={'thing':'a'})
        self.assertEqual('needs_input_binding',c.exception.code)
        # Binding-only fixture: explicit graph/outer ROI annotations are not a
        # claim that these synthetic patches qualify as measured SVG cards.
        manifest=load_package(self.intake/'package');selection=deepcopy(original);selection['objects']=[]
        for name in 'abcd':
            obj=deepcopy(original['objects'][0]);index=19 if name=='d' else 0
            obj.update(id=name,frame_index=index,template_frame_sha256=manifest['frames'][index]['sha256'],
                active_intervals_s=[[manifest['frames'][index]['time_s'],manifest['frames'][-1]['time_s']]],
                geometry={'method':'contour','roi':[48,98,16,16]})
            selection['objects'].append(obj)
        selection['connections']=[{'id':'edge-'+name,'from_object':'a','to_object':name,'method':'line'} for name in 'bcd']
        resume_selection(self.intake,selection)
        binding=binding_preset(self.intake,object_map={name:name for name in 'abcd'},initial_node_count=3)
        self.assertEqual(4,binding['scenario']['expected_final_node_count']);self.assertEqual(.3,binding['scenario']['events'][0]['time_s'])
        self.assertEqual(selection['connections'],binding['connections'])
        self.assertEqual({'a','b','c','d'},set(binding['lifecycle_bindings']))
        result=create_media_session(self.intake,self.session,binding)
        self.assertEqual('svg',result['domain']);self.assertFalse(result['model_started'])
        self.assertEqual('not_provided',result['heldout_status'])

    def test_svg_initial_count_cannot_make_partial_icon_graph_transfer_ready(self):
        self._project('svg');self.prepare(domain='svg')
        with self.assertRaises(VisualError) as caught:
            binding_preset(self.intake,object_map={'thing':'a'},initial_node_count=3)
        self.assertEqual('needs_connection_binding',caught.exception.code)
        self.assertFalse(self.session.exists())

    def measured(self):
        selection=read_json(self.intake/'selection.json');obs=analyze_package(self.intake/'package',selection)
        scenario=binding_preset(self.intake,object_map={'thing':'handle'})['scenario']
        return {'scenario':scenario,'package':str(self.intake/'package'),'selection':selection,
            'functional':{'status':'pass'},'measurement_status':'ok','observations':obs,'events':extract_events(obs,selection)}

    def captured_record(self, measured, *args, **kwargs):
        """Synthetic renderer double obeying the real job evidence contract.

        Pixel analysis above is real; this does not claim a native capture or
        worker execution. Planning must inspect the full bound recipe and this
        job's package, never a shared intake path or an ID-only mock scenario.
        """
        from kiroclops.local_engine.session import source_hashes
        project,output,binding=args[:3];output=Path(output)
        value=deepcopy(measured);package=output/'package'
        copy_package(value['package'],package)
        value.update(scenario=deepcopy(binding['scenario']),package=str(package),source_sha256=digest(source_hashes(project)))
        analysis=output/'measurement/analysis.json'
        write_json(analysis,{'kind':'SyntheticTargetTestDouble','measurement_status':value['measurement_status'],
            'observations':value['observations'],'events':value['events'],'native_capture_executed':False})
        value['analysis']=str(analysis)
        return value

    def test_user_declared_alignment_is_conditional_and_does_not_mutate_source_times(self):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'});reference=self.measured();original=deepcopy(reference)
        candidate=deepcopy(reference);candidate['observations']['inputs']['provenance']='recorder_verified'
        result=_compare(reference,candidate,candidate,binding)
        self.assertTrue(result['pass']);self.assertEqual('conditional_on_user_declared_inputs',result['claim'])
        self.assertEqual(reference,original);self.assertEqual('user_declared',reference['observations']['inputs']['provenance'])
        self.assertAlmostEqual(0,result['score'],places=8)

    def test_missing_candidate_input_is_capture_error_not_motion_failure(self):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'});reference=self.measured();candidate=deepcopy(reference)
        candidate['observations']['inputs']['events']=[]
        with self.assertRaises(VisualError) as c:_compare(reference,reference,candidate,binding)
        self.assertEqual('capture_timing_failure',c.exception.code)

    def test_valid_tracker_rows_do_not_hide_missing_temporal_support(self):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'})
        reference=self.measured();candidate=deepcopy(reference)
        for obj in candidate['observations']['objects']:
            obj['frames']=[f for f in obj['frames'] if not .55<f['time_s']<.85]
            for index,frame in enumerate(obj['frames']):frame['index']=index
            self.assertTrue(all(f['valid'] for f in obj['frames']))
        result=_compare(reference,reference,candidate,binding)
        self.assertFalse(result['pass'])
        self.assertEqual('insufficient_observability',result['measurement_status'])
        self.assertEqual('comparison_coverage_failure',result['failure_class'])
        self.assertTrue(any(r['category']=='measurement_coverage' for r in result['diagnostic']['failed_gates']))

    def test_media_dispatch_real_analysis_worker_and_immutable_finalize_remain_partial(self):
        binding,result=self.bound();captured=self.measured();session=Session(self.session)
        from kiroclops.local_engine.evaluation import run_job
        def target(*args,**kwargs):
            value=self.captured_record(captured,*args,**kwargs)
            if args[7]=='before':
                for frame in value['observations']['objects'][0]['frames']:
                    if frame['valid']:frame['x']=56+(frame['x']-56)*.7
            return value
        def job(op):
            with patch.object(Session,'_spawn'):created=session.submit(op)
            run_session_job(self.session,created['job_id'],runner=run_job)
            completed=session.get_job(created['job_id']);self.assertEqual('succeeded',completed['status'],completed.get('error'));return completed
        with patch('kiroclops.local_engine.media_session._target_record',side_effect=target):
            prepared=job('prepare');self.assertIn('profile',prepared['result'])
            evaluated=job('evaluate');self.assertTrue(evaluated['result']['eligible'])
            final=job('finalize');self.assertFalse(final['result']['pass']);self.assertTrue(final['result']['calibration_pass'])
            self.assertEqual('partial_unobserved_holdout',final['result']['acceptance_status'])
        self.assertTrue(session.inspect()['sealed_at'])
        with self.assertRaises(VisualError):session.submit('evaluate')

    def test_neutral_mode_preparation_and_evaluation_do_not_run_visual_analysis(self):
        binding,result=self.bound(mode='baseline');captured={'scenario':binding['scenario'],'package':str(self.intake/'package'),
            'functional':{'status':'pass'}};state=read_json(self.session/'session.json')
        with patch('kiroclops.local_engine.media_session._target_record',return_value=captured) as target,\
             patch('kiroclops.local_engine.evaluation._analysis',side_effect=AssertionError('No CV in neutral feedback')):
            prepared=run_media_job(self.session,{'job_id':'prepare','operation':'prepare'},state,lambda:None,lambda *a:None,lambda:False)
            self.assertNotIn('profile',prepared);self.assertFalse(target.call_args.kwargs['analyze'])
            evaluated=run_media_job(self.session,{'job_id':'evaluate','operation':'evaluate','source_path':state['target'],'candidate_id':'candidate-01'},state,lambda:None,lambda *a:None,lambda:False)
            self.assertTrue(evaluated['eligible']);self.assertIsNone(evaluated['score']);self.assertTrue(evaluated['neutral_capture'])

    def test_rejected_measurement_is_tool_failure_preserves_capture_and_consumes_attempt(self):
        self.bound();session=Session(self.session)
        from kiroclops.local_engine.evaluation import run_job
        captured=self.measured()
        with patch.object(Session,'_spawn'),patch('kiroclops.local_engine.media_session._target_record',side_effect=lambda *a,**kw:self.captured_record(captured,*a,**kw)):
            prepared=session.submit('prepare')
            run_session_job(self.session,prepared['job_id'],runner=run_job)
            self.assertEqual('succeeded',session.get_job(prepared['job_id'])['status'])
        rejected=deepcopy(captured);rejected['measurement_status']='rejected'
        rejected['observations']['status']='rejected'
        rejected['observations']['errors']=[{'code':'tracking_failure','object_id':'thing'}]
        for frame in rejected['observations']['objects'][0]['frames'][2:]:
            frame.update(valid=False,x=None,y=None)
        with patch.object(Session,'_spawn'),patch('kiroclops.local_engine.media_session._target_record',side_effect=lambda *a,**kw:self.captured_record(rejected,*a,**kw)):
            submitted=session.submit('evaluate')
            run_session_job(self.session,submitted['job_id'],runner=run_job)
            completed=session.get_job(submitted['job_id'])
        self.assertEqual('failed',completed['status'])
        self.assertEqual('tracking_failure',completed['error']['code'])
        self.assertIsNone(completed['result'])
        self.assertEqual(1,session.inspect()['budget']['used_candidates'])
        saved=self.session/'measurements'/submitted['job_id']/'candidate.json'
        self.assertEqual('rejected',read_json(saved)[0]['measurement_status'])
        self.assertFalse((saved.parent/'comparison.json').exists())

    def test_prepared_cache_tamper_and_reference_swap_fail_before_candidate_capture(self):
        self.bound();session=Session(self.session)
        from kiroclops.local_engine.evaluation import run_job
        captured=self.measured()
        with patch.object(Session,'_spawn'),patch('kiroclops.local_engine.media_session._target_record',side_effect=lambda *a,**kw:self.captured_record(captured,*a,**kw)):
            prepared=session.submit('prepare');run_session_job(self.session,prepared['job_id'],runner=run_job)
            self.assertEqual('succeeded',session.get_job(prepared['job_id'])['status'])
        manifest_path=self.session/'prepared-private.json';original=read_json(manifest_path)
        before_path=self.session/original['before'];before=read_json(before_path)
        for mutation in ('cached_position','reference_path'):
            with self.subTest(mutation=mutation):
                write_json(manifest_path,original);write_json(before_path,before)
                if mutation=='cached_position':
                    changed=deepcopy(before);changed[0]['observations']['objects'][0]['frames'][0]['x']+=40
                    write_json(before_path,changed)
                else:
                    changed=deepcopy(original);changed['reference']=changed['before'];changed['records']['reference']=changed['records']['before']
                    write_json(manifest_path,changed)
                with patch.object(Session,'_spawn'),patch('kiroclops.local_engine.media_session._target_record',side_effect=AssertionError('Stale evidence must be rejected before capture')):
                    attempt=session.submit('evaluate');run_session_job(self.session,attempt['job_id'],runner=run_job)
                    result=session.get_job(attempt['job_id'])
                self.assertEqual('failed',result['status']);self.assertEqual('measurement_binding_changed',result['error']['code'])
                self.assertIsNone(result['result'])
        self.assertEqual(2,session.inspect()['budget']['used_candidates'])

    def test_final_gain_is_enforced_per_window_and_zero_baseline_not_claimed_improved(self):
        self.prepare();binding=binding_preset(self.intake,object_map={'thing':'handle'});reference=self.measured()
        same=_compare(reference,reference,reference,binding,require_improvement=True)
        self.assertFalse(same['pass']);self.assertFalse(same['improvement_pass'])
        before=deepcopy(reference)
        for frame in before['observations']['objects'][0]['frames']:
            if frame['valid']:frame['x']=56+(frame['x']-56)*.7
        changed=_compare(reference,before,reference,binding,require_improvement=True)
        self.assertTrue(changed['pass']);self.assertTrue(all(g['pass'] for g in changed['improvement_gates']))

    def test_reference_declared_parent_child_roles_are_passed_to_analysis(self):
        from kiroclops.local_engine.media_session import _reference_record
        binding,result=self.bound();bundle_dir=Path(result['reference_bundle']);bundle=load_bundle(bundle_dir)
        # Test the analysis handoff using explicit already-bound role metadata.
        value=read_json(bundle_dir/'capture-binding.json');value['relative_motion']=[{'object_id':'thing','relative_to':'parent'}]
        write_json(bundle_dir/'capture-binding.json',value)
        observed={'inputs':{'provenance':'user_declared','events':[]}}
        with patch('kiroclops.local_engine.evaluation._analysis',return_value={'measurement_status':'ok','observations':observed,'events':{}}) as analyze:
            _reference_record(bundle_dir,bundle,lambda:None,lambda *a:None,self.root/'analysis-handoff',analyze=True)
        self.assertEqual(value['relative_motion'],analyze.call_args.args[1]['relative_motion'])
        with self.assertRaises(VisualError):load_bundle(bundle_dir)

    def test_bundle_rejects_extra_code_and_initial_contract_with_unsupported_limits(self):
        binding,result=self.bound();bundle_dir=Path(result['reference_bundle'])
        (bundle_dir/'private.js').write_text('secret')
        with self.assertRaises(VisualError):load_bundle(bundle_dir)
        (bundle_dir/'private.js').unlink()
        contract=read_json(bundle_dir/'behavior-contract.json');contract['position_rmse_fraction']=.99;write_json(bundle_dir/'behavior-contract.json',contract)
        bundle=read_json(bundle_dir/'bundle.json');bundle['file_hashes']['behavior-contract.json']=file_sha256(bundle_dir/'behavior-contract.json')
        write_json(bundle_dir/'bundle.json',bundle);write_json(bundle_dir/'binding.json',{'bundle_sha256':digest(bundle)})
        with self.assertRaises(VisualError) as c:load_bundle(bundle_dir)
        self.assertEqual('invalid_contract',c.exception.code)


if __name__=='__main__':unittest.main()
