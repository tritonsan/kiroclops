"""Compare independent pixel observations in actual seconds, without time warp."""
from __future__ import annotations

import bisect
import math

from .schema import VisualError, digest
from .events import extract_events

# V6 plan WP3.1: feedback-only window diagnostics. No gate reads them.
LAG_SEARCH_S = .3
LAG_STEP_S = .01
LAG_GRID_POINTS = 32
TRAJECTORY_POINTS = 16


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def _origin(observations, alignment):
    mode=alignment.get('mode','absolute')
    if mode=='absolute':return 0.
    if mode!='input':raise VisualError('invalid_contract','Alignment is absolute or verified input; no time warp')
    timeline=observations.get('inputs',{})
    if timeline.get('provenance')!='recorder_verified':
        raise VisualError('insufficient_observability','Input alignment requires recorder-verified events')
    events=[e for e in timeline.get('events',[]) if
            (alignment.get('action') is None or e.get('action')==alignment['action']) and
            ('selector' not in alignment or e.get('selector')==alignment['selector'])]
    index=alignment.get('ordinal',alignment.get('event_index',0))
    if type(index) is not int or not 0<=index<len(events) or not _finite(events[index].get('time_s')):
        raise VisualError('invalid_contract','Requested alignment event is missing')
    return float(events[index]['time_s'])


def window_timing(reference, candidate, contract, window):
    """Resolve a fixed-duration response against each matching actual input.

    Every window is translated once, never stretched. If the next command
    interrupts either recording, compare only their shared pre-command duration.
    The original requested bound and both interruption bounds remain visible.
    """
    alignment=window.get('alignment',contract.get('alignment',{}))
    ro,co=(_origin(item,alignment) for item in (reference,candidate))
    start,end=window['start_s'],window['end_s']
    stops=[None,None]
    last_images=[None,None]
    if window.get('end_alignment') is not None:
        for i,(item,origin) in enumerate(zip((reference,candidate),(ro,co))):
            stops[i]=_origin(item,window['end_alignment'])-origin
            if stops[i]<=start+1e-9:
                raise VisualError('capture_timing_failure','Terminating input precedes its response window')
        end=min(end,*stops)
        if contract.get('sampling_policy') in ('observed_response_v2', 'observed_pre_input_v1'):
            from .visibility import sample_before
            for i,(item,origin) in enumerate(zip((reference,candidate),(ro,co))):
                last_images[i]=sample_before(item,origin,stops[i],contract.get('max_interpolation_gap_s',.12))
            end=min(end,*last_images)
    if end<=start:
        raise VisualError('capture_timing_failure','Input-aligned comparison window is empty')
    endpoint_required=window.get('endpoint_required',True)
    if type(endpoint_required) is not bool:
        raise VisualError('invalid_contract','endpoint_required must be boolean')
    return {'reference_origin_s':ro,'candidate_origin_s':co,
            'requested_window_s':[start,window['end_s']], 'time_window_s':[start,end],
            'next_input_after_origin_s':stops,
            **({'last_pre_input_sample_s':last_images} if contract.get('sampling_policy') in ('observed_response_v2', 'observed_pre_input_v1') else {}),
            'endpoint_required':endpoint_required and not any(v is not None and v<=window['end_s']+1e-9 for v in stops),
            'alignment':alignment,'duration_rescaled':False}


def _before_input(times, frames, bound):
    # Interpolation may not borrow a point rendered after the next command.
    if bound is None:return times,frames
    end=bisect.bisect_left(times,bound-1e-9)
    return times[:end],frames[:end]


def _series(obj, origin):
    frames=obj.get('frames',[]);previous=-float('inf')
    for i,f in enumerate(frames):
        if f.get('index')!=i or not _finite(f.get('time_s')) or f['time_s']<=previous:
            raise VisualError('capture_timing_failure','Comparison frames must be strictly ordered')
        previous=f['time_s']
        if f.get('valid') is True and not all(_finite(f.get(k)) for k in ('x','y','width','height')):
            raise VisualError('invalid_observation','Valid geometry must be finite')
    if not frames:raise VisualError('invalid_observation','No observations')
    return [float(f['time_s']-origin) for f in frames],frames


def relative_object(obj,parent):
    """Measured child-minus-parent anchors; an invalid parent never becomes zero."""
    _series(obj,0.);_series(parent,0.)
    frames=obj['frames'];anchors=parent['frames']
    if len(frames)!=len(anchors) or any(a['time_s']!=b['time_s'] for a,b in zip(frames,anchors)):
        raise VisualError('capture_timing_failure','Relative motion requires synchronized native images')
    result=[]
    for child,anchor in zip(frames,anchors):
        frame=dict(child);frame['valid']=bool(child.get('valid') and anchor.get('valid'))
        if frame['valid']:
            frame.update(x=child['x']-anchor['x'],y=child['y']-anchor['y'],
                         left=child.get('left',child['x']-child['width']/2)-anchor['x'],
                         top=child.get('top',child['y']-child['height']/2)-anchor['y'])
        else:frame.update(x=None,y=None,left=None,top=None,width=None,height=None)
        result.append(frame)
    return {**obj,'frames':result,'coordinate_space':'relative_visible_anchor','relative_to':parent['id']}


def _sample(times,frames,time,max_gap):
    index=bisect.bisect_left(times,time)
    if index<len(times) and abs(times[index]-time)<=1e-9:
        return frames[index] if frames[index].get('valid') else None
    if index==0 or index==len(times):return None
    a,b=frames[index-1],frames[index]
    if not a.get('valid') or not b.get('valid') or times[index]-times[index-1]>max_gap:return None
    fraction=(time-times[index-1])/(times[index]-times[index-1])
    return {k:float(a[k]+fraction*(b[k]-a[k])) for k in ('x','y','width','height')}


def compare_observations(reference,candidate,contract=None):
    """Require an explicit contract before granting numerical acceptance.

    contract: object_pairs[{reference_id,candidate_id,scale_px?,rmse_limit?,
    size_rmse_limit?,endpoint_limit?}], windows[{id,start_s,end_s}],
    alignment{mode:'absolute'|'input',action?,event_index?}, min_valid_ratio,
    max_interpolation_gap_s. Optional required_event_types,event_tolerance_s,
    require_connections,required_connections and forbid_boundary_cross.
    A scale is fixed from the reference/contract, never from each candidate.
    """
    if not isinstance(contract,dict) or not contract.get('object_pairs') or not contract.get('windows'):
        return {'schema_version':1,'kind':'AnalysisResult','status':'pending','numerical_status':'pending',
                'measurement_status':'pending','manual_validation_status':'pending','success':False,
                'errors':[{'code':'missing_contract','message':'Explicit objects, fixed tolerances and time windows are required'}]}
    minimum=contract.get('min_valid_ratio',.95);gap=contract.get('max_interpolation_gap_s',.12)
    if not _finite(minimum) or not 0<minimum<=1 or not _finite(gap) or gap<=0:
        raise VisualError('invalid_contract','Validity and interpolation bounds must be positive')
    origins=[_origin(o,contract.get('alignment',{})) for o in (reference,candidate)]
    refs={o['id']:o for o in reference.get('objects',[])};targets={o['id']:o for o in candidate.get('objects',[])}
    if len(refs)!=len(reference.get('objects',[])) or len(targets)!=len(candidate.get('objects',[])):
        raise VisualError('invalid_observation','Duplicate object identity')
    pairs=contract['object_pairs'];windows=contract['windows'];used_ref=set();used_target=set();window_ids=set()
    for window in windows:
        if not isinstance(window,dict) or not isinstance(window.get('id'),str) or window['id'] in window_ids or not all(_finite(window.get(k)) for k in ('start_s','end_s')) or window['end_s']<=window['start_s']:
            raise VisualError('invalid_contract','Time windows require distinct IDs and positive finite durations')
        window_ids.add(window['id'])
    timings={window['id']:window_timing(reference,candidate,contract,window) for window in windows}
    rows=[];errors=[];lifecycle_checks=[];censored_segments=[]
    lifecycle_tolerance=contract.get('lifecycle_tolerance_s',0.)
    if not _finite(lifecycle_tolerance) or lifecycle_tolerance<0:
        raise VisualError('invalid_contract','Lifecycle tolerance must be finite and nonnegative')
    if reference.get('image_size')!=candidate.get('image_size'):
        errors.append({'code':'image_scale_mismatch','message':'Native image dimensions must match; implicit scaling is forbidden'})
    for role,observation in [('reference',reference),('candidate',candidate)]:
        if observation.get('status')=='rejected' or observation.get('measurement_status')=='fail':
            errors.append({'code':'rejected_measurement','role':role})
    for pair in pairs:
        rid,cid=pair.get('reference_id'),pair.get('candidate_id')
        if rid in used_ref or cid in used_target:
            raise VisualError('invalid_contract','Each tracked object can be bound only once')
        used_ref.add(rid);used_target.add(cid)
        if rid not in refs or cid not in targets:
            errors.append({'code':'missing_object','reference_id':rid,'candidate_id':cid});continue
        scale=pair.get('scale_px',1.)
        limits={k:pair.get(k) for k in ('rmse_limit','size_rmse_limit','endpoint_limit')}
        if not _finite(scale) or scale<=0 or any(not _finite(v) or v<0 for v in limits.values()):
            raise VisualError('invalid_contract','Each object requires fixed positive scale and finite position/size/endpoint tolerances')
        ref_obj,target_obj=refs[rid],targets[cid]
        rparent,cparent=pair.get('relative_to_reference'),pair.get('relative_to_candidate')
        if rparent is not None or cparent is not None:
            if rparent not in refs or cparent not in targets or rparent==rid or cparent==cid:
                errors.append({'code':'missing_relative_anchor','reference_id':rid,'candidate_id':cid});continue
            ref_obj=relative_object(ref_obj,refs[rparent]);target_obj=relative_object(target_obj,targets[cparent])
        rt,rf=_series(ref_obj,origins[0]);ct,cf=_series(target_obj,origins[1])
        intervals=refs[rid].get('active_intervals_s')
        expected=None
        if intervals:
            expected=[[a-origins[0],b-origins[0]] for a,b in intervals]
            declared=[[a-origins[1],b-origins[1]] for a,b in targets[cid].get('active_intervals_s',[])]
            mapped=pair.get('candidate_active_intervals_s',expected)
            if not isinstance(mapped,list) or len(mapped)!=len(expected) or any(not isinstance(v,(list,tuple)) or len(v)!=2 or not all(_finite(t) for t in v) or v[1]<v[0] for v in mapped):
                raise VisualError('invalid_contract','Candidate lifecycle mapping must bind every reference interval')
            censored=[[abs(a-rt[0])<=1e-9,abs(b-rt[-1])<=1e-9] for a,b in expected]
            compatible=len(expected)==len(declared)
            for i,((a,b),(c,d)) in enumerate(zip(mapped,declared)):
                compatible &= (abs(c-ct[0])<=lifecycle_tolerance+1e-9 if censored[i][0] else abs(a-c)<=lifecycle_tolerance+1e-9)
                compatible &= (abs(d-ct[-1])<=lifecycle_tolerance+1e-9 if censored[i][1] else abs(b-d)<=lifecycle_tolerance+1e-9)
            outside=[f for t,f in zip(ct,cf) if not any((censored[i][0] or a-lifecycle_tolerance-1e-9<=t) and (censored[i][1] or t<=b+lifecycle_tolerance+1e-9) for i,(a,b) in enumerate(mapped))]
            lifecycle_checks.append({'reference_id':rid,'candidate_id':cid,
                'expected_intervals_s':expected,'declared_intervals_s':declared,
                'mapped_candidate_intervals_s':mapped,
                'clip_boundary_censored':censored,'tolerance_s':lifecycle_tolerance,
                'inactive_unknown_frames':sum(f.get('present') is None for f in outside),
                'inactive_present_frames':sum(f.get('present') is True for f in outside),
                'pass':bool(compatible and all(f.get('present') is False for f in outside))})
        pair_rows=0
        for window in windows:
            if window.get('object_ids') is not None and rid not in window['object_ids']:continue
            timing=timings[window['id']]
            ro,co=timing['reference_origin_s'],timing['candidate_origin_s']
            wrt=[t+origins[0]-ro for t in rt];wct=[t+origins[1]-co for t in ct]
            wrt,wrf=_before_input(wrt,rf,timing['next_input_after_origin_s'][0])
            wct,wcf=_before_input(wct,cf,timing['next_input_after_origin_s'][1])
            start,end=timing['time_window_s']
            active=None if intervals is None else [(a-ro,b-ro) for a,b in intervals]
            segments=[(start,end)] if active is None else [(max(start,a),min(end,b)) for a,b in active if min(end,b)>max(start,a)]
            for segment_index,(start,end) in enumerate(segments):
                endpoint_required=timing['endpoint_required']
                if contract.get('sampling_policy') == 'observed_response_v2' and intervals:
                    from .visibility import shared_lifecycle_segment
                    candidate_intervals=targets[cid].get('active_intervals_s',[])
                    matching=[(a-co,b-co) for a,b in candidate_intervals
                              if min(end,b-co)>=max(start,a-co)-lifecycle_tolerance]
                    original=next(((a-ro,b-ro) for a,b in intervals
                                   if min(end,b-ro)>=end-1e-9 and max(start,a-ro)<=start+1e-9),None)
                    if original is not None and len(matching)==1:
                        # Removal censors a trajectory; it is not a settled
                        # endpoint. Visibility itself is checked independently.
                        if original[1] < timing['time_window_s'][1]-1e-9:
                            endpoint_required=False
                        interval,note=shared_lifecycle_segment(start,end,original,matching[0],gap,lifecycle_tolerance)
                        censored_segments.append({'reference_id':rid,'window_id':window['id'],**note})
                        if interval is None:continue
                        start,end=interval
                row=_compare_window(wrt,wrf,wct,wcf,start,end,gap,scale,limits,minimum,
                                    endpoint_required=endpoint_required)
                pair_rows+=1
                rows.append({'reference_id':rid,'candidate_id':cid,'window_id':window['id'],
                             'relative_to_reference':rparent,'relative_to_candidate':cparent,
                             'segment_index':segment_index,'input_timing':timing,**row})
        if not pair_rows:
            errors.append({'code':'missing_active_window','reference_id':rid,'candidate_id':cid})
    if not contract.get('allow_unlisted_objects',False) and (used_ref!=set(refs) or used_target!=set(targets)):
        errors.append({'code':'unbound_objects','message':'Every observed object must have an explicit comparison binding'})
    event_checks=[];preservation=[]
    required_types=contract.get('required_event_types',[])
    if required_types or contract.get('require_connections') or contract.get('forbid_boundary_cross'):
        reference_events=extract_events(reference,contract=contract);candidate_events=extract_events(candidate,contract=contract)
        tolerance=contract.get('event_tolerance_s',.1)
        if not _finite(tolerance) or tolerance<0:raise VisualError('invalid_contract','Invalid event tolerance')
        for pair in pairs:
            for kind in required_types:
                a=[e for e in reference_events['events'] if e['type']==kind and e['object_ids']==[pair['reference_id']]]
                b=[e for e in candidate_events['events'] if e['type']==kind and e['object_ids']==[pair['candidate_id']]]
                passed=bool(a) and len(a)==len(b) and all(e.get('timing_status')=='resolved' for e in a+b)
                distances=[]
                for x,y in zip(a,b):
                    xa,xb=[v-origins[0] for v in x['time_interval_s']];ya,yb=[v-origins[1] for v in y['time_interval_s']]
                    distance=max(0.,ya-xb,xa-yb);distances.append(distance)
                    passed &= distance<=tolerance
                event_checks.append({'reference_id':pair['reference_id'],'candidate_id':pair['candidate_id'],'type':kind,'reference_count':len(a),'candidate_count':len(b),'interval_distance_s':distances,'pass':bool(passed)})
        if contract.get('require_connections'):
            expected_connections=set(contract.get('required_connections',[]))
            existing={c['id'] for c in candidate.get('connections',[])}
            if not expected_connections:raise VisualError('invalid_contract','Declare required_connections explicitly')
            for cid in sorted(expected_connections):
                relations=[r for r in candidate_events['relations'] if r.get('connection_id')==cid]
                missing=[r for r in candidate_events['unobservable'] if r.get('connection_id')==cid]
                preservation.append({'type':'visible_connection','id':cid,'pass':cid in existing and bool(relations) and not missing and all(r['visually_attached'] for r in relations)})
        if contract.get('forbid_boundary_cross'):
            for boundary_id in contract['forbid_boundary_cross']:
                relation=[r for r in candidate_events['relations'] if r.get('boundary_id')==boundary_id]
                crossings=[e for e in candidate_events['events'] if e.get('boundary_id')==boundary_id and e['type']=='boundary_cross']
                preservation.append({'type':'no_visible_boundary_cross','id':boundary_id,'pass':bool(relation) and not crossings and not candidate_events['unobservable']})
    measurement_ok=bool(rows) and all(r['gates']['coverage'] for r in rows) and not any(e['code']=='missing_object' for e in errors)
    passed=measurement_ok and not errors and all(r['pass'] for r in rows) and all(e['pass'] for e in event_checks+preservation+lifecycle_checks)
    return {'schema_version':1,'kind':'AnalysisResult','status':'pass' if passed else 'fail',
            'numerical_status':'pass' if passed else 'fail','measurement_status':'pass' if measurement_ok else 'fail',
            'manual_validation_status':'pending','success':passed,'object_windows':rows,'event_checks':event_checks,
            'preservation_checks':preservation,'lifecycle_checks':lifecycle_checks,'errors':errors,'reference_package_sha256':reference.get('package_sha256'),
            **({'censored_segments':censored_segments,'sampling_policy':'observed_response_v2'}
               if contract.get('sampling_policy') == 'observed_response_v2' else {}),
            'candidate_package_sha256':candidate.get('package_sha256'),'contract_sha256':digest(contract),
            'reference_observation_sha256':digest(reference),'candidate_observation_sha256':digest(candidate),
            'alignment':{'mode':contract.get('alignment',{}).get('mode','absolute'),'reference_origin_s':origins[0],'candidate_origin_s':origins[1]},
            'scope':'Pixel comparison only; numerical success does not grant independent manual, source or application acceptance.'}


def _compare_window(rt,rf,ct,cf,start,end,gap,scale,limits,minimum,*,endpoint_required=True):
    # Both real timelines prevent a fast excursion from hiding between samples.
    grid=sorted(set([start,end]+[t for t in rt+ct if start<t<end]))
    covered=0.;position_integral=0.;size_integral=0.;samples=[];offset_integral=[0.,0.]
    for t in grid:
        a,b=_sample(rt,rf,t,gap),_sample(ct,cf,t,gap)
        if a is None or b is None:samples.append(None);continue
        pe=((a['x']-b['x'])**2+(a['y']-b['y'])**2)/(scale*scale)
        se=((a['width']-b['width'])**2+(a['height']-b['height'])**2)/(scale*scale)
        samples.append((pe,se,(b['x']-a['x'])/scale,(b['y']-a['y'])/scale))
    for i in range(1,len(grid)):
        duration=grid[i]-grid[i-1]
        if samples[i-1] is None or samples[i] is None or duration>gap:continue
        covered+=duration
        position_integral+=duration*(samples[i-1][0]+samples[i][0])/2
        size_integral+=duration*(samples[i-1][1]+samples[i][1])/2
        for axis in (0,1):offset_integral[axis]+=duration*(samples[i-1][2+axis]+samples[i][2+axis])/2
    ratio=covered/(end-start)
    rmse=math.sqrt(position_integral/covered) if covered>0 else None
    size_rmse=math.sqrt(size_integral/covered) if covered>0 else None
    endpoint=math.sqrt(samples[-1][0]) if samples[-1] is not None else None
    gates={'coverage':ratio>=minimum,'position':rmse is not None and rmse<=limits['rmse_limit'],
           'size':size_rmse is not None and size_rmse<=limits['size_rmse_limit'],
           'endpoint':not endpoint_required or endpoint is not None and endpoint<=limits['endpoint_limit']}
    return {'time_window_s':[start,end], 'valid_time_ratio':ratio,'position_rmse':rmse,
            'size_rmse':size_rmse,'endpoint_error':endpoint,'endpoint_required':endpoint_required,'scale_px':scale,'limits':limits,
            'gates':gates,'pass':all(gates.values()),
            'mean_offset':[offset_integral[0]/covered,offset_integral[1]/covered] if covered>0 else None,
            **_window_diagnostics(rt,rf,ct,cf,start,end,gap,scale)}


def _window_diagnostics(rt,rf,ct,cf,start,end,gap,scale):
    """Feedback-only signals for one window (V6 plan WP3.1); no gate reads them.

    ``lag_s`` is the shift of the candidate clock within +-LAG_SEARCH_S that
    minimises the position error on a uniform grid (positive: the candidate moves
    later than the reference), with the error at that shift and at zero shift in
    units of ``scale_px``. ``trajectory`` holds TRAJECTORY_POINTS reference and
    candidate positions in pixels; a missing sample is null.
    """
    def uniform(count):
        return [start+(end-start)*k/(count-1) for k in range(count)]

    def mse(shift,grid):
        errors=[]
        for t in grid:
            a,b=_sample(rt,rf,t,gap),_sample(ct,cf,t+shift,gap)
            if a is not None and b is not None:
                errors.append(((a['x']-b['x'])**2+(a['y']-b['y'])**2)/(scale*scale))
        return sum(errors)/len(errors) if len(errors)>=max(4,.75*len(grid)) else None

    grid=uniform(LAG_GRID_POINTS)
    steps=int(round(LAG_SEARCH_S/LAG_STEP_S))
    scored=[(value,abs(k),k*LAG_STEP_S) for k in range(-steps,steps+1) for value in [mse(k*LAG_STEP_S,grid)] if value is not None]
    best=min(scored) if scored else None
    zero=mse(0.,grid)
    points=uniform(TRAJECTORY_POINTS)

    def position(times,frames,t):
        value=_sample(times,frames,t,gap)
        return None if value is None else [round(value['x'],1),round(value['y'],1)]
    return {'lag_s':None if best is None else round(best[2],3),
            'lag_position_rmse':None if best is None else math.sqrt(best[0]),
            'zero_lag_position_rmse':None if zero is None else math.sqrt(zero),
            'trajectory':{'t_s':[round(t,3) for t in points],'reference':[position(rt,rf,t) for t in points],
                          'candidate':[position(ct,cf,t) for t in points]}}
