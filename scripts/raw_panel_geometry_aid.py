"""Unsigned raw-pixel panel geometry proposals for independent visual review.

Reads only the existing independently extracted glyphs and their source PNGs.
The large panel box is measured from its painted border, never inferred from
the drag icon. Small glyph geometry means the originally selected fixed patch,
whose padding was visually marked in the first image. No ObservationSet or
EventSet is read. This helper does not approve a review.
"""
from pathlib import Path
import argparse
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.visual.package import load_package,read_image,frame_paths


def border_box(image,anchor):
    # Painted outline sampled directly in the first raw image at (37,400).
    # This independent color-bound method differs from production's learned
    # two-fill contour union. Whole-clip appearance changes are not supported.
    distance=np.linalg.norm(image.astype(float)-np.array([156,132,104]),axis=2)
    mask=np.uint8(distance<=95)*255
    # Opening isolates the actual long border fragments from nearby text and
    # the background card. Rounded corners need not form a closed component.
    joined=cv2.bitwise_or(cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((120,1),np.uint8)),
                         cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,120),np.uint8)))
    contours,_=cv2.findContours(joined,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    vertical=[];horizontal=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if h>=180 and w<=4:vertical.append([x,y,w,h])
        if w>=180 and h<=4:horizontal.append([x,y,w,h])
    boxes=[]
    for left in vertical:
        for right in vertical:
            if not left[0]<anchor['x']<right[0] or right[0]-left[0]<180:continue
            for top in horizontal:
                for bottom in horizontal:
                    if not top[1]<anchor['y']<bottom[1] or bottom[1]-top[1]<180:continue
                    # The four independently visible straight edge fragments
                    # must meet through the small rounded corner regions.
                    if max(abs(top[0]-left[0]),abs(bottom[0]-left[0]),
                           abs(top[0]+top[2]-right[0]),abs(bottom[0]+bottom[2]-right[0]),
                           abs(left[1]-top[1]),abs(right[1]-top[1]),
                           abs(left[1]+left[3]-bottom[1]),abs(right[1]+right[3]-bottom[1]))<=24:
                        boxes.append([left[0],top[1],right[0]+right[2]-left[0],bottom[1]+bottom[3]-top[1]])
    if len(boxes)!=1:return {'status':'ambiguous','boxes':boxes}
    return {'status':'proposal','box':boxes[0]}


def run(package,raw_directory):
    package=Path(package);directory=Path(raw_directory)
    manifest=load_package(package);data=read_json(directory/'raw-points.json')
    if data['package_sha256']!=manifest['package_sha256']:raise ValueError('Different raw package')
    result=[];tiles=[]
    indexes={int(i) for i in np.linspace(0,len(data['rows'])-1,24)}
    for row,path in zip(data['rows'],frame_paths(package,manifest)):
        image=read_image(path);points=row['points'];geometry=border_box(image,points['handle'])
        item={'index':row['index'],'time_s':row['time_s'],'image_sha256':row['image_sha256'],
              'painted_panel':geometry,'objects':{}}
        for oid in ('bookmark','star'):
            p=points[oid];anchor=(p['x'],p['y']-(.5 if oid=='bookmark' else 0))
            width,height,ax,ay=(22,27,10.5,13) if oid=='bookmark' else (30,28,14.5,13.5)
            item['objects'][oid]={'anchor':list(anchor),'box':[anchor[0]-ax,anchor[1]-ay,width,height],
                'definition':'Selected fixed glyph patch, with visually marked first-frame padding'}
        if geometry['status']=='proposal':
            item['objects']['handle']={'anchor':[points['handle']['x'],points['handle']['y']],
                'box':geometry['box'],'definition':'Actual painted panel outline; identity anchor is drag glyph'}
        result.append(item)
        if row['index'] in indexes:
            tile=np.full((342,600,3),22,np.uint8)
            cx=max(0,int(points['handle']['x']-45));cy=max(0,int(points['handle']['y']-36))
            crop=image[cy:cy+305,cx:cx+298]
            if crop.shape[0] and crop.shape[1]:
                tile[30:30+crop.shape[0],:crop.shape[1]]=crop
                tile[30:30+crop.shape[0],300:300+crop.shape[1]]=crop
            if geometry['status']=='proposal':
                x,y,w,h=geometry['box'];cv2.rectangle(tile,(300+x-cx,30+y-cy),(300+x+w-1-cx,30+y+h-1-cy),(30,240,80),1)
            cv2.putText(tile,f"{row['index']} {row['time_s']:.3f}s raw / outline proposal",(5,19),cv2.FONT_HERSHEY_SIMPLEX,.48,(230,230,230),1)
            tiles.append(tile)
    while len(tiles)%2:tiles.append(np.zeros_like(tiles[0]))
    sheet=np.vstack([np.hstack(tiles[i:i+2]) for i in range(0,len(tiles),2)])
    cv2.imencode('.png',sheet)[1].tofile(directory/'raw-geometry.png')
    output={'kind':'UnsignedIndependentGeometryProposals','package_sha256':manifest['package_sha256'],
        'raw_points_sha256':file_sha256(directory/'raw-points.json'),'helper_sha256':file_sha256(__file__),
        'status':'unreviewed','rows':result,'ambiguous_frames':sum(r['painted_panel']['status']!='proposal' for r in result)}
    write_json(directory/'geometry-proposals.json',output)
    return {'frames':len(result),'ambiguous_frames':output['ambiguous_frames']}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);parser.add_argument('raw_directory',type=Path)
    args=parser.parse_args();index=read_json(args.corpus/'capture-index.json');records=[]
    for row in index['references']:
        if row['domain']=='panel':records.append((f"reference-{row['scenario_index']}",row['record']['package']))
    for row in index['cases']:
        if row['case']['domain']=='panel':records.append((row['case']['id'],row['record']['package']))
    for name,package in records:print(name,run(package,args.raw_directory/name),flush=True)
