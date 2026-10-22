"""Propose raw color-glyph anchors for a reviewer; NEVER signs or accepts marks.

This helper reads only a frozen review request and raw images. It does not read
observations, target code, DOM state, or tracking coordinates. Its proposals must
be visually checked/corrected before they become independent image marks.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from kiroclops.visual.package import read_image
from kiroclops.visual.schema import digest,read_json,write_json,file_sha256,safe_path


# Hue bands are chosen from visibly distinct genuine illustrations. They are
# review aids, not additional acceptance rules or application marker encoding.
COLORS={'bookmark':(93,7),'handle':(132,10),'star':(21,6),
        'a':(93,7),'b':(21,6),'c':(161,7),'d':(132,10),'e':(70,9),'f':(108,8)}


def propose(directory,output):
    directory=Path(directory);output=Path(output)
    if output.exists():raise ValueError('Use a fresh proposal directory')
    output.mkdir(parents=True);request=read_json(directory/'request.json');points=[];tiles=[];sheets=[]
    for number,task in enumerate(request['tasks']):
        image=read_image(safe_path(directory,task['image']))
        if file_sha256(directory/task['image'])!=task['image_sha256']:raise ValueError('Frame binding changed')
        center,tolerance=COLORS[task['object_id']]
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        dist=np.minimum(abs(hsv[:,:,0].astype(float)-center),180-abs(hsv[:,:,0].astype(float)-center))
        mask=np.uint8((dist<=tolerance)&(hsv[:,:,1]>=50)&(hsv[:,:,2]>=150))*255
        joined=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8)) if task['object_id']=='handle' else mask
        count,labels,stats,centroids=cv2.connectedComponentsWithStats(joined,8)
        boxes=[]
        for x,y,w,h,area in stats[1:]:
            if 9<=w<=55 and 12<=h<=55 and area>=24:
                pixels=np.argwhere(mask[y:y+h,x:x+w]>0)
                if not len(pixels):continue
                top,left=pixels.min(axis=0);bottom,right=pixels.max(axis=0)
                boxes.append((int(x+left),int(y+top),int(right-left+1),int(bottom-top+1)))
        point={key:task[key] for key in ('record_id','object_id','index','image_sha256')}
        point['status']='proposed' if len(boxes)==1 else 'ambiguous'
        point['boxes']=[list(b) for b in boxes]
        if len(boxes)==1:
            x,y,w,h=boxes[0];point.update(x=x+(w-1)/2,y=y+(h-1)/2)
            left=max(0,x-12);top=max(0,y-12);crop=image[top:min(image.shape[0],y+h+12),left:min(image.shape[1],x+w+12)].copy()
            crop=cv2.resize(crop,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST)
            px=int(round((point['x']-left)*3));py=int(round((point['y']-top)*3))
            cv2.drawMarker(crop,(px,py),(255,255,255),cv2.MARKER_CROSS,9,1)
        else:
            crop=cv2.resize(image,(180,120));left=top=0
        tile=np.full((220,220,3),24,np.uint8);h,w=crop.shape[:2];scale=min(1,200/w,160/h)
        if scale<1:crop=cv2.resize(crop,(int(w*scale),int(h*scale)),interpolation=cv2.INTER_NEAREST)
        tile[40:40+crop.shape[0],10:10+crop.shape[1]]=crop
        for line,text in enumerate([f"#{number} {task['object_id']} f{task['index']}",task['record_id']+' '+point['status']]):
            cv2.putText(tile,text,(8,14+18*line),cv2.FONT_HERSHEY_SIMPLEX,.4,(230,230,230),1)
        if 'x' in point:cv2.putText(tile,f"{point['x']:.1f}, {point['y']:.1f}",(8,215),cv2.FONT_HERSHEY_SIMPLEX,.4,(150,200,230),1)
        points.append(point);tiles.append(tile)
        if len(tiles)==20 or number==len(request['tasks'])-1:
            tiles.extend(np.zeros_like(tile) for _ in range((4-len(tiles)%4)%4))
            sheet=np.vstack([np.hstack(tiles[i:i+4]) for i in range(0,len(tiles),4)])
            name=f'proposals-{len(sheets):03}.png';cv2.imencode('.png',sheet)[1].tofile(output/name);sheets.append(name);tiles=[]
    value={'kind':'UnacceptedReviewProposals','request_sha256':digest(request),'helper_sha256':file_sha256(__file__),
           'method':'Independent raw color bounds; requires visual confirmation and corrections.',
           'points':points,'sheets':sheets,'status':'unreviewed'}
    write_json(output/'proposals.json',value)
    return {'status':'unreviewed','points':len(points),'ambiguous':sum(p['status']=='ambiguous' for p in points),'sheets':sheets}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('review',type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(propose(args.review,args.output),indent=2))
