"""Opt-in native click release scheduling never invents event timestamps."""
import unittest
from unittest.mock import AsyncMock, patch
from kiroclops.web_local.capture import TrustedPointer
from kiroclops.web_local.specs import validate_scenario


class ReleaseScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_click_requires_actual_hover_position(self):
        cdp=AsyncMock();pointer=TrustedPointer(cdp)
        with self.assertRaisesRegex(Exception,'Prepared click position'):
            await pointer.finish_prepared_click((20,30))
        await pointer.move(20,30)
        await pointer.finish_prepared_click((20,30))
        self.assertEqual([c.args[1]['type'] for c in cdp.send.call_args_list],['mouseMoved','mousePressed','mouseReleased'])

    async def test_prepared_hover_waits_before_press_and_boundary(self):
        calls=[];cdp=AsyncMock()
        async def send(method,params):calls.append(params['type'])
        async def boundary():calls.append('frame')
        async def wait(due,check):calls.append('wait_before_press')
        cdp.send.side_effect=send
        with patch('kiroclops.web_local.capture.wait_until_epoch',side_effect=wait):
            await TrustedPointer(cdp).click(20,30,before_release=boundary,press_not_before=100.)
        self.assertEqual(calls,['mouseMoved','wait_before_press','mousePressed','frame','mouseReleased'])

    async def test_press_and_frame_precede_wait_and_native_release(self):
        calls=[]; cdp=AsyncMock()
        async def send(method,params):
            self.assertNotIn('timestamp',params);calls.append(params['type'])
        async def boundary():calls.append('frame')
        async def wait(due,check):
            self.assertEqual(due,100.);check();calls.append('wait_until_due')
        cdp.send.side_effect=send
        with patch('kiroclops.web_local.capture.wait_until_epoch',side_effect=wait):
            await TrustedPointer(cdp).click(20,30,before_release=boundary,release_not_before=100.)
        self.assertEqual(calls,['mouseMoved','mousePressed','frame','wait_until_due','mouseReleased'])

    async def test_cancelled_wait_does_not_release(self):
        cdp=AsyncMock()
        with patch('kiroclops.web_local.capture.wait_until_epoch',side_effect=RuntimeError('cancelled')):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                await TrustedPointer(cdp).click(20,30,release_not_before=100.)
        self.assertEqual([c.args[1]['type'] for c in cdp.send.call_args_list],['mouseMoved','mousePressed'])

    async def test_default_keeps_legacy_dispatch(self):
        with patch('kiroclops.web_local.capture.wait_until_epoch',new_callable=AsyncMock) as wait:
            await TrustedPointer(AsyncMock()).click(20,30)
        wait.assert_not_awaited()

    def test_unknown_schedule_rejected(self):
        with self.assertRaisesRegex(ValueError,'Unsupported click schedule'):
            validate_scenario({'domain':'panel','click_schedule':'invent_timestamps'})

    def test_unqualified_policy_cannot_enter_model_calibration(self):
        for policy in ('release_at_due_v1','prepared_hover_v2'):
            with self.assertRaisesRegex(ValueError,'qualification-only'):
                validate_scenario({'domain':'panel','click_schedule':policy,'split':'calibration'})
