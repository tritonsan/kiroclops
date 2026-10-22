"""Unsigned subpixel glyph-mass proposals from raw panel PNGs only.

HSV boxes can grow asymmetrically by one AA pixel. Color-mass centroids provide
an independent check of the actual shape translation; first-frame physical
anchors were already visually marked. Both floating and nearest-native-pixel
translations are retained. No production observations/events are read.
"""
from pathlib import Path
import argparse
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from kiroclops.visual.package import read_image,load_package,frame_paths
from kiroclops.visual.schema import read_json,write_json,file_sha256


def mass(image,point):
    x,y,w,h=point['color_bounds'];left,top=max(0,x-3),max(0,y-3)
    crop=image[top:y+h+3,left:x+w+3].astype(float)
    # A border of background pixels surrounds these isolated natural glyphs.
    edge=np.concatenate((crop[0],crop[-1],crop[:,0],crop[:,-1]))
    bg=np.median(edge,axis=0)
    colors,counts=np.unique(crop.reshape(-1,3),axis=0,return_counts=True)
    far=np.linalg.norm(colors-bg,axis=1)>80
    if not far.any():raise ValueError('Glyph color is not separable')
    fg=colors[far][np.argmax(counts[far])];vector=fg-bg
    alpha=np.clip((crop-bg)@vector/(vector@vector),0,1)
    residual=np.linalg.norm(crop-bg-alpha[:,:,None]*vector,axis=2)
    alpha[residual>35]=0
    yy,xx=np.indices(alpha.shape);total=alpha.sum()
    if total<20:raise ValueError('Insufficient glyph mass')
    return [float((xx*alpha).sum()/total+left),float((yy*alpha).sum()/total+top)]


def run(package,directory):
    package=Path(package);directory=Path(directory);manifest=load_package(package)
    raw=read_json(directory/'raw-points.json');origin=None;out=[]
    for row,path in zip(raw['rows'],frame_paths(package,manifest)):
        image=read_image(path);centroids={oid:mass(image,p) for oid,p in row['points'].items()}
        if origin is None:origin=centroids
        points={}
        for oid,center in centroids.items():
            base=raw['rows'][0]['points'][oid];anchor=np.array([base['x'],base['y']-(.5 if oid=='bookmark' else 0)])
            shift=np.array(center)-np.array(origin[oid]);p=anchor+np.rint(shift)
            points[oid]={'x':float(p[0]),'y':float(p[1]),'subpixel_anchor':(anchor+shift).tolist(),
                        'centroid':center,'quantization':'nearest natural pixel displacement from first visual anchor'}
        out.append({k:row[k] for k in ('index','time_s','image_sha256')}|{'points':points})
    result={'kind':'UnsignedIndependentMassProposals','status':'unreviewed','package_sha256':manifest['package_sha256'],
        'raw_points_sha256':file_sha256(directory/'raw-points.json'),'helper_sha256':file_sha256(__file__),'rows':out,
        'method':'Independent color-mass translation and explicit nearest native pixel quantization; not template correlation.'}
    write_json(directory/'mass-points.json',result);return len(out)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);parser.add_argument('raw_directory',type=Path)
    args=parser.parse_args();index=read_json(args.corpus/'capture-index.json');records=[]
    for row in index['references']:
        if row['domain']=='panel':records.append((f"reference-{row['scenario_index']}",row['record']['package']))
    for row in index['cases']:
        if row['case']['domain']=='panel':records.append((row['case']['id'],row['record']['package']))
    for name,package in records:print(name,run(package,args.raw_directory/name),flush=True)
