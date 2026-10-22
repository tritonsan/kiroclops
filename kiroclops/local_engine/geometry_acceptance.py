"""SVG preservation gates over measured outer boxes and visible stroke ends.

These checks consume original pixel observations, never DOM coordinates or
comparison-relative icon positions. Missing geometry cannot certify a gate.
"""
from itertools import combinations
import math

from kiroclops.visual.schema import VisualError, digest


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def validate_svg_selection(selection, *, expected_ids=None):
    """Require an explicit, complete visible graph and independently selected boxes."""
    objects=selection.get('objects',[])
    if not isinstance(objects,list) or any(not isinstance(obj,dict) or not isinstance(obj.get('id'),str) for obj in objects):
        raise VisualError('needs_connection_binding','Select the visible SVG objects before their graph.')
    ids={obj.get('id') for obj in objects}
    if len(ids)!=len(objects) or len(ids)<2 or (expected_ids is not None and ids!=set(expected_ids)):
        raise VisualError('needs_connection_binding','Select every visible SVG node and its correspondence.')
    for obj in objects:
        geometry=obj.get('geometry',{})
        if not isinstance(geometry,dict):raise VisualError('needs_geometry_binding','Expected a measured outer contour selection.')
        if obj.get('method')!='contour' and (geometry.get('method')!='contour' or not geometry.get('roi')):
            raise VisualError('needs_geometry_binding','SVG nodes need separately selected outer contours; an icon patch is not a node box.')
    connections=selection.get('connections',[])
    if not isinstance(connections,list) or not connections:
        raise VisualError('needs_connection_binding','Select the visible SVG connections before transfer.')
    seen=set();pairs=set();covered=set()
    for edge in connections:
        if not isinstance(edge,dict):raise VisualError('invalid_connection_binding','Expected a visible connection object.')
        cid=edge.get('id');a=edge.get('from_object');b=edge.get('to_object')
        if not isinstance(cid,str) or not cid or cid in seen or not isinstance(a,str) or not isinstance(b,str) or a not in ids or b not in ids or a==b or frozenset((a,b)) in pairs:
            raise VisualError('invalid_connection_binding','Connections need unique IDs and distinct selected endpoints.')
        seen.add(cid);pairs.add(frozenset((a,b)));covered.update((a,b))
        method=edge.get('method','line')
        if method not in ('line','polyline'):
            raise VisualError('invalid_connection_binding','Use line or polyline visible connections.')
        if method=='polyline':
            rgb=edge.get('stroke_rgb')
            if not isinstance(rgb,list) or len(rgb)!=3 or any(type(v) is not int or not 0<=v<=255 for v in rgb):
                raise VisualError('needs_connection_binding','Select the polyline stroke color from a raw frame.')
            for key,default,maximum in (('stroke_tolerance',12,40),('search_margin_px',64,1920)):
                value=edge.get(key,default)
                if not _finite(value) or not 0<=value<=maximum:
                    raise VisualError('invalid_connection_binding',f'Invalid {key}.')
    if covered!=ids:
        raise VisualError('needs_connection_binding','Every selected SVG node needs its visible connection binding.')
    return connections


def _box(obj, frame):
    if not frame or not frame.get('valid') or (frame.get('geometry_method')!='contour' and obj.get('method')!='contour'):
        return None
    values=[frame.get(k) for k in ('left','top','width','height')]
    if not all(_finite(v) for v in values) or min(values[2:])<=0:
        return None
    left,top,width,height=values
    return left,top,left+width,top+height


def _distance(point, box):
    if not isinstance(point,(list,tuple)) or len(point)!=2 or not all(_finite(v) for v in point):return None
    x,y=point;left,top,right,bottom=box
    if left<=x<=right and top<=y<=bottom:
        return min(x-left,right-x,y-top,bottom-y)
    return math.hypot(max(left-x,0,x-right),max(top-y,0,y-bottom))


def svg_preservation(reference, candidate, connections):
    """Check final non-overlap and every required visible connection sample.

    Final means the last recorded candidate frame, never its last valid frame.
    Which nodes remain is fixed by the reference's final expected lifecycle.
    """
    refs={o['id']:o for o in reference.get('objects',[])}
    targets={o['id']:o for o in candidate.get('objects',[])}
    errors=[];pairs=[];edges=[]
    if not refs or not targets:
        errors.append({'code':'missing_node_geometry'})
    if set(refs)!=set(targets):errors.append({'code':'unbound_node_geometry'})
    reference_end=max((f['time_s'] for o in refs.values() for f in o.get('frames',[])),default=None)
    candidate_end=max((f['time_s'] for o in targets.values() for f in o.get('frames',[])),default=None)
    expected=[];boxes={}
    for oid,obj in refs.items():
        frames=obj.get('frames',[]);last=frames[-1] if frames else None
        if last is None or last['time_s']!=reference_end:
            errors.append({'code':'incomplete_reference_timeline','object_id':oid});continue
        if last.get('expected_active') is False:continue
        expected.append(oid)
        if _box(obj,last) is None:
            errors.append({'code':'reference_outer_geometry_unavailable','object_id':oid})
        target=targets.get(oid);actual=target.get('frames',[]) if target else []
        final=actual[-1] if actual else None
        box=_box(target,final) if target else None
        if box is None or final['time_s']!=candidate_end or final.get('expected_active') is False:
            errors.append({'code':'final_outer_geometry_unavailable','object_id':oid});continue
        boxes[oid]=box
    if len(expected)<2:
        errors.append({'code':'insufficient_final_nodes'})
    for a,b in combinations(expected,2):
        if a not in boxes or b not in boxes:
            pairs.append({'object_ids':[a,b],'pass':False,'reason':'outer_geometry_unavailable'});continue
        x,y=boxes[a],boxes[b]
        area=max(0,min(x[2],y[2])-max(x[0],y[0]))*max(0,min(x[3],y[3])-max(x[1],y[1]))
        pairs.append({'object_ids':[a,b],'overlap_area_px2':area,'pass':area==0})
    final_pass=bool(pairs) and not errors and all(row['pass'] for row in pairs)
    observed={edge['id']:edge for edge in candidate.get('connections',[])}
    unobservable=bool(errors)
    violations=any('overlap_area_px2' in row and not row['pass'] for row in pairs)
    if not connections:
        errors.append({'code':'required_connections_missing'})
        unobservable=True
    for spec in connections:
        cid=spec['id'];a=spec['from_object'];b=spec['to_object'];edge=observed.get(cid)
        row={'id':cid,'from_object':a,'to_object':b,'pass':False,'samples':[]}
        if not edge or (edge.get('from_object'),edge.get('to_object'))!=(a,b) or a not in targets or b not in targets:
            row['reason']='connection_missing_or_wrong_binding';edges.append(row);unobservable=True;continue
        aa={f['index']:f for f in targets[a]['frames']};bb={f['index']:f for f in targets[b]['frames']}
        ef={f['index']:f for f in edge.get('frames',[])}
        if len(ef)!=len(edge.get('frames',[])):
            row['reason']='duplicate_connection_frames';edges.append(row);unobservable=True;continue
        for index in sorted(set(aa)|set(bb)):
            fa,fb=aa.get(index),bb.get(index)
            if fa and fb and (fa.get('expected_active') is False or fb.get('expected_active') is False):continue
            f=ef.get(index);ba=_box(targets[a],fa);bc=_box(targets[b],fb)
            item={'index':index,'pass':False}
            if (not f or not f.get('valid') or not ba or not bc or fa['time_s']!=fb['time_s'] or f.get('time_s')!=fa['time_s']):
                item['reason']='visible_connection_or_outer_geometry_unavailable'
                unobservable=True
            else:
                distances=[_distance(f.get('start'),ba),_distance(f.get('end'),bc)]
                tolerances=[max(2.,(box[2]-box[0])*.01) for box in (ba,bc)]
                # V3: an end covered by a painted occluder (focus ring) that
                # touches its node is unobservable, not a detachment. A covered
                # end still needs its measured visible end. V5: the ring-aware
                # production reader (tracking axis_band_v3_ring_aware) also sets
                # these flags; the independent raw review checks every such
                # production decision against the raw reader's own.
                occluded=[f.get('start_occluded') is True,f.get('end_occluded') is True]
                item.update(endpoint_distances_px=distances,endpoint_limits_px=tolerances)
                if any(occluded):item['occluded_ends']=occluded
                item['pass']=all(d is not None and (hidden or d<=limit)
                                 for d,limit,hidden in zip(distances,tolerances,occluded))
                if any(d is None for d in distances):unobservable=True
                elif not item['pass']:violations=True
            row['samples'].append(item)
        row['pass']=bool(row['samples']) and all(item['pass'] for item in row['samples'])
        if not row['samples']:
            row['reason']='required_connection_never_observed';unobservable=True
        edges.append(row)
    return {'status':'pass' if final_pass and connections and all(row['pass'] for row in edges) else 'fail',
            'pass':bool(final_pass and connections and all(row['pass'] for row in edges)),
            'final_no_overlap':{'pass':final_pass,'time_s':candidate_end,'expected_object_ids':expected,'pairs':pairs},
            'visible_connections':edges,'errors':errors,
            'measurement_status':'insufficient_observability' if unobservable else 'ok',
            'failure_class':'insufficient_observability' if unobservable else 'preservation_violation' if violations else None,
            'observed_preservation_violation':violations,
            'reference_observation_sha256':digest(reference),'candidate_observation_sha256':digest(candidate),
            'scope':'Measured outer contours and actual visible endpoints; final frame non-overlap, no DOM or icon-box substitution.'}
