"""Bounded agent feedback with lossless pixel evidence available separately."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from kiroclops.visual.schema import digest,write_json,file_sha256


def _sample(values,limit):
    if len(values)<=limit:return list(values)
    return [values[round(i*(len(values)-1)/(limit-1))] for i in range(limit)]


def _event(value):
    keys=('type','object_ids','relative_to_object_id','time_interval_s','timing_status',
          'uncertainty_s','definition','amplitude_px','peak_time_interval_s','peak_timing_status',
          'axis','axis_selection','motion_basis','endpoint_source','intended_target_inferred')
    return {k:value[k] for k in keys if k in value}


def _relations(values):
    groups=defaultdict(list)
    for row in values:
        key=(row['type'],row.get('connection_id'),row.get('object_id'),row.get('boundary_id'),
             tuple(row.get('object_ids',[])),row.get('relative_to_object_id'),row.get('event_type'))
        groups[key].append(row)
    result=[]
    for key,rows in groups.items():
        identity={k:rows[0][k] for k in ('type','connection_id','object_id','boundary_id','object_ids',
                                      'relative_to_object_id','event_type') if k in rows[0]}
        if key[0]=='relative_event_timing':
            result.extend({k:r[k] for k in ('type','object_ids','event_type','occurrence','lag_interval_s','order','timing_status') if k in r} for r in rows)
            continue
        ranges={}
        for field in ('distance_px','anchor_distance_px','edge_distance_px','overlap_area_px2','intersection_over_union','dx_px','dy_px'):
            numbers=[r[field] for r in rows if isinstance(r.get(field),(int,float))]
            if numbers:ranges[field]=[min(numbers),max(numbers)]
        endpoints=[v for r in rows for v in r.get('endpoint_distances_px',[]) if isinstance(v,(int,float))]
        if endpoints:ranges['endpoint_distances_px']=[min(endpoints),max(endpoints)]
        failures=[r for r in rows if r.get('visually_attached') is False or r.get('valid') is False]
        result.append({**identity,'sample_count':len(rows),'ranges':ranges,'invalid_or_detached_count':len(failures),
            'failure_examples':[{k:r[k] for k in ('index','time_s','reason','endpoint_distances_px','visually_attached') if k in r} for r in _sample(failures,4)]})
    return result


def motion_feedback(records,*,full_output=None):
    """No imputation or changed scoring; this only bounds the agent's context.

    Sixty-four actual samples per track span the clip. All exact observations
    and events remain in an optional, explicitly linked local JSON file.
    """
    profiles=[];full=[]
    for record in records:
        observations=record['observations'];events=record['events'];objects=[]
        for obj in observations['objects']:
            frames=obj['frames'];valid=[f for f in frames if f.get('valid')]
            # Preserve missing-data ranges independently of sampled trajectories.
            gaps=[]
            for f in frames:
                if f.get('expected_active') is False or f.get('valid'):continue
                if gaps and gaps[-1]['end_index']==f['index']-1:
                    gaps[-1].update(end_index=f['index'],end_s=f['time_s'])
                else:gaps.append({'start_index':f['index'],'end_index':f['index'],'start_s':f['time_s'],'end_s':f['time_s'],'reason':f.get('reason')})
            objects.append({'id':obj['id'],'valid_ratio':obj['valid_ratio'],'frame_count':len(frames),
                'trajectory':[{k:f[k] for k in ('index','time_s','x','y','width','height')} for f in _sample(valid,64)],
                'sampling':'At most 64 actual frame observations; no interpolation. Full evidence linked separately.',
                'invalid_ranges':gaps,'units':'natural pixels and seconds'})
        profiles.append({'scenario':record['scenario'],'objects':objects,
            'events':[_event(e) for e in events.get('events',[])],
            'relations':_relations(events.get('relations',[])),
            'unobservable_count':len(events.get('unobservable',[])),
            'unobservable_examples':_sample(events.get('unobservable',[]),6),
            'inputs':observations['inputs'],'observations_sha256':digest(observations),
            'events_sha256':digest(events),'source':'rendered_pixels_only'})
        full.append({'scenario':record['scenario'],'observations':observations,'events':events})
    result={'profiles':profiles,'inference_limit':'Observed response; original coefficients are not uniquely identified.',
        'feedback_is_summary':True,'motion_basis':'Selected visible anchors and independently measured geometry; no DOM/motor coordinates.'}
    if full_output is not None:
        full_output=Path(full_output);write_json(full_output,{'kind':'PixelCalibrationEvidence','records':full})
        result['full_pixel_evidence']={'path':str(full_output),'sha256':file_sha256(full_output),
            'description':'Lossless measured positions, invalid frames, events and visible relationships. Read if the summary is insufficient.'}
    return result
