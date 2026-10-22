"""Owned clock/runtime cleanup and real-frame priority without native capture."""
import asyncio
import ctypes
import json
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import Mock,patch

from kiroclops.web_local import clock_runtime as runtime


class ClockRuntimeTests(unittest.TestCase):
    def services(self,begin=0,end=0):
        return Mock(build=26100,begin=Mock(return_value=begin),end=Mock(return_value=end))

    def test_filetime_integer_epoch_conversion_preserves_fraction(self):
        self.assertEqual(runtime.filetime_to_epoch(runtime.FILETIME_UNIX_OFFSET),0)
        self.assertEqual(runtime.filetime_to_epoch(runtime.FILETIME_UNIX_OFFSET+12_345_678),1.2345678)
        self.assertEqual(runtime.filetime_to_epoch(runtime.FILETIME_UNIX_OFFSET-10_000_000),-1)

    @unittest.skipUnless(runtime.os.name=='nt','Native UTC API layout is Windows-specific')
    def test_utc_reads_native_filetime_with_no_synthetic_offset(self):
        ticks=runtime.FILETIME_UNIX_OFFSET+17_900_223_341_234_567
        def read(pointer):
            value=ctypes.cast(pointer,ctypes.POINTER(runtime._FILETIME)).contents
            value.low=ticks&0xffffffff;value.high=ticks>>32
        with patch.object(runtime,'_precise_clock',read):
            self.assertEqual(runtime.utc_seconds(),runtime.filetime_to_epoch(ticks))

    def test_local_loop_uses_actual_performance_clock_without_policy_change(self):
        policy=asyncio.get_event_loop_policy();loop=runtime.RealTimeCaptureLoop()
        try:
            before=time.perf_counter();value=loop.time();after=time.perf_counter()
            self.assertLessEqual(before,value);self.assertLessEqual(value,after)
            self.assertEqual(loop._clock_resolution,time.get_clock_info('perf_counter').resolution)
            self.assertIs(asyncio.get_event_loop_policy(),policy)
        finally:loop.close()

    def test_timer_is_released_exactly_once_on_success_error_and_cancel(self):
        for error in (None,RuntimeError('operation failed'),asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                services=self.services();receipt={}
                try:
                    with runtime.capture_timer_scope(receipt,services):
                        if error is not None:raise error
                except BaseException as actual:self.assertIs(actual,error)
                services.begin.assert_called_once_with(1);services.end.assert_called_once_with(1)
                self.assertTrue(receipt['timer_period_released'])
                self.assertEqual(receipt['timer_end_result'],0)

    def test_failed_begin_never_claims_active_timer_or_issues_unmatched_end(self):
        services=self.services(begin=97);receipt={}
        with self.assertRaises(runtime.CaptureEnvironmentError):
            with runtime.capture_timer_scope(receipt,services):self.fail('Failed timer cannot run capture')
        services.end.assert_not_called();self.assertFalse(receipt['timer_period_released'])

    def test_release_error_cannot_be_a_success(self):
        services=self.services(end=97);receipt={}
        with self.assertRaises(runtime.CaptureEnvironmentError):
            with runtime.capture_timer_scope(receipt,services):pass
        self.assertFalse(receipt['timer_period_released']);self.assertIn('timer_release_error',receipt)

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_doctor_reads_capabilities_without_requesting_a_timer_period(self):
        services=self.services()
        with patch.object(runtime,'_WindowsTimerServices',return_value=services):
            result=runtime.capability_check()
        self.assertEqual(result['status'],'ready');self.assertFalse(result['timer_period_requested'])
        services.begin.assert_not_called();services.end.assert_not_called()

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_doctor_reports_unsupported_environment_explicitly(self):
        with patch.object(runtime,'_WindowsTimerServices',side_effect=runtime.CaptureEnvironmentError('API unavailable')):
            result=runtime.capability_check()
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['error']['code'],'capture_environment_failure')

    def qos_services(self,*,waits=(.001,)*16,previous=None,set_error=None):
        services=self.services();services.power_throttling_supported=True;services.timer_probe_supported=True
        services.power_throttling=Mock(return_value=previous or {'control_mask':0,'state_mask':0})
        services.set_power_throttling=Mock(side_effect=set_error)
        services.timer_probe=Mock(return_value=list(waits))
        return services

    def test_power_throttling_opt_out_is_scoped_and_previous_state_restored(self):
        previous={'control_mask':runtime.POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
                  'state_mask':runtime.POWER_THROTTLING_IGNORE_TIMER_RESOLUTION}
        services=self.qos_services(previous=previous);receipt={}
        with runtime.capture_timer_scope(receipt,services):
            services.set_power_throttling.assert_called_once_with(
                runtime.POWER_THROTTLING_EXECUTION_SPEED|runtime.POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,0)
        self.assertEqual(services.set_power_throttling.call_args_list[-1].args,
                         (previous['control_mask'],previous['state_mask']))
        self.assertTrue(receipt['power_throttling']['applied'])
        self.assertEqual(receipt['power_throttling']['restored'],previous)
        self.assertTrue(receipt['effective_timer']['honored']);self.assertTrue(receipt['timer_period_released'])

    def test_unhonored_timer_fails_before_capture_and_still_releases_everything(self):
        services=self.qos_services(waits=(.0156,)*16);receipt={}
        with self.assertRaises(runtime.CaptureEnvironmentError):
            with runtime.capture_timer_scope(receipt,services):self.fail('Coarse timer must not start a capture')
        self.assertFalse(receipt['effective_timer']['honored'])
        services.end.assert_called_once_with(1);self.assertTrue(receipt['timer_period_released'])
        self.assertEqual(services.set_power_throttling.call_args_list[-1].args,(0,0))

    def test_failed_opt_out_is_recorded_and_the_wait_probe_still_decides(self):
        services=self.qos_services(set_error=OSError('access denied'));receipt={}
        with runtime.capture_timer_scope(receipt,services):pass
        self.assertFalse(receipt['power_throttling']['applied'])
        self.assertIn('access denied',receipt['power_throttling']['error'])
        self.assertNotIn('restored',receipt['power_throttling'])
        self.assertTrue(receipt['effective_timer']['honored'])

    @unittest.skipUnless(runtime.os.name=='nt','Real Windows timer and QoS services')
    def test_real_services_honor_one_millisecond_waits_inside_scope(self):
        receipt={}
        with runtime.capture_timer_scope(receipt):
            waits=runtime._WindowsTimerServices().timer_probe()
        self.assertTrue(receipt['effective_timer']['honored'],receipt['effective_timer'])
        self.assertLess(sorted(waits)[len(waits)//2],runtime.TIMER_PROBE['maximum_median_wait_s'])
        self.assertTrue(receipt['timer_period_released'])

    def execute(self,factory,*,loop_factory=None,services=None):
        services=services or self.services()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'receipt.json'
            with patch.object(runtime,'_WindowsTimerServices',return_value=services):
                if loop_factory is None:
                    try:value=runtime.run_capture(factory,path);error=None
                    except BaseException as exception:value=None;error=exception
                else:
                    with patch.object(runtime,'RealTimeCaptureLoop',side_effect=loop_factory):
                        try:value=runtime.run_capture(factory,path);error=None
                        except BaseException as exception:value=None;error=exception
            return value,error,json.loads(path.read_text()),services

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_run_capture_success_drains_owned_pending_tasks(self):
        stopped=[];policy=asyncio.get_event_loop_policy()
        async def child():
            try:await asyncio.Event().wait()
            finally:stopped.append(True)
        async def operation():
            asyncio.create_task(child());await asyncio.sleep(0)
            self.assertIsInstance(asyncio.get_running_loop(),runtime.RealTimeCaptureLoop)
            return 42
        value,error,receipt,services=self.execute(operation)
        self.assertIsNone(error);self.assertEqual(value,42);self.assertEqual(stopped,[True])
        self.assertEqual(receipt['status'],'completed');self.assertTrue(receipt['loop_closed'])
        services.end.assert_called_once_with(1);self.assertIs(asyncio.get_event_loop_policy(),policy)

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_run_capture_cancellation_closes_loop_and_releases_timer(self):
        async def operation():raise asyncio.CancelledError()
        _,error,receipt,services=self.execute(operation)
        self.assertIsInstance(error,asyncio.CancelledError)
        self.assertEqual(receipt['status'],'cancelled');self.assertTrue(receipt['loop_closed'])
        services.end.assert_called_once_with(1)

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_loop_startup_failure_releases_timer_without_creating_coroutine(self):
        factory=Mock();failure=RuntimeError('loop startup failed')
        _,error,receipt,services=self.execute(factory,loop_factory=Mock(side_effect=failure))
        self.assertIs(error,failure);factory.assert_not_called();self.assertEqual(receipt['status'],'failed')
        services.end.assert_called_once_with(1);self.assertIsNone(receipt['loop_closed'])

    @unittest.skipUnless(runtime.os.name=='nt','Windows timer service injection')
    def test_cleanup_failure_still_closes_loop_and_releases_timer(self):
        original_class=runtime.RealTimeCaptureLoop;created=[]
        class CleanupFailureLoop(original_class):
            async def shutdown_asyncgens(self):raise RuntimeError('cleanup failed')
        def create():
            loop=CleanupFailureLoop();created.append(loop);return loop
        async def operation():return 42
        _,error,receipt,services=self.execute(operation,loop_factory=create)
        self.assertEqual(str(error),'cleanup failed');self.assertTrue(created[0].is_closed())
        self.assertEqual(receipt['status'],'failed');self.assertTrue(receipt['loop_closed'])
        services.end.assert_called_once_with(1)


class FramePriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_routine_advance_is_image_free_and_yields_to_input_boundary(self):
        entered=asyncio.Event();release=asyncio.Event();sequence=[];traces=[]
        async def initial_png():
            sequence.append('explicit-png');return {'data':'actual-pixels'}
        async def routine():
            sequence.append('routine-advance-start');entered.set();await release.wait()
            sequence.append('routine-advance-end');return {'image_produced':False}
        async def boundary():
            sequence.append('input-boundary');return {'image_produced':False}
        coordinator=runtime.FrameCoordinator(initial_png,traces.append,
            boundary_draw=boundary,routine_draw=routine)
        running=asyncio.create_task(coordinator.checkpoint(routine=True));await entered.wait()
        queued=asyncio.create_task(coordinator.checkpoint(routine=True));await asyncio.sleep(0)
        input_boundary=asyncio.create_task(coordinator.checkpoint(priority=True));await asyncio.sleep(0)
        release.set()
        first,skipped,actual_boundary=await asyncio.gather(running,queued,input_boundary)
        self.assertFalse(first['image_produced']);self.assertIsNone(skipped)
        self.assertFalse(actual_boundary['image_produced'])
        self.assertEqual(sequence,['routine-advance-start','routine-advance-end','input-boundary'])
        self.assertEqual([t['draw_kind'] for t in traces],
                         ['routine_advance_only','routine_advance_only','advance_only'])
        self.assertTrue(traces[1]['skipped_for_pending_boundary'])
        self.assertEqual(coordinator._priority_waiters,0)
        self.assertFalse(coordinator._lock.locked())

    async def test_routine_cannot_claim_input_boundary_priority(self):
        coordinator=runtime.FrameCoordinator(Mock())
        with self.assertRaises(ValueError):
            await coordinator.checkpoint(priority=True,routine=True)
        self.assertEqual(coordinator._priority_waiters,0)

    async def test_distinct_boundary_draw_keeps_hit_mutex_and_does_not_create_png(self):
        sequence=[];traces=[]
        async def image():sequence.append('png');return 'actual-png'
        async def advance():sequence.append('completed-native-frame');return {'image_produced':False}
        async def hit():
            self.assertTrue(coordinator._lock.locked());sequence.append('hit')
        coordinator=runtime.FrameCoordinator(image,traces.append,boundary_draw=advance)
        boundary=await coordinator.checkpoint(priority=True,before_draw=hit)
        self.assertFalse(boundary['image_produced']);self.assertFalse(coordinator._lock.locked())
        self.assertEqual(await coordinator.checkpoint(),'actual-png')
        self.assertEqual(sequence,['hit','completed-native-frame','png'])
        self.assertEqual([t['draw_kind'] for t in traces],['advance_only','actual_png'])

    async def test_one_current_hit_lookup_and_one_boundary_replace_queued_pump(self):
        entered=asyncio.Event();release=asyncio.Event();sequence=[];traces=[]
        async def draw():
            sequence.append('draw')
            if len(sequence)==1:entered.set();await release.wait()
            return 'actual-image'
        async def hit():sequence.append('current-visible-hit')
        coordinator=runtime.FrameCoordinator(draw,traces.append)
        first=asyncio.create_task(coordinator.checkpoint());await entered.wait()
        queued_pump=asyncio.create_task(coordinator.checkpoint());await asyncio.sleep(0)
        boundary=asyncio.create_task(coordinator.checkpoint(priority=True,before_draw=hit));await asyncio.sleep(0)
        release.set();results=await asyncio.gather(first,queued_pump,boundary)
        self.assertEqual(results,['actual-image',None,'actual-image'])
        self.assertEqual(sequence,['draw','current-visible-hit','draw'])
        self.assertEqual(sum(t['skipped_for_pending_boundary'] for t in traces),1)
        self.assertEqual(coordinator._priority_waiters,0)
        self.assertTrue(all(t['lock_wait_s']>=0 for t in traces))

    async def test_cancelled_waiting_boundary_does_not_starve_future_frames(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def draw():entered.set();await release.wait();return 'image'
        coordinator=runtime.FrameCoordinator(draw)
        first=asyncio.create_task(coordinator.checkpoint());await entered.wait()
        boundary=asyncio.create_task(coordinator.checkpoint(priority=True));await asyncio.sleep(0)
        boundary.cancel()
        with self.assertRaises(asyncio.CancelledError):await boundary
        self.assertEqual(coordinator._priority_waiters,0)
        release.set();await first
        self.assertEqual(await coordinator.checkpoint(),'image')

    async def test_failed_hit_lookup_produces_no_frame_and_releases_priority(self):
        draw=Mock();coordinator=runtime.FrameCoordinator(draw)
        async def hit():raise ValueError('Target disappeared')
        with self.assertRaises(ValueError):await coordinator.checkpoint(priority=True,before_draw=hit)
        draw.assert_not_called();self.assertEqual(coordinator._priority_waiters,0)
        self.assertFalse(coordinator._lock.locked())

    async def test_frame_lock_is_released_before_native_ack_wait(self):
        count=0;ack=asyncio.Event()
        async def draw():
            nonlocal count
            count+=1;return 'real-image'
        coordinator=runtime.FrameCoordinator(draw)
        async def pointer_operation():
            await coordinator.checkpoint(priority=True)
            await ack.wait()
        pointer=asyncio.create_task(pointer_operation());await asyncio.sleep(0)
        self.assertFalse(coordinator._lock.locked())
        self.assertEqual(await asyncio.wait_for(coordinator.checkpoint(),.1),'real-image')
        self.assertEqual(count,2);ack.set();await pointer


if __name__=='__main__':unittest.main()
