"""Visible SVG clock for prospective recorder qualification only.

A counter binds pixels to an observed rendering-batch interval. Transport
metadata stays separate. This is not a physical display-presentation clock.
"""
import base64
import hashlib
import math
import cv2
import numpy as np

POLICY='svg_raf_tag_v1'
CLOCK_SCRIPT = r'''(()=>{
  const rows=[];let id=0,canvas=null,cells=[];const ns='http://www.w3.org/2000/svg';
  window.__kcPrivate.visual_clock={rows,policy:'svg_raf_tag_v1'};
  function tick(raf){
    if(!canvas && document.documentElement){canvas=document.createElementNS(ns,'svg');
      canvas.setAttribute('width','84');canvas.setAttribute('height','6');canvas.setAttribute('aria-hidden','true');
      canvas.style.cssText='position:fixed;bottom:0;right:0;width:84px;height:6px;z-index:2147483647;pointer-events:none';
      for(let i=0;i<28;i++)for(let row=0;row<2;row++){const r=document.createElementNS(ns,'rect');
        r.setAttribute('x',i*3);r.setAttribute('y',row*3);r.setAttribute('width',3);r.setAttribute('height',3);
        canvas.append(r);cells.push(r);}
      document.documentElement.append(canvas);}
    if(canvas){id++;if(id>65535)throw Error('visual_clock_overflow');
      const before=(performance.timeOrigin+performance.now())/1000;
      const bits=[1,0,1,0];for(let i=15;i>=0;i--)bits.push((id>>i)&1);
      const crc=(id^(id>>8)^0xa7)&255;for(let i=7;i>=0;i--)bits.push((crc>>i)&1);
      bits.forEach((bit,i)=>{for(let row=0;row<2;row++)cells[i*2+row].style.fill=(bit^row)?'#fafafa':'#050505';});
      const row={id,callback_epoch_s:before,raf_epoch_s:(performance.timeOrigin+raf)/1000,
        callback_end_epoch_s:(performance.timeOrigin+performance.now())/1000,after_task_epoch_s:null};
      rows.push(row);setTimeout(()=>{row.after_task_epoch_s=(performance.timeOrigin+performance.now())/1000;},0);
    }requestAnimationFrame(tick);
  }requestAnimationFrame(tick);
})();'''

def decode(image):
    patch=image[-6:,-84:,:3];bits=[]
    if patch.shape!=(6,84,3):raise ValueError('Missing visual clock')
    for i in range(28):
        cells=[patch[row*3:(row+1)*3,i*3:(i+1)*3] for row in range(2)]
        vals=[1 if (cell>235).all() else 0 if (cell<20).all() else None for cell in cells]
        if None in vals or vals[0]==vals[1]:
            raise ValueError('Ambiguous visual clock cell')
        bits.append(vals[0])
    if bits[:4]!=[1,0,1,0]:raise ValueError('Missing visual clock preamble')
    def number(values):
        n=0
        for bit in values:n=n*2+bit
        return n
    ident=number(bits[4:20]);crc=number(bits[20:])
    if crc!=((ident^(ident>>8)^0xa7)&255):raise ValueError('Visual clock checksum mismatch')
    return ident


def bind_frame_times(frames, clock, *, clock_tolerance_s=.001):
    """Decode every image; reject unknown IDs, tearing and unbounded batches.

    Identical duplicate observations of one generated image are logged once.
    A repeated ID with different pixels is ambiguous and cannot be repaired.
    """
    from .capture import CaptureError
    def fail(message):raise CaptureError('capture_timing_failure',message)
    if (type(clock_tolerance_s) not in (float,int) or not math.isfinite(clock_tolerance_s)
            or clock_tolerance_s<0):fail('Invalid clock mapping tolerance')
    if not isinstance(clock,dict) or clock.get('policy')!=POLICY:
        fail('Missing visible frame clock')
    if not isinstance(clock.get('rows'),list) or any(not isinstance(r,dict) for r in clock['rows']):
        fail('Invalid clock counter table')
    lookup={}
    for row in clock.get('rows',[]):
        ident=row.get('id')
        if type(ident) is not int or not 1<=ident<=65535 or ident in lookup:fail('Invalid clock counter table')
        lookup[ident]=row
    kept=[];associations=[];duplicates=[];seen={};previous_id=0;previous_end=-math.inf
    for frame in frames:
        # A request/response bracket does not observe pixels at its midpoint.
        # Preserve both bounds; a compositor observation remains a point.
        stamp=frame.get('epoch_s');interval=frame.get('interval_epoch_s');source=frame.get('timestamp_source')
        if (type(stamp) not in (float,int) or not math.isfinite(stamp)
                or not isinstance(interval,list) or len(interval)!=2
                or any(type(v) not in (float,int) or not math.isfinite(v) for v in interval)
                or not interval[0]<=stamp<=interval[1]
                or source not in ('cdp_compositor','cdp_screenshot_bracket','cdp_begin_frame_bracket')
                or (source=='cdp_compositor' and interval!=[stamp,stamp])):
            fail('Invalid image observation interval')
        try:
            raw=base64.b64decode(frame['data'],validate=True)
            pixels=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
            if pixels is None:raise ValueError('Missing PNG')
            ident=decode(pixels)
        except (ValueError,TypeError,KeyError,cv2.error) as exc:fail('Invalid visible frame clock: '+str(exc))
        if ident not in lookup:fail('Visible counter has no independent clock receipt')
        row=lookup[ident];low=row.get('callback_epoch_s');high=row.get('after_task_epoch_s')
        if (any(type(v) not in (float,int) or not math.isfinite(v) for v in (low,high))
                or not 0<=high-low<=.040):fail('Unbounded rendering-batch clock interval')
        midpoint=(low+high)/2;lag=stamp-midpoint
        lag_interval=[interval[0]-midpoint,interval[1]-midpoint]
        # Causality fails only when even the observation's upper bound precedes
        # the decoded callback. Retain the120ms stale-image bound for the whole
        # actual observation interval, not merely its convenient midpoint.
        if interval[1]<low-clock_tolerance_s or lag_interval[1]>.120:
            fail('Visible counter does not belong to this image observation interval')
        raster_sha=hashlib.sha256(pixels.tobytes()).hexdigest()
        association={'counter':ident,'original_timestamp_source':frame['timestamp_source'],
            'original_epoch_s':stamp,'original_interval_epoch_s':list(interval),
            'rendering_batch_interval_epoch_s':[low,high],'transport_minus_batch_midpoint_s':lag,
            'transport_minus_batch_midpoint_interval_s':lag_interval,'clock_tolerance_s':clock_tolerance_s,
            'observation_association_basis':'point_observation' if source=='cdp_compositor' else 'bounded_request_response',
            'png_sha256':hashlib.sha256(raw).hexdigest(),'raster_sha256':raster_sha}
        if ident in seen:
            if raster_sha!=seen[ident]:fail('One visible frame counter labels different pixel states')
            duplicates.append({**association,'reason':'Identical image observed more than once; original receipts retained'})
            continue
        if ident<=previous_id or low<=previous_end:fail('Visible frame clock is unordered or overlaps')
        previous_id=ident;previous_end=high;seen[ident]=raster_sha
        associations.append(association)
        kept.append({**frame,'epoch_s':midpoint,'interval_epoch_s':[low,high],
                     'timestamp_source':'svg_visible_raf_batch','visual_counter':ident,
                     'original_timestamp_source':source,'original_epoch_s':stamp,
                     'original_interval_epoch_s':list(interval)})
    if not kept:fail('No image with a visible frame clock')
    return kept,{'policy':POLICY,'associations':associations,'duplicate_images':duplicates,
                 'observation_association_policy':'actual_observation_bracket_v1','clock_tolerance_s':clock_tolerance_s,
                 'maximum_observation_transport_lag_s':.120,
                 'clock_meaning':'Observed recorder RAF callback through following task; rendering-batch interval',
                 'physical_display_presentation_claimed':False,'source_geometry_used':False,
                 'interpolated_images':False,'virtual_animation_clock':False,
                 'marker':{'position':'bottom_right','width_px':84,'height_px':6}}
