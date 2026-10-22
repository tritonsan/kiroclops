"""Independent color-bound sequence proposals; never approves an analysis.

Only reads raw PNG packages and a recording inventory. No tracker positions,
event times, reference implementation, or DOM geometry are used. The output is
a review aid requiring visual inspection, not an independent-review receipt.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from kiroclops.visual.package import load_package,frame_paths,read_image
from kiroclops.visual.schema import write_json,file_sha256,read_json

COLORS={'bookmark':(93,7),'handle':(132,10),'star':(21,6)}


def raw_anchors(image):
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV);out={}
    for name,(center,tolerance) in COLORS.items():
        distance=np.minimum(abs(hsv[:,:,0].astype(float)-center),180-abs(hsv[:,:,0].astype(float)-center))
        mask=np.uint8((distance<=tolerance)&(hsv[:,:,1]>=50)&(hsv[:,:,2]>=150))*255
        joined=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8)) if name=='handle' else mask
        count,labels,stats,centers=cv2.connectedComponentsWithStats(joined,8);boxes=[]
        for x,y,w,h,area in stats[1:]:
            if 9<=w<=55 and 12<=h<=55 and area>=24:
                pixels=np.argwhere(mask[y:y+h,x:x+w]>0)
                if len(pixels):
                    top,left=pixels.min(axis=0);bottom,right=pixels.max(axis=0)
                    boxes.append([int(x+left),int(y+top),int(right-left+1),int(bottom-top+1)])
        if len(boxes)==1:
            x,y,w,h=boxes[0];out[name]={'x':x+(w-1)/2,'y':y+(h-1)/2,'color_bounds':boxes[0]}
        else:out[name]={'status':'ambiguous','boxes':boxes}
    return out


def sequence(package,output):
    package=Path(package);output=Path(output);manifest=load_package(package)
    output.mkdir(parents=True,exist_ok=False);rows=[];strips=[]
    for frame,path in zip(manifest['frames'],frame_paths(package,manifest)):
        image=read_image(path);points=raw_anchors(image)
        rows.append({'index':frame['index'],'time_s':frame['time_s'],'image_sha256':frame['sha256'],'points':points})
        # Raw small glyphs, laid along real time. Labels identify sample numbers,
        # not expected events. Every fourth frame keeps the sheet legible.
        if frame['index']%4==0:
            tile=np.full((110,150,3),24,np.uint8)
            cv2.putText(tile,f"{frame['index']}  {frame['time_s']:.3f}s",(3,14),cv2.FONT_HERSHEY_SIMPLEX,.4,(240,240,240),1)
            for k,(name,p) in enumerate(points.items()):
                if 'x' not in p:continue
                x,y,w,h=p['color_bounds'];crop=image[max(0,y-2):y+h+2,max(0,x-2):x+w+2]
                ch,cw=crop.shape[:2];tile[23:23+ch,4+k*48:4+k*48+cw]=crop
                cv2.putText(tile,f"{p['x']:.0f}",(3+k*48,82),cv2.FONT_HERSHEY_SIMPLEX,.35,(240,240,240),1)
                cv2.putText(tile,f"{p['y']:.0f}",(3+k*48,98),cv2.FONT_HERSHEY_SIMPLEX,.35,(240,240,240),1)
            strips.append(tile)
    strips.extend(np.zeros((110,150,3),np.uint8) for _ in range((-len(strips))%8))
    sheet=np.vstack([np.hstack(strips[i:i+8]) for i in range(0,len(strips),8)])
    cv2.imencode('.png',sheet)[1].tofile(output/'raw-sequence.png')
    curves=[]
    for oid in COLORS:
        for axis in ('x','y'):
            values=[r['points'][oid].get(axis) for r in rows]
            if any(v is None for v in values):continue
            curves.append((oid+' '+axis,values))
    for oid in ('bookmark','star'):
        curves.append((oid+' - handle y',[r['points'][oid]['y']-r['points']['handle']['y'] for r in rows]))
    plot=np.full((180*len(curves),1500,3),245,np.uint8)
    first,last=rows[0]['time_s'],rows[-1]['time_s']
    for n,(label,values) in enumerate(curves):
        lo,hi=min(values),max(values);span=max(1,hi-lo)
        cv2.putText(plot,f'{label}: {lo:.1f} .. {hi:.1f}',(8,n*180+16),cv2.FONT_HERSHEY_SIMPLEX,.45,(30,30,30),1)
        coords=[(int(80+(r['time_s']-first)/(last-first)*1370),int(n*180+145-(v-lo)/span*105)) for r,v in zip(rows,values)]
        cv2.polylines(plot,[np.asarray(coords)],False,(180,70,30),1)
        for index in range(0,len(rows),8):
            x,y=coords[index];cv2.circle(plot,(x,y),2,(30,50,190),-1)
            cv2.putText(plot,f'{index}',(x-7,n*180+173),cv2.FONT_HERSHEY_SIMPLEX,.32,(40,40,40),1)
    cv2.imencode('.png',plot)[1].tofile(output/'raw-curves.png')
    result={'kind':'UnacceptedRawSequenceProposals','package_sha256':manifest['package_sha256'],
        'helper_sha256':file_sha256(__file__),'rows':rows,'status':'unreviewed',
        'method':'Independent HSV glyph bounds; no predicted observations or event definitions read.',
        'ambiguities':sum('x' not in p for r in rows for p in r['points'].values())}
    write_json(output/'raw-points.json',result);return {'frames':len(rows),'ambiguities':result['ambiguities']}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();inventory=read_json(args.corpus/'capture-index.json');records=[]
    for row in inventory['references']:
        if row['domain']=='panel':records.append((f"reference-{row['scenario_index']}",row['record']['package']))
    for row in inventory['cases']:
        if row['case']['domain']=='panel':records.append((row['case']['id'],row['record']['package']))
    for name,package in records:print(name,sequence(package,args.output/name),flush=True)
