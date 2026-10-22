"""Bounded progress I/O outside the real-time capture event loop."""
from __future__ import annotations

import threading
import time

from .clock_runtime import utc_seconds


class ProgressPublishError(RuntimeError):
    code = 'capture_progress_failure'


class ProgressPublisher:
    """One callback in flight plus one replaceable pending progress value.

    Progress is a latest-status display, not the input audit trail. The latter
    remains in capture receipts. A blocked external callback cannot be killed
    safely in a Python thread; bounded shutdown reports that condition and its
    daemon thread cannot make the capture wait indefinitely.
    """
    def __init__(self,callback,*,record_limit=64):
        self._callback=callback;self._condition=threading.Condition()
        self._pending=None;self._active=None;self._closing=False;self._error=None
        self._submitted=0;self._completed=0;self._coalesced=0;self._discarded=0
        self._maximum_duration=0.;self._records=[];self._record_limit=record_limit
        self._record_overflow=0;self._close_status='open'
        self._thread=threading.Thread(target=self._run,daemon=True,name='capture-progress')
        self._thread.start()

    def publish(self,value):
        """Constant-space enqueue; never call the sink on the capture thread."""
        with self._condition:
            if self._error is not None:raise self._error
            if self._closing:raise ProgressPublishError('Progress publisher is closed')
            self._submitted+=1
            if self._pending is not None:self._coalesced+=1
            self._pending=(self._submitted,value,time.perf_counter())
            self._condition.notify()

    def check(self):
        with self._condition:error=self._error
        if error is not None:raise error

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closing:self._condition.wait()
                if self._pending is None:return
                sequence,value,queued=self._pending;self._pending=None
                started=time.perf_counter()
                self._active={'sequence':sequence,'phase':str(value),
                    'callback_start_epoch_s':utc_seconds(),'queue_wait_s':started-queued}
            error=None
            try:self._callback(value)
            except BaseException as failure:error=failure
            finished=time.perf_counter();ended=utc_seconds()
            with self._condition:
                row={**self._active,'callback_end_epoch_s':ended,
                     'callback_duration_s':finished-started,
                     'error':None if error is None else {
                         'type':type(error).__name__,'code':getattr(error,'code',None),'message':str(error)}}
                self._active=None;self._completed+=1
                self._maximum_duration=max(self._maximum_duration,finished-started)
                if len(self._records)<self._record_limit:self._records.append(row)
                else:self._record_overflow+=1
                if error is not None:
                    self._error=error;self._closing=True
                    if self._pending is not None:self._discarded+=1
                    self._pending=None
                self._condition.notify_all()
                if error is not None:return

    def finish(self,*,timeout=1.,check=lambda:None):
        """Flush the final value outside the capture loop; propagate failures."""
        check()
        with self._condition:
            self._closing=True;self._close_status='flushing';self._condition.notify()
        deadline=time.perf_counter()+timeout
        while self._thread.is_alive():
            check()
            remaining=deadline-time.perf_counter()
            if remaining<=0:
                with self._condition:self._close_status='flush_timeout'
                raise ProgressPublishError(f'Progress callback did not finish within {timeout:.3f}s flush bound')
            self._thread.join(min(.010,remaining))
        check();self.check()
        with self._condition:self._close_status='flushed'

    def abort(self,*,timeout=.100):
        """Discard queued status and wait at most the cleanup grace period."""
        with self._condition:
            self._closing=True
            if self._pending is not None:self._discarded+=1
            self._pending=None
            if self._close_status not in ('flushed','flush_timeout'):self._close_status='aborted'
            self._condition.notify()
        self._thread.join(timeout)

    def snapshot(self):
        with self._condition:
            return {'kind':'CaptureProgressRuntime','mode':'single_worker_latest_pending',
                'maximum_active_callbacks':1,'maximum_pending_values':1,
                'status':self._close_status,'submitted_count':self._submitted,
                'completed_count':self._completed,'coalesced_count':self._coalesced,
                'discarded_count':self._discarded,'maximum_callback_duration_s':self._maximum_duration,
                'callback_records':list(self._records),'callback_record_limit':self._record_limit,
                'callback_records_omitted':self._record_overflow,
                'active_callback':None if self._active is None else dict(self._active),
                'worker_alive':self._thread.is_alive(),
                'error':None if self._error is None else {'type':type(self._error).__name__,
                    'code':getattr(self._error,'code',None),'message':str(self._error)}}
