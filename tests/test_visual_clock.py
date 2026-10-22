import base64
import unittest
import cv2

from tests.test_pixel_clock_probe import marker
from kiroclops.web_local.visual_clock import bind_frame_times,POLICY,decode
from kiroclops.web_local.capture import CaptureError
from kiroclops.web_local.specs import scenarios,validate_scenario


def frame(ident, time, image=None):
    success,raw=cv2.imencode('.png',marker(ident) if image is None else image)
    assert success
    return {'data':base64.b64encode(raw).decode(),'epoch_s':time,'interval_epoch_s':[time,time],
            'receive_epoch_s':time+.003,'timestamp_source':'cdp_compositor'}


def clock():
    return {'policy':POLICY,'rows':[{'id':i,'callback_epoch_s':100+i*.02,
        'after_task_epoch_s':100+i*.02+.001} for i in (1,2,3)]}


class VisualClockTests(unittest.TestCase):
    def test_pixels_bind_to_real_clock_receipt_preserving_transport(self):
        frames,receipt=bind_frame_times([frame(1,100.04),frame(2,100.06)],clock())
        self.assertAlmostEqual(frames[0]['epoch_s'],100.0205)
        self.assertEqual(frames[0]['original_epoch_s'],100.04)
        self.assertEqual(frames[0]['timestamp_source'],'svg_visible_raf_batch')
        self.assertEqual(frames[0]['interval_epoch_s'],[100.02,100.021])
        self.assertFalse(receipt['physical_display_presentation_claimed'])
        self.assertFalse(receipt['source_geometry_used'])

    def test_duplicate_same_pixels_is_audited_but_different_state_fails(self):
        original=frame(1,100.04)
        frames,receipt=bind_frame_times([original,frame(1,100.041)],clock())
        self.assertEqual(len(frames),1);self.assertEqual(len(receipt['duplicate_images']),1)
        image=marker(1);image[0,0]=250
        with self.assertRaisesRegex(CaptureError,'different pixel states'):
            bind_frame_times([original,frame(1,100.041,image)],clock())

    def test_invalid_counter_and_unknown_receipt_fail(self):
        with self.assertRaisesRegex(CaptureError,'no independent clock receipt'):
            bind_frame_times([frame(14,100.04)],clock())
        image=marker(1);image[-5,-1]=100
        with self.assertRaises(CaptureError):bind_frame_times([frame(1,100.04,image)],clock())

    def test_counter_reversal_or_clock_overlap_cannot_be_sorted_into_validity(self):
        with self.assertRaisesRegex(CaptureError,'unordered'):
            bind_frame_times([frame(2,100.06),frame(1,100.065)],clock())
        value=clock();value['rows'][0]['after_task_epoch_s']=100.045
        with self.assertRaisesRegex(CaptureError,'overlaps'):
            bind_frame_times([frame(1,100.05),frame(2,100.06)],value)

    def test_unbounded_batch_or_stale_marker_does_not_get_precise_time(self):
        for end in (None,100.1,float('nan')):
            value=clock();value['rows'][0]['after_task_epoch_s']=end
            with self.assertRaisesRegex(CaptureError,'Unbounded'):
                bind_frame_times([frame(1,100.05)],value)
        with self.assertRaisesRegex(CaptureError,'does not belong'):
            bind_frame_times([frame(1,100.2)],clock())

    def test_duplicate_clock_table_id_is_rejected(self):
        value=clock();value['rows'].append(value['rows'][0])
        with self.assertRaisesRegex(CaptureError,'counter table'):
            bind_frame_times([frame(1,100.04)],value)

    def test_clock_requires_explicit_qualified_svg_scenario(self):
        scenario=scenarios('svg','calibration')[0];scenario['visual_frame_clock']=POLICY
        with self.assertRaisesRegex(ValueError,'qualification-only'):validate_scenario(scenario)
        scenario.update(split='qualification',analysis_policy='observed_response_v2',frame_driver='natural_browser_raf_v1')
        validate_scenario(scenario)
        scenario['domain']='panel'
        with self.assertRaisesRegex(ValueError,'qualification-only'):validate_scenario(scenario)


if __name__=='__main__':unittest.main()
