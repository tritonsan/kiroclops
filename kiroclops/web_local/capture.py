"""Headless localhost capture with browser observation times and actual inputs.

All browser and server lifetimes belong to this call. Sources are copied by
the caller; this module never edits project files. Private oracle data remains
outside the visual package. No desktop focus or operating-system input.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
from contextlib import contextmanager
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import statistics
import threading
import time

from . import VERSION
from .oracle import INIT_SCRIPT, validate_oracle
from .specs import canonical_domain, fixture_project, scenarios, validate_scenario, TIMING_LIMITS
from .clock_runtime import utc_seconds, run_capture, FrameCoordinator, clock_description
from .transport import normalize_compositor_delivery, SOURCE_CLOCK_SEMANTICS, MAX_HOST_DELIVERY_LAG_S
from .progress import ProgressPublisher

CONTROL_DIRECTORIES = frozenset({'.kiroclops-scratch', '.kiroclops-evidence', '.kiro'})
CHECKPOINT_POLICY = {'recent_frame_s':.030,'tail_pixel_stale_s':.040,
                     'imminent_input_s':.080,'post_input_s':.050,
                     'skip_input_in_flight':True,
                     # Routine PNGs stay outside the input preparation window.
                     # An explicit awaited stale-image sample is allowed at
                     # preparation; it retains the native deadline failure path.
                     'first_input_quiet_s':.160}
FRAME_CONTROL_POLICY = {'mode':'headless_begin_frame_real_time','nominal_interval_s':1/60,
                        'frameTimeTicks_supplied':False,'maximum_command_lateness_s':.120,
                        'input_preparation_lead_s':.040,'input_schedule':'absolute_t0_plus_planned',
                        'input_preparation_image':'actual_png_if_stale_before_click_drag_start_or_drag_end',
                        'routine_frame':'completed_native_frame_no_png',
                        'post_input_timer':'routine_frames_continue_while_fallback_waits',
                        'post_png_routine_cooldown_s':1/60,
                        'optional_png_eligibility':'rechecked_under_frame_mutex_before_draw',
                        'pre_first_input':'routine_frames_continue_png_quiet_input_guard',
                        'routine_pump_stop':'after_last_input_and_all_1_8s_response_windows',
                        'tail_capture':'actual_png_when_image_older_than_40ms_until_scenario_end_no_routine_advance',
                        'stale_pixel_fallback':'actual_png_when_received_image_older_than_30ms_away_from_input',
                        'response_boundary':'actual_due_then_completed_native_frame_before_down_or_up',
                        'click_boundary':'trusted_hover_down_completed_native_frame_up',
                        'drag_start_boundary':'priority_hit_lookup_single_completed_native_frame_trusted_down',
                        'boundary_screenshot':False,'video_pixels':'actual_screencast_or_explicit_png_checkpoint_only',
                        'pump_boundary_priority':True,'scheduler_clock':'real_performance_counter',
                        'host_utc_clock':'native_precise_UTC'}
DELIVERY_BACKPRESSURE = {'unobserved_frame_quiet_s':.040,
                         # Delivery congestion is independent of image cadence.
                         'late_delivery_s':.065}


def idle_checkpoint_due(*,since_frame_s,upcoming_input_s,input_in_flight,since_input_s):
    """Avoid screenshot commands competing with trusted input/compositor delivery."""
    if input_in_flight:return False
    if since_input_s is not None and since_input_s<CHECKPOINT_POLICY['post_input_s']:return False
    if upcoming_input_s is not None and upcoming_input_s<CHECKPOINT_POLICY['imminent_input_s']:return False
    return since_frame_s is None or since_frame_s>=CHECKPOINT_POLICY['recent_frame_s']


def pre_first_input_capture_action(*,since_frame_s,upcoming_input_s,input_in_flight):
    """Keep routine PNG encoding out of first-input preparation.

    The real BeginFrame pump continues so Chromium can service trusted input.
    A separate explicit stale-image checkpoint is awaited at the 40ms
    preparation lead. Its 120ms native bound can exhaust input headroom, so
    actual input lateness is still checked and fails closed. This routine-only
    guard never substitutes a frame or relaxes a timing gate.
    """
    if upcoming_input_s is None or upcoming_input_s<=CHECKPOINT_POLICY['first_input_quiet_s']:
        return 'hold'
    return ('actual_png' if idle_checkpoint_due(since_frame_s=since_frame_s,
            upcoming_input_s=upcoming_input_s,input_in_flight=input_in_flight,
            since_input_s=None) else 'hold')


def pump_deadline(previous_due,now,*,after_png=False):
    """Pace a native frame after an actual PNG; never issue it immediately."""
    anchor=now if after_png else previous_due
    return max(anchor+FRAME_CONTROL_POLICY['nominal_interval_s'],now)


def routine_pump_can_stop(*,next_input_index,input_count,response_end_epoch_s,now_epoch_s):
    """End periodic work only after all inputs and measured responses finish."""
    return (next_input_index==input_count and response_end_epoch_s is not None and
            now_epoch_s>=response_end_epoch_s)


class DeliveryBackpressure:
    """Bound routine draws while real screencast delivery catches up.

    A completed BeginFrame is not an image. If it produces no delivered image,
    allow another routine draw after a short quiet interval so static pages can
    still progress. Input boundary draws never consult this guard.
    """
    def __init__(self):
        self.pending_acks={};self.ack_count=0;self.last_native_completion=None
        self.acks_at_native_completion=0;self.recovery_until=0.
        self.maximum_pending_acks=0;self.maximum_delivery_lag_s=0.
        self.maximum_ack_age_s=0.;self.skips={}

    def received(self,index,source_epoch,received_epoch,*,now=None):
        now=time.perf_counter() if now is None else now
        self.pending_acks[index]=now
        self.maximum_pending_acks=max(self.maximum_pending_acks,len(self.pending_acks))
        if type(source_epoch) in (int,float) and math.isfinite(source_epoch):
            lag=max(0.,received_epoch-source_epoch)
            self.maximum_delivery_lag_s=max(self.maximum_delivery_lag_s,lag)
            if lag>DELIVERY_BACKPRESSURE['late_delivery_s']:
                self.recovery_until=max(self.recovery_until,
                    now+DELIVERY_BACKPRESSURE['unobserved_frame_quiet_s'])

    def ack_finished(self,index,*,now=None):
        now=time.perf_counter() if now is None else now
        started=self.pending_acks.pop(index,None)
        if started is not None:
            self.maximum_ack_age_s=max(self.maximum_ack_age_s,now-started)
        self.ack_count+=1

    def native_completed(self,*,now=None):
        self.last_native_completion=time.perf_counter() if now is None else now
        self.acks_at_native_completion=self.ack_count

    def _delivery_reason(self,final_input_boundary,now,*,png):
        if final_input_boundary:return 'final_input_boundary'
        # The real-image fallback may run while an earlier screencast ACK is
        # outstanding. Waiting for that ACK caused >120ms empty pixel windows
        # after a click even though BeginFrame PNG itself remained fast. Its
        # own native 120ms timeout and pixel-clock gates still fail closed.
        if self.pending_acks and not png:return 'screencast_ack_pending'
        # A late screencast observation means another image-free native frame
        # would add pressure without filling the visible gap. An explicit PNG
        # checkpoint is the fallback that can fill it, once its ACK has settled.
        if not png and now<self.recovery_until:return 'late_delivery_recovery'
        return None

    def skip_reason(self,*,final_input_boundary=False,png=False,now=None):
        now=time.perf_counter() if now is None else now
        reason=self._delivery_reason(final_input_boundary,now,png=png)
        if reason is None and not png and self.last_native_completion is not None:
            if (self.ack_count==self.acks_at_native_completion and
                    now-self.last_native_completion<DELIVERY_BACKPRESSURE['unobserved_frame_quiet_s']):
                reason='unobserved_native_frame'
        if reason:self.skips[reason]=self.skips.get(reason,0)+1
        return reason

    def snapshot(self):
        return {'policy':dict(DELIVERY_BACKPRESSURE),'pending_ack_count':len(self.pending_acks),
                'maximum_pending_ack_count':self.maximum_pending_acks,
                'maximum_delivery_lag_s':self.maximum_delivery_lag_s,
                'maximum_ack_age_s':max(self.maximum_ack_age_s,
                    max((time.perf_counter()-started for started in self.pending_acks.values()),default=0.)),
                'routine_or_png_skips':dict(self.skips)}


class CaptureError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


CANCEL_MONITOR_POLICY={'interval_s':.020,'capture_thread_probe':'in_memory_event_only',
    'caller_probe_thread':'capture-cancel-monitor'}


class CancelMonitor:
    """Run a caller's cancel probe off the real-time capture thread.

    The session probe stats job_dir/cancel.json, the directory where the
    durable progress writer creates and renames job.json. On the 3A HDD a
    concurrent create/rename held that stat for 80-180 ms and the capture loop
    stopped receiving frames. The capture thread now reads only this Event;
    the caller's probe still decides cancellation and its errors still fail
    the capture.
    """
    def __init__(self,probe,*,interval_s=CANCEL_MONITOR_POLICY['interval_s']):
        self._probe=probe;self._interval=interval_s
        self._event=threading.Event();self._stop=threading.Event()
        self._error=None;self._probes=0;self._maximum_probe_s=0.;self._observed_epoch_s=None
        # The first answer is known before any browser or loop starts.
        self._poll()
        self._thread=threading.Thread(target=self._run,daemon=True,name=CANCEL_MONITOR_POLICY['caller_probe_thread'])
        self._thread.start()

    def _poll(self):
        started=time.perf_counter();requested=False
        try:
            requested=bool(self._probe.is_set() if hasattr(self._probe,'is_set') else self._probe())
        except BaseException as error:
            self._error=error;requested=True
        finally:
            self._probes+=1;self._maximum_probe_s=max(self._maximum_probe_s,time.perf_counter()-started)
        if requested and not self._event.is_set():
            self._observed_epoch_s=utc_seconds();self._event.set()

    def _run(self):
        while not self._event.is_set() and not self._stop.wait(self._interval):
            self._poll()

    def is_set(self):
        if self._error is not None:raise self._error
        return self._event.is_set()

    def close(self,timeout=1.):
        self._stop.set();self._thread.join(timeout)

    def snapshot(self):
        return {'kind':'CaptureCancelMonitor','policy':dict(CANCEL_MONITOR_POLICY),
            'probe_count':self._probes,'maximum_probe_s':self._maximum_probe_s,
            'cancel_observed':self._event.is_set(),'cancel_observed_epoch_s':self._observed_epoch_s,
            'worker_alive':self._thread.is_alive(),
            'error':None if self._error is None else {'type':type(self._error).__name__,
                'code':getattr(self._error,'code',None),'message':str(self._error)}}


def require_input_deadline(planned_s,elapsed_s):
    if elapsed_s-planned_s>FRAME_CONTROL_POLICY['maximum_command_lateness_s']:
        raise CaptureError('capture_timing_failure',
            f'Input deadline missed by {elapsed_s-planned_s:.6f}s; late commands will not be compressed into a burst')


async def wait_until_epoch(due,check=lambda:None):
    """An asyncio timer can wake early on Windows; recheck the actual clock."""
    while True:
        check();remaining=due-utc_seconds()
        if remaining<=0:return
        await asyncio.sleep(min(.010,remaining))


async def post_input_checkpoint(completed_epoch,checkpoint,needed,check=lambda:None):
    """Wait independently; only the actual PNG owns the shared frame lock."""
    await wait_until_epoch(completed_epoch+CHECKPOINT_POLICY['post_input_s'],check)
    check()
    return await checkpoint(eligible=needed) if needed() else None


async def pre_input_checkpoint(action,checkpoint,latest_image_end,*,input_in_flight=lambda:False,
                               check=lambda:None):
    """Sample stale real pixels at preparation, before trusted input dispatch.

    This explicit awaited checkpoint bridges the routine PNG quiet window.
    It retains the shared draw lock, actual bracket and ordinary PNG cooldown;
    fresh compositor delivery while queued cancels an unnecessary screenshot.
    """
    check()
    if action not in {'click','drag_start','drag_end'}:return None
    def needed():
        check()
        if input_in_flight():return False
        latest=latest_image_end()
        return latest is None or utc_seconds()-latest>=CHECKPOINT_POLICY['recent_frame_s']
    return await checkpoint(eligible=needed) if needed() else None


class TrustedPointer:
    """Direct Chromium pointer input, without Playwright's HTML DnD interceptor.

    These fixtures implement pointer capture, not HTML drag-and-drop. Awaited
    CDP input remains serialized and the browser supplies isTrusted/timestamps.
    No input timestamp or synthetic DOM event is supplied.
    """
    def __init__(self,cdp,trace=None):
        self.cdp=cdp;self.position=(0.,0.);self.pressed=False;self.trace=trace

    async def _send(self,kind,point,pressed,click_count=0):
        params={'type':kind,'x':point[0],'y':point[1],
            'button':'left' if pressed or kind=='mouseReleased' else 'none',
            'buttons':1 if pressed else 0,'pointerType':'mouse'}
        if kind in ('mousePressed','mouseReleased'):params['clickCount']=click_count or 1
        before=utc_seconds();started=time.perf_counter();error=None
        try:await asyncio.wait_for(self.cdp.send('Input.dispatchMouseEvent',params),.120)
        except asyncio.TimeoutError as exc:
            error={'type':type(exc).__name__,'message':'Trusted pointer dispatch exceeded120ms'}
            raise CaptureError('capture_timing_failure','Trusted pointer dispatch exceeded120ms') from exc
        except BaseException as exc:
            error={'type':type(exc).__name__,'message':str(exc)};raise
        finally:
            if self.trace:self.trace({'operation':'native_'+kind,'host_before_s':before,
                'host_after_s':utc_seconds(),'duration_s':time.perf_counter()-started,'error':error,
                'parameters':params})
        self.position=point;self.pressed=pressed

    async def move(self,x,y):
        await self._send('mouseMoved',(x,y),self.pressed)

    async def down(self,point=None,*,before_press=None):
        # Hit-target lookup can take long enough to leave an unobserved tail
        # before pointerdown. Its final boundary image must follow that lookup.
        # Dispatch still uses native input; a moved/missed target is rejected
        # by the independent received-input checks, never synthetically hit.
        if before_press is not None:await before_press()
        await self._send('mousePressed',self.position if point is None else point,True,1)

    async def up(self):
        await self._send('mouseReleased',self.position,False,1)

    async def finish_prepared_click(self,point,*,before_release=None):
        if self.pressed or tuple(point)!=tuple(self.position):
            raise CaptureError('invalid_input','Prepared click position changed or pointer is pressed')
        await self.down()
        if before_release is not None:await before_release()
        await self.up()

    async def click(self,x,y,*,before_release=None,release_not_before=None,press_not_before=None,check=lambda:None):
        await self.move(x,y)
        if press_not_before is not None:await wait_until_epoch(press_not_before,check)
        await self.down()
        # A trusted hover may await a compositor acknowledgement. Capture the
        # actual state after that wait and before the release that emits click.
        # The browser still owns every pointer event and its actual timestamp.
        if before_release is not None:await before_release()
        # Optional prospective policy: prepare the real press first, then
        # schedule release (the click trigger). Never backdate browser events.
        if release_not_before is not None:
            await wait_until_epoch(release_not_before,check)
        await self.up()


async def visible_hit_box(page,selector):
    """Read current input hit geometry without Playwright locator wait cycles."""
    return await page.evaluate('''selector=>{const e=document.querySelector(selector);if(!e)return null;
        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'
            ? {x:r.x,y:r.y,width:r.width,height:r.height}:null;}''',selector)


async def begin_frame_image(cdp):
    """Draw at the browser's actual current clock; never set virtual time.

    Page.captureScreenshot can wait indefinitely for a quiet compositor. A
    controlled real-time BeginFrame explicitly asks it to draw, including idle
    content. Its real host bracket stays visible; browser consumer observations
    retain their original metadata timestamp without a presentation-time claim.
    """
    before=utc_seconds()
    try:
        value=await asyncio.wait_for(cdp.send('HeadlessExperimental.beginFrame',
            {'screenshot':{'format':'png','optimizeForSpeed':True}}),.120)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_timing_failure','BeginFrame exceeded the120ms capture deadline') from exc
    after=utc_seconds()
    if not isinstance(value.get('screenshotData'),str) or not value['screenshotData']:
        raise CaptureError('capture_timing_failure','BeginFrame supplied no actual image')
    return {'epoch_s':(before+after)/2,'receive_epoch_s':after,'data':value['screenshotData'],
            'metadata':{'hasDamage':value.get('hasDamage')},'timestamp_source':'cdp_begin_frame_bracket',
            'interval_epoch_s':[before,after]}


async def advance_frame(cdp):
    """Complete a real frame without waiting for PNG encoding or creating pixels."""
    before=utc_seconds()
    try:
        result=await asyncio.wait_for(cdp.send('HeadlessExperimental.beginFrame',{}),.120)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_timing_failure','Native frame advance exceeded120ms') from exc
    after=utc_seconds()
    if not isinstance(result,dict) or type(result.get('hasDamage')) is not bool:
        raise CaptureError('capture_timing_failure','Native frame advance supplied no completed-frame receipt')
    return {'kind':'NativeCompletedFrameReceipt','host_before_s':before,'host_after_s':after,
        'hasDamage':result['hasDamage'],'screenshot_requested':False,'image_produced':False,
        'frameTimeTicks_supplied':False,'timestamp_source':'actual_host_command_bracket',
        'note':'Real frame completion only. Actual image observations must arrive independently and pass unchanged gates.'}


def _write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")


def preserve_capture_receipts(output,frames,commands,timings,*,private=None,error=None,
                              boundary_advances=None,routine_advances=None,fallback_checkpoints=None,
                              routine_pump_stop=None,delivery_backpressure=None):
    """Keep actual received pixels even when a recording never becomes valid.

    Arrival order, original timestamps and excluded candidates are evidence;
    this private receipt is deliberately not a VideoPackage or a success flag.
    """
    directory=Path(output)/'private/received-frames';directory.mkdir(parents=True,exist_ok=True)
    entries=[]
    for index,frame in enumerate(frames):
        row={k:v for k,v in frame.items() if k!='data'}
        try:
            raw=base64.b64decode(frame['data'],validate=True)
            path=directory/f'{index:06d}.png';path.write_bytes(raw)
            row.update(path=path.relative_to(Path(output)).as_posix(),png_sha256=hashlib.sha256(raw).hexdigest())
        except Exception as exc:
            row['pixel_save_error']={'type':type(exc).__name__,'message':str(exc)}
        entries.append(row)
    _write(Path(output)/'private/received-evidence.json',{
        'kind':'RawCaptureDiagnosticEvidence','status':'interrupted' if error else 'unvalidated',
        'recording_complete':error is None,'valid_analysis_package':False,
        'frames':entries,'commands':commands,'command_timings':timings,'private_oracle':private,
        'completed_native_boundaries':boundary_advances or [],
        'completed_native_routine_frames':routine_advances or [],
        'actual_png_fallback_checkpoints':fallback_checkpoints or [],
        'routine_pump_stop':routine_pump_stop,
        'delivery_backpressure':delivery_backpressure,
        'error':error,'note':'Raw arrival evidence only. No missing frames, timestamps or input receipts are invented.'})


def project_hashes(project):
    root=Path(project).resolve();result={}
    for path in sorted(root.rglob('*')):
        if any(part.lower() in CONTROL_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or getattr(path.stat(),'st_file_attributes',0)&0x400:
            raise CaptureError('source_integrity_failure','Project links are forbidden')
        if path.is_file():result[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if 'index.html' not in result:
        raise CaptureError('invalid_project','Local browser project requires index.html')
    return result


def adapter_hashes():
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}


def merge_frame_receipts(frames):
    """Merge two actual timestamp sources; never repair source clock order."""
    for source in ('cdp_compositor','cdp_screenshot_bracket','cdp_begin_frame_bracket'):
        group=[f for f in frames if f.get('timestamp_source')==source]
        stamps=[f.get('epoch_s') for f in group]
        if any(type(t) not in (int,float) or not math.isfinite(t) for t in stamps):
            raise CaptureError('capture_timing_failure','Missing frame timestamp')
        if any(b<=a for a,b in zip(stamps,stamps[1:])):
            raise CaptureError('capture_timing_failure','Nonmonotonic source timestamps cannot be sorted into validity')
    if any(f.get('timestamp_source') not in {'cdp_compositor','cdp_screenshot_bracket','cdp_begin_frame_bracket'} for f in frames):
        raise CaptureError('capture_timing_failure','Unknown frame time source')
    result=sorted(frames,key=lambda item:item['epoch_s'])
    previous_end=None
    for frame in result:
        interval=frame.get('interval_epoch_s')
        if not isinstance(interval,list) or len(interval)!=2 or not interval[0]<=frame['epoch_s']<=interval[1]:
            raise CaptureError('capture_timing_failure','Invalid frame time bracket')
        if previous_end is not None and interval[0]<=previous_end:
            raise CaptureError('capture_timing_failure','Frame time intervals overlap; capture order is not identifiable')
        previous_end=interval[1]
    return result


def exclude_redundant_checkpoints(frames):
    """Prefer a real compositor sample over an overlapping screenshot bracket.

    This is declared source selection, not invented ordering: all excluded
    PNG hashes and actual brackets remain in the private capture receipt.
    """
    compositor = [f['epoch_s'] for f in frames if f.get('timestamp_source') == 'cdp_compositor']
    retained=[];excluded=[]
    for frame in frames:
        if frame.get('timestamp_source') in ('cdp_screenshot_bracket','cdp_begin_frame_bracket'):
            low,high=frame['interval_epoch_s']
            overlaps=[time for time in compositor if low<=time<=high]
            if overlaps:
                excluded.append({k:v for k,v in frame.items() if k!='data'} | {
                    'reason':'Redundant screenshot bracket overlaps primary browser consumer observation',
                    'overlapping_compositor_times':overlaps,
                    'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest()})
                continue
        retained.append(frame)
    return retained,excluded


def exclude_input_ambiguous_frames(frames,input_epochs):
    """A bounded image spanning a trusted input has no known side of that input."""
    retained=[];excluded=[]
    for frame in frames:
        low,high=frame['interval_epoch_s']
        crossings=[t for t in input_epochs if low<=t<=high]
        if low<high and crossings:
            excluded.append({k:v for k,v in frame.items() if k!='data'} | {
                'reason':'Image clock bracket crosses a trusted input; pre/post-command identity is uncertain',
                'input_epochs':crossings,'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest()})
        else:retained.append(frame)
    return retained,excluded


class _Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        # A browser target may not read worker evidence or agent configuration.
        # translate_path performs URL decoding before this Windows-aware check.
        resolved = Path(self.translate_path(self.path)).resolve()
        root = Path(self.directory).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            self.send_error(403, 'Outside project');return None
        if any(part.lower() in CONTROL_DIRECTORIES for part in relative.parts):
            self.send_error(403, 'Private control directory');return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(403, 'Directory listing disabled');return None

    def log_message(self,*args):
        pass
    def end_headers(self):
        self.send_header('Cache-Control','no-store')
        super().end_headers()


@contextmanager
def local_server(project):
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(_Handler,directory=str(Path(project).resolve())))
    server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{server.server_port}/'
    finally:server.shutdown();server.server_close();worker.join(timeout=2)


def timing_summary(timestamps,clock_samples,events,limits=None,frame_metadata=None):
    limits=dict(limits or TIMING_LIMITS);errors=[]
    if len(timestamps)<10 or any(not isinstance(t,(int,float)) or not math.isfinite(t) or t<0 for t in timestamps):
        errors.append('missing_or_invalid_frame_timestamp')
    intervals=[b-a for a,b in zip(timestamps,timestamps[1:])]
    if any(dt<=0 for dt in intervals):errors.append('non_monotonic_frame_timestamp')
    ordered=sorted(intervals)
    median=statistics.median(ordered) if ordered else None
    p95=ordered[min(len(ordered)-1,math.ceil(.95*len(ordered))-1)] if ordered else None
    # Screencast can omit unchanged compositor frames. Demand real image
    # coverage across every complete active response window, including its
    # leading and trailing edges; absent frames must never erase a gap.
    windows=[(e['time_s'],e['time_s']+1.8) for e in events if e.get('action') in {'click','drag_end'}]
    motion_gaps=[]
    for lo,hi in windows:
        covered=[lo]+[t for t in timestamps if lo<=t<=hi]+[hi]
        motion_gaps.extend(b-a for a,b in zip(covered,covered[1:]))
    max_gap=max(motion_gaps,default=0.)
    # The visual comparator can use responses longer than the recorder's
    # historical 1.8s activity window. Check the complete interpolation
    # brackets it could consume, including a bracket straddling input time.
    # An image after the next command cannot support the preceding response.
    comparison_windows=[]
    for index,event in enumerate(events):
        if event.get('action') not in {'click','drag_end'}:continue
        start=event['time_s']
        stop=events[index+1]['time_s'] if index+1<len(events) else None
        allowed=[t for t in timestamps if stop is None or t<stop]
        gaps=[{'from_s':a,'to_s':b,'gap_s':b-a} for a,b in zip(allowed,allowed[1:]) if b>start]
        largest=max(gaps,key=lambda r:r['gap_s'],default=None)
        comparison_windows.append({'input_index':index,'start_s':start,
            'end_s':allowed[-1] if allowed else None,'next_input_s':stop,
            'largest_bracket':largest})
    comparison_max=max((w['largest_bracket']['gap_s'] for w in comparison_windows if w['largest_bracket']),default=0.)
    uncertainties=[s['uncertainty_s'] for s in clock_samples]
    uncertainties.extend(item['uncertainty_s'] for item in (frame_metadata or []))
    clock=max(uncertainties,default=float('inf'))
    if median is None or median>limits['median_interval_s']:errors.append('median_frame_interval_exceeded')
    if p95 is None or p95>limits['p95_interval_s']:errors.append('p95_frame_interval_exceeded')
    if max_gap>limits['maximum_motion_gap_s']:errors.append('motion_frame_gap_exceeded')
    if comparison_max>limits['maximum_motion_gap_s']:errors.append('comparison_frame_gap_exceeded')
    if clock>limits['clock_uncertainty_s']:errors.append('clock_mapping_uncertainty_exceeded')
    if any(not s.get('epoch_in_host_bracket',False) for s in clock_samples):errors.append('clock_epoch_mismatch')
    return {'status':'fail' if errors else 'pass','errors':errors,'frame_count':len(timestamps),
            'median_interval_s':median,'p95_interval_s':p95,'maximum_motion_gap_s':max_gap,
            'clock_uncertainty_s':clock if math.isfinite(clock) else None,'limits':limits,
            'comparison_maximum_gap_s':comparison_max,'comparison_response_windows':comparison_windows,
            'comparison_coverage_policy':'full_brackets_until_next_command_or_last_image_v1',
            'frame_clock':'Browser screencast metadata times plus independently bracketed real PNG checkpoints; image-free frame advances are not video observations',
            'source_clock_semantics':dict(SOURCE_CLOCK_SEMANTICS),
            'input_clock':'performance.timeOrigin + performance.now(); epoch seconds',
            'nominal_fps_assumed':False}


async def _clock(page):
    samples=[]
    for _ in range(7):
        before=utc_seconds()
        value=await page.evaluate('({epoch:(performance.timeOrigin+performance.now())/1000,wall:Date.now()/1000})')
        after=utc_seconds()
        samples.append({'host_before_s':before,'host_after_s':after,'browser_epoch_s':value['epoch'],
                        'browser_wall_s':value['wall'],'uncertainty_s':(after-before)/2,
                        'epoch_in_host_bracket':before-.002<=value['epoch']<=after+.002})
    return min(samples,key=lambda s:s['uncertainty_s'])


async def drain_delivery_tasks(tasks,*,ignore_errors=False,max_wait_s=MAX_HOST_DELIVERY_LAG_S):
    """Bound ACK cleanup; an unsettled receipt cannot become a valid capture."""
    deadline=time.perf_counter()+max_wait_s
    while tasks:
        remaining=deadline-time.perf_counter()
        if remaining<=0:
            for task in list(tasks):task.cancel()
            raise CaptureError('capture_timing_failure','Screencast ACK drain exceeded the existing one-second delivery bound')
        done,pending=await asyncio.wait(list(tasks),timeout=remaining)
        if pending:
            for task in pending:task.cancel()
            raise CaptureError('capture_timing_failure','Screencast ACK drain exceeded the existing one-second delivery bound')
        for task in done:
            if task.cancelled():
                if not ignore_errors:raise CaptureError('capture_timing_failure','Screencast ACK task was cancelled')
            elif ignore_errors:task.exception()
            else:task.result()


async def close_browser_delivery(browser, tasks):
    """Close the owned browser channel before ending its actual receipt window."""
    # Browser shutdown is process cleanup, not a frame-delivery deadline. It
    # can take longer than one second after a valid capture, especially when
    # Chromium has just encoded a screencast frame. Keep the one-second bound
    # on receipt-task draining and on each delivered image's host lag below.
    try:await asyncio.wait_for(browser.close(),10.)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_timing_failure','Browser shutdown exceeded ten seconds') from exc
    # Encoding completions already delivered during close can have scheduled
    # receipt tasks. Their images precede the first await; drain their ACKs,
    # including expected closed-channel ACK failures, after transport closure.
    await drain_delivery_tasks(tasks,ignore_errors=True)
    return utc_seconds()


async def _record(project,output,scenario,cancel=None,progress=None,progress_check=None):
    from playwright.async_api import async_playwright
    from .natural_capture import browser_arguments,screenshot_image,warmup_screenshot_image,checkpoint_needed,wait_native_animation_frame,await_stream_image,endpoint_checkpoint_needed,input_pixel_policy,input_boundary_stage,schedule_input_observation
    natural=scenario.get('frame_driver') in {'natural_browser_v1','natural_browser_raf_v1'}
    native_boundary=scenario.get('frame_driver')=='natural_browser_raf_v1'
    sample_image=screenshot_image if natural else begin_frame_image
    output=Path(output);domain=canonical_domain(scenario['domain'])
    NATURAL_POLICY=input_pixel_policy(domain)
    frames=[];tasks=set();delivery_task_errors=[];post_input_tasks=set();post_input_errors=[]
    console_errors=[];command_receipts=[];excluded_frames=[];command_timings=[]
    boundary_advances=[];routine_advances=[];fallback_checkpoints=[];routine_pump_stop={}
    # Private receipts produced while the browser is live are written only
    # after the real-time segment: a slow disk must not hold the capture loop.
    deferred_receipts={}
    idle_task=None
    async def timed(name,operation,command_id):
        before=utc_seconds();started=time.perf_counter();error=None
        try:return await asyncio.wait_for(operation,.120)
        except asyncio.TimeoutError as exc:
            error={'type':type(exc).__name__,'message':f'{name} exceeded120ms'}
            raise CaptureError('capture_timing_failure',f'Input preparation/dispatch {name} exceeded120ms') from exc
        except BaseException as exc:
            error={'type':type(exc).__name__,'message':str(exc)};raise
        finally:
            command_timings.append({'command_id':command_id,'operation':name,'host_before_s':before,
                'host_after_s':utc_seconds(),'duration_s':time.perf_counter()-started,'error':error})
    def check():
        if cancel is not None and (cancel.is_set() if hasattr(cancel,'is_set') else cancel()):
            raise CaptureError('cancelled','Local capture cancelled')
        if progress_check is not None:progress_check()
        if delivery_task_errors:
            raise CaptureError('capture_timing_failure','Screencast frame delivery/ACK failed') from delivery_task_errors[0]
        if post_input_errors:
            raise post_input_errors[0]
        if idle_task is not None and idle_task.done() and not idle_task.cancelled():
            failure=idle_task.exception()
            if failure is not None:raise failure
    with local_server(project) as url:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,args=browser_arguments(natural))
            priority_receipt=None
            try:
                context=await browser.new_context(viewport={'width':scenario['viewport'][0],'height':scenario['viewport'][1]},device_scale_factor=1,locale='en-US',timezone_id='UTC',reduced_motion='no-preference')
                await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(url) else route.abort())
                initial_script=INIT_SCRIPT
                if scenario.get('visual_frame_clock')=='svg_raf_tag_v1':
                    from .visual_clock import CLOCK_SCRIPT
                    # Concatenate to preserve creation order of the private
                    # receipt holder and marker; Playwright does not promise
                    # ordering between separately added initialization scripts.
                    initial_script+='\n'+CLOCK_SCRIPT
                await context.add_init_script(initial_script)
                page=await context.new_page();page.on('pageerror',lambda error:console_errors.append(str(error)))
                target_url=url+(f"?nodes={scenario['initial_node_count']}" if domain=='svg' else '')
                await page.goto(target_url,wait_until='networkidle');await page.evaluate('document.fonts.ready')
                await page.wait_for_timeout(100)
                clock_start=await _clock(page)
                cdp=await context.new_cdp_session(page)
                # V6 plan WP2.2: the owned capture and its Chromium processes
                # (browser, GPU, renderer) run above normal priority while the
                # recording is live; the receipt lists every process changed.
                from .host_quiet import raise_capture_priority
                priority_receipt=raise_capture_priority()
                deferred_receipts['priority.json']=priority_receipt
                active_command=None;next_event_index=0;input_in_flight=False;last_input_epoch=None
                input_preparing=False;observation_lock=asyncio.Lock()
                final_input_boundary=False;delivery_pressure=DeliveryBackpressure()
                last_response_epoch=None
                pointer=TrustedPointer(cdp,trace=lambda row:command_timings.append(
                    dict(row,command_id=active_command)))
                await cdp.send('Page.enable')
                # Fixed warmup precedes the experiment. The prospective path
                # records the primary stream, rather than assigning a warmup
                # PNG to the experimental timeline. No active frame is retried,
                # backdated or dropped for being slow.
                seed=None
                if scenario.get('analysis_policy')=='observed_response_v2':
                    preflight=[]
                    for _ in range(3):
                        await wait_native_animation_frame(page)
                        seed=await warmup_screenshot_image(cdp)
                        preflight.append({k:v for k,v in seed.items() if k!='data'} |
                                         {'png_sha256':hashlib.sha256(base64.b64decode(seed['data'])).hexdigest()})
                    deferred_receipts['preflight.json']={'fixed_samples':3,'samples':preflight,
                        'retries':0,'last_sample_is_recording_baseline':False,
                        'recording_baseline':'first_fresh_primary_stream_image',
                        'experimental_clock_starts_after_fixed_warmup':True}
                elif natural:
                    warmup=await warmup_screenshot_image(cdp)
                    deferred_receipts['preflight.json']={'fixed_samples':1,'retries':0,
                        'samples':[{k:v for k,v in warmup.items() if k!='data'} |
                                   {'png_sha256':hashlib.sha256(base64.b64decode(warmup['data'])).hexdigest()}],
                        'last_sample_is_recording_baseline':False,
                        'experimental_clock_starts_after_fixed_warmup':True}
                else:await sample_image(cdp)
                # Bind the first real command before the capture clock starts.
                # A separate page.evaluate near its due time can contend with
                # compositor work and miss the first trusted input entirely.
                start_epoch=await page.evaluate('''index=>{const state=window.__kcPrivate;
                    state.enabled=true;state.activeCommand=index;
                    return (performance.timeOrigin+performance.now())/1000;}''',
                    0 if scenario['events'] else None)
                async def draw_frame():
                    if frames:
                        previous_end=max(f['interval_epoch_s'][1] for f in frames)
                        # Wait in real time if a browser timestamp is still
                        # ahead; never shift either source's timestamp.
                        while utc_seconds()<=previous_end:
                            check();await asyncio.sleep(.001)
                    async with observation_lock:
                        frame=await sample_image(cdp)
                    if not natural:delivery_pressure.native_completed()
                    frames.append(frame)
                    return frame
                async def advance_boundary():
                    if natural:raise CaptureError('invalid_input','Natural observer cannot drive animation frames')
                    receipt=await advance_frame(cdp)
                    delivery_pressure.native_completed()
                    boundary_advances.append(dict(receipt,command_id=active_command))
                    return receipt
                async def advance_routine():
                    if natural:raise CaptureError('invalid_input','Natural observer cannot drive animation frames')
                    receipt=await advance_frame(cdp)
                    delivery_pressure.native_completed()
                    routine_advances.append(dict(receipt,next_input_index=next_event_index))
                    return receipt
                coordinator=FrameCoordinator(draw_frame,trace=lambda row:command_timings.append(
                    dict(row,command_id=active_command if row['operation']=='priority_frame' else None,
                         next_input_index=next_event_index if row['operation']=='pump_frame' else None)),
                    boundary_draw=advance_boundary,
                    routine_draw=advance_routine,
                    routine_png_cooldown_s=FRAME_CONTROL_POLICY['post_png_routine_cooldown_s'])
                checkpoint=coordinator.checkpoint
                if seed is None:await checkpoint()
                stream_start=utc_seconds()
                stream_ready=asyncio.get_running_loop().create_future()
                async def frame_received(event,receive_index,received_at):
                    try:
                        timestamp=event.get('metadata',{}).get('timestamp')
                        item={'epoch_s':timestamp,'receive_epoch_s':received_at,'data':event['data'],
                                       'receive_index':receive_index,'cdp_screencast_session_id':event.get('sessionId'),
                                       'receive_index_semantics':'local synchronous CDP event arrival ordinal; not a browser frame counter',
                                       'metadata':event.get('metadata',{}),'timestamp_source':'cdp_compositor',
                                       'interval_epoch_s':[timestamp,timestamp]}
                        if isinstance(timestamp,(int,float)) and timestamp<stream_start:
                            excluded_frames.append({'epoch_s':timestamp,'receive_index':receive_index,
                                'receive_epoch_s':received_at,'stream_start_epoch_s':stream_start,
                                'reason':'Cached compositor image predates this capture segment',
                                'png_sha256':hashlib.sha256(base64.b64decode(event['data'])).hexdigest()})
                        else:
                            frames.append(item)
                            if (isinstance(timestamp,(int,float)) and timestamp>=stream_start
                                    and not stream_ready.done()):stream_ready.set_result(item)
                        await cdp.send('Page.screencastFrameAck',{'sessionId':event['sessionId']})
                    finally:
                        delivery_pressure.ack_finished(receive_index)
                receive_count=0
                def received(event):
                    nonlocal receive_count
                    received_at=utc_seconds();receive_index=receive_count;receive_count+=1
                    delivery_pressure.received(receive_index,event.get('metadata',{}).get('timestamp'),received_at)
                    task=asyncio.create_task(frame_received(event,receive_index,received_at));tasks.add(task)
                    def completed(task):
                        tasks.discard(task)
                        if not task.cancelled():
                            failure=task.exception()
                            if failure is not None:delivery_task_errors.append(failure)
                    task.add_done_callback(completed)
                cdp.on('Page.screencastFrame',received)
                await cdp.send('Page.startScreencast',{'format':'png','everyNthFrame':1,
                    'maxWidth':scenario['viewport'][0],'maxHeight':scenario['viewport'][1]})
                if seed is not None:
                    first_image=await await_stream_image(stream_ready)
                    deferred_receipts['stream-baseline.json']={
                        'experiment_start_epoch_s':start_epoch,'stream_start_epoch_s':stream_start,
                        'first_received_image':{k:v for k,v in first_image.items() if k!='data'},
                        'png_sha256':hashlib.sha256(base64.b64decode(first_image['data'])).hexdigest(),
                        'input_schedule_origin_changed':False,'retries':0}
                start_monotonic=time.perf_counter();drag=None
                recording_active=True
                async def post_input_pixel(completed_epoch,command_id):
                    """Take one real pixel sample after a quiet click/release if delivery is absent.

                    This independent timer prevents an ACK backlog from deferring the
                    regular pump's stale-image fallback past the response gap limit.
                    It never substitutes an image or changes its real timestamp.
                    """
                    def needed():
                        if not recording_active:return False
                        upcoming=(start_epoch+scenario['events'][next_event_index]['time_s']-utc_seconds()
                                  if next_event_index<len(scenario['events']) else None)
                        if input_in_flight or (natural and input_preparing) or (upcoming is not None and
                                upcoming<CHECKPOINT_POLICY['imminent_input_s']):return False
                        latest=max((f['interval_epoch_s'][1] for f in frames),default=None)
                        return latest is None or latest<completed_epoch+.015
                    frame=await post_input_checkpoint(completed_epoch,checkpoint,needed,check)
                    if frame is not None:
                        fallback_checkpoints.append({'timestamp_source':frame['timestamp_source'],
                            'interval_epoch_s':frame['interval_epoch_s'],
                            'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest(),
                            'next_input_index':next_event_index,'post_input_capture':True,
                            'command_id':command_id,'screenshot_requested':True,
                            'actual_image_produced':True})
                async def frame_pump():
                    scheduled_due=time.perf_counter();after_png=False
                    while recording_active:
                        scheduled_due=pump_deadline(scheduled_due,time.perf_counter(),after_png=after_png)
                        after_png=False
                        await asyncio.sleep(max(0,scheduled_due-time.perf_counter()))
                        check()
                        # Once every trusted input and its full response window
                        # finish, stop image-free 60Hz advances. Keep sampling
                        # real pixels through the clip's actual end; otherwise
                        # the final PNG alone leaves an unsupported time gap.
                        tail_mode=routine_pump_can_stop(next_input_index=next_event_index,
                                input_count=len(scenario['events']),response_end_epoch_s=last_response_epoch,
                                now_epoch_s=utc_seconds())
                        if tail_mode and not routine_pump_stop:
                            routine_pump_stop.update(reason='all_response_windows_complete',
                                host_epoch_s=utc_seconds(),last_response_end_epoch_s=last_response_epoch,
                                scenario_end_epoch_s=start_epoch+scenario['duration_s'],
                                next_input_index=next_event_index)
                        # Continue native frame advances through input 0.
                        # Headless Chromium may need a completed real frame to
                        # service trusted pointer dispatch. Only PNG encoding
                        # is suppressed near the first input.
                        if not recording_active:continue
                        if not tail_mode and delivery_pressure.skip_reason(
                                final_input_boundary=final_input_boundary) is None:
                            await checkpoint(routine=True)
                        # A completed frame has no pixels. Static pre-input
                        # scenes and omitted screencast updates still need
                        # genuine image samples. Request a bounded PNG only
                        # when the latest real image is stale and no input
                        # boundary is near or in flight; the same coordinator
                        # lock gives actual input boundaries priority.
                        png_context={}
                        def png_needed():
                            # A queued PNG must re-read freshness and live input
                            # state after the previous draw releases the mutex.
                            if not recording_active:return False
                            now=utc_seconds()
                            current_tail=routine_pump_can_stop(next_input_index=next_event_index,
                                input_count=len(scenario['events']),response_end_epoch_s=last_response_epoch,
                                now_epoch_s=now)
                            before_first_input=bool(scenario['events']) and next_event_index==0
                            latest=max((f['interval_epoch_s'][1] for f in frames),default=None)
                            since_frame=None if latest is None else max(0.,now-latest)
                            upcoming=(start_epoch+scenario['events'][next_event_index]['time_s']-now
                                      if next_event_index<len(scenario['events']) else None)
                            since_input=None if last_input_epoch is None else max(0.,now-last_input_epoch)
                            needs_png=(since_frame is None or since_frame>=CHECKPOINT_POLICY['tail_pixel_stale_s']
                                 if current_tail else
                                 pre_first_input_capture_action(since_frame_s=since_frame,
                                    upcoming_input_s=upcoming,input_in_flight=input_in_flight)=='actual_png'
                                 if before_first_input else
                                 idle_checkpoint_due(since_frame_s=since_frame,upcoming_input_s=upcoming,
                                                     input_in_flight=input_in_flight,since_input_s=since_input))
                            png_context.update(since_frame=since_frame,tail_mode=current_tail,
                                               next_input_index=next_event_index)
                            return needs_png and delivery_pressure.skip_reason(
                                final_input_boundary=final_input_boundary,png=True) is None
                        if png_needed():
                            frame=await checkpoint(eligible=png_needed)
                            if frame is not None:
                                # The prior source of a native command stall was
                                # a routine BeginFrame launched <1ms after PNG.
                                # Restart the cadence from actual PNG completion.
                                after_png=True
                                fallback_checkpoints.append({'timestamp_source':frame['timestamp_source'],
                                    'interval_epoch_s':frame['interval_epoch_s'],
                                    'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest(),
                                    'next_input_index':png_context['next_input_index'],
                                    'stale_image_age_s':png_context['since_frame'],
                                    'tail_capture':png_context['tail_mode'],
                                    'screenshot_requested':True,'actual_image_produced':True})
                async def natural_observer():
                    while recording_active:
                        await asyncio.sleep(.010);check()
                        def needed():
                            now=utc_seconds()
                            upcoming=(start_epoch+scenario['events'][next_event_index]['time_s']-now
                                if next_event_index<len(scenario['events']) else None)
                            latest=max((f['interval_epoch_s'][1] for f in frames),default=None)
                            return recording_active and not input_preparing and checkpoint_needed(now,latest,upcoming,input_in_flight)
                        frame=await checkpoint(eligible=needed) if needed() else None
                        if frame is not None:
                            fallback_checkpoints.append({'timestamp_source':frame['timestamp_source'],
                                'interval_epoch_s':frame['interval_epoch_s'],
                                'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest(),
                                'next_input_index':next_event_index,'screenshot_requested':True,
                                'actual_image_produced':True,'natural_observer':True})
                idle_task=asyncio.create_task(natural_observer() if natural else frame_pump())
                for index,event in enumerate(scenario['events']):
                    check()
                    active_command=index
                    due=start_epoch+event['time_s']
                    # Resolve the visible hit target before its deadline. The
                    # actual trusted dispatch still waits for the real due time.
                    prepared_hover=(event['action']=='click' and (natural or scenario.get('click_schedule')=='prepared_hover_v2'))
                    preparation_lead=.100 if prepared_hover else FRAME_CONTROL_POLICY['input_preparation_lead_s']
                    await wait_until_epoch(due-preparation_lead,check)
                    require_input_deadline(event['time_s'],utc_seconds()-start_epoch)
                    check()
                    if natural:
                        # Stop optional PNG submissions before preparing input.
                        # Let any already-started native screenshot finish; its
                        # own120ms deadline remains in force. Actual browser RAF
                        # continues. The input clock/deadline is never shifted.
                        input_preparing=True
                        async with observation_lock:pass
                    if index:
                        await timed('set_active_command',page.evaluate('(i)=>window.__kcPrivate.activeCommand=i',index),index)
                    action=event['action']
                    # Routine PNG quiet windows must not erase coverage of a
                    # settled prior response. Take real stale pixels during
                    # the existing preparation lead, then retain the absolute
                    # trusted-input deadline and native boundary behavior.
                    frame=None
                    if not natural and action in {'click','drag_start','drag_end'}:
                        frame=await timed('pre_input_pixel',pre_input_checkpoint(action,checkpoint,
                            lambda:max((f['interval_epoch_s'][1] for f in frames),default=None),
                            input_in_flight=lambda:input_in_flight,check=check),index)
                    if frame is not None:
                        fallback_checkpoints.append({'timestamp_source':frame['timestamp_source'],
                            'interval_epoch_s':frame['interval_epoch_s'],
                            'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest(),
                            'next_input_index':index,'pre_input_capture':True,'command_id':index,
                            'screenshot_requested':True,'actual_image_produced':True})
                    if event.get('replay_client_point') is not None:
                        # A prospective reference replay owns the physical
                        # pointer path. Do not adapt it to candidate geometry.
                        point=list(event['replay_client_point'])
                        if action=='drag_start':drag=(point,point)
                        if action=='drag_end' and list(pointer.position)!=point:
                            raise CaptureError('invalid_input','Reference pointer-up must match the preceding real pointer point')
                    elif action in ('click','drag_start'):
                        box=await timed('target_bounding_box',visible_hit_box(page,event['selector']),index)
                        if not box:raise CaptureError('runtime_error','Input target not visible')
                        point=[box['x']+box['width']/2,box['y']+box['height']/2]
                        if action=='drag_start':
                            origin=point
                            if domain=='panel':
                                geometry=await timed('accessible_drag_range',page.evaluate('(()=>{let t=document.querySelector("#track"),p=document.querySelector("#panel"),h=document.querySelector("#panel-handle");let min=Number(h.getAttribute("aria-valuemin")),max=Number(h.getAttribute("aria-valuemax"));return {min,travel:max-min,x:p.getBoundingClientRect().x-t.getBoundingClientRect().x};})()'),index)
                                if not geometry['travel']>0:raise CaptureError('invalid_input','Panel slider has no accessible travel range')
                                destination=[origin[0]+geometry['min']+event['destination']['travel_fraction']*geometry['travel']-geometry['x'],origin[1]]
                            else:destination=[scenario['viewport'][0]*event['destination']['viewport_x'],scenario['viewport'][1]*event['destination']['viewport_y']]
                            drag=(origin,destination)
                    elif action=='drag_move':
                        if drag is None:raise CaptureError('invalid_input','Drag missing')
                        origin,dest=drag;f=event['fraction']
                        point=[origin[0]+f*(dest[0]-origin[0]),origin[1]+f*(dest[1]-origin[1])]
                    # Early preparation must not masquerade as a boundary
                    # observation. Wait for the actual due time before completing
                    # the pre-response native frame. Pointer moves stay unblocked
                    # by frame waits so their declared cadence is not stretched.
                    scheduled_release=(action=='click' and scenario.get('click_schedule')=='release_at_due_v1')
                    if prepared_hover:
                        # Native hover can await compositor acknowledgement.
                        # Prepare it before the deadline while real frames keep
                        # advancing. Scheduled waiting is not a dispatch timeout.
                        await timed('prepared_click_hover',pointer.move(*point),index)
                    input_boundary=None
                    if natural:
                        boundary_stage=input_boundary_stage(scenario,event)
                        frame,input_boundary=await schedule_input_observation(due,
                            checkpoint=checkpoint if action in {'click','drag_start','drag_end'} else None,
                            latest_image_time=lambda:max((f['epoch_s'] for f in frames),default=None),
                            check=check,policy=NATURAL_POLICY,
                            boundary=(lambda:timed('await_native_raf',wait_native_animation_frame(page),index))
                                if boundary_stage is not None else None,
                            boundary_stage=boundary_stage,
                            dispatch_ready=lambda:require_input_deadline(event['time_s'],utc_seconds()-start_epoch))
                        if frame is not None:
                            fallback_checkpoints.append({'timestamp_source':frame['timestamp_source'],
                                'interval_epoch_s':frame['interval_epoch_s'],
                                'png_sha256':hashlib.sha256(base64.b64decode(frame['data'])).hexdigest(),
                                'command_id':index,'pre_input_capture':True,'after_preparation':True,
                                'screenshot_requested':True,'actual_image_produced':True})
                    if not natural and not scheduled_release:await wait_until_epoch(due,check)
                    require_input_deadline(event['time_s'],utc_seconds()-start_epoch)
                    input_frame='natural_frames_no_forced_boundary' if natural else 'pointer_move_no_frame_wait'
                    if input_boundary is not None:
                        boundary_advances.append(dict(input_boundary,command_id=index,
                                                      input_boundary_stage=boundary_stage))
                        input_frame=('awaited_real_browser_raf_during_replay_preparation' if boundary_stage=='preparation'
                                     else 'awaited_real_browser_raf_no_forced_frame')
                    if action=='drag_end' and not natural:
                        await timed('pre_input_frame',checkpoint(priority=True),index)
                        final_input_boundary=True;input_frame='completed_native_frame_at_due_no_png'
                    if action=='drag_start':
                        current=None
                        async def refreshed_hit():
                            nonlocal current,drag
                            if event.get('replay_client_point') is not None:
                                current=list(event['replay_client_point'])
                                return
                            box=await timed('refreshed_drag_bounding_box',visible_hit_box(page,event['selector']),index)
                            if not box:raise CaptureError('runtime_error','Drag handle disappeared')
                            current=[box['x']+box['width']/2,box['y']+box['height']/2]
                            # Destination remains the declared absolute viewport
                            # point even if the visible origin moved.
                            drag=(current,drag[1])
                        if natural:
                            await refreshed_hit()
                            if input_boundary is None:input_frame='natural_frames_no_forced_boundary'
                        else:
                            await timed('pre_drag_press_frame',checkpoint(priority=True,before_draw=refreshed_hit),index)
                            input_frame='completed_native_frame_after_hit_lookup_before_drag_press_no_png'
                    check();before=utc_seconds()
                    require_input_deadline(event['time_s'],before-start_epoch)
                    # Trusted input follows its real pre-command frame. No
                    # forced animation progress or synthetic input timestamp.
                    input_in_flight=True
                    if action=='click':
                        async def before_release():
                            nonlocal final_input_boundary
                            await timed('pre_click_release_frame',checkpoint(priority=True),index)
                            final_input_boundary=True
                        if natural:
                            await timed('trusted_click',pointer.finish_prepared_click(point),index)
                        elif prepared_hover:
                            await timed('trusted_click',pointer.finish_prepared_click(point,before_release=before_release),index)
                        else:
                            await timed('trusted_click',pointer.click(*point,before_release=before_release,
                                release_not_before=due if scheduled_release else None,check=check),index)
                        if not natural:input_frame='completed_native_frame_before_click_release_no_png'
                    elif action=='drag_start':
                        await timed('trusted_down',pointer.down(current),index)
                    elif action=='drag_move':await timed('trusted_move',pointer.move(*point),index)
                    elif action=='drag_end':await timed('trusted_up',pointer.up(),index);drag=None
                    after=utc_seconds()
                    input_in_flight=False;input_preparing=False;final_input_boundary=False;last_input_epoch=after
                    if action in ('click','drag_end'):
                        last_response_epoch=max(last_response_epoch or 0.,after+1.8+
                            scenario.get('timing_limits',TIMING_LIMITS)['clock_uncertainty_s'])
                    command_receipts.append({'index':index,'planned':event,'host_send_s':before,
                                             'host_completed_s':after,'pre_input_frame':input_frame,
                                             'scheduled_not_before_epoch_s':due,
                                             'schedule_semantics':('natural_browser_hover_early_press_release_at_due' if natural else
                                                'click_release_at_due_press_may_be_early' if scheduled_release else
                                                'hover_early_press_at_due_then_native_frame_release' if prepared_hover else 'dispatch_at_due'),
                                             'click_schedule':NATURAL_POLICY['click_schedule'] if natural else scenario.get('click_schedule','dispatch_at_due_v1'),
                                             'input_transport':'CDP_Input.dispatchMouseEvent'})
                    if natural:command_receipts[-1]['input_boundary_stage']=boundary_stage
                    next_event_index=index+1
                    if action in ('click','drag_end'):
                        task=asyncio.create_task(post_input_pixel(after,index))
                        post_input_tasks.add(task)
                        def completed_post_input(done):
                            post_input_tasks.discard(done)
                            if not done.cancelled():
                                failure=done.exception()
                                if failure is not None:post_input_errors.append(failure)
                        task.add_done_callback(completed_post_input)
                    if progress:progress(f"{scenario['id']}: input {index+1}/{len(scenario['events'])}")
                remaining=scenario['duration_s']-(utc_seconds()-start_epoch)
                while remaining>0:
                    check();await asyncio.sleep(min(.1,remaining));remaining=scenario['duration_s']-(utc_seconds()-start_epoch)
                post_input_results=await asyncio.gather(*list(post_input_tasks),return_exceptions=True)
                for result in post_input_results:
                    if isinstance(result,BaseException):raise result
                check()
                recording_active=False
                await idle_task
                await cdp.send('Page.stopScreencast')
                stream_end=utc_seconds()
                await drain_delivery_tasks(tasks)
                check()
                if seed is None or endpoint_checkpoint_needed(utc_seconds(),max((f['epoch_s'] for f in frames),default=None)):
                    await checkpoint()
                clock_end=await _clock(page)
                private=await page.evaluate('window.__kcPrivate.enabled=false;window.__kcPrivate')
                browser_version=browser.version
                elapsed=time.perf_counter()-start_monotonic
            except BaseException as exc:
                recording_active=False
                for task in list(post_input_tasks):task.cancel()
                if post_input_tasks:await asyncio.gather(*list(post_input_tasks),return_exceptions=True)
                if idle_task is not None:
                    if not idle_task.done():idle_task.cancel()
                    await asyncio.gather(idle_task,return_exceptions=True)
                if 'cdp' in locals():
                    try:await asyncio.wait_for(cdp.send('Page.stopScreencast'),.5)
                    except Exception:pass
                try:await drain_delivery_tasks(tasks,ignore_errors=True)
                except CaptureError:pass
                partial_private=None
                if 'page' in locals():
                    try:partial_private=await asyncio.wait_for(page.evaluate('window.__kcPrivate.enabled=false;window.__kcPrivate'),.5)
                    except Exception as failure:partial_private={'unavailable':str(failure)}
                for name,value in deferred_receipts.items():
                    # Best effort: evidence I/O must never replace the capture error.
                    try:_write(output/'private'/name,value)
                    except Exception:pass
                preserve_capture_receipts(output,frames,command_receipts,command_timings,
                    private=partial_private,error={'code':getattr(exc,'code','capture_failed'),'type':type(exc).__name__,'message':str(exc)},
                    boundary_advances=boundary_advances,routine_advances=routine_advances,
                    fallback_checkpoints=fallback_checkpoints,routine_pump_stop=routine_pump_stop or None,
                    delivery_backpressure=delivery_pressure.snapshot() if 'delivery_pressure' in locals() else None)
                raise
            finally:
                if idle_task is not None:
                    recording_active=False
                    if not idle_task.done():idle_task.cancel()
                    await asyncio.gather(idle_task,return_exceptions=True)
                stream_delivery_end=await close_browser_delivery(browser,tasks)
                if priority_receipt is not None:
                    from .host_quiet import restore_priorities
                    priority_receipt['restored']=restore_priorities(priority_receipt)
    for name,value in deferred_receipts.items():_write(output/'private'/name,value)
    preserve_capture_receipts(output,frames,command_receipts,command_timings,private=private,
        boundary_advances=boundary_advances,routine_advances=routine_advances,
        fallback_checkpoints=fallback_checkpoints,routine_pump_stop=routine_pump_stop or None,
        delivery_backpressure=delivery_pressure.snapshot())
    if console_errors:raise CaptureError('runtime_error','; '.join(console_errors[:6]))
    _write(output/'private/transport-context.json',{'browser_version':browser_version,
        'browser_observation_interval_epoch_s':[stream_start,stream_end],
        'actual_delivery_interval_epoch_s':[stream_start,stream_delivery_end],
        'observation_end_semantics':'Actual Page.stopScreencast completion',
        'delivery_end_semantics':'Owned browser closed and all scheduled receipt tasks settled',
        'source_clock_semantics':dict(SOURCE_CLOCK_SEMANTICS)})
    frames,transport_order=normalize_compositor_delivery(frames,segment_start_s=stream_start,
        segment_end_s=stream_end,delivery_end_s=stream_delivery_end,browser_version=browser_version)
    transport_order['raw_received_evidence_sha256']=hashlib.sha256(
        (output/'private/received-evidence.json').read_bytes()).hexdigest()
    _write(output/'private/transport-order.json',transport_order)
    frames,overlapping_checkpoints=exclude_redundant_checkpoints(frames)
    events=[]
    expected_type={'click':'click','drag_start':'pointerdown','drag_move':'pointermove','drag_end':'pointerup'}
    for command in command_receipts:
        receipt=next((e for e in private['events'] if e['command']==command['index'] and e['event']==expected_type[command['planned']['action']] and e['trusted']),None)
        if receipt is None:raise CaptureError('capture_timing_failure',f"No trusted receipt for input {command['index']}")
        require_input_deadline(command['planned']['time_s'],receipt['epoch_s']-start_epoch)
        events.append({**command['planned'],'planned_time_s':command['planned']['time_s'],
                       'time_s':receipt['epoch_s']-start_epoch,'receipt_event':receipt['event'],
                       'client_x':receipt['client_x'],'client_y':receipt['client_y'],'pointer_id':receipt['pointer_id'],
                       'command_id':command['index'],'clock_uncertainty_s':max(clock_start['uncertainty_s'],clock_end['uncertainty_s'])})
    # Down/move can change a visual before the recipe's selected click/up.
    # Use every real trusted pointer receipt when deciding a frame's side.
    frames,input_ambiguous=exclude_input_ambiguous_frames(frames,
        [e['epoch_s'] for e in private['events'] if e.get('trusted') is True])
    _write(output/'private/frame-receipts.json',{'frames':[{k:v for k,v in f.items() if k!='data'} for f in frames],
                                               'transport_order_receipt':'transport-order.json',
                                               'source_clock_semantics':dict(SOURCE_CLOCK_SEMANTICS),
                                               'excluded_outside_capture_segments':excluded_frames,
                                               'excluded_redundant_checkpoints':overlapping_checkpoints,
                                               'excluded_input_ambiguous_frames':input_ambiguous})
    raw_dir=output/'raw-frames';raw_dir.mkdir()
    paths=[];timestamps=[];frame_receipts=[];frame_metadata=[]
    ordered_frames=merge_frame_receipts(frames)
    visual_clock_receipt=None
    if scenario.get('visual_frame_clock')=='svg_raf_tag_v1':
        from .visual_clock import bind_frame_times
        ordered_frames,visual_clock_receipt=bind_frame_times(ordered_frames,private.get('visual_clock'),
            clock_tolerance_s=max(clock_start['uncertainty_s'],clock_end['uncertainty_s']))
        ordered_frames,clock_ambiguous=exclude_input_ambiguous_frames(ordered_frames,
            [e['epoch_s'] for e in private['events'] if e.get('trusted') is True])
        visual_clock_receipt['input_ambiguous_images']=clock_ambiguous
        _write(output/'private/visual-clock.json',visual_clock_receipt)
    for item in ordered_frames:
        ts=item['epoch_s']
        if not isinstance(ts,(int,float)) or not math.isfinite(ts):raise CaptureError('capture_timing_failure','Missing compositor timestamp')
        if ts<start_epoch:continue
        if abs(item['receive_epoch_s']-ts)>1:raise CaptureError('capture_timing_failure','Compositor/input epoch mismatch')
        interval=item['interval_epoch_s']
        path=raw_dir/f'{len(paths):06d}.png';path.write_bytes(base64.b64decode(item['data'],validate=True))
        paths.append(path);timestamps.append(ts-start_epoch)
        frame_metadata.append({'timestamp_source':item['timestamp_source'],'uncertainty_s':(interval[1]-interval[0])/2,
                               'time_interval_s':[interval[0]-start_epoch,interval[1]-start_epoch]})
        frame_receipts.append({k:v for k,v in item.items() if k!='data'})
    timing=timing_summary(timestamps,[clock_start,clock_end],events,scenario.get('timing_limits'),frame_metadata)
    if visual_clock_receipt is not None:
        timing['frame_clock']='Visible SVG counter bound to real rendering-batch interval; CDP transport times retained privately'
        timing['source_clock_semantics']={**timing['source_clock_semantics'],
            'measurement_clock':'svg_visible_raf_batch','physical_display_presentation_claimed':False,
            'source_geometry_used':False}
    private.update(schema_version=1,domain=domain,start_epoch_s=start_epoch,scenario=scenario)
    private['functional']=validate_oracle(private,domain)
    _write(output/'private/oracle.json',private)
    _write(output/'private/clock.json',{'start':clock_start,'end':clock_end,'frames':frame_receipts,
        'commands':command_receipts,'host_clock':clock_description()})
    _write(output/'capture-record.json',{'schema_version':1,'version':VERSION,'domain':domain,'scenario':scenario,
        'browser_version':browser_version,'timing':timing,'timestamps':timestamps,'inputs':{'provenance':'recorder_verified','events':events},
        'frames':[p.relative_to(output).as_posix() for p in paths],'frame_metadata':frame_metadata,
        'checkpoint_policy':dict(NATURAL_POLICY,mode=scenario['frame_driver'],await_native_input_boundary=native_boundary) if natural else FRAME_CONTROL_POLICY,
        'image_provenance':{'sources':['cdp_compositor','cdp_screenshot_bracket' if natural else 'cdp_begin_frame_bracket'],
            'native_routine_advance_count':len(routine_advances),
            'native_input_boundary_count':len(boundary_advances),
            'actual_png_stale_fallback_count':len(fallback_checkpoints),
            'actual_png_tail_count':sum(bool(item.get('tail_capture')) for item in fallback_checkpoints),
            'routine_pump_stop':{'reason':routine_pump_stop.get('reason'),
                'time_s':routine_pump_stop['host_epoch_s']-start_epoch}
                if routine_pump_stop else None,
            'native_advances_are_video_frames':False},
        'elapsed_wall_s':elapsed})
    return paths,timestamps,events,timing,private,browser_version,frame_metadata


def capture_run(project,output,scenario,*,variant='target',cancel=None,progress=None,build_package=True):
    scenario=validate_scenario(scenario);project=Path(project).resolve();output=Path(output).resolve()
    if output.exists():raise CaptureError('output_exists','Capture output must be fresh')
    if output==project or project in output.parents or output in project.parents:
        raise CaptureError('unsafe_path','Capture output and project must be separate')
    before=project_hashes(project);adapter_before=adapter_hashes();output.mkdir(parents=True)
    # V6 plan WP2.2: sample host scheduling before any browser starts. A host
    # that is already stalling refuses the recording instead of spending it.
    from .host_quiet import host_quiet_report,quiet_enough
    quiet=host_quiet_report()
    _write(output/'capture-start.json',{'version':VERSION,'scenario':scenario,'source_hashes':before,'adapter_hashes':adapter_before,'variant':variant,'host_quiet':quiet})
    if not quiet_enough(quiet):
        _write(output/'capture-error.json',{'status':'error','code':'machine_not_quiet','host_quiet':quiet,
            'message':f"Host stalled {quiet['spikes']} time(s) over {quiet['spike_s']:.3f}s in the pre-capture sample; recording not started"})
        raise CaptureError('machine_not_quiet',f"Host stalled {quiet['spikes']} time(s) over {quiet['spike_s']:.3f}s in the pre-capture sample; recording not started")
    publisher=ProgressPublisher(progress) if progress is not None else None
    def check_progress_cancel():
        if cancel is not None and (cancel.is_set() if hasattr(cancel,'is_set') else cancel()):
            raise CaptureError('cancelled','Local capture cancelled')
    # The live recording reads only this monitor; the caller's own probe (a
    # job-directory stat for sessions) runs on the monitor thread.
    monitor=CancelMonitor(cancel) if cancel is not None else None
    try:
        paths,timestamps,events,timing,oracle,browser,frame_metadata=run_capture(
            lambda:_record(project,output,scenario,monitor,
                publisher.publish if publisher else None,publisher.check if publisher else None),
            output/'private/clock-runtime.json')
        if monitor:monitor.close()
        # Durable UI status must finish before success, but never on the owned
        # capture event loop or in the next-input scheduling path.
        if publisher:publisher.finish(check=check_progress_cancel)
        if before!=project_hashes(project):raise CaptureError('source_integrity_failure','Project changed during capture')
        if adapter_before!=adapter_hashes():raise CaptureError('source_integrity_failure','Capture adapter changed during capture')
        result={'status':'ok' if timing['status']=='pass' and oracle['functional']['status']=='pass' else 'failed',
                'package_dir':None,'oracle_path':str(output/'private/oracle.json'),'timing':timing,
                'functional':oracle['functional'],'record_path':str(output/'capture-record.json'),'source_hashes':before,'adapter_hashes':adapter_before}
        if build_package:
            from kiroclops.visual.package import create_package
            package=output/'package'
            create_package(package,paths,timestamps,inputs={'provenance':'recorder_verified','events':events},
                metadata={'domain':canonical_domain(scenario['domain']),'variant':variant,'scenario':scenario,
                          'viewport':scenario['viewport'],'device_scale_factor':1,'browser_version':browser,
                          'capture_version':VERSION,'timing':timing,'source_hashes':before,'adapter_hashes':adapter_before,
                          'source_clock_semantics':timing['source_clock_semantics'],
                          'coordinate_units':'natural_image_pixels','clock':'real_CDP_timestamps_or_explicit_bounded_checkpoints'},frame_metadata=frame_metadata)
            result['package_dir']=str(package)
        # Commit the required progress diagnostic before the success receipt.
        # A diagnostic I/O failure must not leave a successful capture result.
        if publisher:_write(output/'private/progress-runtime.json',publisher.snapshot())
        if monitor:_write(output/'private/cancel-runtime.json',monitor.snapshot())
        _write(output/'capture-result.json',result);return result
    except BaseException as exc:
        if monitor:monitor.close()
        if publisher:publisher.abort()
        # Best-effort error evidence must never replace the original failure.
        if publisher:
            try:_write(output/'private/progress-runtime.json',publisher.snapshot())
            except Exception:pass
        if monitor:
            try:_write(output/'private/cancel-runtime.json',monitor.snapshot())
            except Exception:pass
        try:_write(output/'capture-error.json',{'status':'error','code':getattr(exc,'code','capture_failed'),'message':str(exc)})
        except Exception:pass
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain',choices=['panel','web','svg'],default='panel')
    parser.add_argument('--variant',choices=['reference','target'],default='target')
    parser.add_argument('--project');parser.add_argument('--output',required=True)
    parser.add_argument('--split',choices=['calibration','validation'],default='calibration')
    parser.add_argument('--scenario',type=int,default=0);parser.add_argument('--no-package',action='store_true')
    args=parser.parse_args()
    result=capture_run(args.project or fixture_project(args.domain,args.variant),args.output,
        scenarios(args.domain,args.split)[args.scenario],variant=args.variant,build_package=not args.no_package)
    print(json.dumps(result,ensure_ascii=False));return 0 if result['status']=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
