"""Game-agnostic events from visible geometry, never engine or DOM state."""
from __future__ import annotations

import math
import statistics
from itertools import combinations

from .schema import VisualError, digest

LEGACY_MOTION_START_POLICY = {
    'version': 'observed-rest-band-departure-v2',
    'definition': 'first_observed_2d_departure_strictly_beyond_rest_band',
    'instantaneous_speed_required': False,
    'speed_threshold_scope': 'direction_and_reversal_not_motion_start',
    'physical_motion_onset_inferred': False,
    'rest_meaning': 'sustained_bounded_geometry_not_physical_zero_velocity',
}
MOTION_START_POLICY = {
    **LEGACY_MOTION_START_POLICY,
    'version': 'observed-confirmed-rest-departure-v3',
    'next_departure_anchor': 'last_image_of_confirmed_rest',
    'relative_order_resolution': 'max_50ms_or_1.5_median_frame_interval_capped_100ms',
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _frames(obj):
    frames = obj.get('frames', [])
    previous = -float('inf')
    for i, frame in enumerate(frames):
        if frame.get('index') != i or not _finite(frame.get('time_s')) or frame['time_s'] <= previous:
            raise VisualError('capture_timing_failure', 'Event input requires ordered real frame times')
        previous = frame['time_s']
        if frame.get('valid') is True and not all(_finite(frame.get(k)) for k in ('x', 'y', 'width', 'height')):
            raise VisualError('invalid_observation', 'Valid observations must have finite visible geometry')
    return frames


def _event(kind, ids, start, end, frames, **extra):
    return {'type': kind, 'object_ids': ids, 'time_interval_s': [float(start), float(end)],
            'frame_indices': frames, 'source': 'pixels_only', **extra}


def _point_segment(point, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    squared = dx*dx+dy*dy
    if squared <= 0:
        raise VisualError('invalid_selection', 'Boundary segment must have length')
    t = max(0., min(1., ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/squared))
    nearest = (a[0]+t*dx, a[1]+t*dy)
    signed = ((point[0]-a[0])*dy-(point[1]-a[1])*dx)/math.sqrt(squared)
    return math.hypot(point[0]-nearest[0], point[1]-nearest[1]), signed


def _box(frame):
    left = frame.get('left', frame['x']-frame['width']/2)
    top = frame.get('top', frame['y']-frame['height']/2)
    if not _finite(left) or not _finite(top) or frame['width'] <= 0 or frame['height'] <= 0:
        raise VisualError('invalid_observation', 'Visible box must be finite and positive')
    return float(left), float(top), float(left+frame['width']), float(top+frame['height'])


def _boundary_geometry(frame, points):
    left, top, right, bottom = _box(frame)
    center = ((left+right)/2, (top+bottom)/2)
    corners = [(left,top),(right,top),(right,bottom),(left,bottom)]
    choices = []
    for a,b in zip(points, points[1:]):
        corner_values = [_point_segment(p,a,b) for p in corners]
        center_value = _point_segment(center,a,b)
        # Distance is between visible rectangle and the finite segment, not an
        # imaginary infinite wall. Endpoints inside the box have zero distance.
        distances = [v[0] for v in corner_values]
        for endpoint in (a,b):
            dx=max(left-endpoint[0],0.,endpoint[0]-right)
            dy=max(top-endpoint[1],0.,endpoint[1]-bottom)
            distances.append(math.hypot(dx,dy))
        signs=[v[1] for v in corner_values]
        intersects_line=min(signs)<=0<=max(signs)
        segment_bbox_overlaps=(max(a[0],b[0])>=left and min(a[0],b[0])<=right and max(a[1],b[1])>=top and min(a[1],b[1])<=bottom)
        distance=0. if intersects_line and segment_bbox_overlaps else min(distances)
        choices.append((distance,center_value[1]))
    return min(choices,key=lambda v:v[0])


def _endpoint_distance(point, frame):
    left, top, right, bottom = _box(frame)
    x,y=point
    if left<=x<=right and top<=y<=bottom:
        return min(x-left,right-x,y-top,bottom-y)
    return math.hypot(max(left-x,0.,x-right),max(top-y,0.,y-bottom))


def _input_transitions(inputs):
    """Preserve commands in every domain; repeated held-state receipts are not changes."""
    states={};changes=[]
    for event in inputs:
        if not isinstance(event,dict) or not _finite(event.get('time_s')) or not isinstance(event.get('action'),str):continue
        pressed=event.get('pressed')
        if type(pressed) is bool:
            key=(event['action'],event.get('selector'),event.get('key'),event.get('button'))
            if key in states and states[key] is pressed:continue
            states[key]=pressed
        changes.append(event)
    return changes


def _motion_axis(frames, requested):
    if requested not in (None,'auto','x','y'):
        raise VisualError('invalid_contract','Event axis must be auto, x or y')
    if requested in ('x','y'):return requested,'explicit'
    valid=[f for f in frames if f.get('valid') and f.get('expected_active') is not False]
    ranges={k:max((f[k] for f in valid),default=0)-min((f[k] for f in valid),default=0) for k in ('x','y')}
    return ('y' if ranges['y']>ranges['x'] else 'x'),'observed_dominant_displacement_range'


def _motion_events(frames, ids, values, axis, inputs, axis_selection='explicit', policy=None):
    policy = policy or MOTION_START_POLICY
    events=[];episodes=[];previous=None;active=False;quiet=[];episode=[];direction=0;extreme=None;turn=None
    radius=values['rest_radius_px'];commands=_input_transitions(inputs)
    for frame in frames:
        if (not frame.get('valid') or frame.get('expected_active') is False or
                previous is not None and frame['time_s']-previous['time_s']>values['max_gap_s']):
            if active:episodes.append({'object_ids':ids,'endpoint_observed':False,'reason':'observation_gap'})
            previous=None;active=False;quiet=[];episode=[];direction=0;extreme=None;turn=None
            if not frame.get('valid') or frame.get('expected_active') is False:continue
        if previous is None:
            previous=frame;quiet=[frame];continue
        dt=frame['time_s']-previous['time_s']
        speed=math.hypot(frame['x']-previous['x'],frame['y']-previous['y'])/dt
        signed=(frame[axis]-previous[axis])/dt
        current=1 if signed>values['speed_threshold_px_s'] else -1 if signed < -values['speed_threshold_px_s'] else 0
        if not active:
            anchor=quiet[0]
            departure=math.hypot(frame['x']-anchor['x'],frame['y']-anchor['y'])
            # This event describes a visible spatial departure. Differentiating
            # adjacent noisy points adds an unrelated, cadence-sensitive gate:
            # subpixel disagreement around 5 px/s can shift an entire episode.
            # Preserve actual crossing brackets; infer no subframe onset.
            if departure>radius:
                events.append(_event('motion_start',ids,previous['time_s'],frame['time_s'],[previous['index'],frame['index']],
                                     definition='visible_departure_beyond_rest_band',rest_radius_px=radius,
                                     predicate_version=policy['version'],
                                     rest_anchor_frame_index=anchor['index'],rest_anchor_time_s=anchor['time_s'],
                                     rest_anchor_px=[anchor['x'],anchor['y']],departure_distance_px=departure,
                                     departure_comparison='strictly_greater_than_rest_radius',
                                     observed_pair_speed_px_s=speed,instantaneous_speed_required=False,
                                     physical_motion_onset_inferred=False,
                                     start_context='observed_rest' if previous['time_s']-quiet[0]['time_s']>=values['rest_duration_s']-1e-9 else 'clip_or_gap_censored'))
                active=True;episode=[previous,frame];quiet=[];direction=current;extreme=frame;turn=None
            else:quiet.append(frame)
        else:
            episode.append(frame)
            if current:
                if not direction:direction=current;extreme=frame
                elif current==direction:
                    if direction*(frame[axis]-extreme[axis])>=0:extreme=frame
                    turn=None
                else:
                    if turn is None:turn=(previous,frame)
                    if direction*(extreme[axis]-frame[axis])>radius:
                        events.append(_event('reversal',ids,turn[0]['time_s'],turn[1]['time_s'],[turn[0]['index'],turn[1]['index']],
                                             axis=axis,confirmed_at_s=frame['time_s'],confirmed_frame_index=frame['index'],
                                             minimum_displacement_px=radius))
                        direction=current;extreme=frame;turn=None
            if not quiet:quiet=[previous]
            quiet.append(frame)
            # A short repeated image is not a stop. Sustained bounded geometry
            # is required, even when quantization alternates zero/nonzero speed.
            while len(quiet)>1 and math.hypot(max(f['x'] for f in quiet)-min(f['x'] for f in quiet),
                                              max(f['y'] for f in quiet)-min(f['y'] for f in quiet))>radius:
                quiet.pop(0)
            if len(quiet)>1 and quiet[-1]['time_s']-quiet[0]['time_s']>=values['rest_duration_s']-1e-9:
                onset=quiet[:2];support=[f['index'] for f in quiet]
                extra={'confirmed_at_s':frame['time_s'],'confirmed_frame_index':frame['index'],
                       'support_frame_indices':support,'support_interval_s':[quiet[0]['time_s'],quiet[-1]['time_s']],
                       'rest_duration_s':quiet[-1]['time_s']-quiet[0]['time_s'],
                       'rest_radius_px':radius}
                for kind in ('stop','settled'):
                    events.append(_event(kind,ids,onset[0]['time_s'],onset[1]['time_s'],[f['index'] for f in onset],**extra))
                endpoint=[statistics.median(f[k] for f in quiet) for k in ('x','y')]
                inside=[e for e in commands if episode[1]['time_s']<e['time_s']<quiet[0]['time_s']]
                record={'object_ids':ids,'start_s':episode[0]['time_s'],'end_s':quiet[0]['time_s'],
                        'endpoint_observed':True,'endpoint_px':endpoint,'endpoint_source':'observed_stable_tail',
                        'support_frame_indices':support,'interrupted_by_recorded_input':bool(inside),
                        'interrupting_inputs':[{k:e[k] for k in ('time_s','action','pressed','selector') if k in e} for e in inside]}
                episodes.append(record)
                if not inside:events.extend(_endpoint_events(episode,quiet,endpoint,ids,values))
                active=False;episode=[];direction=0;extreme=None;turn=None
                if policy['version'] == MOTION_START_POLICY['version']:
                    # The old anchor can already be almost one rest radius
                    # behind this confirmation image. Keeping it manufactures
                    # an immediate restart from tiny residual raster drift.
                    quiet=[frame]
        previous=frame
    if active:episodes.append({'object_ids':ids,'endpoint_observed':False,'reason':'clip_ended_before_observed_rest'})
    for item in events+episodes:
        item.update(axis=axis,axis_selection=axis_selection,
                    motion_basis='relative_tracked_anchors' if len(ids)>1 else 'tracked_anchor',
                    rigid_body_translation_inferred=False,
                    motion_scope='Visible selected image anchors; sprite pose can move an anchor without locomotion.')
    return events,episodes


def _endpoint_events(frames,quiet,endpoint,ids,values):
    """Retrospective excursion around a visible stable tail, never a hidden goal."""
    first=frames[0];dx,dy=endpoint[0]-first['x'],endpoint[1]-first['y'];length=math.hypot(dx,dy)
    if length>values['rest_radius_px']:unit=(dx/length,dy/length);basis='start_to_observed_endpoint'
    else:
        axis=max(('x','y'),key=lambda k:max(f[k] for f in frames)-min(f[k] for f in frames))
        unit=(1.,0.) if axis=='x' else (0.,1.);basis='dominant_observed_axis'
    values_at=[(f['x']-endpoint[0])*unit[0]+(f['y']-endpoint[1])*unit[1] for f in frames]
    events=[];extra={'endpoint_px':endpoint,'endpoint_source':'observed_stable_tail',
        'support_frame_indices':[f['index'] for f in quiet],'confirmed_at_s':quiet[-1]['time_s'],
        'support_interval_s':[quiet[0]['time_s'],quiet[-1]['time_s']],
        'projection':list(unit),'projection_basis':basis,'intended_target_inferred':False}
    peak=max(range(len(frames)),key=lambda i:values_at[i])
    if length>values['rest_radius_px'] and values_at[peak]>values['overshoot_min_px']:
        onset=next(i for i,value in enumerate(values_at) if value>values['overshoot_min_px'])
        onset_a,onset_b=frames[max(0,onset-1)],frames[onset]
        last_peak=peak
        while last_peak+1<len(frames) and abs(values_at[last_peak+1]-values_at[peak])<=1e-9:last_peak+=1
        a,b=frames[max(0,peak-1)],frames[min(len(frames)-1,last_peak+1)]
        peak_uncertainty=b['time_s']-a['time_s']
        events.append(_event('overshoot',ids,onset_a['time_s'],onset_b['time_s'],[onset_a['index'],onset_b['index']],
                             definition='first_visible_excursion_beyond_observed_endpoint_band',
                             onset_threshold_px=values['overshoot_min_px'],
                             amplitude_px=values_at[peak],peak_frame_index=frames[peak]['index'],
                             peak_time_interval_s=[a['time_s'],b['time_s']],peak_uncertainty_s=peak_uncertainty,
                             peak_timing_status='resolved' if peak_uncertainty<=values['max_event_uncertainty_s']+1e-9 else 'unresolved',
                             peak_sample_interval_s=[frames[peak]['time_s'],frames[last_peak]['time_s']],
                             peak_support_frame_indices=[f['index'] for f in frames[peak:last_peak+1]],
                             physical_peak_time_inferred=False,**extra))
    # Ignore the initial approach side. Alternation after the first far-side
    # excursion is visible ringing only when two meaningful crossings occur.
    signs=[]
    for index,value in enumerate(values_at):
        sign=1 if value>values['overshoot_min_px'] else -1 if value < -values['overshoot_min_px'] else 0
        if not sign:continue
        if not signs and sign<0 and length>values['rest_radius_px']:continue
        if not signs or signs[-1][0]!=sign:signs.append((sign,index))
    if len(signs)>=3:
        first_index=signs[0][1];a,b=frames[max(0,first_index-1)],frames[first_index]
        support={**extra,'support_interval_s':[b['time_s'],frames[signs[-1][1]]['time_s']],
                 'support_frame_indices':[f['index'] for f in frames[first_index:]]}
        events.append(_event('oscillation',ids,a['time_s'],b['time_s'],[a['index'],b['index']],
                             lobe_frame_indices=[frames[i]['index'] for _,i in signs],sign_changes=len(signs)-1,
                             peak_to_peak_px=max(values_at[first_index:])-min(values_at[first_index:]),**support))
    return events


def extract_events(observations, selection=None, contract=None):
    """Produce motion events and visible boundary/connection relations.

    contract event_options: speed_threshold_px_s (5, direction/reversal only), axis ('auto'|'x'|'y'),
    max_gap_s (.12), rest_duration_s (.2), rest_radius_px (1.5),
    boundary_tolerance_px (2), connection_tolerance_px (2), overshoot_min_px (1.5),
    max_event_uncertainty_s (.1). Relative child motion is requested by
    selection.relative_motion[{object_id,relative_to,axis?}]. Unspecified axes
    use each absolute/relative track's dominant observed displacement range.
    Explicit axes remain fixed. Stable-tail endpoint
    events describe observations, not an unseen goal or physical collision.
    A gap resets state; it never supplies a zero-velocity sample.
    Motion start is spatial departure beyond the observed rest band, not
    physical actuation or an instantaneous speed threshold. Bounded slow creep
    can produce multiple rest/departure episodes. A near-band crossing still
    needs independent image review; decimal coordinates are no accuracy bound.
    """
    contract = contract or {}
    options = contract.get('event_options', {})
    if not isinstance(options, dict):
        raise VisualError('invalid_contract', 'event_options must be an object')
    policy_id=options.get('motion_policy',MOTION_START_POLICY['version'])
    policies={p['version']:p for p in (MOTION_START_POLICY,LEGACY_MOTION_START_POLICY)}
    if policy_id not in policies:
        raise VisualError('invalid_contract','Unknown observed motion policy')
    policy=policies[policy_id]
    values={'speed_threshold_px_s':5.,'max_gap_s':.12,'rest_duration_s':.2,'rest_radius_px':1.5,
            'boundary_tolerance_px':2.,'connection_tolerance_px':2.,'overshoot_min_px':1.5,
            'max_event_uncertainty_s':.1}
    for key,default in list(values.items()):
        value=options.get(key,default)
        if not _finite(value) or value<=0:
            raise VisualError('invalid_contract', f'{key} must be positive and finite')
        values[key]=float(value)
    axis=options.get('axis','auto')
    if axis not in ('auto','x','y'):
        raise VisualError('invalid_contract','Event axis must be auto, x or y')
    selection=selection or observations.get('selection',{})
    boundaries=selection.get('boundaries',[])
    boundary_ids=set()
    for boundary in boundaries:
        points=boundary.get('points',[])
        if not isinstance(boundary.get('id'),str) or boundary['id'] in boundary_ids or len(points)<2 or any(not isinstance(p,(list,tuple)) or len(p)!=2 or not all(_finite(v) for v in p) for p in points) or any(a==b for a,b in zip(points,points[1:])):
            raise VisualError('invalid_selection','Boundary needs unique ID and finite nonzero segments')
        boundary_ids.add(boundary['id'])
    objects=observations.get('objects',[])
    ids=[o.get('id') for o in objects]
    if not objects or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
        raise VisualError('invalid_observation','Unique observed object IDs required')
    lookup={o['id']:_frames(o) for o in objects}
    events=[];relations=[];unobservable=[];episodes=[]
    for oid,frames in lookup.items():
        chosen,basis=_motion_axis(frames,axis)
        motion,observed_episodes=_motion_events(frames,[oid],values,chosen,observations.get('inputs',{}).get('events',[]),basis,policy)
        events.extend(motion);episodes.extend(observed_episodes)
        previous=None
        boundary_previous={};boundary_sides={}
        for f in frames:
            if f.get('expected_active') is False:
                previous=None;boundary_previous={};boundary_sides={}
                continue
            if not f.get('valid'):
                previous=None;boundary_previous={};boundary_sides={}
                unobservable.append({'object_id':oid,'index':f['index'],'reason':'invalid_geometry'})
                continue
            if previous is not None and f['time_s']-previous['time_s']>values['max_gap_s']:
                previous=None;boundary_previous={};boundary_sides={}
                unobservable.append({'object_id':oid,'index':f['index'],'reason':'temporal_gap'})
            for boundary in boundaries:
                if boundary.get('object_ids') and oid not in boundary['object_ids']:continue
                distance,signed=_boundary_geometry(f,boundary['points'])
                touching=distance<=values['boundary_tolerance_px']
                relations.append({'type':'boundary_distance','object_id':oid,'boundary_id':boundary['id'],'index':f['index'],'time_s':f['time_s'],'distance_px':distance,'signed_center_distance_px':signed,'visually_touching':touching,'source':'pixels_only'})
                old=boundary_previous.get(boundary['id'])
                if old:
                    old_f,old_touch,old_signed=old
                    if touching!=old_touch:
                        events.append(_event('boundary_enter' if touching else 'boundary_leave',[oid],old_f['time_s'],f['time_s'],[old_f['index'],f['index']],boundary_id=boundary['id']))
                tolerance=values['boundary_tolerance_px']
                if abs(signed)>tolerance:
                    prior_side=boundary_sides.get(boundary['id'])
                    if prior_side and prior_side[1]*signed<0:
                        # Retain the last unambiguous side through contact frames,
                        # including an exact zero; do not lose slow crossings.
                        prior_f=prior_side[0]
                        if distance<=max(f['width'],f['height']) or (old and old[1]):
                            events.append(_event('boundary_cross',[oid],prior_f['time_s'],f['time_s'],[prior_f['index'],f['index']],boundary_id=boundary['id']))
                    boundary_sides[boundary['id']]=(f,signed)
                boundary_previous[boundary['id']]=(f,touching,signed)
            previous=f
    relative_specs=selection.get('relative_motion',[])
    relative_ids=set()
    for spec in relative_specs:
        oid,parent=spec.get('object_id'),spec.get('relative_to')
        if oid not in lookup or parent not in lookup or oid==parent or oid in relative_ids:
            raise VisualError('invalid_selection','Relative motion needs a distinct observed child and anchor')
        relative_ids.add(oid)
        from .comparison import relative_object
        obj=relative_object({'id':oid,'frames':lookup[oid]},{'id':parent,'frames':lookup[parent]})
        chosen,basis=_motion_axis(obj['frames'],spec.get('axis',axis))
        motion,observed_episodes=_motion_events(obj['frames'],[oid,parent],values,chosen,observations.get('inputs',{}).get('events',[]),basis,policy)
        for event in motion:event.update(motion_reference='relative',relative_to_object_id=parent)
        for episode in observed_episodes:episode.update(motion_reference='relative',relative_to_object_id=parent)
        events.extend(motion);episodes.extend(observed_episodes)
        for frame in obj['frames']:
            if frame.get('expected_active') is False:continue
            if frame['valid']:
                relations.append({'type':'relative_anchor','object_ids':[oid,parent],'index':frame['index'],
                    'time_s':frame['time_s'],'dx_px':frame['x'],'dy_px':frame['y'],'source':'pixels_only'})
            else:unobservable.append({'object_ids':[oid,parent],'index':frame['index'],'reason':'relative_anchor_unobservable'})
    for aid,bid in combinations(lookup,2):
        a_frames,b_frames=lookup[aid],lookup[bid]
        if len(a_frames)!=len(b_frames) or any(a['time_s']!=b['time_s'] for a,b in zip(a_frames,b_frames)):
            raise VisualError('capture_timing_failure','Object relations require synchronized native image times')
        previous=None
        for a,b in zip(a_frames,b_frames):
            if a.get('expected_active') is False or b.get('expected_active') is False:
                previous=None;continue
            if not a.get('valid') or not b.get('valid'):
                unobservable.append({'object_ids':[aid,bid],'index':a['index'],'reason':'pair_geometry_unobservable'})
                previous=None;continue
            al,at,ar,ab=_box(a);bl,bt,br,bb=_box(b)
            width=max(0.,min(ar,br)-max(al,bl));height=max(0.,min(ab,bb)-max(at,bt))
            area=width*height;union=(ar-al)*(ab-at)+(br-bl)*(bb-bt)-area;overlap=area>0
            distance=math.hypot(max(al-br,bl-ar,0.),max(at-bb,bt-ab,0.))
            relations.append({'type':'object_pair_geometry','object_ids':[aid,bid],'index':a['index'],'time_s':a['time_s'],
                'anchor_distance_px':math.hypot(a['x']-b['x'],a['y']-b['y']),
                'edge_distance_px':distance,'overlap_area_px2':area,'intersection_over_union':area/union,
                'visually_overlapping':overlap,'source':'pixels_only','physical_contact_inferred':False})
            if previous and a['time_s']-previous[0]['time_s']<=values['max_gap_s'] and overlap!=previous[1]:
                events.append(_event('overlap_enter' if overlap else 'overlap_leave',[aid,bid],previous[0]['time_s'],a['time_s'],[previous[0]['index'],a['index']]))
            previous=(a,overlap)
    for connection in observations.get('connections',[]):
        from_id,to_id=connection.get('from_object'),connection.get('to_object')
        if from_id not in lookup or to_id not in lookup:
            raise VisualError('invalid_observation','Connection refers to unknown object')
        previous=None
        for f in connection.get('frames',[]):
            if f.get('expected_active') is False:
                previous=None;continue
            index=f.get('index')
            if type(index) is not int or not 0<=index<min(len(lookup[from_id]),len(lookup[to_id])):
                raise VisualError('invalid_observation','Connection frame index differs')
            a,b=lookup[from_id][index],lookup[to_id][index]
            if f.get('time_s')!=a['time_s'] or f.get('time_s')!=b['time_s']:
                raise VisualError('capture_timing_failure','Connection and object times differ')
            if not f.get('valid') or not a.get('valid') or not b.get('valid'):
                unobservable.append({'connection_id':connection.get('id'),'index':index,'reason':'connection_unobservable'})
                previous=None;continue
            for key in ('start','end'):
                if not isinstance(f.get(key),(list,tuple)) or len(f[key])!=2 or not all(_finite(v) for v in f[key]):
                    raise VisualError('invalid_observation','Visible endpoints must be finite')
            distances=[_endpoint_distance(f['start'],a),_endpoint_distance(f['end'],b)]
            # PHASE3D_V5B_PLAN.md 3.1: an end the measurement flags as covered by a
            # painted occluder (focus ring) is attached; other ends keep the tolerance.
            occluded=[f.get('start_occluded') is True,f.get('end_occluded') is True]
            attached=all(o or d<=values['connection_tolerance_px'] for o,d in zip(occluded,distances))
            relations.append({'type':'connection_attachment','connection_id':connection['id'],'index':index,'time_s':f['time_s'],'endpoint_distances_px':distances,'occluded_ends':occluded,'visually_attached':attached,'source':'pixels_only'})
            if not attached and (previous is None or previous.get('attached')):
                events.append(_event('connection_detached',[from_id,to_id],f['time_s'] if previous is None else previous['time_s'],f['time_s'],[index],connection_id=connection['id']))
            if attached and previous is not None and not previous['attached']:
                events.append(_event('connection_attached',[from_id,to_id],previous['time_s'],f['time_s'],[index],connection_id=connection['id']))
            previous={'time_s':f['time_s'],'attached':attached}
    motion_tracks={}
    for event in events:
        if event['type'] in ('motion_start','stop','settled','reversal'):
            key=(event['object_ids'][0],event.get('relative_to_object_id'))
            motion_tracks.setdefault(key,{}).setdefault(event['type'],[]).append(event)
    for (aid,ap),(bid,bp) in combinations(motion_tracks,2):
        if ap!=bp:continue # Absolute and relative clocks describe different motion.
        cadence=[b['time_s']-a['time_s'] for oid in (aid,bid)
                 for a,b in zip(lookup[oid],lookup[oid][1:])
                 if 0<b['time_s']-a['time_s']<=values['max_gap_s']]
        resolution=(min(.1,max(.05,1.5*statistics.median(cadence))) if cadence else .1) if policy is MOTION_START_POLICY else 0.
        for kind in ('motion_start','stop','settled','reversal'):
            left=motion_tracks[aid,ap].get(kind,[]);right=motion_tracks[bid,bp].get(kind,[])
            for ordinal,(a,b) in enumerate(zip(left,right)):
                aa,ab=a['time_interval_s'];ba,bb=b['time_interval_s']
                relations.append({'type':'relative_event_timing','event_type':kind,'object_ids':[aid,bid],
                    'relative_to_object_id':ap,'occurrence':ordinal,'lag_interval_s':[ba-ab,bb-aa],
                    'order':'before' if ba-ab>resolution else 'after' if bb-aa < -resolution else 'overlapping_uncertainty',
                    **({'order_resolution_s':resolution,
                        'order_definition':'lag_interval_excludes_observation_resolution_band'} if policy is MOTION_START_POLICY else {}),
                    'reference_frame_indices':a['frame_indices'],'candidate_frame_indices':b['frame_indices'],
                    'uncertainty_s':(ab-aa)+(bb-ba),
                    'timing_status':'resolved' if (ab-aa)+(bb-ba)<=values['max_event_uncertainty_s']+1e-9 else 'unresolved',
                    'source':'pixels_only','causal_dependency_inferred':False})
    for event in events:
        uncertainty=event['time_interval_s'][1]-event['time_interval_s'][0]
        event.update(uncertainty_s=uncertainty,timing_status='resolved' if uncertainty<=values['max_event_uncertainty_s']+1e-9 else 'unresolved')
    return {'schema_version':1,'kind':'EventSet','status':'partial' if unobservable else 'ok',
            'motion_start_policy':dict(policy),
            'package_sha256':observations.get('package_sha256'),'observation_sha256':digest(observations),
            'events':sorted(events,key=lambda e:(e['time_interval_s'][0],e['type'],e['object_ids'])),
            'relations':relations,'motion_episodes':episodes,'unobservable':unobservable,'options':{**values,'axis':axis},
            'scope':'Visible motion and geometry only; visual proximity is not proof of physics contact or graph semantics.'}
