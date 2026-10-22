"""Slow durable progress must not run on the capture loop. No real browser."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kiroclops.web_local import capture
from kiroclops.web_local.progress import ProgressPublisher,ProgressPublishError


class ProgressPublisherTests(unittest.TestCase):
    def test_single_worker_coalesces_dense_inputs_and_flushes_latest(self):
        entered=threading.Event();release=threading.Event();values=[];active=[0];maximum=[0]
        def callback(value):
            active[0]+=1;maximum[0]=max(maximum[0],active[0]);values.append(value)
            if value=='first':entered.set();release.wait(1.)
            active[0]-=1
        publisher=ProgressPublisher(callback)
        try:
            publisher.publish('first');self.assertTrue(entered.wait(.5))
            for n in range(1000):publisher.publish(f'input-{n}')
            self.assertEqual(publisher.snapshot()['coalesced_count'],999)
            release.set();publisher.finish()
            self.assertEqual(values,['first','input-999']);self.assertEqual(maximum[0],1)
            self.assertFalse(publisher.snapshot()['worker_alive'])
        finally:release.set();publisher.abort()

    def test_callback_error_propagates_from_check_and_final_flush(self):
        failure=RuntimeError('durable progress failed');entered=threading.Event()
        def callback(value):entered.set();raise failure
        publisher=ProgressPublisher(callback)
        try:
            publisher.publish('input-1');self.assertTrue(entered.wait(.5))
            with self.assertRaises(RuntimeError) as raised:publisher.finish()
            self.assertIs(raised.exception,failure)
            with self.assertRaises(RuntimeError):publisher.check()
            self.assertEqual(publisher.snapshot()['error']['message'],str(failure))
        finally:publisher.abort()

    def test_slow_callback_does_not_block_cancellation_or_unbounded_cleanup(self):
        entered=threading.Event();release=threading.Event();values=[]
        def callback(value):values.append(value);entered.set();release.wait(1.)
        publisher=ProgressPublisher(callback)
        try:
            publisher.publish('first');self.assertTrue(entered.wait(.5));publisher.publish('pending')
            cancelled=RuntimeError('cancelled')
            def check():raise cancelled
            with self.assertRaises(RuntimeError) as raised:publisher.finish(check=check)
            self.assertIs(raised.exception,cancelled)
            started=time.perf_counter();publisher.abort(timeout=.020)
            self.assertLess(time.perf_counter()-started,.2)
            status=publisher.snapshot()
            self.assertEqual(status['discarded_count'],1);self.assertTrue(status['worker_alive'])
            self.assertEqual(status['status'],'aborted')
            release.set();publisher._thread.join(.5)
            self.assertEqual(values,['first'])
        finally:release.set();publisher.abort()

    def test_final_flush_is_bounded_and_never_claims_success_for_stuck_sink(self):
        entered=threading.Event();release=threading.Event()
        def callback(value):entered.set();release.wait(1.)
        publisher=ProgressPublisher(callback)
        try:
            publisher.publish('input-1');self.assertTrue(entered.wait(.5))
            with self.assertRaises(ProgressPublishError):publisher.finish(timeout=.025)
            self.assertEqual(publisher.snapshot()['status'],'flush_timeout')
            self.assertTrue(publisher.snapshot()['worker_alive'])
        finally:release.set();publisher.abort()


class CaptureProgressIntegrationTests(unittest.TestCase):
    @staticmethod
    def result():
        return [],[],[],{'status':'pass'}, {'functional':{'status':'pass'}},'fake-browser',[]

    def run_fake(self,record,callback,cancel=None):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);project=root/'project';project.mkdir()
        (project/'index.html').write_text('<html></html>');output=root/'capture'
        scenario=capture.scenarios('panel','calibration')[0]
        with patch.object(capture,'_record',record):
            result=capture.capture_run(project,output,scenario,progress=callback,cancel=cancel,build_package=False)
        return result,output

    def test_160ms_callback_allows_post_input_timer_and_frame_pump_to_continue(self):
        entered=threading.Event();release=threading.Event();values=[];observed={}
        def callback(value):
            values.append(value)
            if value=='input-1':entered.set();release.wait(1.)
        async def record(project,output,scenario,cancel,progress,progress_check):
            ticks=[];fallback=[];recording=True
            async def advance():ticks.append(time.perf_counter());return {'advanced':True}
            async def png():fallback.append(time.perf_counter());return {'data':'synthetic-test-only'}
            coordinator=capture.FrameCoordinator(png,routine_draw=advance,routine_png_cooldown_s=1/60)
            async def pump():
                while recording:
                    await asyncio.sleep(.010);await coordinator.checkpoint(routine=True);progress_check()
            progress('input-1')
            while not entered.is_set():await asyncio.sleep(.001)
            started=time.perf_counter();pump_task=asyncio.create_task(pump())
            fallback_task=asyncio.create_task(capture.post_input_checkpoint(capture.utc_seconds(),coordinator.checkpoint,lambda:True))
            # This is the dense next-input path: publishing never waits for I/O.
            for n in range(2,22):progress(f'input-{n}')
            await asyncio.sleep(.160)
            observed.update(ticks_before_release=len(ticks),fallback_before_release=len(fallback),
                            callback_still_pending=not release.is_set(),elapsed_s=time.perf_counter()-started)
            release.set();recording=False;await pump_task;await fallback_task
            return self.result()
        try:
            result,output=self.run_fake(record,callback)
            self.assertEqual(result['status'],'ok')
            self.assertGreaterEqual(observed['ticks_before_release'],5)
            self.assertEqual(observed['fallback_before_release'],1)
            self.assertEqual(values,['input-1','input-21'])
            receipt=json.loads((output/'private/progress-runtime.json').read_text())
            self.assertEqual(receipt['status'],'flushed');self.assertFalse(receipt['worker_alive'])
            self.assertGreaterEqual(receipt['maximum_callback_duration_s'],.150)
            self.assertEqual(receipt['coalesced_count'],19)
        finally:release.set()

    def test_final_progress_failure_prevents_capture_success(self):
        failure=RuntimeError('final durable write failed')
        def callback(value):raise failure
        async def record(project,output,scenario,cancel,progress,progress_check):
            progress('last-input');return self.result()
        with self.assertRaises(RuntimeError) as raised:self.run_fake(record,callback)
        self.assertIs(raised.exception,failure)

    def test_cancellation_during_flush_is_not_missed(self):
        entered=threading.Event();release=threading.Event();cancel=threading.Event()
        def callback(value):entered.set();release.wait(1.)
        async def record(project,output,scenario,cancel_arg,progress,progress_check):
            progress('last-input')
            while not entered.is_set():await asyncio.sleep(.001)
            cancel.set();return self.result()
        try:
            with self.assertRaises(capture.CaptureError) as raised:self.run_fake(record,callback,cancel)
            self.assertEqual(raised.exception.code,'cancelled')
        finally:release.set()

    def test_required_progress_receipt_failure_prevents_success_receipt(self):
        failure=OSError('progress receipt write failed');attempted=[];original=capture._write
        def write(path,value):
            attempted.append(path.name)
            if path.name=='progress-runtime.json':raise failure
            return original(path,value)
        async def record(project,output,scenario,cancel,progress,progress_check):
            progress('last-input');return self.result()
        with patch.object(capture,'_write',write):
            with self.assertRaises(OSError) as raised:self.run_fake(record,lambda value:None)
        self.assertIs(raised.exception,failure)
        self.assertNotIn('capture-result.json',attempted)
        self.assertIn('capture-error.json',attempted)

    def test_diagnostic_failures_preserve_original_capture_error(self):
        failure=RuntimeError('original capture failed');original=capture._write
        def write(path,value):
            if path.name in ('progress-runtime.json','capture-error.json'):raise OSError('disk failed')
            return original(path,value)
        async def record(project,output,scenario,cancel,progress,progress_check):raise failure
        with patch.object(capture,'_write',write):
            with self.assertRaises(RuntimeError) as raised:self.run_fake(record,lambda value:None)
        self.assertIs(raised.exception,failure)


if __name__=='__main__':unittest.main()
