"""Prospective sampling policy must not excuse delay, disappearance or gaps."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests.test_visual_comparison import obs, contract
from tests.test_local_evaluation import record
from kiroclops.visual.comparison import compare_observations, window_timing
from kiroclops.visual.visibility import observed_brackets
from kiroclops.local_engine.evaluation import _contract, _compare_web
from kiroclops.web_local.specs import scenarios, validate_scenario
from kiroclops.visual.schema import VisualError


class ObservedResponseTests(unittest.TestCase):
    def test_media_contract_metadata_is_allowed_but_limits_are_fixed(self):
        from kiroclops.local_engine.media_session import _compare,LIMITS
        ref=record();ref['scenario'].update(domain='svg',analysis_policy='observed_response_v2')
        binding={'scenario':ref['scenario'],'spatial_alignment':{'mode':'viewport','units':'natural_image_pixels','size_metric':'absolute_visible_outline'},
            'input_provenance':'recorder_verified','claim':'recorded_input_response'}
        full={'schema_version':1,'kind':'BehaviorContract','intent':'Match the supplied motion',
            'status':'calibration_only','heldout_status':'not_provided',**LIMITS}
        # This test is about the real media routing/contract boundary. Pixel
        # and geometry acceptance are independently covered by their suites.
        with patch('kiroclops.local_engine.geometry_acceptance.svg_preservation',return_value={'pass':True,'measurement_status':'ok','failure_class':None}):
            bare=_compare(ref,ref,ref,binding,limits=LIMITS)
            self.assertEqual(_compare(ref,ref,ref,binding,limits=full),bare)
            for key in LIMITS:
                changed=dict(full);changed[key]*=2
                with self.assertRaisesRegex(VisualError,'fixed limits'):_compare(ref,ref,ref,binding,limits=changed)
                missing=dict(full);missing.pop(key)
                with self.assertRaisesRegex(VisualError,'fixed limits'):_compare(ref,ref,ref,binding,limits=missing)

    def cfg(self):
        cfg=contract(.2);cfg['sampling_policy']='observed_response_v2'
        cfg['windows'][0].update(alignment={'mode':'input','action':'click','ordinal':0},
                                end_alignment={'mode':'input','action':'click','ordinal':1})
        return cfg

    def sequence(self,phase=0.):
        value=obs([i/60+phase for i in range(31)])
        value['inputs']={'provenance':'recorder_verified','events':[
            {'action':'click','time_s':.1},{'action':'click','time_s':.3}]}
        return value

    def test_interrupt_ends_at_real_pre_command_image_and_reports_censoring(self):
        a,b=self.sequence(),self.sequence(.007)
        result=compare_observations(a,b,self.cfg())
        row=result['object_windows'][0]
        self.assertTrue(result['success'],result)
        self.assertAlmostEqual(row['time_window_s'][1],17/60-.1)
        self.assertEqual(row['valid_time_ratio'],1.)
        self.assertFalse(row['input_timing']['duration_rescaled'])
        self.assertEqual(row['input_timing']['requested_window_s'],[0.,.2])

    def test_missing_interior_images_cannot_be_censored(self):
        a,b=self.sequence(),self.sequence()
        for f in b['objects'][0]['frames']:
            if .16<f['time_s']<.24:f['valid']=False
        result=compare_observations(a,b,self.cfg())
        self.assertFalse(result['success'])
        self.assertFalse(result['object_windows'][0]['gates']['coverage'])

    def test_missing_pre_interrupt_capture_fails_instead_of_shortening_to_nothing(self):
        a,b=self.sequence(),self.sequence()
        b['objects'][0]['frames']=[f for f in b['objects'][0]['frames'] if f['time_s']<.15 or f['time_s']>=.3]
        for i,f in enumerate(b['objects'][0]['frames']):f['index']=i
        with self.assertRaisesRegex(VisualError,'terminating input'):
            window_timing(a,b,self.cfg(),self.cfg()['windows'][0])

    def born(self,phase=0.,delay=0.):
        value=obs([i/60+phase for i in range(61)])
        o=value['objects'][0];present=[f for f in o['frames'] if f['time_s']>=.3+delay]
        o['active_intervals_s']=[[present[0]['time_s'],present[-1]['time_s']]]
        for f in o['frames']:
            if f['time_s']<present[0]['time_s']:
                f.update(valid=False,present=False,x=None,y=None,width=None,height=None)
            else:f['present']=True
        return value

    def test_pixel_visibility_edges_share_geometry_but_keep_latency_gate(self):
        a,b=self.born(),self.born(.008)
        cfg=contract(.6);cfg.update(sampling_policy='observed_response_v2',lifecycle_tolerance_s=.05)
        result=compare_observations(a,b,cfg)
        self.assertTrue(result['success'],result)
        self.assertAlmostEqual(result['object_windows'][0]['time_window_s'][0],.308)
        delayed=compare_observations(a,self.born(.008,.12),cfg)
        self.assertFalse(delayed['success'])
        self.assertFalse(delayed['lifecycle_checks'][0]['pass'])

    def test_declared_lifetime_cannot_excuse_a_visible_object_outside_it(self):
        a,b=self.born(),self.born(.008)
        b['objects'][0]['frames'][0]['present']=True
        cfg=contract(.6);cfg.update(sampling_policy='observed_response_v2',lifecycle_tolerance_s=.05)
        self.assertFalse(compare_observations(a,b,cfg)['success'])

    def test_removal_censors_endpoint_but_not_trajectory_or_early_disappearance(self):
        def removed(end):
            value=obs([i/60 for i in range(61)])
            o=value['objects'][0];o['active_intervals_s']=[[0.,end]]
            for f in o['frames']:
                f['present']=f['time_s']<=end
                if not f['present']:f['valid']=False
            return value
        a,b=removed(.5),removed(.5)
        cfg=contract(.8);cfg.update(sampling_policy='observed_response_v2',lifecycle_tolerance_s=.05)
        result=compare_observations(a,b,cfg)
        self.assertTrue(result['success'])
        self.assertFalse(result['object_windows'][0]['endpoint_required'])
        for f in b['objects'][0]['frames']:
            if f['valid']:f['x']+=50
        self.assertFalse(compare_observations(a,b,cfg)['success'])
        self.assertFalse(compare_observations(a,removed(.3),cfg)['success'])
        live=obs([i/60 for i in range(61)])
        self.assertTrue(compare_observations(live,live,cfg)['object_windows'][0]['endpoint_required'])

    def test_visibility_brackets_include_actual_image_time_intervals(self):
        frames=[{'time_s':i*.1,'time_interval_s':[i*.1-.002,i*.1+.002]} for i in range(5)]
        value=observed_brackets(frames,1,3)
        self.assertEqual(value['start']['interval_s'][0],-.002)
        self.assertAlmostEqual(value['start']['interval_s'][1],.102)
        self.assertAlmostEqual(value['end']['interval_s'][0],.298)
        self.assertEqual(value['end']['interval_s'][1],.402)

    def test_reference_scale_is_fixed_across_small_tail_and_drag_anchors_to_movement(self):
        ref=record(extra_inputs=[{'action':'drag_start','time_s':.8},
            {'action':'drag_move','time_s':.83},{'action':'drag_move','time_s':.86},
            {'action':'drag_end','time_s':1.2}])
        ref['scenario']['analysis_policy']='observed_response_v2'
        cfg=_contract(ref,ref)
        self.assertEqual(cfg['object_pairs'][0]['scale_px'],100.)
        self.assertEqual(cfg['object_pairs'][0]['rmse_limit'],.03)
        self.assertEqual(cfg['scale_policy'],'whole_reference_object_motion')
        actions=[w['alignment']['action'] for w in cfg['windows'] if w['id']!='initial-preservation']
        self.assertEqual(actions,['click','drag_move','drag_end'])
        before=deepcopy(ref)
        for frame in before['observations']['objects'][0]['frames']:frame['x']*=.65
        self.assertTrue(_compare_web(ref,before,ref,improvement=True)['pass'])

    def test_new_policy_is_not_admitted_to_model_trials_before_qualification(self):
        scenario=scenarios('svg','validation')[0];scenario['analysis_policy']='observed_response_v2'
        with self.assertRaisesRegex(ValueError,'qualification-only'):validate_scenario(scenario)
        scenario['split']='qualification'
        with self.assertRaisesRegex(ValueError,'natural RAF'):validate_scenario(scenario)
        scenario['frame_driver']='natural_browser_raf_v1';validate_scenario(scenario)


if __name__=='__main__':unittest.main()
