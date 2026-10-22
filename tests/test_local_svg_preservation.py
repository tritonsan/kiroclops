"""Pure measured-geometry gates and uploaded graph transport; no renderer."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.geometry_acceptance import svg_preservation, validate_svg_selection
from kiroclops.local_engine.media_session import binding_preset, _validate_binding, _target_record, _compare, VIEWPORT_ALIGNMENT
from kiroclops.local_engine.evaluation import _compare_web, _lifecycle_bindings
from kiroclops.visual.schema import VisualError


def observations(width=100, count=3):
    objects=[]
    for n in range(count):
        left=n*(width+20.)
        frames=[{'index':i,'time_s':i*.05,'valid':True,'present':True,'expected_active':True,
                 'x':left+10.,'y':30.,'left':left,'top':10.,'width':float(width),'height':40.,
                 'geometry_method':'contour'} for i in range(31)]
        objects.append({'id':chr(97+n),'method':'template','valid_ratio':1.,'frames':frames,'active_intervals_s':[[0.,1.5]]})
    edges=[]
    for n in range(1,count):
        a,b=objects[0],objects[n]
        edges.append({'id':'a-'+b['id'],'from_object':'a','to_object':b['id'],
                      'frames':[{'index':i,'time_s':i*.05,'valid':True,
                                 'start':[float(width),30.],'end':[b['frames'][i]['left'],30.]} for i in range(31)]})
    return {'image_size':[640,360],'objects':objects,'connections':edges,'status':'ok','inputs':{'provenance':'user_declared',
            'events':[{'action':'click','selector':'#add-node','time_s':.3}]}}


def specs(obs):
    return [{k:e[k] for k in ('id','from_object','to_object')}|{'method':'line'} for e in obs['connections']]


def record(obs):
    return {'scenario':{'id':'test','domain':'svg'},'observations':obs,
            'selection':{'connections':specs(obs)},'functional':{'status':'pass'},'measurement_status':'ok'}


class SvgGeometryTests(unittest.TestCase):
    def test_true_outer_geometry_and_visible_endpoints_pass(self):
        obs=observations();result=svg_preservation(obs,deepcopy(obs),specs(obs))
        self.assertTrue(result['pass']);self.assertEqual('ok',result['measurement_status'])
        self.assertIsNone(result['failure_class'])

    def test_overlap_is_observed_preservation_failure_not_tracking_failure(self):
        ref=observations();candidate=deepcopy(ref)
        candidate['objects'][1]['frames'][-1]['left']=99.
        result=svg_preservation(ref,candidate,specs(ref))
        self.assertFalse(result['pass']);self.assertEqual('preservation_violation',result['failure_class'])
        self.assertEqual('ok',result['measurement_status']);self.assertTrue(result['observed_preservation_violation'])
        self.assertEqual(40.,result['final_no_overlap']['pairs'][0]['overlap_area_px2'])

    def test_touching_rectangles_have_zero_overlap(self):
        obs=observations();obs['objects'][1]['frames'][-1]['left']=100.
        obs['connections'][0]['frames'][-1]['end']=[100.,30.]
        result=svg_preservation(obs,deepcopy(obs),specs(obs))
        self.assertTrue(result['pass'])

    def test_icon_rectangles_cannot_substitute_for_measured_card_boxes(self):
        obs=observations();candidate=deepcopy(obs)
        for obj in candidate['objects']:
            for f in obj['frames']:f.pop('geometry_method')
        result=svg_preservation(obs,candidate,specs(obs))
        self.assertFalse(result['pass']);self.assertEqual('insufficient_observability',result['failure_class'])
        self.assertFalse(result['observed_preservation_violation'])

    def test_invalid_final_frame_never_uses_earlier_valid_box(self):
        obs=observations();candidate=deepcopy(obs);candidate['objects'][0]['frames'][-1]['valid']=False
        result=svg_preservation(obs,candidate,specs(obs))
        self.assertFalse(result['final_no_overlap']['pass'])
        self.assertEqual('insufficient_observability',result['measurement_status'])

    def test_missing_or_wrong_bound_or_unobservable_connection_cannot_pass(self):
        ref=observations()
        for mode in ('missing','binding','invalid','duplicate','nonfinite'):
            with self.subTest(mode=mode):
                candidate=deepcopy(ref)
                if mode=='missing':candidate['connections']=[]
                elif mode=='binding':candidate['connections'][0]['to_object']='c'
                elif mode=='invalid':candidate['connections'][0]['frames'][2]['valid']=False
                elif mode=='duplicate':candidate['connections'][0]['frames'].append(candidate['connections'][0]['frames'][0])
                else:candidate['connections'][0]['frames'][0]['start']=[None,30.]
                result=svg_preservation(ref,candidate,specs(ref))
                self.assertFalse(result['pass']);self.assertEqual('insufficient_observability',result['failure_class'])

    def test_width_scaled_endpoint_distance_is_measured(self):
        for width,limit in ((100,2.),(300,3.)):
            ref=observations(width);candidate=deepcopy(ref)
            candidate['connections'][0]['frames'][4]['start'][0]=width+limit
            self.assertTrue(svg_preservation(ref,candidate,specs(ref))['pass'])
            candidate['connections'][0]['frames'][4]['start'][0]+=.01
            result=svg_preservation(ref,candidate,specs(ref))
            self.assertEqual('preservation_violation',result['failure_class'])
            self.assertEqual('ok',result['measurement_status'])

    def test_removed_node_is_not_forced_into_final_boxes_but_live_edges_still_required(self):
        ref=observations(count=4)
        for f in ref['objects'][-1]['frames'][8:]:f.update(expected_active=False,valid=False,present=False)
        ref['connections'][-1]['frames']=ref['connections'][-1]['frames'][:8]
        result=svg_preservation(ref,deepcopy(ref),specs(ref))
        self.assertTrue(result['pass']);self.assertEqual(['a','b','c'],result['final_no_overlap']['expected_object_ids'])

    def test_web_final_gate_cannot_be_bypassed_by_other_pass_summaries(self):
        ref=record(observations());candidate=deepcopy(ref)
        candidate['observations']['objects'][0]['frames'][-1]['valid']=False
        contract={'require_final_no_overlap':True,'object_pairs':[]}
        with patch('kiroclops.local_engine.evaluation._contract',return_value=contract),\
             patch('kiroclops.visual.comparison.compare_observations',return_value={'success':True,'measurement_status':'pass','object_windows':[]}),\
             patch('kiroclops.local_engine.evaluation._dynamic_checks',return_value=[]):
            result=_compare_web(ref,ref,candidate,improvement=False)
        self.assertFalse(result['pass']);self.assertFalse(result['measurement_pass'])
        self.assertEqual('insufficient_observability',result['failure_class'])

    def test_media_gate_uses_original_outer_coordinates_not_aligned_icon_positions(self):
        ref=record(observations());binding={'version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT),'scenario':{'domain':'svg'},'connections':specs(ref['observations']),
            'input_provenance':'user_declared','claim':'conditional_on_user_declared_inputs',
            'lifecycle_bindings':_lifecycle_bindings({'objects':ref['observations']['objects']},
                {'frames':ref['observations']['objects'][0]['frames'],'inputs':ref['observations']['inputs']})}
        self.assertTrue(_compare(ref,ref,deepcopy(ref),binding)['pass'])
        candidate=deepcopy(ref);candidate['observations']['objects'][1]['frames'][-1]['left']=99.
        result=_compare(ref,ref,candidate,binding)
        self.assertFalse(result['pass']);self.assertEqual('preservation_violation',result['failure_class'])

    def test_media_removed_initial_object_has_preinput_evidence_and_absence_checks(self):
        obs=observations(count=4);obs['inputs']['events'][0]['selector']='#remove-node'
        obs['objects'][-1]['active_intervals_s']=[[0.,.25]]
        for f in obs['objects'][-1]['frames'][6:]:f.update(expected_active=False,present=False,valid=False)
        obs['connections'][-1]['frames']=obs['connections'][-1]['frames'][:6]
        ref=record(obs);binding={'version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT),'scenario':{'domain':'svg'},'connections':specs(obs),'input_provenance':'user_declared','claim':'conditional',
            'lifecycle_bindings':_lifecycle_bindings({'objects':obs['objects']},{'frames':obs['objects'][0]['frames'],'inputs':obs['inputs']})}
        result=_compare(ref,ref,deepcopy(ref),binding)
        self.assertTrue(result['pass'],result)
        self.assertTrue(any(r['window_id']=='initial-preservation' and r['reference_id']=='d' for w in result['windows'] for r in w['object_windows']))
        candidate=deepcopy(ref);candidate['observations']['objects'][-1]['frames'][-1]['present']=True
        self.assertFalse(_compare(ref,ref,candidate,binding)['pass'])

    def test_short_first_actual_response_is_not_hidden_by_later_long_response(self):
        obs=observations();obs['inputs']['events']=[
            {'action':'click','selector':'#open-panel','time_s':.3},
            {'action':'click','selector':'#close-panel','time_s':.34}]
        ref=record(obs);binding={'version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT),'scenario':{'domain':'panel'},'input_provenance':'user_declared','claim':'conditional'}
        before=deepcopy(ref)
        with self.assertRaises(VisualError) as caught:_compare(ref,before,deepcopy(ref),binding)
        self.assertEqual('insufficient_observability',caught.exception.code)
        self.assertEqual(before,ref) # No altered input times or manufactured pixels.

    def test_missing_reference_edge_is_not_qualified_by_a_complete_candidate(self):
        complete=record(observations());ref=deepcopy(complete)
        ref['observations']['connections'][0]['frames'][4]['valid']=False
        binding={'version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT),'scenario':{'domain':'svg'},'connections':specs(ref['observations']),
            'input_provenance':'user_declared','claim':'conditional',
            'lifecycle_bindings':_lifecycle_bindings({'objects':ref['observations']['objects']},
                {'frames':ref['observations']['objects'][0]['frames'],'inputs':ref['observations']['inputs']})}
        result=_compare(ref,complete,deepcopy(complete),binding)
        self.assertFalse(result['pass']);self.assertTrue(result['svg_geometry_preservation']['pass'])
        self.assertEqual('insufficient_observability',result['measurement_status'])
        self.assertEqual('insufficient_observability',result['reference_failure_class'])
        self.assertFalse(result['reference_svg_geometry_preservation']['observed_preservation_violation'])

    def test_measured_detached_reference_is_invalid_matching_contract_not_missing_measurement(self):
        complete=record(observations());ref=deepcopy(complete)
        ref['observations']['connections'][0]['frames'][4]['start'][0]+=5.
        binding={'version':2,'spatial_alignment':deepcopy(VIEWPORT_ALIGNMENT),'scenario':{'domain':'svg'},'connections':specs(ref['observations']),
            'input_provenance':'user_declared','claim':'conditional',
            'lifecycle_bindings':_lifecycle_bindings({'objects':ref['observations']['objects']},
                {'frames':ref['observations']['objects'][0]['frames'],'inputs':ref['observations']['inputs']})}
        result=_compare(ref,complete,deepcopy(complete),binding)
        self.assertFalse(result['pass']);self.assertEqual('reference_invalid',result['failure_class'])
        self.assertEqual('preservation_violation',result['reference_failure_class'])
        self.assertEqual('ok',result['measurement_status'])

    def test_media_graph_cannot_infer_an_omitted_spatial_binding(self):
        ref=record(observations())
        binding={'scenario':{'domain':'svg'},'connections':specs(ref['observations']),
                 'input_provenance':'user_declared','claim':'conditional'}
        with self.assertRaises(VisualError) as caught:
            _compare(ref,ref,deepcopy(ref),binding)
        self.assertEqual('needs_spatial_binding',caught.exception.code)


class SvgBindingTests(unittest.TestCase):
    def inputs(self):
        manifest={'image_size':[320,320],'package_sha256':'p','frames':[{'time_s':i*.2} for i in range(8)],
            'inputs':{'provenance':'user_declared','events':[{'action':'click','selector':'#add-node','time_s':.4}]}}
        selection={'objects':[{'id':x,'roi':[5,5,10,10],'geometry':{'method':'contour','roi':[0,0,30,30]},
                 'active_intervals_s':[[.4 if x=='d' else 0.,1.4]]} for x in 'abcd'],
            'connections':[{'id':'edge-'+x,'from_object':'a','to_object':x,'method':'polyline',
             'stroke_rgb':[12,84,201],'stroke_tolerance':9,'search_margin_px':80,
             'stroke_sample':{'frame_index':3,'pixel':[4,5],'frame_sha256':'s'}} for x in 'bcd']}
        # Preserve the exact native floating timestamp, as the real UI does.
        for o in selection['objects']:o['active_intervals_s'][0][-1]=manifest['frames'][-1]['time_s']
        project={'domain':'svg','recommended_capture_actions':[{'action':'click','selector':'#add-node'}]}
        return {},project,manifest,selection

    def test_complete_graph_and_polyline_options_are_bound(self):
        state,project,manifest,selection=self.inputs()
        with patch('kiroclops.local_engine.media_session._intake',return_value=(state,project,manifest,selection)):
            binding=binding_preset('unused',object_map={x:x for x in 'abcd'},initial_node_count=3)
        self.assertEqual(selection['connections'],binding['connections'])
        self.assertEqual(4,binding['scenario']['expected_final_node_count'])
        changed=deepcopy(binding);changed['connections'][0]['stroke_rgb'][0]+=1
        with self.assertRaises(VisualError):_validate_binding(changed,project,manifest,selection)

    def test_partial_missing_and_icon_only_graphs_block_transfer(self):
        data=self.inputs()
        for mutation in ('missing','partial','icon','invalid'):
            with self.subTest(mutation=mutation):
                selection=deepcopy(data[3])
                if mutation=='missing':selection['connections']=[]
                elif mutation=='partial':selection['connections']=selection['connections'][:1]
                elif mutation=='icon':selection['objects'][0].pop('geometry')
                else:selection['connections'][0]=None
                with self.assertRaises(VisualError):validate_svg_selection(selection)

    def test_missing_new_node_correspondence_is_not_silently_omitted(self):
        state,project,manifest,selection=self.inputs();selection['objects']=selection['objects'][:3];selection['connections']=selection['connections'][:2]
        with patch('kiroclops.local_engine.media_session._intake',return_value=(state,project,manifest,selection)):
            with self.assertRaises(VisualError) as caught:binding_preset('unused',object_map={x:x for x in 'abc'},initial_node_count=3)
        self.assertEqual('needs_connection_binding',caught.exception.code)

    def test_unknown_input_is_still_blocked_without_graph_inference(self):
        state,project,manifest,selection=self.inputs();manifest['inputs']={'provenance':'unknown','events':[]}
        with patch('kiroclops.local_engine.media_session._intake',return_value=(state,project,manifest,selection)):
            result=binding_preset('unused',object_map={x:x for x in 'abcd'},initial_node_count=3)
        self.assertEqual('needs_input_binding',result['status']);self.assertFalse(result['inferred_events'])

    def test_target_mapping_keeps_reference_graph_and_actual_pixel_selected_geometry(self):
        _,_,manifest,selection=self.inputs();mapping={o['id']+'-ref':o['id'] for o in selection['objects']}
        edges=deepcopy(selection['connections'])
        for edge in edges:edge['from_object']+='-ref';edge['to_object']+='-ref'
        life=_lifecycle_bindings(selection,manifest);life={k+'-ref':v for k,v in life.items()}
        binding={'object_map':mapping,'scenario':{'domain':'svg'},'connections':edges,'lifecycle_bindings':life}
        detected=deepcopy(selection);detected['connections']=[]
        reference_selection=deepcopy(selection)
        for obj in reference_selection['objects']:obj['id']+='-ref'
        with tempfile.TemporaryDirectory() as tmp,\
             patch('kiroclops.local_engine.evaluation._capture_web',return_value={'package':'unused','source_sha256':'source'}),\
             patch('kiroclops.web_local.selection.select_package_from_pixels',return_value=detected),\
             patch('kiroclops.local_engine.media_session.load_package',return_value=manifest),\
             patch('kiroclops.local_engine.evaluation._analysis',return_value={'measurement_status':'ok','observations':{},'events':{}}) as analysis:
            result=_target_record('project',Path(tmp),binding,{'selection':reference_selection},lambda:None,lambda *a:None,lambda:False,'after',analyze=True)
        actual=analysis.call_args.args[1]
        self.assertEqual(edges,actual['connections']);self.assertEqual(set(mapping),{o['id'] for o in actual['objects']})
        self.assertEqual(selection['objects'][0]['geometry'],actual['objects'][0]['geometry'])
        self.assertEqual(edges,result['selection']['connections'])
        self.assertEqual(2.,analysis.call_args.kwargs['contract']['event_options']['connection_tolerance_px'])


if __name__=='__main__':unittest.main()
