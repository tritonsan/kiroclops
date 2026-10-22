"""A caller's cancel probe must never run on the live capture loop. No browser."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kiroclops.web_local import capture


class CancelMonitorTests(unittest.TestCase):
    def test_probe_runs_on_monitor_thread_and_event_reports_cancellation(self):
        calls=[];requested=threading.Event()
        def probe():
            calls.append(threading.current_thread().name);return requested.is_set()
        monitor=capture.CancelMonitor(probe,interval_s=.005)
        try:
            self.assertFalse(monitor.is_set())
            requested.set();deadline=time.perf_counter()+1.
            while not monitor.is_set() and time.perf_counter()<deadline:time.sleep(.002)
            self.assertTrue(monitor.is_set())
        finally:monitor.close()
        # The first answer is taken by the constructing thread before any loop.
        self.assertEqual(calls[0],threading.current_thread().name)
        self.assertTrue(all(name==capture.CANCEL_MONITOR_POLICY['caller_probe_thread'] for name in calls[1:]))
        snapshot=monitor.snapshot()
        self.assertTrue(snapshot['cancel_observed']);self.assertFalse(snapshot['worker_alive'])
        self.assertIsNone(snapshot['error'])

    def test_blocked_probe_does_not_block_event_reads(self):
        entered=threading.Event();release=threading.Event();first=[True]
        def probe():
            if first[0]:first[0]=False;return False
            entered.set();release.wait(1.);return False
        monitor=capture.CancelMonitor(probe,interval_s=.001)
        try:
            self.assertTrue(entered.wait(.5))
            started=time.perf_counter()
            for _ in range(1000):self.assertFalse(monitor.is_set())
            self.assertLess(time.perf_counter()-started,.05)
        finally:release.set();monitor.close()

    def test_probe_error_fails_the_reader_with_the_original_error(self):
        failure=OSError('cancel probe unreadable')
        def probe():raise failure
        monitor=capture.CancelMonitor(probe,interval_s=.005)
        try:
            with self.assertRaises(OSError) as raised:monitor.is_set()
            self.assertIs(raised.exception,failure)
        finally:monitor.close()
        self.assertEqual(monitor.snapshot()['error']['message'],str(failure))

    def test_event_like_probe_is_supported(self):
        requested=threading.Event();requested.set()
        monitor=capture.CancelMonitor(requested,interval_s=.005)
        try:self.assertTrue(monitor.is_set())
        finally:monitor.close()


class CaptureCancelIntegrationTests(unittest.TestCase):
    @staticmethod
    def result():
        return [],[],[],{'status':'pass'},{'functional':{'status':'pass'}},'fake-browser',[]

    def run_fake(self,record,cancel):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);project=root/'project';project.mkdir()
        (project/'index.html').write_text('<html></html>');output=root/'capture'
        scenario=capture.scenarios('panel','calibration')[0]
        with patch.object(capture,'_record',record):
            result=capture.capture_run(project,output,scenario,cancel=cancel,build_package=False)
        return result,output

    def test_live_record_never_calls_a_slow_caller_probe(self):
        loop_thread=[];in_record=threading.Event();probe_threads=[];slow=threading.Event()
        def probe():
            probe_threads.append((threading.get_ident(),in_record.is_set()))
            if in_record.is_set():slow.set();time.sleep(.150)  # like a stalled job-directory stat
            return False
        observed={}
        async def record(project,output,scenario,cancel_arg,progress,progress_check):
            loop_thread.append(threading.get_ident());in_record.set()
            while not slow.is_set():await asyncio.sleep(.001)
            started=time.perf_counter();longest=0.
            for _ in range(200):
                began=time.perf_counter();self.assertFalse(cancel_arg.is_set())
                longest=max(longest,time.perf_counter()-began);await asyncio.sleep(0)
            observed.update(longest_check_s=longest,total_s=time.perf_counter()-started)
            in_record.clear();return self.result()
        result,output=self.run_fake(record,probe)
        self.assertEqual(result['status'],'ok')
        self.assertLess(observed['longest_check_s'],.010)
        live_calls=[ident for ident,live in probe_threads if live]
        self.assertTrue(live_calls)
        self.assertNotIn(loop_thread[0],live_calls)
        receipt=json.loads((output/'private/cancel-runtime.json').read_text())
        self.assertFalse(receipt['cancel_observed']);self.assertFalse(receipt['worker_alive'])
        self.assertGreaterEqual(receipt['maximum_probe_s'],.150)

    def test_cancellation_reaches_the_live_record_through_the_monitor(self):
        requested=threading.Event()
        async def record(project,output,scenario,cancel_arg,progress,progress_check):
            requested.set();deadline=time.perf_counter()+1.
            while not cancel_arg.is_set():
                if time.perf_counter()>deadline:self.fail('Monitor did not report cancellation')
                await asyncio.sleep(.002)
            raise capture.CaptureError('cancelled','Local capture cancelled')
        with self.assertRaises(capture.CaptureError) as raised:self.run_fake(record,requested)
        self.assertEqual(raised.exception.code,'cancelled')


if __name__=='__main__':unittest.main()
