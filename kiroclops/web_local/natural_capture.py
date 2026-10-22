"""Experimental observer of Chromium's own animation clock, not a frame driver.

No BeginFrame, injected animation clock, or synthetic input. Static-image
checkpoints retain their actual command brackets; they are not video frames
at an invented 60 Hz. This path is qualification-only until proven stable.
"""
import asyncio
import math
from .clock_runtime import utc_seconds

POLICY = {'mode':'natural_browser_v1','frameTimeTicks_supplied':False,
          'animation_driver':'unmodified_browser_requestAnimationFrame',
          'routine_frame_advances':False,'input_boundary_frame_advances':False,
          'static_image_stale_s':.040,'input_image_quiet_s':.030,
          'input_preparation_lead_s':.100,'click_schedule':'hover_early_press_release_at_due',
          'input_preparation':'suppress_new_png_and_drain_existing_screenshot_before_input_commands',
          'final_input_image_check_lead_s':.040,'oldest_final_input_image_before_due_s':.060,
          'source_documentation':'https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-captureScreenshot'}


def input_pixel_policy(domain):
    """Keep the last static panel image near its actual input boundary.

    A 40 ms preparation lead left 60–67 ms onset brackets, making two-event
    timing unresolved. Four independent native captures verified the later
    checkpoint. Inputs still retain real dispatch times and hard deadlines;
    no image is synthesized or assigned the scheduled input timestamp.
    SVG retains its separately qualified visible-clock capture policy.
    """
    policy=dict(POLICY)
    if domain=='panel':
        policy.update(final_input_image_check_lead_s=.010,
                      oldest_final_input_image_before_due_s=.020,
                      pre_input_pixel_policy='late_checkpoint_v2')
    return policy


def browser_arguments(natural=False):
    args=['--disable-background-timer-throttling','--disable-renderer-backgrounding',
          '--disable-backgrounding-occluded-windows']
    if not natural:args+=['--enable-begin-frame-control','--run-all-compositor-stages-before-draw']
    return args


async def _screenshot(cdp, timeout_s, *, warmup=False):
    from .capture import CaptureError
    before=utc_seconds()
    try:
        value=await asyncio.wait_for(cdp.send('Page.captureScreenshot',
            {'format':'png','fromSurface':True,'captureBeyondViewport':False,'optimizeForSpeed':True}),timeout_s)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_startup_failure' if warmup else 'capture_timing_failure',
                           'Warmup screenshot exceeded2s' if warmup else 'Native screenshot exceeded120ms') from exc
    after=utc_seconds()
    if not isinstance(value.get('data'),str) or not value['data']:
        raise CaptureError('capture_timing_failure','Native screenshot supplied no actual pixels')
    return {'epoch_s':(before+after)/2,'receive_epoch_s':after,'data':value['data'],
        'metadata':{},'timestamp_source':'cdp_screenshot_bracket','interval_epoch_s':[before,after]}


async def screenshot_image(cdp):
    """Active experiment images keep the unchanged 120 ms deadline."""
    return await _screenshot(cdp,.120)


async def warmup_screenshot_image(cdp):
    """One cold-start request before any experimental clock/input is enabled.

    These pixels cannot be recording evidence. There are no retries, discarded
    active frames, new timestamps or changes to the active capture thresholds.
    """
    result=await _screenshot(cdp,2.,warmup=True)
    return {**result,'recording_eligible':False,'phase':'warmup'}


async def wait_native_animation_frame(page):
    """Observe one real RAF completion; never drive or replace its clock."""
    from .capture import CaptureError
    before=utc_seconds()
    try:
        stamp=await asyncio.wait_for(page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(t=>resolve(t)))'),.120)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_timing_failure','Native RAF boundary exceeded120ms') from exc
    after=utc_seconds()
    if type(stamp) not in (float,int) or not math.isfinite(stamp):raise CaptureError('capture_timing_failure','Native RAF has no timestamp')
    return {'host_before_s':before,'host_after_s':after,'browser_raf_timestamp_ms':stamp,
        'image_produced':False,'screenshot_requested':False,'frameTimeTicks_supplied':False,
        'driver':'browser','observation':'awaited_native_animation_callback'}


def needs_native_boundary(scenario, action):
    """Prospective observer synchronizes pointer moves to a real RAF as well.

    A fixed wall-clock pointer cadence otherwise alternates between the two
    sides of the browser paint boundary. Waiting observes the browser clock;
    it neither drives frames nor supplies an invented input timestamp.
    """
    return (scenario.get('frame_driver')=='natural_browser_raf_v1' and
            (action in {'click','drag_start','drag_end'} or
             scenario.get('analysis_policy')=='observed_response_v2' and action=='drag_move'))


def input_boundary_stage(scenario, event):
    """Observe a panel replay's RAF before its already measured input time.

    Reference replay deadlines contain the real reference event's boundary
    wait. Waiting for another RAF after that deadline adds that latency twice,
    especially at pointer-up. Canonical recordings and the qualified SVG
    recorder retain their existing dispatch-time boundary.
    """
    if not needs_native_boundary(scenario, event['action']):return None
    if (scenario.get('domain')=='panel'
            and scenario.get('suite_version')=='reference-suite-web-v2'
            and event.get('replay_client_point') is not None):
        return 'preparation'
    return 'dispatch'


async def schedule_input_observation(due, *, checkpoint=None, latest_image_time=None,
                                     check=lambda:None, policy=None, boundary=None,
                                     boundary_stage=None, dispatch_ready=lambda:None):
    """Prepare real pixels/RAF, then keep the absolute trusted-input deadline.

    The caller enters during its existing preparation lead. An early RAF is
    an observation of the browser clock, not an input or a replacement frame.
    A slow screenshot can still overrun the deadline: its true timing remains
    visible to the lateness and input-history checks. Nothing is backdated.
    """
    from .capture import wait_until_epoch
    if boundary_stage not in {None,'preparation','dispatch'}:
        raise ValueError('Unknown native input boundary stage')
    if (boundary is None)!=(boundary_stage is None):
        raise ValueError('Native input boundary requires an explicit stage')
    check()
    receipt=await boundary() if boundary_stage=='preparation' else None
    check()
    frame=(await final_input_image(due,checkpoint,latest_image_time,check,policy=policy)
           if checkpoint is not None else None)
    await wait_until_epoch(due,check)
    dispatch_ready()
    if boundary_stage=='dispatch':receipt=await boundary()
    return frame,receipt


async def await_stream_image(ready):
    """Require a real initial stream image before scheduling experiment inputs."""
    from .capture import CaptureError
    try:return await asyncio.wait_for(ready,.120)
    except asyncio.TimeoutError as exc:
        raise CaptureError('capture_timing_failure','Initial stream image exceeded120ms') from exc


def endpoint_checkpoint_needed(now, latest_image_time):
    """A fresh primary stream image already observes the recording endpoint."""
    return latest_image_time is None or now-latest_image_time>POLICY['static_image_stale_s']


def checkpoint_needed(now,latest_end,upcoming_input_s,input_in_flight):
    return (not input_in_flight and (upcoming_input_s is None or upcoming_input_s>POLICY['input_image_quiet_s'])
        and (latest_end is None or now-latest_end>=POLICY['static_image_stale_s']))


async def final_input_image(due,checkpoint,latest_image_time,check=lambda:None,*,policy=None):
    """Bridge a static pre-input gap after hover, outside input command work.

    A naturally delivered fresh image cancels the PNG request. Otherwise a
    real screenshot precedes trusted input, retaining any real lateness. Check the
    image's declared observation time, not the end of its encoding bracket.
    """
    from .capture import wait_until_epoch
    policy=POLICY if policy is None else policy
    await wait_until_epoch(due-policy['final_input_image_check_lead_s'],check)
    def needed():
        check();latest=latest_image_time()
        return latest is None or latest<due-policy['oldest_final_input_image_before_due_s']
    return await checkpoint(eligible=needed) if needed() else None
