"""Real-image preparation and static cadence; no browser or manufactured video."""
import asyncio
import unittest
from unittest.mock import patch

from kiroclops.web_local import capture,clock_runtime


class StaticCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_aged_preclose_png_bridges_saved_gap_with_actual_bracket(self):
        # Saved failed replay: settled scene, close planned inside open's 1.8s
        # response. Simulate one genuine PNG command at the existing prep lead.
        planned=1.820638656616211;now=[planned-.040];images=[]
        previous_midpoint=1.6847608089447021;previous_end=1.68998122215271
        next_observation=1.903167724609375
        async def png():
            before=now[0];now[0]+=.010
            frame={'data':'actual-test-png','timestamp_source':'cdp_begin_frame_bracket',
                'interval_epoch_s':[before,now[0]],'epoch_s':(before+now[0])/2}
            images.append(frame);return frame
        coordinator=clock_runtime.FrameCoordinator(png)
        self.assertFalse(capture.idle_checkpoint_due(since_frame_s=now[0]-previous_end,
            upcoming_input_s=planned-now[0],input_in_flight=False,since_input_s=1.2))
        with patch.object(capture,'utc_seconds',lambda:now[0]):
            frame=await capture.pre_input_checkpoint('click',coordinator.checkpoint,lambda:previous_end)
        self.assertIs(frame,images[0]);self.assertEqual(len(images),1)
        self.assertEqual(frame['interval_epoch_s'],[planned-.040,planned-.030])
        self.assertLess(now[0],planned)  # Native input still waits for its actual due time.
        self.assertGreater(next_observation-previous_midpoint,.120)
        self.assertLess(frame['epoch_s']-previous_midpoint,.120)
        self.assertLess(next_observation-frame['epoch_s'],.120)

    async def test_fresh_moving_image_avoids_redundant_preparation_png(self):
        draws=[]
        async def png():draws.append('png');return {'data':'unexpected'}
        coordinator=clock_runtime.FrameCoordinator(png)
        with patch.object(capture,'utc_seconds',lambda:100.):
            result=await capture.pre_input_checkpoint('click',coordinator.checkpoint,lambda:99.990)
        self.assertIsNone(result);self.assertEqual(draws,[])

    async def test_queued_preparation_rechecks_fresh_pixels_and_input_state(self):
        for invalidation in ('fresh_pixel','input_started'):
            with self.subTest(invalidation=invalidation):
                entered=asyncio.Event();release=asyncio.Event();draws=[];state={'end':99.,'input':False}
                async def routine():entered.set();await release.wait();return {'image_produced':False}
                async def png():draws.append('png');return {'data':'unexpected'}
                coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine)
                running=asyncio.create_task(coordinator.checkpoint(routine=True));await entered.wait()
                with patch.object(capture,'utc_seconds',lambda:100.):
                    queued=asyncio.create_task(capture.pre_input_checkpoint('click',coordinator.checkpoint,
                        lambda:state['end'],input_in_flight=lambda:state['input']))
                    await asyncio.sleep(0);self.assertFalse(queued.done())
                    if invalidation=='fresh_pixel':state['end']=99.999
                    else:state['input']=True
                    release.set();await running;self.assertIsNone(await queued)
                self.assertEqual(draws,[]);self.assertFalse(coordinator._lock.locked())

    async def test_only_discrete_response_inputs_request_preparation_pixels(self):
        draws=[]
        async def png():draws.append('png');return {'data':'real-test-png'}
        coordinator=clock_runtime.FrameCoordinator(png)
        for action in ('drag_move','pointer_move','unknown'):
            self.assertIsNone(await capture.pre_input_checkpoint(action,coordinator.checkpoint,lambda:None))
        self.assertEqual(draws,[])
        for action in ('click','drag_start','drag_end'):
            self.assertIsNotNone(await capture.pre_input_checkpoint(action,coordinator.checkpoint,lambda:None))
        self.assertEqual(len(draws),3)

    async def test_checkpoint_failure_is_not_replaced_by_invented_image(self):
        failure=RuntimeError('real PNG failed')
        async def png():raise failure
        coordinator=clock_runtime.FrameCoordinator(png)
        with self.assertRaises(RuntimeError) as raised:
            await capture.pre_input_checkpoint('drag_end',coordinator.checkpoint,lambda:None)
        self.assertIs(raised.exception,failure);self.assertIsNone(coordinator._last_png_completed)
        self.assertFalse(coordinator._lock.locked())

    async def test_cancellation_while_queued_releases_frame_lock(self):
        entered=asyncio.Event();release=asyncio.Event();cancel=[False];draws=[]
        async def routine():entered.set();await release.wait();return {'image_produced':False}
        async def png():draws.append('png');return {'data':'unexpected'}
        def check():
            if cancel[0]:raise capture.CaptureError('cancelled','test cancelled')
        coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine)
        running=asyncio.create_task(coordinator.checkpoint(routine=True));await entered.wait()
        queued=asyncio.create_task(capture.pre_input_checkpoint('click',coordinator.checkpoint,lambda:None,check=check))
        await asyncio.sleep(0);cancel[0]=True;release.set();await running
        with self.assertRaises(capture.CaptureError) as raised:await queued
        self.assertEqual(raised.exception.code,'cancelled');self.assertEqual(draws,[])
        self.assertFalse(coordinator._lock.locked())

    async def test_preparation_png_preserves_routine_cooldown_and_boundary_priority(self):
        actions=[]
        async def png():actions.append('png');return {'data':'real-test-png'}
        async def routine():actions.append('routine');return {'image_produced':False}
        async def boundary():actions.append('boundary');return {'image_produced':False}
        coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine,boundary_draw=boundary,
            routine_png_cooldown_s=capture.FRAME_CONTROL_POLICY['post_png_routine_cooldown_s'])
        with patch.object(clock_runtime.time,'perf_counter',lambda:10.):
            await capture.pre_input_checkpoint('click',coordinator.checkpoint,lambda:None)
            self.assertIsNone(await coordinator.checkpoint(routine=True))
            self.assertFalse((await coordinator.checkpoint(priority=True))['image_produced'])
        self.assertEqual(actions,['png','boundary'])

    def test_static_sampling_has_headroom_without_changing_delivery_or_timing_gates(self):
        now=0.;previous_end=0.;timestamps=[0.];due=0.;after_png=False
        # Actual simulated commands take 12ms; only their genuine midpoint is
        # an observation. No image-free native slot is added to timestamps.
        while now<2.:
            due=capture.pump_deadline(due,now,after_png=after_png);now=due;after_png=False
            if capture.idle_checkpoint_due(since_frame_s=now-previous_end,
                    upcoming_input_s=None,input_in_flight=False,since_input_s=2.):
                before=now;now+=.012;timestamps.append((before+now)/2)
                previous_end=now;after_png=True
        intervals=[b-a for a,b in zip(timestamps,timestamps[1:])]
        self.assertGreater(len(intervals),20);self.assertLess(max(intervals),.080)
        self.assertEqual(capture.DELIVERY_BACKPRESSURE['late_delivery_s'],.065)
        self.assertEqual(capture.TIMING_LIMITS['p95_interval_s'],.080)
        self.assertEqual(capture.TIMING_LIMITS['maximum_motion_gap_s'],.120)


if __name__=='__main__':unittest.main()
