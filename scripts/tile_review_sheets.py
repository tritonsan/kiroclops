"""Arrange existing unsigned image-review sheets without altering their pixels."""
from pathlib import Path
import argparse
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from kiroclops.visual.package import read_image
from kiroclops.visual.schema import read_json,write_json,file_sha256


def run(source,output):
    source,output=Path(source),Path(output)
    output.mkdir(parents=True,exist_ok=False)
    names=read_json(source/'proposals.json')['sheets'];rows=[]
    for start in range(0,len(names),4):
        images=[]
        for name in names[start:start+4]:
            raw=read_image(source/name);tile=np.zeros((1100,880,3),np.uint8)
            tile[:raw.shape[0],:raw.shape[1]]=raw;images.append(tile)
        while len(images)<4:images.append(np.zeros((1100,880,3),np.uint8))
        board=np.vstack((np.hstack(images[:2]),np.hstack(images[2:])))
        name=f'board-{start//4:02}.png';cv2.imencode('.png',board)[1].tofile(output/name)
        rows.append({'board':name,'sha256':file_sha256(output/name),
            'sheets':[{'name':s,'sha256':file_sha256(source/s)} for s in names[start:start+4]]})
    write_json(output/'manifest.json',{'proposal_sha256':file_sha256(source/'proposals.json'),'boards':rows})
    print({'boards':len(rows),'sheets':len(names)},flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.source,a.output)
