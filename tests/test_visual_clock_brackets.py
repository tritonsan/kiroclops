"""Actual image intervals remain intervals when binding the decoded RAF tag."""
import unittest
from copy import deepcopy

from kiroclops.web_local.capture import CaptureError
from kiroclops.web_local.visual_clock import POLICY, bind_frame_times
from tests.test_visual_clock import frame


def bracket(ident, low, high, source='cdp_screenshot_bracket'):
    value=frame(ident,(low+high)/2)
    value.update(interval_epoch_s=[low,high],timestamp_source=source)
    return value


def receipt(ident, low, high):
    return {'policy':POLICY,'rows':[{'id':ident,'callback_epoch_s':low,'after_task_epoch_s':high}]}


class VisualClockBracketTests(unittest.TestCase):
    def test_actual_early_screenshot_midpoint_binds_its_visible_batch(self):
        # Values copied from the two retained counter receipts; no new capture.
        for ident,start,end,low,high in (
            (222,1790939623.4293125,1790939623.4571528,1790939623.4443998,1790939623.4448001),
            (227,1790939623.5144777,1790939623.540522,1790939623.528,1790939623.5284)):
            with self.subTest(counter=ident):
                observed=bracket(ident,start,end);saved=deepcopy(observed)
                bound,evidence=bind_frame_times([observed],receipt(ident,low,high),clock_tolerance_s=.0001)
                self.assertEqual(saved,observed)
                self.assertEqual([low,high],bound[0]['interval_epoch_s'])
                self.assertEqual(saved['epoch_s'],bound[0]['original_epoch_s'])
                association=evidence['associations'][0]
                self.assertEqual([start,end],association['original_interval_epoch_s'])
                self.assertEqual('bounded_request_response',association['observation_association_basis'])
                self.assertEqual([start-(low+high)/2,end-(low+high)/2],
                    association['transport_minus_batch_midpoint_interval_s'])
                self.assertEqual(.0001,association['clock_tolerance_s'])
                self.assertFalse(evidence['physical_display_presentation_claimed'])

    def test_point_observation_cannot_borrow_a_screenshot_interval(self):
        observed=frame(1,100.0188)
        with self.assertRaisesRegex(CaptureError,'does not belong'):
            bind_frame_times([observed],receipt(1,100.02,100.021),clock_tolerance_s=.001)
        observed['interval_epoch_s']=[100.01,100.04]
        with self.assertRaisesRegex(CaptureError,'Invalid image observation interval'):
            bind_frame_times([observed],receipt(1,100.02,100.021))

    def test_request_finishes_before_marker_callback_still_fails(self):
        with self.assertRaisesRegex(CaptureError,'does not belong'):
            bind_frame_times([bracket(1,100.01,100.0189)],receipt(1,100.02,100.021),clock_tolerance_s=.001)

    def test_worst_case_bracket_transport_age_still_has_120ms_bound(self):
        # Midpoint is only105ms old, but the actual response bound is125ms old.
        with self.assertRaisesRegex(CaptureError,'does not belong'):
            bind_frame_times([bracket(1,100.1055,100.1455)],receipt(1,100.02,100.021))

    def test_invalid_observation_brackets_never_get_precise_batch_time(self):
        malformed=(None,[100.01],[100.04,100.01],[100.01,float('nan')],
                   [False,100.04],[100.01,float('inf')])
        for interval in malformed:
            with self.subTest(interval=interval):
                observed=bracket(1,100.01,100.04);observed['interval_epoch_s']=interval
                with self.assertRaisesRegex(CaptureError,'Invalid image observation interval'):
                    bind_frame_times([observed],receipt(1,100.02,100.021))
        observed=bracket(1,100.01,100.04);observed['epoch_s']=100.05
        with self.assertRaisesRegex(CaptureError,'Invalid image observation interval'):
            bind_frame_times([observed],receipt(1,100.02,100.021))

    def test_unknown_timestamp_source_and_invalid_mapping_tolerance_fail(self):
        observed=bracket(1,100.01,100.04,source='guessed_screenshot_time')
        with self.assertRaisesRegex(CaptureError,'Invalid image observation interval'):
            bind_frame_times([observed],receipt(1,100.02,100.021))
        for tolerance in (-.001,float('nan'),float('inf'),True,None):
            with self.subTest(tolerance=tolerance):
                with self.assertRaisesRegex(CaptureError,'Invalid clock mapping tolerance'):
                    bind_frame_times([frame(1,100.04)],receipt(1,100.02,100.021),clock_tolerance_s=tolerance)


if __name__=='__main__':unittest.main()
