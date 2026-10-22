import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from kiroclops.web_local.natural_capture import browser_arguments,screenshot_image,checkpoint_needed,wait_native_animation_frame,final_input_image
from kiroclops.web_local.capture import CaptureError
from kiroclops.web_local.specs import validate_scenario
from kiroclops.web_local.natural_capture import needs_native_boundary,await_stream_image,endpoint_checkpoint_needed,warmup_screenshot_image


class NaturalCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_baseline_requires_a_real_receipt_and_never_synthesizes_one(self):
        ready=asyncio.get_running_loop().create_future()
        actual={'data':'actual_png','epoch_s':100.}
        ready.set_result(actual)
        self.assertIs(await await_stream_image(ready),actual)
        never=asyncio.get_running_loop().create_future()
        with self.assertRaisesRegex(CaptureError,'Initial stream image'):
            await await_stream_image(never)

    def test_fresh_stream_endpoint_avoids_a_competing_png_but_stale_requires_image(self):
        self.assertFalse(endpoint_checkpoint_needed(100.,99.983))
        self.assertTrue(endpoint_checkpoint_needed(100.,99.9))
        self.assertTrue(endpoint_checkpoint_needed(100.,None))

    def test_move_boundary_is_prospective_only_and_does_not_change_legacy_capture(self):
        old={'frame_driver':'natural_browser_raf_v1'}
        new={**old,'analysis_policy':'observed_response_v2'}
        self.assertFalse(needs_native_boundary(old,'drag_move'))
        self.assertTrue(needs_native_boundary(new,'drag_move'))
        self.assertTrue(needs_native_boundary(old,'click'))
        self.assertFalse(needs_native_boundary({'frame_driver':'natural_browser_v1'},'drag_move'))

    async def test_pre_input_actual_png_fills_static_gap_after_scheduled_wait(self):
        checkpoint=AsyncMock(return_value={'data':'actual_png'})
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock) as wait:
            result=await final_input_image(100.,checkpoint,lambda:99.926)
        wait.assert_awaited_once();self.assertAlmostEqual(wait.call_args.args[0],99.96)
        self.assertEqual(result,{'data':'actual_png'})
        self.assertTrue(checkpoint.call_args.kwargs['eligible']())

    async def test_fresh_natural_observation_avoids_extra_screenshot(self):
        checkpoint=AsyncMock()
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock):
            self.assertIsNone(await final_input_image(100.,checkpoint,lambda:99.955))
        checkpoint.assert_not_awaited()

    async def test_queued_capture_rechecks_late_browser_delivery(self):
        latest=[99.926]
        async def checkpoint(*,eligible):
            latest[0]=99.97
            self.assertFalse(eligible())
            return None
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock):
            self.assertIsNone(await final_input_image(100.,checkpoint,lambda:latest[0]))

    def test_browser_keeps_own_animation_driver(self):
        self.assertNotIn('--enable-begin-frame-control',browser_arguments(True))
        self.assertNotIn('--run-all-compositor-stages-before-draw',browser_arguments(True))
        self.assertIn('--enable-begin-frame-control',browser_arguments(False))

    async def test_observer_awaits_real_raf_without_setting_clock_or_coordinates(self):
        page=AsyncMock();page.evaluate.return_value=123.4
        result=await wait_native_animation_frame(page)
        self.assertEqual(result['browser_raf_timestamp_ms'],123.4)
        self.assertFalse(result['frameTimeTicks_supplied']);self.assertFalse(result['image_produced'])
        page.evaluate.assert_awaited_once_with('()=>new Promise(resolve=>requestAnimationFrame(t=>resolve(t)))')

    async def test_screenshot_preserves_actual_bracket_and_bytes(self):
        cdp=AsyncMock();cdp.send.return_value={'data':'UE5H'}
        with patch('kiroclops.web_local.natural_capture.utc_seconds',side_effect=[100.,100.02]):
            result=await screenshot_image(cdp)
        self.assertEqual(result['interval_epoch_s'],[100.,100.02])
        self.assertEqual(result['timestamp_source'],'cdp_screenshot_bracket')
        self.assertEqual(result['data'],'UE5H')
        self.assertEqual(cdp.send.call_args.args[0],'Page.captureScreenshot')

    async def test_missing_image_and_timed_out_command_fail(self):
        cdp=AsyncMock();cdp.send.return_value={}
        with self.assertRaises(CaptureError):await screenshot_image(cdp)
        cdp.send.side_effect=asyncio.TimeoutError()
        with self.assertRaisesRegex(CaptureError,'exceeded120ms'):await screenshot_image(cdp)

    async def test_cold_start_is_excluded_and_active_deadline_is_unchanged(self):
        async def slow_png(*args,**kwargs):
            await asyncio.sleep(.15)
            return {'data':'UE5H'}
        cdp=AsyncMock();cdp.send.side_effect=slow_png
        with self.assertRaisesRegex(CaptureError,'exceeded120ms'):
            await screenshot_image(cdp)
        result=await warmup_screenshot_image(cdp)
        self.assertFalse(result['recording_eligible'])
        self.assertEqual(result['phase'],'warmup')
        self.assertGreater(result['interval_epoch_s'][1]-result['interval_epoch_s'][0],.12)
        self.assertEqual(cdp.send.await_count,2)  # Each call is one request, no retry.

    async def test_warmup_failure_is_startup_not_movement_failure(self):
        cdp=AsyncMock();cdp.send.side_effect=asyncio.TimeoutError()
        with self.assertRaises(CaptureError) as caught:
            await warmup_screenshot_image(cdp)
        self.assertEqual(caught.exception.code,'capture_startup_failure')

    def test_stale_images_sampled_without_disturbing_nearby_input(self):
        self.assertTrue(checkpoint_needed(100.,99.9,None,False))
        self.assertFalse(checkpoint_needed(100.,99.9,.02,False))
        self.assertFalse(checkpoint_needed(100.,99.9,None,True))
        self.assertFalse(checkpoint_needed(100.,99.98,None,False))

    def test_unqualified_driver_rejected_for_model_runs(self):
        with self.assertRaisesRegex(ValueError,'qualification-only'):
            validate_scenario({'domain':'panel','frame_driver':'natural_browser_v1','split':'calibration'})


if __name__=='__main__':unittest.main()
