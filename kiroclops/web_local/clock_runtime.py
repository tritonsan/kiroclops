"""Owned real-time capture clocks and frame scheduling; no virtual clock.

Python 3.10's Windows default loop clock is GetTickCount64 (15.625ms).
This loop uses the actual performance counter and pairs its scoped timer
resolution request with release, without replacing any process clock/policy.
The owned capture also opts out of power throttling for its duration and
fails closed when a measured 1 ms wait shows the request was not honored.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import statistics
import sys
import time


class CaptureEnvironmentError(RuntimeError):
    code = 'capture_environment_failure'


FILETIME_UNIX_OFFSET = 116444736000000000


def filetime_to_epoch(ticks):
    # Subtract the integer epoch first; avoid losing UTC sub-microsecond bits
    # by converting a 1601-based absolute tick count to float too early.
    return (int(ticks)-FILETIME_UNIX_OFFSET)/10_000_000


class _FILETIME(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]


_precise_clock = None


def utc_seconds():
    """Read actual UTC, with no calibrated/synthetic epoch offset."""
    global _precise_clock
    if os.name != 'nt':
        return time.time()
    if _precise_clock is None:
        try:
            function=ctypes.WinDLL('kernel32',use_last_error=True).GetSystemTimePreciseAsFileTime
            function.argtypes=[ctypes.POINTER(_FILETIME)];function.restype=None
            _precise_clock=function
        except (OSError,AttributeError) as error:
            raise CaptureEnvironmentError('Precise native UTC clock is unavailable') from error
    value=_FILETIME();_precise_clock(ctypes.byref(value))
    return filetime_to_epoch((value.high<<32)|value.low)


def clock_description():
    performance=time.get_clock_info('perf_counter')
    return {'utc_backend':'GetSystemTimePreciseAsFileTime' if os.name=='nt' else time.get_clock_info('time').implementation,
        'utc_documented_precision_upper_bound_s':1e-6 if os.name=='nt' else time.get_clock_info('time').resolution,
        'utc_accuracy_claimed':False,
        'utc_float_representation':'IEEE754 epoch seconds; no source timestamp replacement',
        'scheduler_backend':performance.implementation,'scheduler_resolution_s':performance.resolution,
        'python':sys.version,'platform':sys.platform,'virtual_time':False,
        'global_event_loop_policy_changed':False}


PROCESS_POWER_THROTTLING=4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
POWER_THROTTLING_EXECUTION_SPEED=0x1
POWER_THROTTLING_IGNORE_TIMER_RESOLUTION=0x4
# Windows 11 may silently ignore a background process's timer request; the
# 3A cohort then waited in 15.6 ms steps. A 1 ms wait probe must confirm the
# requested period before any browser starts.
TIMER_PROBE={'requested_wait_s':.001,'samples':16,'maximum_median_wait_s':.004}


class _PowerThrottlingState(ctypes.Structure):
    _fields_=[('Version',ctypes.c_ulong),('ControlMask',ctypes.c_ulong),('StateMask',ctypes.c_ulong)]


class _WindowsTimerServices:
    timer_probe_supported=True

    def __init__(self):
        # Older Windows changes this setting globally. The supported local
        # adapter requires the documented per-process Windows 10 2004+ scope.
        version=sys.getwindowsversion()
        if version.build<19041:
            raise CaptureEnvironmentError('Per-process timer control requires Windows 10 build19041 or newer')
        self.build=version.build
        try:
            dll=ctypes.WinDLL('winmm',use_last_error=True)
            self.begin=dll.timeBeginPeriod;self.end=dll.timeEndPeriod
            for function in (self.begin,self.end):
                function.argtypes=[ctypes.c_uint];function.restype=ctypes.c_uint
        except (OSError,AttributeError) as error:
            raise CaptureEnvironmentError('Windows per-process timer API is unavailable') from error
        try:
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self._process=kernel.GetCurrentProcess;self._process.restype=ctypes.c_void_p
            self._set=kernel.SetProcessInformation;self._get=kernel.GetProcessInformation
            for function in (self._set,self._get):
                function.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_ulong]
                function.restype=ctypes.c_int
            self.power_throttling_supported=True
        except (OSError,AttributeError):
            self.power_throttling_supported=False

    def power_throttling(self):
        state=_PowerThrottlingState(1,0,0)
        if not self._get(self._process(),PROCESS_POWER_THROTTLING,ctypes.byref(state),ctypes.sizeof(state)):
            raise OSError(ctypes.get_last_error(),'GetProcessInformation(ProcessPowerThrottling) failed')
        return {'control_mask':state.ControlMask,'state_mask':state.StateMask}

    def set_power_throttling(self,control_mask,state_mask):
        state=_PowerThrottlingState(1,control_mask,state_mask)
        if not self._set(self._process(),PROCESS_POWER_THROTTLING,ctypes.byref(state),ctypes.sizeof(state)):
            raise OSError(ctypes.get_last_error(),'SetProcessInformation(ProcessPowerThrottling) failed')

    def timer_probe(self):
        """Actual durations of short kernel waits, as the proactor loop uses."""
        samples=[]
        for _ in range(TIMER_PROBE['samples']):
            started=time.perf_counter();time.sleep(TIMER_PROBE['requested_wait_s'])
            samples.append(time.perf_counter()-started)
        return samples


def capability_check():
    """Read-only doctor check: load/read APIs, never request a timer period."""
    result={**clock_description(),'timer_period_requested':False,'native_capture_started':False}
    try:
        if os.name=='nt':
            services=_WindowsTimerServices()
            result.update(windows_build=services.build,timer_api='winmm.timeBeginPeriod/timeEndPeriod',
                          power_throttling_api=('kernel32.SetProcessInformation(ProcessPowerThrottling)'
                                                if getattr(services,'power_throttling_supported',False) is True else None),
                          timer_probe_policy=dict(TIMER_PROBE))
        else:
            result['timer_api']='not_required'
        result.update(status='ready',utc_read_epoch_s=utc_seconds())
    except CaptureEnvironmentError as error:
        result.update(status='unavailable',error={'code':error.code,'message':str(error)})
    return result


@contextmanager
def capture_timer_scope(receipt,services=None):
    """A successful Begin always has exactly one matching End, even on cancel."""
    receipt.update(clock_description())
    receipt.update(timer_period_requested_ms=None,timer_period_released=False)
    if os.name!='nt' and services is None:
        receipt.update(timer_period_scope='not_required',timer_period_released=True)
        yield
        return
    services=services or _WindowsTimerServices()
    receipt.update(timer_period_requested_ms=1,timer_period_scope='current_process_during_owned_capture',
                   windows_build=getattr(services,'build',None))
    result=services.begin(1);receipt['timer_begin_result']=result
    if result!=0:
        raise CaptureEnvironmentError(f'timeBeginPeriod(1) failed: {result}')
    original_error=None;previous_qos=None;qos_changed=False
    try:
        if getattr(services,'power_throttling_supported',False) is True:
            # HighQoS and an explicit "honor timer resolution" for this owned
            # capture only; the previous process setting is restored below.
            try:previous_qos=services.power_throttling()
            except OSError as error:receipt['power_throttling_read_error']=str(error)
            requested={'control_mask':POWER_THROTTLING_EXECUTION_SPEED|POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,'state_mask':0}
            try:
                services.set_power_throttling(requested['control_mask'],requested['state_mask']);qos_changed=True
                receipt['power_throttling']={'requested':requested,'previous':previous_qos,'applied':True}
            except OSError as error:
                receipt['power_throttling']={'requested':requested,'previous':previous_qos,'applied':False,'error':str(error)}
        if getattr(services,'timer_probe_supported',False) is True:
            samples=services.timer_probe();median=statistics.median(samples)
            honored=median<=TIMER_PROBE['maximum_median_wait_s']
            receipt['effective_timer']={'policy':dict(TIMER_PROBE),'median_wait_s':median,
                                        'maximum_wait_s':max(samples),'honored':honored}
            if not honored:
                raise CaptureEnvironmentError(
                    f'Requested 1 ms timer period is not honored: median 1 ms wait took {median*1000:.2f} ms')
        yield
    except BaseException as error:
        original_error=error
        raise
    finally:
        failures=[]
        try:
            released=services.end(1);receipt['timer_end_result']=released
            receipt['timer_period_released']=released==0
            if released!=0:
                raise CaptureEnvironmentError(f'timeEndPeriod(1) failed: {released}')
        except BaseException as error:
            receipt['timer_release_error']={'type':type(error).__name__,'message':str(error)}
            failures.append(error)
        if qos_changed:
            restore=previous_qos or {'control_mask':0,'state_mask':0}
            try:
                services.set_power_throttling(restore['control_mask'],restore['state_mask'])
                receipt['power_throttling']['restored']=restore
            except BaseException as error:
                receipt['power_throttling_restore_error']={'type':type(error).__name__,'message':str(error)}
                failures.append(error)
        if failures and original_error is None:
            raise failures[0]


_LoopBase=getattr(asyncio,'ProactorEventLoop',asyncio.SelectorEventLoop)


class RealTimeCaptureLoop(_LoopBase):
    def __init__(self):
        super().__init__()
        self._clock_resolution=time.get_clock_info('perf_counter').resolution

    def time(self):
        return time.perf_counter()


def run_capture(coroutine_factory,receipt_path):
    """Run one owned capture; clean its tasks/loop and release its timer scope."""
    receipt={'schema_version':1,'kind':'CaptureClockRuntime','status':'starting'}
    loop=None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CaptureEnvironmentError('Synchronous capture needs its own non-running event-loop thread')
        with capture_timer_scope(receipt):
            # Verify the real UTC API before starting any browser process.
            receipt['started_epoch_s']=utc_seconds()
            loop=RealTimeCaptureLoop()
            try:
                value=loop.run_until_complete(coroutine_factory())
            finally:
                try:
                    pending=asyncio.all_tasks(loop)
                    for task in pending:task.cancel()
                    if pending:loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                finally:
                    loop.close()
        receipt['status']='completed'
        return value
    except BaseException as error:
        receipt.update(status='cancelled' if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt)) else 'failed',
                       error={'type':type(error).__name__,'code':getattr(error,'code','capture_failed'),'message':str(error)})
        raise
    finally:
        receipt['loop_closed']=loop.is_closed() if loop is not None else None
        path=Path(receipt_path);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(receipt,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')


class FrameCoordinator:
    """One draw at a time; a pending boundary takes precedence over a pump draw.

    No lock is held for a native input dispatch/ACK. Before-draw hit lookup is
    scoped only to the single boundary frame. An optional completed-frame
    callback avoids waiting for PNG encoding before a response-changing input.
    A separate routine callback can advance the compositor without requesting
    pixels; it still shares the same lock and yields to pending boundaries.
    """
    def __init__(self,draw,trace=lambda row:None,*,boundary_draw=None,routine_draw=None,
                 routine_png_cooldown_s=0.):
        self._draw=draw;self._boundary_draw=boundary_draw;self._routine_draw=routine_draw
        self._trace=trace;self._lock=asyncio.Lock();self._priority_waiters=0
        self._routine_png_cooldown_s=routine_png_cooldown_s;self._last_png_completed=None

    async def checkpoint(self,*,priority=False,before_draw=None,routine=False,eligible=None):
        if priority and routine:raise ValueError('A routine frame cannot be an input boundary')
        before=utc_seconds();started=time.perf_counter();acquired=None;draw_started=None;error=None;skipped=False;png_cooldown=False;ineligible=False
        if priority:self._priority_waiters+=1
        try:
            async with self._lock:
                acquired=time.perf_counter()
                if not priority and self._priority_waiters:
                    skipped=True
                    return None
                # Check after acquiring the lock: a PNG may have completed
                # while this routine request was queued. Skip this optional
                # slot without holding up a response-changing input boundary.
                if (routine and self._last_png_completed is not None and
                        acquired-self._last_png_completed<self._routine_png_cooldown_s):
                    png_cooldown=True
                    return None
                if before_draw is not None:await before_draw()
                # Optional PNG conditions can change while a request waits for
                # another draw. Evaluate the synchronous predicate under the
                # mutex, after any awaited preparation, before entering draw.
                if eligible is not None and not eligible():
                    ineligible=True
                    return None
                draw_started=time.perf_counter()
                draw=(self._boundary_draw if priority and self._boundary_draw is not None else
                      self._routine_draw if routine and self._routine_draw is not None else self._draw)
                result=await draw()
                if draw is self._draw and result is not None:
                    self._last_png_completed=time.perf_counter()
                return result
        except BaseException as failure:
            error={'type':type(failure).__name__,'message':str(failure)}
            raise
        finally:
            finished=time.perf_counter()
            if priority:self._priority_waiters-=1
            self._trace({'operation':'priority_frame' if priority else 'pump_frame',
                'draw_kind':('advance_only' if priority and self._boundary_draw is not None else
                             'routine_advance_only' if routine and self._routine_draw is not None else 'actual_png'),
                'host_before_s':before,'host_after_s':utc_seconds(),'duration_s':finished-started,
                'lock_wait_s':None if acquired is None else acquired-started,
                'hit_lookup_s':None if acquired is None or draw_started is None else draw_started-acquired,
                'draw_s':None if draw_started is None else finished-draw_started,
                'skipped_for_pending_boundary':skipped,
                'skipped_for_png_cooldown':png_cooldown,
                'skipped_for_ineligible':ineligible,'error':error})
