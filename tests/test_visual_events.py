import unittest

from kiroclops.visual.events import extract_events
from kiroclops.visual.schema import VisualError


def observed(xs,times=None,ys=None,oid='a'):
    times=times or [i*.05 for i in range(len(xs))]
    ys=ys or [10]*len(xs)
    return {'id':oid,'frames':[{'index':i,'time_s':t,'valid':x is not None,'x':x,'y':ys[i] if x is not None else None,
        'left':x-2 if x is not None else None,'top':ys[i]-2 if x is not None else None,'width':4 if x is not None else None,'height':4 if x is not None else None} for i,(t,x) in enumerate(zip(times,xs))]}


class EventTests(unittest.TestCase):
    def test_actual_time_motion_and_reversal(self):
        result=extract_events({'objects':[observed([10,11,12,10],[0,.1,.2,.25])]},contract={'event_options':{'max_gap_s':.11}})
        types=[e['type'] for e in result['events']]
        self.assertEqual(types,['motion_start','reversal'])
        self.assertEqual(result['events'][-1]['time_interval_s'],[.2,.25])

    def test_speed_uses_seconds_not_index(self):
        result=extract_events({'objects':[observed([10,10.1,10.2],[0,.1,.2])]})
        self.assertFalse(result['events'])

    def test_rest_duration_emits_visible_settlement(self):
        result=extract_events({'objects':[observed([10,12,14,14,14,14,14,14])]})
        settled=[e for e in result['events'] if e['type']=='settled']
        self.assertEqual(len(settled),1)
        self.assertLessEqual(settled[0]['time_interval_s'][1]-settled[0]['time_interval_s'][0],.1)
        self.assertGreaterEqual(settled[0]['rest_duration_s'],.2)
        self.assertGreater(settled[0]['confirmed_at_s'],settled[0]['time_interval_s'][1])
        self.assertTrue(settled[0]['support_frame_indices'])
        self.assertEqual(1,len([e for e in result['events'] if e['type']=='stop']))

    def test_gap_and_invalid_frame_do_not_supply_rest(self):
        for obj in [observed([10,12,None,14,14,14,14,14]),observed([10,12,14,14,14,14],[0,.05,.5,.55,.6,.7])]:
            result=extract_events({'objects':[obj]})
            self.assertFalse([e for e in result['events'] if e['type']=='settled'])
            self.assertEqual(result['status'],'partial')

    def test_slow_crossing_through_zero_not_missed(self):
        selection={'boundaries':[{'id':'wall','points':[[20,0],[20,30]]}]}
        result=extract_events({'objects':[observed([14,17,19,20,21,23,26])]},selection)
        crossing=[e for e in result['events'] if e['type']=='boundary_cross']
        self.assertEqual(len(crossing),1)
        self.assertEqual(crossing[0]['frame_indices'],[1,5])

    def test_finite_boundary_does_not_become_infinite_wall(self):
        selection={'boundaries':[{'id':'wall','points':[[20,60],[20,80]]}]}
        result=extract_events({'objects':[observed([14,17,19,20,21,23,26])]},selection)
        self.assertFalse([e for e in result['events'] if e['type'].startswith('boundary_')])
        self.assertTrue(all(r['distance_px']>40 for r in result['relations']))

    def test_connection_detachment_uses_visible_endpoints_not_centers(self):
        a,b=observed([10]*3),observed([30]*3,oid='b')
        connection={'id':'ab','from_object':'a','to_object':'b','frames':[
            {'index':i,'time_s':i*.05,'valid':True,'start':start,'end':[28,10]} for i,start in enumerate([[12,10],[19,10],[12,10]])]}
        result=extract_events({'objects':[a,b],'connections':[connection]})
        self.assertEqual([e['type'] for e in result['events']],['connection_detached','connection_attached'])
        self.assertEqual([r['visually_attached'] for r in result['relations'] if r['type']=='connection_attachment'],[True,False,True])

    def test_real_panel_repeated_frame_does_not_restart_continuous_motion(self):
        # Literal native pixel observations from web-corpus-01/reference panel0,
        # bookmark frames5..20. Frame13 repeats frame12 during the same flight.
        times=[.242974281311,.250910758972,.291636228561,.301256895065,.412097930908,
               .417113780975,.447701692581,.460376739502,.468305826187,.480237722397,
               .495938777924,.512442827225,.528919696808,.546159744263,.562911748886,.580473661423]
        xs=[264.5]*6+[279.5,313.5,313.5,361.5,417.5,477.5,538.5,597.5,652.5,702.5]
        result=extract_events({'objects':[observed(xs,times)]})
        self.assertEqual(1,len([e for e in result['events'] if e['type']=='motion_start']))
        self.assertFalse([e for e in result['events'] if e['type'] in ('stop','settled')])

    def test_restart_requires_sustained_rest_and_pixel_noise_is_not_motion(self):
        xs=[0]*5+[3,6,6,6,6,6,6,9,12]
        result=extract_events({'objects':[observed(xs)]})
        self.assertEqual(2,len([e for e in result['events'] if e['type']=='motion_start']))
        self.assertEqual(1,len([e for e in result['events'] if e['type']=='stop']))
        noise=extract_events({'objects':[observed([0,1,0,1,0,1,0,1])]})
        self.assertFalse(noise['events'])

    def test_overshoot_and_oscillation_need_observed_stable_endpoint(self):
        xs=[0]*5+[3,7,12,8,12,10]+[10]*7
        result=extract_events({'objects':[observed(xs)]})
        over=[e for e in result['events'] if e['type']=='overshoot'];osc=[e for e in result['events'] if e['type']=='oscillation']
        self.assertEqual(1,len(over));self.assertEqual(2.,over[0]['amplitude_px'])
        self.assertEqual('observed_stable_tail',over[0]['endpoint_source']);self.assertFalse(over[0]['intended_target_inferred'])
        self.assertEqual(1,len(osc));self.assertEqual(2,osc[0]['sign_changes'])
        self.assertEqual(4.,osc[0]['peak_to_peak_px'])
        self.assertLessEqual(osc[0]['uncertainty_s'],.1)
        unfinished=extract_events({'objects':[observed(xs[:10])]})
        self.assertFalse([e for e in unfinished['events'] if e['type'] in ('overshoot','oscillation')])
        self.assertFalse(unfinished['motion_episodes'][-1]['endpoint_observed'])

    def test_new_command_censors_endpoint_interpretation_but_not_visible_reversal(self):
        xs=[0]*5+[3,7,12,8,12,10]+[10]*7
        value={'objects':[observed(xs)],'inputs':{'events':[{'time_s':.4,'action':'click'}]}}
        result=extract_events(value)
        self.assertTrue([e for e in result['events'] if e['type']=='reversal'])
        self.assertFalse([e for e in result['events'] if e['type']=='overshoot'])
        self.assertTrue(result['motion_episodes'][-1]['interrupted_by_recorded_input'])

    def test_overshoot_onset_and_broad_visible_peak_plateau_are_separate(self):
        xs=[0]*12+[3,7]+[12]*8+[10]*15
        times=[i*.02 for i in range(len(xs))]
        result=extract_events({'objects':[observed(xs,times)]})
        event=next(e for e in result['events'] if e['type']=='overshoot')
        self.assertEqual(event['frame_indices'],[13,14])
        self.assertAlmostEqual(event['uncertainty_s'],.02)
        self.assertEqual(event['timing_status'],'resolved')
        self.assertGreater(event['peak_uncertainty_s'],.1)
        self.assertEqual(event['peak_timing_status'],'unresolved')
        self.assertEqual(event['peak_support_frame_indices'],list(range(14,22)))
        self.assertEqual(event['amplitude_px'],2.)
        self.assertEqual(event['onset_threshold_px'],1.5)
        self.assertFalse(event['physical_peak_time_inferred'])
        self.assertEqual(event['definition'],'first_visible_excursion_beyond_observed_endpoint_band')

    def test_overshoot_onset_remains_unresolved_when_its_own_observation_gap_is_large(self):
        xs=[0]*12+[3,7]+[12]*8+[10]*15
        times=[i*.02+(.09 if i>=14 else 0) for i in range(len(xs))]
        result=extract_events({'objects':[observed(xs,times)]})
        event=next(e for e in result['events'] if e['type']=='overshoot')
        self.assertAlmostEqual(event['uncertainty_s'],.11)
        self.assertEqual(event['timing_status'],'unresolved')
        self.assertEqual(event['amplitude_px'],2.)

    def test_initial_input_inside_first_motion_bracket_is_not_an_interruption(self):
        xs=[0]*5+[3,7,12,8,12,10]+[10]*7
        value={'objects':[observed(xs)],'inputs':{'events':[{'time_s':.23,'action':'click'}]}}
        result=extract_events(value)
        self.assertTrue([e for e in result['events'] if e['type']=='overshoot'])
        self.assertFalse(result['motion_episodes'][-1]['interrupted_by_recorded_input'])

    def test_native_direction_and_release_commands_censor_endpoint_claim(self):
        xs=[0]*5+[3,7,12,8,12,10]+[10]*7
        for action,pressed in [('left',True),('right',False),('jump',False)]:
            with self.subTest(action=action,pressed=pressed):
                commands=[{'time_s':.23,'action':'right','pressed':True},
                          {'time_s':.4,'action':action,'pressed':pressed}]
                result=extract_events({'objects':[observed(xs)],'inputs':{'provenance':'recorder_verified','events':commands}})
                self.assertTrue([e for e in result['events'] if e['type']=='reversal'])
                self.assertFalse([e for e in result['events'] if e['type'] in ('overshoot','oscillation')])
                episode=result['motion_episodes'][-1]
                self.assertTrue(episode['interrupted_by_recorded_input'])
                self.assertEqual(episode['interrupting_inputs'],[commands[1]])

    def test_repeated_held_state_is_not_a_new_command(self):
        xs=[0]*5+[3,7,12,8,12,10]+[10]*7
        events=[{'time_s':.23,'action':'right','pressed':True},{'time_s':.4,'action':'right','pressed':True}]
        result=extract_events({'objects':[observed(xs)],'inputs':{'events':events}})
        self.assertTrue([e for e in result['events'] if e['type']=='overshoot'])
        self.assertFalse(result['motion_episodes'][-1]['interrupted_by_recorded_input'])

    def test_pose_sensitive_anchor_motion_never_claims_body_translation(self):
        obj=observed([0]*15,ys=[0]*6+[2]*9)
        result=extract_events({'objects':[obj]})
        starts=[e for e in result['events'] if e['type']=='motion_start']
        self.assertEqual(len(starts),1)
        for value in result['events']+result['motion_episodes']:
            self.assertEqual(value['motion_basis'],'tracked_anchor')
            self.assertFalse(value['rigid_body_translation_inferred'])
            self.assertIn('pose',value['motion_scope'])

    def test_vertical_relative_reversal_uses_own_observed_axis(self):
        ys=[0]*5+[3,6,9,6,3,0]+[0]*6
        parent=observed([i*20 for i in range(len(ys))],oid='parent')
        child=observed([i*20+30 for i in range(len(ys))],ys=ys,oid='child')
        selection={'relative_motion':[{'object_id':'child','relative_to':'parent'}]}
        result=extract_events({'objects':[parent,child]},selection)
        relative=[e for e in result['events'] if e.get('motion_reference')=='relative']
        self.assertEqual(1,len([e for e in relative if e['type']=='reversal']))
        self.assertTrue(all(e['axis']=='y' and e['axis_selection']=='observed_dominant_displacement_range' for e in relative))
        self.assertTrue(all(e['axis']=='x' for e in result['events'] if e.get('motion_reference')!='relative'))

    def test_explicit_axis_is_preserved_and_relative_override_is_supported(self):
        ys=[0]*5+[3,6,9,6,3,0]+[0]*6
        parent=observed([0]*len(ys),oid='parent');child=observed([30]*len(ys),ys=ys,oid='child')
        selection={'relative_motion':[{'object_id':'child','relative_to':'parent'}]}
        contract={'event_options':{'axis':'x'}}
        result=extract_events({'objects':[parent,child]},selection,contract)
        self.assertFalse([e for e in result['events'] if e['type']=='reversal'])
        self.assertTrue(all(e['axis']=='x' and e['axis_selection']=='explicit' for e in result['events']))
        selection['relative_motion'][0]['axis']='y'
        result=extract_events({'objects':[parent,child]},selection,contract)
        self.assertEqual(1,len([e for e in result['events'] if e['type']=='reversal' and e.get('motion_reference')=='relative']))
        self.assertTrue(all(e['axis']=='y' and e['axis_selection']=='explicit' for e in result['events'] if e.get('motion_reference')=='relative'))

    def test_overlap_and_distance_are_visible_geometry_not_physics(self):
        result=extract_events({'objects':[observed([0,4,8,4,0]),observed([10]*5,oid='b')]})
        pair=[r for r in result['relations'] if r['type']=='object_pair_geometry']
        self.assertEqual([6.,2.,0.,2.,6.],[r['edge_distance_px'] for r in pair])
        self.assertEqual(8.,pair[2]['overlap_area_px2'])
        self.assertFalse(any(r['physical_contact_inferred'] for r in pair))
        self.assertEqual(['overlap_enter','overlap_leave'],[e['type'] for e in result['events'] if e['type'].startswith('overlap_')])

    def test_relative_child_onsets_ignore_shared_parent_translation(self):
        parent=observed([i*10 for i in range(20)],oid='parent')
        a=observed([i*10+30 for i in range(20)],ys=[20]*6+[23,26,29,32]+[32]*10,oid='title')
        b=observed([i*10+60 for i in range(20)],ys=[40]*9+[43,46,49,52]+[52]*7,oid='content')
        selection={'relative_motion':[{'object_id':'title','relative_to':'parent'},{'object_id':'content','relative_to':'parent'}]}
        result=extract_events({'objects':[parent,a,b]},selection)
        starts=[e for e in result['events'] if e['type']=='motion_start' and e.get('motion_reference')=='relative']
        self.assertEqual(2,len(starts));self.assertGreater(starts[0]['time_interval_s'][0],.2)
        order=[r for r in result['relations'] if r['type']=='relative_event_timing' and r['event_type']=='motion_start' and r['relative_to_object_id']=='parent'][0]
        self.assertEqual('before',order['order']);self.assertAlmostEqual(.1,order['lag_interval_s'][0]);self.assertAlmostEqual(.2,order['lag_interval_s'][1])
        self.assertFalse(order['causal_dependency_inferred'])

    def test_event_onset_uncertainty_is_not_confirmation_duration(self):
        result=extract_events({'objects':[observed([0,0,3],[0,.11,.22])]})
        event=[e for e in result['events'] if e['type']=='motion_start'][0]
        self.assertEqual('unresolved',event['timing_status']);self.assertAlmostEqual(.11,event['uncertainty_s'])

    def test_unobservable_connection_is_not_preserved(self):
        a,b=observed([10]*2),observed([30]*2,oid='b')
        connection={'id':'ab','from_object':'a','to_object':'b','frames':[{'index':0,'time_s':0,'valid':False}]}
        result=extract_events({'objects':[a,b],'connections':[connection]})
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['unobservable'])

    def test_expected_absence_does_not_report_tracking_gap(self):
        obj=observed([None,10,11,None])
        for index in (0,3):obj['frames'][index]['expected_active']=False
        result=extract_events({'objects':[obj]})
        self.assertFalse(result['unobservable'])

    def test_bad_times_and_unknown_connection_rejected(self):
        with self.assertRaises(VisualError):extract_events({'objects':[observed([1,2],[0,0])]})
        with self.assertRaises(VisualError):extract_events({'objects':[observed([1,2])],'connections':[{'from_object':'a','to_object':'missing'}]})


if __name__=='__main__':unittest.main()
