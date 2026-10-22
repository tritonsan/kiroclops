"""Visible endpoints of a colored, non-branching multipart 2D stroke.

The selected stroke appearance and object boxes delimit a pixel search. Its
endpoints come from the thinned observed stroke, never node-boundary snapping.
Crossings/branches and disconnected pieces are deliberately unobservable.
"""
from __future__ import annotations

import math
import cv2
import numpy as np


def _thin(mask):
    # Zhang-Suen two-pass parallel thinning. Work on a padded binary image so
    # border strokes cannot wrap through NumPy's indexing.
    value=np.pad(mask.astype(np.uint8),1)
    for _ in range(sum(mask.shape)):
        changed=False
        for phase in (0,1):
            c=value[1:-1,1:-1]
            n=[value[:-2,1:-1],value[:-2,2:],value[1:-1,2:],value[2:,2:],
               value[2:,1:-1],value[2:,:-2],value[1:-1,:-2],value[:-2,:-2]]
            count=sum(n);crossings=sum((a==0)&(b==1) for a,b in zip(n,n[1:]+n[:1]))
            products=(n[0]*n[2]*n[4],n[2]*n[4]*n[6]) if phase==0 else (n[0]*n[2]*n[6],n[0]*n[4]*n[6])
            remove=(c==1)&(count>=2)&(count<=6)&(crossings==1)&(products[0]==0)&(products[1]==0)
            if np.any(remove):c[remove]=0;changed=True
        if not changed:break
    return value[1:-1,1:-1]


def _box(frame):
    left=frame.get('left',frame['x']-frame['width']/2);top=frame.get('top',frame['y']-frame['height']/2)
    return left,top,left+frame['width'],top+frame['height']


def visible_polyline(image,spec,objects):
    def fail(reason):return {'valid':False,'start':None,'end':None,'reason':reason}
    a,b=objects[spec['from_object']],objects[spec['to_object']]
    if not a.get('valid') or not b.get('valid'):return fail('ambiguous_node_geometry')
    rgb=spec.get('stroke_rgb')
    if not isinstance(rgb,list) or len(rgb)!=3 or any(type(v) is not int or not 0<=v<=255 for v in rgb):
        return fail('polyline_appearance_not_selected')
    tolerance=spec.get('stroke_tolerance',12)
    if not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or not 0<=tolerance<=40:return fail('invalid_stroke_tolerance')
    color=np.asarray(rgb[::-1],np.int16);mask=cv2.inRange(image,np.maximum(0,color-tolerance).astype(np.uint8),np.minimum(255,color+tolerance).astype(np.uint8))>0
    margin=spec.get('search_margin_px',64)
    if not isinstance(margin,(int,float)) or not math.isfinite(margin) or not 0<=margin<=max(image.shape[:2]):return fail('invalid_search_margin')
    boxes=[_box(a),_box(b)];h,w=mask.shape
    x0=max(0,int(min(v[0] for v in boxes)-margin));x1=min(w,int(math.ceil(max(v[2] for v in boxes)+margin)))
    y0=max(0,int(min(v[1] for v in boxes)-margin));y1=min(h,int(math.ceil(max(v[3] for v in boxes)+margin)))
    cropped=np.zeros_like(mask);cropped[y0:y1,x0:x1]=mask[y0:y1,x0:x1];mask=cropped
    # Remove genuine selected-node interiors; leave painted connection ends at
    # their visible edge. This does not extend a short/detached line to a box.
    for frame in objects.values():
        if not frame.get('valid'):continue
        l,t,r,bottom=_box(frame)
        mask[max(0,int(math.ceil(t+1))):min(h,int(bottom-1)),max(0,int(math.ceil(l+1))):min(w,int(r-1))]=False
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask.astype(np.uint8),8)
    if sum(int(row[4])>=8 for row in stats[1:])>1:
        return fail('polyline_multiple_or_disconnected_strokes')
    candidates=[]
    for label in range(1,count):
        lx,ly,cw,ch,area=stats[label]
        if area<8:continue
        component=labels[ly:ly+ch,lx:lx+cw]==label;skeleton=_thin(component)
        padded=np.pad(skeleton,1);c=padded[1:-1,1:-1]
        n=[padded[:-2,1:-1],padded[:-2,2:],padded[1:-1,2:],padded[2:,2:],padded[2:,1:-1],padded[2:,:-2],padded[1:-1,:-2],padded[:-2,:-2]]
        neighbours=sum(n);transitions=sum((u==0)&(v==1) for u,v in zip(n,n[1:]+n[:1]))
        if np.any((c==1)&(transitions>2)):continue
        ends=np.argwhere((c==1)&(neighbours==1))
        if len(ends)!=2:continue
        all_pixels=np.argwhere(component)[:,::-1].astype(float)
        center_pixels=np.argwhere(skeleton)[:,::-1].astype(float)
        thickness=float(cv2.distanceTransform(component.astype(np.uint8),cv2.DIST_L2,5).max())
        endpoints=[]
        for y,x in ends:
            tip=np.asarray([float(x),float(y)]);distance=np.linalg.norm(center_pixels-tip,axis=1)
            interior=center_pixels[(distance>=2)&(distance<=max(8.,4*thickness))]
            if not len(interior):continue
            direction=tip-np.mean(interior,axis=0);norm=np.linalg.norm(direction)
            if norm==0:continue
            direction/=norm
            near=all_pixels[np.linalg.norm(all_pixels-tip,axis=1)<=max(5.,2*thickness+2)]
            projections=near@direction
            # Restore the visible painted tip lost to thinning. Average actual
            # opposing stroke pixels at its extremum, never extrapolate it.
            visible=near[projections>=projections.max()-.75].mean(axis=0)
            endpoints.append(visible+np.asarray([lx,ly]))
        if len(endpoints)!=2:continue
        center_a=np.asarray([(boxes[0][0]+boxes[0][2])/2,(boxes[0][1]+boxes[0][3])/2])
        center_b=np.asarray([(boxes[1][0]+boxes[1][2])/2,(boxes[1][1]+boxes[1][3])/2])
        direct=np.linalg.norm(endpoints[0]-center_a)+np.linalg.norm(endpoints[1]-center_b)
        reverse=np.linalg.norm(endpoints[1]-center_a)+np.linalg.norm(endpoints[0]-center_b)
        if reverse<direct:endpoints.reverse();direct=reverse
        # Both endpoints must span most of the observed node separation. A
        # detached whole stroke is measurable; two fragments are not joined.
        length=float(np.linalg.norm(endpoints[1]-endpoints[0]));separation=float(np.linalg.norm(center_a-center_b))
        if length<max(6.,.35*separation):continue
        candidates.append((float(direct),endpoints,int(skeleton.sum())))
    if not candidates:return fail('polyline_missing_branched_or_disconnected')
    candidates.sort(key=lambda x:x[0])
    if len(candidates)>1 and candidates[1][0]-candidates[0][0]<4:return fail('ambiguous_visible_connection')
    _,ends,support=candidates[0]
    return {'valid':True,'start':ends[0].tolist(),'end':ends[1].tolist(),'score':1.,
        'method':'observed_color_stroke_thinning','support_pixels':support,'reason':None,
        'endpoint_source':'visible_stroke_pixels','node_boundary_snapping':False}
