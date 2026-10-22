"""Spatial origins are explicit; births, delays and actual outlines survive."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from kiroclops.local_engine.media_session import (
    VIEWPORT_ALIGNMENT,_spatial_observations,_common_stationary_anchor,
    _input_time_observations,_relative_observations,_compare,binding_preset,
    _preserve_point_semantics,_target_record,
)
from kiroclops.visual.schema import VisualError,digest


def observations(phase=0.,shift=0.,birth=True,delay=0.,width=20.):
    times=[round(phase+i*.025,6) for i in range(61)]
    objects=[]
    for oid in ('a','d'):
        rows=[]
        for i,t in enumerate(times):
            active=oid=='a' or not birth or t>=.45+delay
            motion=max(0.,min(24.,(t-.45-delay)*30)) if oid=='d' else 0.
            x=40.+shift if oid=='a' else 80.+shift+motion
            rows.append({'index':i,'time_s':t,'valid':active,'expected_active':active,'present':active,
                'x':x if active else None,'y':60. if active else None,
                'left':x-5 if active else None,'top':50. if active else None,
                'width':width if active else None,'height':20. if active else None})
        objects.append({'id':oid,'frames':rows,'active_intervals_s':[[.45+delay if oid=='d' and birth else times[0],times[-1]]]})
    return {'schema_version':1,'status':'ok','image_size':[400,300],'package_sha256':'a'*64,
        'objects':objects,'inputs':{'provenance':'recorder_verified','events':[{'time_s':.4,'action':'click','selector':'#open-panel'}]}}


def binding(policy=None):
    return {'schema_version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT if policy is None else policy),
        'scenario':{'domain':'panel'},'relative_motion':[],'input_provenance':'recorder_verified','claim':'recorded_input_response'}


def record(obs):return {'observations':obs,'functional':{'status':'pass'},'measurement_status':'ok'}


class MediaSpatialAlignmentTests(unittest.TestCase):
    def test_target_autoselection_cannot_upgrade_legacy_reference_anchor(self):
        source={'anchor':[6,6]}
        target={'id':'handle','anchor':[7,7],'pixel_anchor':{'method':'foreground_color_mass_v1','foreground_rgb':[1,2,3]}}
        value=_preserve_point_semantics(source,target,{})
        self.assertNotIn('pixel_anchor',value)
        self.assertEqual(value['anchor'],target['anchor'])
        self.assertIn('pixel_anchor',target)

    def test_target_foreground_appearance_is_its_own_bound_pixel_selection(self):
        source={'pixel_anchor':{'method':'foreground_color_mass_v1','foreground_rgb':[1,2,3]}}
        target={'frame_index':0,'selection_source':'rendered_natural_artwork_pixels',
                'pixel_anchor':{'method':'foreground_color_mass_v1','foreground_rgb':[4,5,6]}}
        manifest={'package_sha256':'a'*64,'frames':[{'sha256':'b'*64}]}
        value=_preserve_point_semantics(source,target,manifest)
        self.assertEqual(value['pixel_anchor']['foreground_rgb'],[4,5,6])
        self.assertEqual(value['point_semantics_binding']['target_template_frame_sha256'],'b'*64)
        for key,replacement in [('pixel_anchor',None),('selection_source','guessed_from_reference')]:
            bad={**target,key:replacement}
            with self.subTest(key=key),self.assertRaises(VisualError):_preserve_point_semantics(source,bad,manifest)

    def test_actual_target_analysis_handoff_preserves_reference_opt_in(self):
        import tempfile
        from pathlib import Path
        reference={'selection':{'objects':[{'id':'a','anchor':[6,6]}]}}
        target={'id':'handle','frame_index':0,'anchor':[7,7],
            'pixel_anchor':{'method':'foreground_color_mass_v1','foreground_rgb':[1,2,3]}}
        detected={'objects':[target],'connections':[],'relative_motion':[]}
        raw={'package':'not-opened','source_sha256':'s'}
        with tempfile.TemporaryDirectory() as directory,\
             patch('kiroclops.local_engine.evaluation._capture_web',return_value=raw),\
             patch('kiroclops.web_local.selection.select_package_from_pixels',return_value=detected),\
             patch('kiroclops.local_engine.media_session.load_package',return_value={'frames':[{'sha256':'x'}]}),\
             patch('kiroclops.local_engine.evaluation._analysis',return_value={'measurement_status':'ok','observations':{},'events':{}}) as measured:
            _target_record('project',Path(directory),{'scenario':{'domain':'panel'},'object_map':{'a':'handle'}},
                           reference,lambda:None,lambda *a:None,lambda:False,'candidate',analyze=True)
            self.assertNotIn('pixel_anchor',measured.call_args.args[1]['objects'][0])
            self.assertEqual(measured.call_args.args[1]['objects'][0]['id'],'a')

    def test_mixed_measured_centroid_and_roi_points_cannot_compare(self):
        ref=observations();target=observations()
        target['objects'][0]['pixel_anchor']={'method':'foreground_color_mass_v1','foreground_rgb':[1,2,3],
                                             'semantics':'centroid_of_observed_foreground_color_mass'}
        with self.assertRaises(VisualError) as caught:_spatial_observations([ref,ref,target],binding())
        self.assertEqual(caught.exception.code,'point_semantics_mismatch')

    def test_new_binding_serializes_explicit_viewport_policy(self):
        manifest={'package_sha256':'b'*64,'image_size':[400,300],
            'frames':[{'time_s':i/10} for i in range(15)],
            'inputs':{'provenance':'recorder_verified','events':[{'time_s':.4,'action':'click','selector':'#open-panel'}]}}
        selection={'objects':[{'id':'chosen'}]}
        with patch('kiroclops.local_engine.media_session._intake',return_value=({}, {'domain':'web_panel'},manifest,selection)),\
             patch('kiroclops.local_engine.media_session._validate_binding'):
            value=binding_preset('unused',object_map={'chosen':'handle'})
        self.assertEqual(value['schema_version'],2)
        self.assertEqual(value['spatial_alignment'],VIEWPORT_ALIGNMENT)

    def test_viewport_preserves_newborn_absolute_coordinates_and_outer_sizes(self):
        ref=observations();target=observations(.007)
        # Different birth samples and 1px initial rasterization cannot bias
        # all later positions or contour widths.
        first=next(f for f in ref['objects'][1]['frames'] if f['valid']);first['width']=21.
        originals=deepcopy([ref,ref,target])
        values,receipt=_spatial_observations([ref,ref,target],binding())
        self.assertEqual(values[0]['objects'],ref['objects'])
        self.assertEqual(values[2]['objects'],target['objects'])
        self.assertEqual(values[0]['objects'][1]['frames'][-1]['width'],20.)
        self.assertEqual([ref,ref,target],originals)
        self.assertTrue(all(r['size_values_unchanged'] for r in receipt['transforms']))

    def test_visible_anchor_applies_one_translation_to_every_object(self):
        sources=[observations(),observations(.007,50),observations(.013,100)]
        policy={**VIEWPORT_ALIGNMENT,'mode':'visible_anchor','object_id':'a'}
        values,receipt=_spatial_observations(sources,binding(policy))
        self.assertEqual([r['translation_px'] for r in receipt['transforms']],[[0.,0.],[-50.,0.],[-100.,0.]])
        self.assertEqual(len({a['common_offset_s'] for a in receipt['anchor_evidence']}),1)
        for original,value,offset in zip(sources,values,[0,50,100]):
            for obj,changed in zip(original['objects'],value['objects']):
                for a,b in zip(obj['frames'],changed['frames']):
                    if a['valid']:
                        self.assertEqual(b['x'],a['x']-offset)
                        self.assertEqual(b['left'],a['left']-offset)
                        self.assertEqual((b['width'],b['height']),(a['width'],a['height']))
            self.assertEqual(original['inputs'],value['inputs'])

    def test_anchor_evidence_binds_actual_common_time_and_source(self):
        sources=[observations(),observations(.007,50),observations(.013,100)]
        proof=_common_stationary_anchor(sources,'a')
        for obs,row in zip(sources,proof):
            self.assertEqual(row['source_observations_sha256'],digest(obs))
            stamp=row['input_origin_s']+row['common_offset_s']
            self.assertLess(stamp,.4)
            self.assertLessEqual(min(row['bracket_times_s']),stamp+1e-9)
            self.assertGreaterEqual(max(row['bracket_times_s']),stamp-1e-9)
            self.assertLessEqual(max(row['bracket_times_s'])-min(row['bracket_times_s']),.12)

    def test_no_anchor_guessed_from_newborn_or_unmapped_id(self):
        sources=[observations()]*3
        for oid in ('d','invented'):
            with self.subTest(oid=oid),self.assertRaises(VisualError) as c:
                _spatial_observations(sources,binding({**VIEWPORT_ALIGNMENT,'mode':'visible_anchor','object_id':oid}))
            self.assertEqual(c.exception.code,'needs_spatial_binding')

    def test_moving_or_growing_preinput_anchor_is_rejected(self):
        for key in ('x','width'):
            obs=observations()
            for f in obs['objects'][0]['frames']:
                if f['time_s']<.4:f[key]+=20*f['time_s']
            with self.subTest(key=key),self.assertRaises(VisualError):_common_stationary_anchor([obs]*3,'a')

    def test_gap_invalid_sample_and_lifecycle_boundary_are_not_bridged(self):
        for kind in ('gap','invalid','lifecycle'):
            obs=observations();obj=obs['objects'][0]
            if kind=='gap':obj['frames']=[f for f in obj['frames'] if not .19<f['time_s']<.35]
            if kind=='invalid':obj['frames'][10]['valid']=False
            if kind=='lifecycle':obj['active_intervals_s']=[[.2,.3],[.325,1.5]]
            with self.subTest(kind=kind),self.assertRaises(VisualError):_common_stationary_anchor([observations(),obs,observations()],'a')

    def test_legacy_binding_readable_but_birth_origin_is_not_invented(self):
        legacy={'schema_version':1,'spatial_alignment':'initial_anchor_displacement'}
        with self.assertRaises(VisualError) as c:_spatial_observations([observations()]*3,legacy)
        self.assertEqual(c.exception.code,'needs_spatial_binding')
        obs=observations(birth=False);old,sizes=_relative_observations(obs,0.)
        rows,receipt=_spatial_observations([obs]*3,legacy)
        self.assertEqual(rows[0],old);self.assertTrue(receipt['historical_metrics_preserved'])
        self.assertEqual(legacy,{'schema_version':1,'spatial_alignment':'initial_anchor_displacement'})

    def test_window_time_translation_keeps_physical_coordinates_and_lifecycle(self):
        obs=observations();saved=deepcopy(obs)
        translated=_input_time_observations(obs,.4)
        self.assertEqual(obs,saved)
        for a,b in zip(obs['objects'][1]['frames'],translated['objects'][1]['frames']):
            self.assertEqual((a['x'],a['y'],a['width'],a['height']),(b['x'],b['y'],b['width'],b['height']))
            self.assertAlmostEqual(b['time_s'],a['time_s']-.4)
        self.assertAlmostEqual(translated['objects'][1]['active_intervals_s'][0][0],.05)

    def test_legacy_vfr_median_cannot_reach_back_into_unguarded_motion(self):
        obs=observations(birth=False)
        for obj in obs['objects']:
            obj['frames']=[f for f in obj['frames'] if f['time_s']<.15 or f['time_s']>=.3]
            for f in obj['frames']:
                if f['time_s']<.15:f['x']-=20*f['time_s']
        # The recent stationary 100ms is observable; the old eight-frame
        # median would additionally consume older moving samples.
        _common_stationary_anchor([obs]*3,'a')
        with self.assertRaises(VisualError) as caught:
            _spatial_observations([obs]*3,{'schema_version':1,'spatial_alignment':'initial_anchor_displacement'})
        self.assertEqual(caught.exception.code,'needs_spatial_binding')

    def test_same_behavior_variable_sample_cadence_compares_successfully(self):
        ref=record(observations());target=record(observations(.007))
        self.assertTrue(_compare(ref,ref,target,binding())['pass'])

    def test_real_delay_endpoint_early_motion_and_size_errors_remain_failures(self):
        ref=record(observations())
        for kind in ('delay','endpoint','early_motion','size'):
            obs=observations(delay=.2 if kind=='delay' else 0.)
            for f in obs['objects'][1]['frames']:
                if not f['valid']:continue
                if kind=='endpoint' and f['time_s']>.9:f['x']+=4
                if kind=='early_motion' and f['time_s']<.8:f['x']+=10
                if kind=='size':f['width']+=2
            with self.subTest(kind=kind):self.assertFalse(_compare(ref,ref,record(obs),binding())['pass'])

    def test_viewport_mismatch_and_undeclared_rescale_fail_closed(self):
        obs=observations();other=observations();other['image_size']=[800,600]
        with self.assertRaises(VisualError):_spatial_observations([obs,obs,other],binding())
        with self.assertRaises(VisualError):_spatial_observations([obs]*3,binding({**VIEWPORT_ALIGNMENT,'scale':2}))


if __name__=='__main__':unittest.main()
