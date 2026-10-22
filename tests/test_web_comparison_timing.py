"""Capture gaps cannot hide beyond a fixed 1.8s motion-check window."""
import unittest
from kiroclops.web_local.capture import timing_summary


class ComparisonTimingTests(unittest.TestCase):
    def check_timing(self, times, events):
        return timing_summary(times,[{'uncertainty_s':.001,'epoch_in_host_bracket':True}],events)

    def test_long_response_gap_is_rejected_even_when_legacy_window_passes(self):
        times=[i/100 for i in range(340) if not 245<i<259]
        result=self.check_timing(times,[{'action':'click','time_s':.4}])
        self.assertNotIn('motion_frame_gap_exceeded',result['errors'])
        self.assertIn('comparison_frame_gap_exceeded',result['errors'])
        self.assertAlmostEqual(.14,result['comparison_maximum_gap_s'])
        self.assertEqual('fail',result['status'])

    def test_complete_bracket_is_checked_when_fixed_window_clips_it(self):
        times=[i/1000 for i in range(2800) if not 2142<i<2265]
        events=[{'action':'click','time_s':.4},{'action':'click','time_s':2.29}]
        result=self.check_timing(times,events)
        self.assertAlmostEqual(.123,result['comparison_response_windows'][0]['largest_bracket']['gap_s'])
        self.assertIn('comparison_frame_gap_exceeded',result['errors'])

    def test_later_input_pixel_cannot_support_preceding_response(self):
        times=[i/100 for i in range(400)]
        events=[{'action':'click','time_s':.4},{'action':'drag_start','time_s':.8},{'action':'drag_end','time_s':1.2}]
        result=self.check_timing(times,events)
        self.assertEqual('pass',result['status'])
        self.assertEqual([0,2],[w['input_index'] for w in result['comparison_response_windows']])
        self.assertLess(result['comparison_response_windows'][0]['end_s'],.8)

    def test_variable_cadence_is_allowed_without_assuming_frame_rate(self):
        times=[0.]
        for i in range(150):times.append(times[-1]+(.017,.029,.023)[i%3])
        result=self.check_timing(times,[{'action':'click','time_s':.4}])
        self.assertEqual('pass',result['status'])
        self.assertFalse(result['nominal_fps_assumed'])


if __name__=='__main__':unittest.main()
