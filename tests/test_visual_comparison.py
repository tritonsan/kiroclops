import copy
import unittest

from kiroclops.visual.comparison import compare_observations, window_timing
from kiroclops.visual.schema import VisualError


def obs(times,xs=None,width=10):
    xs=xs if xs is not None else [10*t for t in times]
    return {'package_sha256':'a'*64,'image_size':[200,120],'objects':[{'id':'a','frames':[
        {'index':i,'time_s':t,'valid':x is not None,'x':x,'y':10 if x is not None else None,
         'left':x-5 if x is not None else None,'top':5 if x is not None else None,
         'width':width if x is not None else None,'height':10 if x is not None else None}
        for i,(t,x) in enumerate(zip(times,xs))]}]}


def contract(end=.3):
    return {'object_pairs':[{'reference_id':'a','candidate_id':'a','scale_px':1,'rmse_limit':.1,'size_rmse_limit':.1,'endpoint_limit':.1}],
        'windows':[{'id':'motion','start_s':0,'end_s':end}],'max_interpolation_gap_s':.12}


class ComparisonTests(unittest.TestCase):
    def two_responses(self, second=.5, *, speed=10., gap=False):
        times=[i/100 for i in range(101)]+[second,second+.4]
        times=sorted(set(times))
        value=obs(times,[10*t if t<second else speed*(t-second) for t in times])
        value['inputs']={'provenance':'recorder_verified','events':[
            {'time_s':0.,'action':'click','selector':'#toggle'},
            {'time_s':second,'action':'click','selector':'#toggle'}]}
        if gap:
            for f in value['objects'][0]['frames']:
                if second+.1<f['time_s']<second+.3:f['valid']=False
        return value

    def two_contract(self):
        cfg=contract();first={'mode':'input','action':'click','selector':'#toggle','ordinal':0}
        second={**first,'ordinal':1}
        cfg['windows']=[{'id':'first','start_s':0.,'end_s':.5,'alignment':first,'end_alignment':second},
                        {'id':'second','start_s':0.,'end_s':.4,'alignment':second}]
        return cfg

    def test_each_response_uses_own_actual_selector_ordinal_without_timewarp(self):
        a,b=self.two_responses(),self.two_responses(.5137)
        result=compare_observations(a,b,self.two_contract())
        self.assertEqual('pass',result['status'],result)
        rows=result['object_windows']
        self.assertAlmostEqual(.5137,rows[1]['input_timing']['candidate_origin_s'])
        self.assertAlmostEqual(.4,rows[1]['time_window_s'][1])
        self.assertFalse(rows[0]['endpoint_required'])
        self.assertTrue(rows[1]['endpoint_required'])
        self.assertFalse(rows[1]['input_timing']['duration_rescaled'])

    def test_earlier_interruption_shares_duration_without_sampling_post_input(self):
        a,b=self.two_responses(),self.two_responses(.487)
        result=compare_observations(a,b,self.two_contract())
        first=result['object_windows'][0]
        self.assertAlmostEqual(.487,first['time_window_s'][1])
        self.assertLess(first['valid_time_ratio'],1.) # No invented sample at the command boundary.
        self.assertIsNone(first['endpoint_error'])
        self.assertLess(first['position_rmse'],1e-8)

    def test_own_anchor_does_not_hide_wrong_speed_or_missing_frames(self):
        a=self.two_responses()
        for b in (self.two_responses(.5137,speed=5),self.two_responses(.5137,gap=True)):
            result=compare_observations(a,b,self.two_contract())
            self.assertFalse(result['success'])
            self.assertFalse(result['object_windows'][1]['pass'])

    def test_window_anchor_wrong_selector_missing_ordinal_and_unverified_reject(self):
        a=self.two_responses();cfg=self.two_contract()
        for kind in ('selector','ordinal','provenance'):
            b=copy.deepcopy(a)
            if kind=='selector':b['inputs']['events'][1]['selector']='#other'
            elif kind=='ordinal':b['inputs']['events'].pop()
            else:b['inputs']['provenance']='user_declared'
            with self.assertRaises(VisualError):compare_observations(a,b,cfg)

    def test_reordered_terminating_command_rejects(self):
        a=self.two_responses();b=copy.deepcopy(a)
        b['inputs']['events'][1]['time_s']=-.1
        with self.assertRaisesRegex(VisualError,'precedes'):
            window_timing(a,b,self.two_contract(),self.two_contract()['windows'][0])

    def test_relative_anchor_missing_frame_cannot_be_filled_with_zero(self):
        a=obs([0,.1,.2,.3]);b=copy.deepcopy(a)
        for item in (a,b):
            parent=copy.deepcopy(item['objects'][0]);parent['id']='parent';item['objects'].append(parent)
        b['objects'][1]['frames'][1]['valid']=False
        cfg=contract();cfg['object_pairs'][0].update(relative_to_reference='parent',relative_to_candidate='parent')
        cfg['object_pairs'].append({**cfg['object_pairs'][0],'reference_id':'parent','candidate_id':'parent','relative_to_reference':None,'relative_to_candidate':None})
        result=compare_observations(a,b,cfg)
        self.assertFalse(result['success']);self.assertFalse(result['object_windows'][0]['gates']['coverage'])

    def test_missing_contract_pending(self):
        result=compare_observations(obs([0,.1]),obs([0,.1]))
        self.assertEqual(result['status'],'pending');self.assertFalse(result['success'])

    def test_irregular_sampling_same_trajectory_passes_without_manual_claim(self):
        result=compare_observations(obs([0,.05,.12,.2,.3]),obs([0,.07,.15,.22,.3]),contract())
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['manual_validation_status'],'pending')
        self.assertLess(result['object_windows'][0]['position_rmse'],1e-8)

    def test_fast_candidate_excursion_between_reference_frames_not_hidden(self):
        result=compare_observations(obs([0,.1,.2,.3]),obs([0,.05,.1,.2,.3],[0,8,1,2,3]),contract())
        self.assertEqual(result['status'],'fail')
        self.assertGreater(result['object_windows'][0]['position_rmse'],1)

    def test_wrong_speed_cannot_be_timewarped(self):
        result=compare_observations(obs([0,.1,.2,.3]),obs([0,.1,.2,.3],[0,.5,1,1.5]),contract())
        self.assertEqual(result['status'],'fail')

    def test_input_alignment_requires_verified_input(self):
        cfg=contract();cfg['alignment']={'mode':'input','action':'go'}
        unknown=obs([0,.1,.2,.3]);unknown['inputs']={'provenance':'user_declared','events':[{'time_s':0,'action':'go'}]}
        with self.assertRaisesRegex(VisualError,'recorder-verified'):compare_observations(unknown,unknown,cfg)

    def test_verified_input_offset_not_speed_warp(self):
        cfg=contract();cfg['alignment']={'mode':'input','action':'go'}
        a=obs([0,.1,.2,.3]);b=obs([.1,.2,.3,.4],[0,1,2,3])
        a['inputs']={'provenance':'recorder_verified','events':[{'time_s':0,'action':'go'}]}
        b['inputs']={'provenance':'recorder_verified','events':[{'time_s':.1,'action':'go'}]}
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'pass')

    def test_missing_or_unbound_object_cannot_disappear_from_denominator(self):
        a=obs([0,.1,.2,.3]);b=copy.deepcopy(a);b['objects']=[]
        self.assertEqual(compare_observations(a,b,contract())['status'],'fail')
        b=copy.deepcopy(a);extra=copy.deepcopy(b['objects'][0]);extra['id']='extra';b['objects'].append(extra)
        self.assertEqual(compare_observations(a,b,contract())['status'],'fail')

    def test_invalid_gap_cannot_be_interpolated(self):
        result=compare_observations(obs([0,.1,.2,.3]),obs([0,.1,.2,.3],[0,None,2,3]),contract())
        self.assertEqual(result['measurement_status'],'fail')
        self.assertLess(result['object_windows'][0]['valid_time_ratio'],.5)

    def test_size_change_and_reference_fixed_scale(self):
        cfg=contract();cfg['object_pairs'][0]['scale_px']=10
        a=obs([0,.1,.2,.3]);b=obs([0,.1,.2,.3],[0,2,4,6],width=20)
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'fail')
        self.assertFalse(result['object_windows'][0]['gates']['size'])

    def test_each_window_checked_no_idle_average_hides_bad_motion(self):
        a=obs([i*.1 for i in range(11)]);b=copy.deepcopy(a);b['objects'][0]['frames'][2]['x']+=3
        cfg=contract(1);cfg['windows']=[{'id':'short','start_s':.1,'end_s':.3},{'id':'rest','start_s':.3,'end_s':1}]
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'fail')
        self.assertFalse(result['object_windows'][0]['pass']);self.assertTrue(result['object_windows'][1]['pass'])

    def test_lifecycle_reference_cannot_be_changed_by_candidate(self):
        a=obs([0,.1,.2,.3],[None,1,2,None]);b=copy.deepcopy(a)
        for item in (a,b):
            item['objects'][0]['active_intervals_s']=[[.1,.2]]
            for i in (0,3):item['objects'][0]['frames'][i]['present']=False
        cfg=contract();cfg['windows']=[{'id':'active','start_s':.1,'end_s':.2}]
        self.assertEqual(compare_observations(a,b,cfg)['status'],'pass')
        b['objects'][0]['active_intervals_s']=[[.1,.1]]
        self.assertEqual(compare_observations(a,b,cfg)['status'],'fail')
        b=copy.deepcopy(a);b['objects'][0]['frames'][3]['present']=True
        self.assertEqual(compare_observations(a,b,cfg)['status'],'fail')

    def test_global_window_intersects_only_reference_lifetime(self):
        a=obs([0,.1,.2,.3,.4],[None,1,2,3,None]);b=copy.deepcopy(a)
        for item in (a,b):
            item['objects'][0]['active_intervals_s']=[[.1,.3]]
            for i in (0,4):item['objects'][0]['frames'][i]['present']=False
        cfg=contract(.4)
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['object_windows'][0]['time_window_s'],[.1,.3])
        # A candidate cannot shrink its declared lifetime to excuse a missing frame.
        b['objects'][0]['active_intervals_s']=[[.2,.3]]
        b['objects'][0]['frames'][1].update(valid=False,x=None,y=None,width=None,height=None)
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'fail')
        self.assertEqual(result['object_windows'][0]['time_window_s'],[.1,.3])
        self.assertFalse(result['object_windows'][0]['gates']['coverage'])

    def test_float_roundoff_in_lifecycle_does_not_change_acceptance(self):
        a=obs([0,.1,.2,.3]);b=copy.deepcopy(a)
        a['objects'][0]['active_intervals_s']=[[0,.3]]
        b['objects'][0]['active_intervals_s']=[[0,.30000000000000004]]
        self.assertEqual(compare_observations(a,b,contract())['status'],'pass')

    def test_input_receipt_mapped_candidate_lifecycle_is_contract_bound(self):
        a=obs([0,.1,.2,.3,.4],[None,1,2,3,None]);b=copy.deepcopy(a)
        a['objects'][0]['active_intervals_s']=[[.1,.3]]
        b['objects'][0]['active_intervals_s']=[[.105,.305]]
        for value in (a,b):
            for i in (0,4):value['objects'][0]['frames'][i]['present']=False
        cfg=contract(.4);cfg['lifecycle_tolerance_s']=.006
        cfg['object_pairs'][0]['candidate_active_intervals_s']=[[.105,.305]]
        self.assertEqual(compare_observations(a,b,cfg)['status'],'pass')
        cfg['object_pairs'][0]['candidate_active_intervals_s']=[[.2,.3]]
        self.assertEqual(compare_observations(a,b,cfg)['status'],'fail')

    def test_rejected_measurement_and_implicit_scale_fail(self):
        a=obs([0,.1,.2,.3]);b=copy.deepcopy(a);b['status']='rejected'
        self.assertEqual(compare_observations(a,b,contract())['status'],'fail')
        b=copy.deepcopy(a);b['image_size']=[400,240]
        self.assertEqual(compare_observations(a,b,contract())['status'],'fail')

    def test_forbidden_boundary_is_measured(self):
        a=obs([0,.05,.1,.15,.2,.25,.3],[14,17,19,20,21,23,26]);b=copy.deepcopy(a)
        for value in (a,b):value['selection']={'boundaries':[{'id':'wall','points':[[20,0],[20,30]]}]}
        cfg=contract();cfg['forbid_boundary_cross']=['wall']
        result=compare_observations(a,b,cfg)
        self.assertEqual(result['status'],'fail')
        self.assertFalse(result['preservation_checks'][0]['pass'])


if __name__=='__main__':unittest.main()
