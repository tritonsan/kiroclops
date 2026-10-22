"""Pixel-derived initial selection from genuine, colored fixture illustrations.

This uses only rendered PNGs. Colors select the visible handle/bookmark/star
or document icon artwork, not a hidden tag encoding position or state.
"""
from __future__ import annotations
from pathlib import Path

COLORS={"panel":{"handle":(191,165,255),"bookmark":(77,208,225),"star":(255,209,102)},
        "svg":{"a":(77,208,225),"b":(255,209,102),"c":(230,139,196),"d":(191,165,255),"e":(126,211,160),"f":(243,146,108)}}


def select_from_pixels(frame_path,domain,required=None):
    import cv2
    import numpy as np
    if domain not in COLORS:
        raise ValueError("Unknown fixture domain")
    data=np.fromfile(str(frame_path),dtype=np.uint8)
    image=cv2.imdecode(data,cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Cannot read rendered PNG")
    result=[]
    required=set((COLORS[domain] if domain=="panel" else ("a","b","c")) if required is None else required)
    for name,rgb in COLORS[domain].items():
        bgr=np.asarray(rgb[::-1],dtype=np.int16)
        mask=cv2.inRange(image,np.maximum(0,bgr-8).astype(np.uint8),np.minimum(255,bgr+8).astype(np.uint8))
        ys,xs=np.nonzero(mask)
        if len(xs)<20:
            if name in required:
                raise ValueError(f"Visible natural artwork missing: {name}")
            continue
        x0,y0,x1,y1=int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())
        if x1-x0>40 or y1-y0>40:
            raise ValueError(f"Ambiguous artwork color: {name}")
        # The opaque-color extent excludes anti-aliased tips. Include a visible
        # background annulus; the centroid observer independently rejects any
        # remaining foreground support touching this crop's boundary.
        pad=4;x0=max(0,x0-pad);y0=max(0,y0-pad);x1=min(image.shape[1]-1,x1+pad);y1=min(image.shape[0]-1,y1+pad)
        roi=[x0,y0,x1-x0+1,y1-y0+1]
        result.append({"id":name,"roi":roi,"anchor":[(roi[2]-1)/2,(roi[3]-1)/2],
                       "method":"template","selection_source":"rendered_natural_artwork_pixels","frame_index":0,
                       "min_score":.75,"min_margin":.015,"max_color_rmse":12,"search_radius":90,
                       "pixel_anchor":{"method":"foreground_color_mass_v1","foreground_rgb":list(rgb)},
                       "reacquisition_policy":"full_image_unique_v1"})
    return {"schema_version":1,"domain":domain,"objects":result,
            "source":"PNG pixel color segmentation; no DOM coordinates or motor state",
            "frame":Path(frame_path).name}


def _card_geometry(image,object_roi):
    """Locate the visible card border around an icon using image contours only."""
    import cv2
    import numpy as np
    # The actual visible card fill is a connected region; its icon/text holes
    # do not become geometry. No known DOM center/width is substituted.
    fill=np.asarray((79,57,38),dtype=np.int16)
    mask=cv2.inRange(image,np.maximum(0,fill-3).astype(np.uint8),np.minimum(255,fill+3).astype(np.uint8))
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    x,y,w,h=object_roi;anchor=(x+w/2,y+h/2)
    candidates=[]
    for contour in contours:
        bx,by,bw,bh=cv2.boundingRect(contour)
        if (90<=bw<=190 and 40<=bh<=85 and bx<anchor[0]<bx+bw and by<anchor[1]<by+bh
                and bw*bh>4*w*h):
            candidates.append((bw*bh,[bx,by,bw,bh]))
    if not candidates:
        raise ValueError('No independent visible card outline around natural icon')
    roi=min(candidates)[1]
    return {"method":"contour","segmentation":"reference_fill","roi":roi,"search_radius":90,"min_area_ratio":.7,
            "selection_source":"Connected rendered card-fill contour; visible outline ROI"}


def _panel_geometry(image,object_roi):
    """Observe header/body fills and their enclosing painted panel outline.

    The handle remains an identity/position anchor. Its small glyph is never
    substituted for the panel's actual visible width, height, or boundary.
    """
    import cv2
    import numpy as np
    x,y,w,h=object_roi;ax,ay=x+w/2,y+h/2
    around=image[max(0,y-4):min(image.shape[0],y+h+4),max(0,x-4):min(image.shape[1],x+w+4)]
    colors,counts=np.unique(around.reshape(-1,3),axis=0,return_counts=True)
    header_color=colors[int(counts.argmax())].astype(np.int16)
    def color_mask(color):
        return cv2.inRange(image,np.clip(color-3,0,255).astype(np.uint8),np.clip(color+3,0,255).astype(np.uint8))
    header_mask=color_mask(header_color)
    contours,_=cv2.findContours(header_mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    headers=[]
    for contour in contours:
        bx,by,bw,bh=cv2.boundingRect(contour)
        if bx<ax<bx+bw and by<ay<by+bh and bw>3*w and bh>h:
            headers.append((bw*bh,[bx,by,bw,bh]))
    if not headers:raise ValueError('No visible header region surrounds the natural handle')
    hx,hy,hw,hh=min(headers)[1]
    band=image[hy+hh:min(image.shape[0],hy+hh+12),hx+hw//5:hx+hw-hw//5]
    if not band.size:raise ValueError('Panel body is clipped below its visible header')
    colors,counts=np.unique(band.reshape(-1,3),axis=0,return_counts=True)
    body_color=colors[int(counts.argmax())].astype(np.int16)
    if np.max(abs(body_color-header_color))<8:raise ValueError('Distinct visible panel header/body fills unavailable')
    mask=header_mask|color_mask(body_color)
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    panels=[]
    for contour in contours:
        bx,by,bw,bh=cv2.boundingRect(contour)
        if bx<ax<bx+bw and by<ay<by+bh and bw>=hw-2 and bh>2*hh:
            panels.append((bw*bh,[bx,by,bw,bh]))
    if not panels:raise ValueError('No connected visible panel body and header')
    return {'method':'contour','segmentation':'reference_fill','fill_palette_count':2,
            'outline_mode':'reference_color','outline_color_tolerance':24,
            'roi':min(panels)[1],'search_radius':90,'min_area_ratio':.7,
            'selection_source':'Rendered header/body fill components and adjacent painted outline; no DOM dimensions'}


def select_package_from_pixels(package,domain):
    """Discover actual appearance intervals in reference pixels.

    Reuse this reference selection for target candidates, not a new candidate
    selection that would excuse absent or invented objects. Missing internal
    detections are not converted into disappearance; first..last is expected
    visibility, and tracking must report occlusion/low confidence within it.
    """
    import cv2
    import numpy as np
    from kiroclops.visual.package import load_package,frame_paths
    root=Path(package);manifest=load_package(root);paths=frame_paths(root,manifest)
    discovered={};last={};last_index={}
    for index,path in enumerate(paths):
        annotation=select_from_pixels(path,domain,required=[])
        for item in annotation['objects']:
            key=item['id'];last[key]=manifest['frames'][index]['time_s'];last_index[key]=index
            if key in discovered:continue
            value={**item,'frame_index':index,'first_appearance_frame':index}
            if domain=='svg':
                image=cv2.imdecode(np.fromfile(str(path),dtype=np.uint8),cv2.IMREAD_COLOR)
                value['geometry']=_card_geometry(image,item['roi'])
            elif key=='handle':
                image=cv2.imdecode(np.fromfile(str(path),dtype=np.uint8),cv2.IMREAD_COLOR)
                value['geometry']=_panel_geometry(image,item['roi'])
            discovered[key]=value
    required=set(COLORS[domain]) if domain=='panel' else {'a','b','c'}
    if not required.issubset(discovered):
        raise ValueError(f'Required visible objects missing: {sorted(required-set(discovered))}')
    objects=[]
    for name,value in sorted(discovered.items()):
        start=manifest['frames'][value['frame_index']]['time_s']
        value['active_intervals_s']=[[start,last[name]]]
        if manifest.get('metadata',{}).get('scenario',{}).get('analysis_policy')=='observed_response_v2':
            from kiroclops.visual.visibility import observed_brackets
            value['visibility_brackets']=observed_brackets(manifest['frames'],value['frame_index'],last_index[name])
        objects.append(value)
    connections=[{'id':f'a-{name}','from_object':'a','to_object':name,'method':'line','corridor_px':8}
                 for name in sorted(discovered) if domain=='svg' and name!='a']
    return {'schema_version':1,'domain':domain,'objects':objects,'connections':connections,
            'relative_motion':[{'object_id':'bookmark','relative_to':'handle'},
                               {'object_id':'star','relative_to':'handle'}] if domain=='panel' else [],
            'background_rois':[[24,12,220,70]],'camera_max_shift_px':1.5,
            'source':'Reference PNG natural artwork and visible contour pixels only; no DOM oracle',
            'reference_package_sha256':manifest['package_sha256']}
