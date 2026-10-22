"""Post-input fallback concurrency without starting Chromium or a capture."""
import asyncio
import unittest
from unittest.mock import patch

from kiroclops.web_local import capture, clock_runtime


class PostInputCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_timer_allows_routine_progress_before_png(self):
        waiting=asyncio.Event();due=asyncio.Event();sequence=[]
        async def timer(deadline,check):
            self.assertEqual(deadline,100.+capture.CHECKPOINT_POLICY['post_input_s'])
            waiting.set();await due.wait()
        async def png():sequence.append('png');return {'data':'actual-image'}
        async def routine():sequence.append('routine');return {'image_produced':False}
        coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine,
            routine_png_cooldown_s=capture.FRAME_CONTROL_POLICY['post_png_routine_cooldown_s'])
        with patch.object(capture,'wait_until_epoch',timer):
            fallback=asyncio.create_task(capture.post_input_checkpoint(100.,coordinator.checkpoint,lambda:True))
            await waiting.wait()
            for _ in range(3):
                result=await asyncio.wait_for(coordinator.checkpoint(routine=True),.1)
                self.assertFalse(result['image_produced'])
            self.assertFalse(fallback.done());self.assertFalse(coordinator._lock.locked())
            due.set();self.assertEqual(await fallback,{'data':'actual-image'})
        self.assertEqual(sequence,['routine','routine','routine','png'])

    async def test_pixel_arriving_during_wait_cancels_unneeded_png(self):
        waiting=asyncio.Event();due=asyncio.Event();fresh=False;draws=[]
        async def timer(deadline,check):waiting.set();await due.wait()
        async def png():draws.append('png');return {'data':'actual-image'}
        coordinator=clock_runtime.FrameCoordinator(png)
        with patch.object(capture,'wait_until_epoch',timer):
            fallback=asyncio.create_task(capture.post_input_checkpoint(100.,coordinator.checkpoint,lambda:not fresh))
            await waiting.wait();fresh=True;due.set()
            self.assertIsNone(await fallback)
        self.assertEqual(draws,[])

    async def test_queued_routine_rechecks_png_cooldown_after_lock_acquisition(self):
        entered=asyncio.Event();release=asyncio.Event();sequence=[];traces=[]
        now=[10.];cooldown=capture.FRAME_CONTROL_POLICY['post_png_routine_cooldown_s']
        async def png():entered.set();await release.wait();sequence.append('png');return {'data':'actual-image'}
        async def routine():sequence.append('routine');return {'image_produced':False}
        async def boundary():sequence.append('boundary');return {'image_produced':False}
        coordinator=clock_runtime.FrameCoordinator(png,traces.append,routine_draw=routine,
            boundary_draw=boundary,routine_png_cooldown_s=cooldown)
        with patch.object(clock_runtime.time,'perf_counter',lambda:now[0]):
            image=asyncio.create_task(coordinator.checkpoint());await entered.wait()
            queued=asyncio.create_task(coordinator.checkpoint(routine=True));await asyncio.sleep(0)
            now[0]=10.010;release.set();await image
            self.assertIsNone(await queued)
            self.assertTrue(traces[-1]['skipped_for_png_cooldown'])
            self.assertIsNone(traces[-1]['draw_s'])
            self.assertFalse(coordinator._lock.locked())
            # Input boundaries retain priority and do not wait for optional pacing.
            self.assertFalse((await coordinator.checkpoint(priority=True))['image_produced'])
            now[0]=10.010+cooldown-.000001
            self.assertIsNone(await coordinator.checkpoint(routine=True))
            now[0]=10.010+cooldown+.000001
            self.assertFalse((await coordinator.checkpoint(routine=True))['image_produced'])
        self.assertEqual(sequence,['png','boundary','routine'])

    async def test_failed_png_does_not_create_a_completion_or_block_routine(self):
        async def png():raise RuntimeError('PNG failed')
        async def routine():return {'image_produced':False}
        coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine,routine_png_cooldown_s=1.)
        with self.assertRaisesRegex(RuntimeError,'PNG failed'):await coordinator.checkpoint()
        self.assertIsNone(coordinator._last_png_completed)
        self.assertFalse((await coordinator.checkpoint(routine=True))['image_produced'])

    async def test_queued_fallback_rechecks_freshness_and_input_state_under_mutex(self):
        for invalidation in ('fresh_pixel','input_started'):
            with self.subTest(invalidation=invalidation):
                entered=asyncio.Event();release=asyncio.Event();evaluated=asyncio.Event()
                state={'fresh':False,'input_in_flight':False};pngs=[];traces=[]
                async def routine():entered.set();await release.wait();return {'image_produced':False}
                async def png():pngs.append('png');return {'data':'actual-image'}
                async def timer(deadline,check):pass
                def needed():
                    evaluated.set()
                    return not state['fresh'] and not state['input_in_flight']
                coordinator=clock_runtime.FrameCoordinator(png,traces.append,routine_draw=routine)
                running=asyncio.create_task(coordinator.checkpoint(routine=True));await entered.wait()
                with patch.object(capture,'wait_until_epoch',timer):
                    fallback=asyncio.create_task(capture.post_input_checkpoint(100.,coordinator.checkpoint,needed))
                    await evaluated.wait();await asyncio.sleep(0)
                    self.assertFalse(fallback.done())
                    state['fresh' if invalidation=='fresh_pixel' else 'input_in_flight']=True
                    release.set();await running
                    self.assertIsNone(await fallback)
                self.assertEqual(pngs,[]);self.assertIsNone(coordinator._last_png_completed)
                self.assertTrue(traces[-1]['skipped_for_ineligible']);self.assertIsNone(traces[-1]['draw_s'])

    async def test_queued_pump_and_timer_pngs_do_not_duplicate_newly_arrived_pixels(self):
        for first_kind in ('timer','pump'):
            with self.subTest(first_kind=first_kind):
                entered=asyncio.Event();release=asyncio.Event();latest=[100.];pngs=[];traces=[]
                async def png():
                    entered.set();await release.wait();latest[0]=100.1
                    pngs.append('png');return {'data':'actual-image'}
                async def timer(deadline,check):pass
                def timer_needed():return latest[0]<100.+.015
                def pump_needed():
                    return capture.idle_checkpoint_due(since_frame_s=100.1-latest[0],
                        upcoming_input_s=.3,input_in_flight=False,since_input_s=.1)
                coordinator=clock_runtime.FrameCoordinator(png,traces.append)
                def request(kind):
                    return (capture.post_input_checkpoint(100.,coordinator.checkpoint,timer_needed)
                            if kind=='timer' else coordinator.checkpoint(eligible=pump_needed))
                with patch.object(capture,'wait_until_epoch',timer):
                    first=asyncio.create_task(request(first_kind));await entered.wait()
                    queued=asyncio.create_task(request('pump' if first_kind=='timer' else 'timer'))
                    await asyncio.sleep(0);self.assertFalse(queued.done())
                    release.set();self.assertEqual(await first,{'data':'actual-image'})
                    self.assertIsNone(await queued)
                self.assertEqual(pngs,['png']);self.assertTrue(traces[-1]['skipped_for_ineligible'])
                self.assertFalse(coordinator._lock.locked())

    async def test_still_eligible_queued_png_executes_once(self):
        entered=asyncio.Event();release=asyncio.Event();pngs=[];checked=[]
        async def routine():entered.set();await release.wait();return {'image_produced':False}
        async def png():pngs.append('png');return {'data':'actual-image'}
        coordinator=clock_runtime.FrameCoordinator(png,routine_draw=routine)
        def eligible():checked.append(coordinator._lock.locked());return True
        running=asyncio.create_task(coordinator.checkpoint(routine=True));await entered.wait()
        queued=asyncio.create_task(coordinator.checkpoint(eligible=eligible));await asyncio.sleep(0)
        self.assertEqual(checked,[])
        release.set();await running
        self.assertEqual(await queued,{'data':'actual-image'})
        self.assertEqual(pngs,['png']);self.assertEqual(checked,[True])

    async def test_eligibility_is_rechecked_after_awaited_preparation(self):
        fresh=False;pngs=[]
        async def png():pngs.append('png');return {'data':'actual-image'}
        async def preparation():
            nonlocal fresh
            await asyncio.sleep(0);fresh=True
        coordinator=clock_runtime.FrameCoordinator(png)
        self.assertIsNone(await coordinator.checkpoint(before_draw=preparation,eligible=lambda:not fresh))
        self.assertEqual(pngs,[]);self.assertIsNone(coordinator._last_png_completed)

    async def test_failed_eligibility_releases_mutex_without_draw_or_cooldown(self):
        pngs=[];traces=[]
        async def png():pngs.append('png');return {'data':'actual-image'}
        async def routine():return {'image_produced':False}
        def eligible():raise RuntimeError('Eligibility failed')
        coordinator=clock_runtime.FrameCoordinator(png,traces.append,routine_draw=routine,
            routine_png_cooldown_s=1.)
        with self.assertRaisesRegex(RuntimeError,'Eligibility failed'):
            await coordinator.checkpoint(eligible=eligible)
        self.assertEqual(pngs,[]);self.assertIsNone(coordinator._last_png_completed)
        self.assertFalse(coordinator._lock.locked());self.assertIsNone(traces[-1]['draw_s'])
        self.assertEqual(traces[-1]['error']['type'],'RuntimeError')
        self.assertFalse((await coordinator.checkpoint(routine=True))['image_produced'])


if __name__=='__main__':unittest.main()
