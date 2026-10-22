import unittest
from unittest.mock import AsyncMock,patch
from kiroclops.web_local.natural_capture import final_input_image, input_pixel_policy, POLICY


class PanelInputCheckpoint(unittest.IsolatedAsyncioTestCase):
    def test_svg_policy_stays_unchanged_and_panel_policy_is_an_explicit_copy(self):
        svg=input_pixel_policy('svg');panel=input_pixel_policy('panel')
        self.assertEqual(svg,POLICY)
        self.assertEqual(.010,panel['final_input_image_check_lead_s'])
        self.assertEqual(.020,panel['oldest_final_input_image_before_due_s'])
        panel['static_image_stale_s']=100
        self.assertEqual(.040,POLICY['static_image_stale_s'])

    async def test_old_static_image_requires_late_real_image(self):
        screenshot=AsyncMock(return_value={'data':'actual_png','epoch_s':100.001})
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock) as wait:
            result=await final_input_image(100.,screenshot,lambda:99.955,policy=input_pixel_policy('panel'))
        self.assertAlmostEqual(99.99,wait.call_args.args[0])
        self.assertEqual(100.001,result['epoch_s'])
        screenshot.assert_awaited_once()

    async def test_fresh_delivered_image_does_not_force_extra_png(self):
        screenshot=AsyncMock()
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock):
            self.assertIsNone(await final_input_image(100.,screenshot,lambda:99.985,policy=input_pixel_policy('panel')))
        screenshot.assert_not_awaited()

    async def test_delivered_frame_cancels_queued_screenshot_without_synthetic_pixels(self):
        latest=[99.955]
        async def screenshot(*,eligible):
            latest[0]=99.99
            self.assertFalse(eligible())
            return None
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock):
            self.assertIsNone(await final_input_image(100.,screenshot,lambda:latest[0],policy=input_pixel_policy('panel')))

if __name__=='__main__':unittest.main()
