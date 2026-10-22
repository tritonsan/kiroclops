"""Regression guards for the verified post-cohort repair; no acceptance relaxation."""
import unittest
from unittest.mock import patch
from kiroclops.integration.storage import IntegrationError
from kiroclops.portable_v2 import measurement as module


def report(rise=15,fall=14,rounded=True):
    frames=[{'frame_index':i,'time_s':round(i/60,6) if rounded else i/60,'valid':True} for i in range(240)]
    start=29;apex=start+rise;land=apex+fall
    return {'fps':60,'frames':frames,'events':{'takeoff_frame':start,'apex_frame':apex,'landing_frame':land},
        'metrics':{'rise_s':frames[apex]['time_s']-frames[start]['time_s'],
                   'fall_s':frames[land]['time_s']-frames[apex]['time_s'],'height_body':2},
        'scenario':{'hold_ticks':8}}

class FrameTimingTests(unittest.TestCase):
    def row(self,a,b):
        with patch('kiroclops.comparison.curve_rmse',side_effect=[.5,.02]):
            return module._jump_row(a,a,b,True)

    def test_actual_rounded_two_frame_boundary_passes(self):
        a,b=report(),report(fall=12,rounded=False)
        self.assertGreater(abs(a['metrics']['fall_s']-b['metrics']['fall_s']),2/60)
        result=self.row(a,b)
        self.assertTrue(result['gates']['fall'])
        self.assertEqual(result['fall_error_s'],2/60)

    def test_three_frames_still_fail(self):
        self.assertFalse(self.row(report(),report(fall=11))['gates']['fall'])

    def test_ten_percent_boundary_uses_exact_integer_ratio(self):
        self.assertTrue(self.row(report(fall=30),report(fall=27))['gates']['fall'])
        self.assertFalse(self.row(report(fall=29),report(fall=26))['gates']['fall'])

    def test_rise_boundary_and_above(self):
        self.assertTrue(self.row(report(rise=20),report(rise=18))['gates']['rise'])
        self.assertFalse(self.row(report(rise=20),report(rise=17))['gates']['rise'])

    def test_no_event_invention(self):
        for value in (None, True, 44.0, -1, 500):
            r=report();r['events']['apex_frame']=value
            with self.subTest(value=value), self.assertRaises(IntegrationError):
                module._jump_frame_spans(r)

    def test_false_or_missing_visual_frame_fails(self):
        for change in ('invalid','missing','wrong-index','bad-time'):
            r=report();idx=r['events']['apex_frame']
            if change=='invalid':r['frames'][idx]['valid']=False
            if change=='missing':r['frames'].pop()
            if change=='wrong-index':r['frames'][idx]['frame_index']+=1
            if change=='bad-time':r['frames'][idx]['time_s']+=.001
            with self.subTest(change=change), self.assertRaises(IntegrationError):
                module._jump_frame_spans(r)

    def test_metrics_must_match_observed_events(self):
        for value in (float('nan'),.1,'0.233334',True):
            r=report();r['metrics']['fall_s']=value
            with self.subTest(value=value), self.assertRaises(IntegrationError):
                module._jump_frame_spans(r)

    def test_other_fps_rejected(self):
        r=report();r['fps']=59.94
        with self.assertRaises(IntegrationError):module._jump_frame_spans(r)

    def test_other_failed_gate_not_promoted(self):
        r=report(fall=12,rounded=False);r['metrics']['height_body']=1.5
        result=self.row(report(),r)
        self.assertTrue(result['gates']['fall'])
        self.assertFalse(result['gates']['height'])
        self.assertFalse(result['pass'])
