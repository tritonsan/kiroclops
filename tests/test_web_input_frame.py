"""Regression: actual frames, deadlines and input boundaries stay observable."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from kiroclops.web_local.capture import (CaptureError, begin_frame_image,
    require_input_deadline, merge_frame_receipts, exclude_redundant_checkpoints,
    exclude_input_ambiguous_frames, TrustedPointer,
    wait_until_epoch, preserve_capture_receipts, close_browser_delivery, drain_delivery_tasks, advance_frame,
    idle_checkpoint_due, routine_pump_can_stop, timing_summary, FRAME_CONTROL_POLICY)


class InputFrameTests(unittest.IsolatedAsyncioTestCase):
    def test_stale_pixel_fallback_respects_input_and_actual_pixel_freshness(self):
        self.assertEqual(FRAME_CONTROL_POLICY['routine_frame'],'completed_native_frame_no_png')
        base={'upcoming_input_s':.2,'input_in_flight':False,'since_input_s':.2}
        self.assertFalse(idle_checkpoint_due(since_frame_s=.029,**base))
        self.assertTrue(idle_checkpoint_due(since_frame_s=.031,**base))
        self.assertFalse(idle_checkpoint_due(since_frame_s=.2,upcoming_input_s=.079,
            input_in_flight=False,since_input_s=.2))
        self.assertFalse(idle_checkpoint_due(since_frame_s=.2,upcoming_input_s=.2,
            input_in_flight=False,since_input_s=.049))
        self.assertFalse(idle_checkpoint_due(since_frame_s=.2,upcoming_input_s=.2,
            input_in_flight=True,since_input_s=.2))

    def test_routine_pump_stops_only_after_last_full_response_window(self):
        kwargs={'next_input_index':18,'input_count':18,'response_end_epoch_s':103.02}
        self.assertFalse(routine_pump_can_stop(now_epoch_s=103.019,**kwargs))
        self.assertTrue(routine_pump_can_stop(now_epoch_s=103.02,**kwargs))
        self.assertFalse(routine_pump_can_stop(next_input_index=17,input_count=18,
            response_end_epoch_s=103.02,now_epoch_s=104.))
        self.assertFalse(routine_pump_can_stop(next_input_index=18,input_count=18,
            response_end_epoch_s=None,now_epoch_s=104.))

    def test_no_png_advances_cannot_be_counted_as_video_frames(self):
        sample={'uncertainty_s':.001,'epoch_in_host_bracket':True}
        result=timing_summary([.01,.03],[sample],[],frame_metadata=[])
        self.assertEqual(result['frame_count'],2)
        self.assertEqual(result['status'],'fail')
        self.assertIn('missing_or_invalid_frame_timestamp',result['errors'])

    def test_missing_active_window_tail_fails_unchanged_120ms_gap_limit(self):
        sample={'uncertainty_s':.001,'epoch_in_host_bracket':True}
        frames=[i*.02 for i in range(70)]  # Images stop at 1.38s.
        result=timing_summary(frames,[sample],[{'time_s':.4,'action':'click'}])
        self.assertEqual(result['limits']['maximum_motion_gap_s'],.120)
        self.assertEqual(result['status'],'fail')
        self.assertIn('motion_frame_gap_exceeded',result['errors'])
        self.assertGreater(result['maximum_motion_gap_s'],.8)

    def test_raw_provenance_separates_pump_boundaries_and_real_fallback_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            preserve_capture_receipts(root,[],[],[],
                boundary_advances=[{'image_produced':False,'command_id':1}],
                routine_advances=[{'image_produced':False,'next_input_index':1}],
                fallback_checkpoints=[{'png_sha256':'real-image-hash','actual_image_produced':True}])
            receipt=json.loads((root/'private/received-evidence.json').read_text())
            self.assertEqual(receipt['frames'],[])
            self.assertEqual(len(receipt['completed_native_boundaries']),1)
            self.assertEqual(len(receipt['completed_native_routine_frames']),1)
            self.assertEqual(len(receipt['actual_png_fallback_checkpoints']),1)
            self.assertFalse(receipt['valid_analysis_package'])

    async def test_advance_uses_real_time_without_requesting_or_inventing_image(self):
        for damaged in [True,False]:
            cdp=AsyncMock();cdp.send.return_value={'hasDamage':damaged}
            with patch('kiroclops.web_local.capture.utc_seconds',side_effect=[100.,100.003]):
                receipt=await advance_frame(cdp)
            cdp.send.assert_awaited_once_with('HeadlessExperimental.beginFrame',{})
            self.assertEqual(receipt['host_before_s'],100.);self.assertEqual(receipt['host_after_s'],100.003)
            self.assertEqual(receipt['hasDamage'],damaged)
            self.assertFalse(receipt['image_produced']);self.assertFalse(receipt['screenshot_requested'])
            self.assertNotIn('data',receipt);self.assertNotIn('epoch_s',receipt)

    async def test_failed_native_advance_blocks_click_release(self):
        cdp=AsyncMock()
        async def send(method,params):
            if method=='HeadlessExperimental.beginFrame':raise asyncio.TimeoutError()
            return {}
        cdp.send.side_effect=send;pointer=TrustedPointer(cdp)
        with self.assertRaises(CaptureError):
            await pointer.click(10,20,before_release=lambda:advance_frame(cdp))
        self.assertNotIn('mouseReleased',[call.args[1].get('type') for call in cdp.send.call_args_list])

    async def test_advance_requires_completed_frame_receipt(self):
        cdp=AsyncMock();cdp.send.return_value={}
        with self.assertRaises(CaptureError):await advance_frame(cdp)

    async def test_delivery_end_follows_browser_close_and_late_receipt_task(self):
        tasks=set();events=[];browser=AsyncMock()
        async def late_receipt():
            await asyncio.sleep(0)
            events.append('real_late_receipt')
            raise RuntimeError('ACK channel closed after receipt')
        async def close():
            events.append('browser_closed')
            task=asyncio.create_task(late_receipt());tasks.add(task);task.add_done_callback(tasks.discard)
        browser.close.side_effect=close
        def now():
            self.assertEqual(events,['browser_closed','real_late_receipt'])
            self.assertFalse(tasks)
            return 103.
        with patch('kiroclops.web_local.capture.utc_seconds',side_effect=now):
            self.assertEqual(await close_browser_delivery(browser,tasks),103.)

    async def test_browser_shutdown_is_not_a_one_second_frame_delivery_gate(self):
        browser=AsyncMock()
        async def close():
            await asyncio.sleep(1.05)
        browser.close.side_effect=close
        with patch('kiroclops.web_local.capture.utc_seconds',return_value=104.):
            self.assertEqual(await close_browser_delivery(browser,set()),104.)

    async def test_unsettled_screencast_ack_cannot_hang_capture_cleanup(self):
        task=asyncio.create_task(asyncio.Event().wait())
        tasks={task}
        with self.assertRaisesRegex(CaptureError,'ACK drain exceeded'):
            await drain_delivery_tasks(tasks,max_wait_s=.005)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())

    async def test_native_pointer_drag_keeps_buttons_and_coordinates(self):
        cdp=AsyncMock();pointer=TrustedPointer(cdp)
        await pointer.down((120.5,40.25));await pointer.move(135.5,42.25);await pointer.up()
        calls=[call.args for call in cdp.send.call_args_list]
        self.assertEqual([call[0] for call in calls],['Input.dispatchMouseEvent']*3)
        self.assertEqual([call[1]['type'] for call in calls],['mousePressed','mouseMoved','mouseReleased'])
        self.assertEqual([call[1]['buttons'] for call in calls],[1,1,0])
        self.assertEqual([call[1]['button'] for call in calls],['left','left','left'])
        self.assertEqual([(call[1]['x'],call[1]['y']) for call in calls],[(120.5,40.25),(135.5,42.25),(135.5,42.25)])
        self.assertTrue(all(call[1]['pointerType']=='mouse' and 'timestamp' not in call[1] for call in calls))

    async def test_native_click_is_ordered_hover_down_up(self):
        cdp=AsyncMock();pointer=TrustedPointer(cdp);await pointer.click(20,30)
        params=[call.args[1] for call in cdp.send.call_args_list]
        self.assertEqual([v['type'] for v in params],['mouseMoved','mousePressed','mouseReleased'])
        self.assertEqual([v['buttons'] for v in params],[0,1,0])
        self.assertEqual([v.get('clickCount') for v in params],[None,1,1])

    async def test_boundary_image_is_after_real_hover_ack_and_before_click_release(self):
        sequence=[]
        async def send(method,params):
            self.assertEqual(method,'Input.dispatchMouseEvent')
            sequence.append(params['type'])
            self.assertNotIn('timestamp',params)
        async def boundary():
            self.assertTrue(pointer.pressed)
            sequence.append('actual_frame')
        cdp=AsyncMock();cdp.send.side_effect=send
        traces=[];pointer=TrustedPointer(cdp,trace=traces.append)
        await pointer.click(20,30,before_release=boundary)
        self.assertEqual(sequence,['mouseMoved','mousePressed','actual_frame','mouseReleased'])
        self.assertEqual([row['operation'] for row in traces],['native_mouseMoved','native_mousePressed','native_mouseReleased'])
        self.assertTrue(all(row['host_before_s']<=row['host_after_s'] and row['error'] is None for row in traces))

    async def test_missing_boundary_image_never_emits_click_release(self):
        cdp=AsyncMock();pointer=TrustedPointer(cdp)
        async def boundary():
            raise CaptureError('capture_timing_failure','Missing real boundary image')
        with self.assertRaises(CaptureError):
            await pointer.click(20,30,before_release=boundary)
        self.assertEqual([call.args[1]['type'] for call in cdp.send.call_args_list],['mouseMoved','mousePressed'])
        self.assertTrue(pointer.pressed)

    async def test_drag_boundary_image_precedes_actual_press_and_retains_hit_point(self):
        sequence=[]
        async def send(method,params):
            sequence.append((params['type'],params['x'],params['y']))
            self.assertNotIn('timestamp',params)
        async def boundary():
            self.assertFalse(pointer.pressed)
            sequence.append('actual_frame_after_hit_lookup')
        cdp=AsyncMock();cdp.send.side_effect=send;pointer=TrustedPointer(cdp)
        await pointer.down((121.5,48.25),before_press=boundary)
        self.assertEqual(sequence,['actual_frame_after_hit_lookup',('mousePressed',121.5,48.25)])
        self.assertEqual(pointer.position,(121.5,48.25));self.assertTrue(pointer.pressed)

    async def test_missing_drag_boundary_image_never_emits_press(self):
        cdp=AsyncMock();pointer=TrustedPointer(cdp)
        async def boundary():
            raise CaptureError('capture_timing_failure','Missing actual pre-drag image')
        with self.assertRaises(CaptureError):
            await pointer.down((20,30),before_press=boundary)
        cdp.send.assert_not_awaited()
        self.assertFalse(pointer.pressed);self.assertEqual(pointer.position,(0.,0.))

    async def test_failed_pointer_dispatch_does_not_advance_state(self):
        cdp=AsyncMock();cdp.send.side_effect=asyncio.TimeoutError()
        pointer=TrustedPointer(cdp)
        with self.assertRaises(CaptureError):await pointer.down((20,30))
        self.assertFalse(pointer.pressed);self.assertEqual(pointer.position,(0.,0.))

    async def test_early_asyncio_wake_does_not_dispatch_early(self):
        with patch('kiroclops.web_local.capture.utc_seconds',side_effect=[100.,100.003,100.006,100.01]),patch('kiroclops.web_local.capture.asyncio.sleep',new_callable=AsyncMock) as sleep:
            await wait_until_epoch(100.01)
        self.assertEqual(sleep.await_count,3)

    def test_failed_capture_keeps_raw_bytes_but_cannot_be_an_analysis_package(self):
        raw=b'actual-received-bytes'
        frame={'data':base64.b64encode(raw).decode(),'epoch_s':1.,'interval_epoch_s':[.99,1.01],
            'timestamp_source':'cdp_begin_frame_bracket'}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);error={'code':'capture_timing_failure','message':'late'}
            preserve_capture_receipts(root,[frame],[],[],private={'events':[]},error=error)
            receipt=json.loads((root/'private/received-evidence.json').read_text())
            self.assertEqual(receipt['status'],'interrupted');self.assertFalse(receipt['recording_complete'])
            self.assertFalse(receipt['valid_analysis_package']);self.assertFalse((root/'package').exists())
            self.assertEqual(receipt['error'],error)
            saved=receipt['frames'][0];self.assertEqual((root/saved['path']).read_bytes(),raw)
            self.assertEqual(saved['png_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(saved['interval_epoch_s'],[.99,1.01])

    async def test_real_clock_bracket_and_no_virtual_time(self):
        cdp=AsyncMock();cdp.send.return_value={'screenshotData':'YWJj','hasDamage':False}
        with patch('kiroclops.web_local.capture.utc_seconds',side_effect=[100.,100.008]):
            frame=await begin_frame_image(cdp)
        method,parameters=cdp.send.call_args.args
        self.assertEqual(method,'HeadlessExperimental.beginFrame')
        self.assertNotIn('frameTimeTicks',parameters)
        self.assertEqual(frame['interval_epoch_s'],[100.,100.008])
        self.assertAlmostEqual(frame['epoch_s'],100.004)
        self.assertEqual(frame['data'],'YWJj')
        self.assertFalse(frame['metadata']['hasDamage'])

    async def test_missing_image_never_becomes_a_frame(self):
        cdp=AsyncMock();cdp.send.return_value={'hasDamage':False}
        with self.assertRaises(CaptureError) as caught:await begin_frame_image(cdp)
        self.assertEqual(caught.exception.code,'capture_timing_failure')

    async def test_frame_timeout_is_not_a_successful_capture(self):
        cdp=AsyncMock();cdp.send.side_effect=asyncio.TimeoutError()
        with self.assertRaises(CaptureError) as caught:await begin_frame_image(cdp)
        self.assertEqual(caught.exception.code,'capture_timing_failure')

    async def test_cancellation_is_not_converted_to_a_frame(self):
        cdp=AsyncMock();cdp.send.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await begin_frame_image(cdp)

    def test_missed_input_cannot_catch_up_as_compressed_clicks(self):
        require_input_deadline(.4,.519)
        with self.assertRaises(CaptureError) as caught:require_input_deadline(.4,1.0747)
        self.assertEqual(caught.exception.code,'capture_timing_failure')

    def test_begin_frame_clock_order_cannot_be_sorted_into_success(self):
        frames=[{'timestamp_source':'cdp_begin_frame_bracket','epoch_s':t,
                 'interval_epoch_s':[t-.001,t+.001]} for t in (2.,1.)]
        with self.assertRaises(CaptureError):merge_frame_receipts(frames)

    def test_overlapping_begin_frame_bracket_keeps_actual_primary(self):
        frames=[{'timestamp_source':'cdp_begin_frame_bracket','epoch_s':1.,
                 'interval_epoch_s':[.99,1.01],'data':'YWJj'},
                {'timestamp_source':'cdp_compositor','epoch_s':1.002,
                 'interval_epoch_s':[1.002,1.002],'data':'YWJj'}]
        retained,excluded=exclude_redundant_checkpoints(frames)
        self.assertEqual(retained,[frames[1]])
        self.assertEqual(excluded[0]['interval_epoch_s'],[.99,1.01])
        self.assertEqual(excluded[0]['overlapping_compositor_times'],[1.002])

    def test_input_inside_bracket_cannot_supply_pre_command_evidence(self):
        frame={'timestamp_source':'cdp_begin_frame_bracket','epoch_s':1.,
               'interval_epoch_s':[.99,1.01],'data':'YWJj'}
        retained,excluded=exclude_input_ambiguous_frames([frame],[1.005])
        self.assertEqual(retained,[])
        self.assertEqual(excluded[0]['input_epochs'],[1.005])
        self.assertEqual(excluded[0]['epoch_s'],1.)

    def test_exact_compositor_point_is_not_ambiguous_bracket(self):
        frame={'timestamp_source':'cdp_compositor','epoch_s':1.,
               'interval_epoch_s':[1.,1.],'data':'YWJj'}
        self.assertEqual(exclude_input_ambiguous_frames([frame],[1.]),([frame],[]))


if __name__=='__main__':unittest.main()
