"""Limited image controls when a short clip cannot provide 20 distinct PNGs.

This never relaxes or invokes the full review gate. Independent hat/head glyph
geometry supplies controls; production positions are read only afterwards for
error comparison. No new model, renderer or capture is started.
"""
from pathlib import Path
import sys
import argparse
import importlib.util
import numpy as np
import cv2

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.visual.package import read_image
from prepare_game_raw_review_aid import anchor


def run(session,output):
    output=Path(output).resolve()
    assert not output.exists()
    helper=ROOT/'artifacts/phase3a/tools/prepare_local_final_review.py'
    spec=importlib.util.spec_from_file_location('frozen_review',helper)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    state,candidate,records,excluded,native,fingerprint=mod.inventory(session)
    output.mkdir(parents=True)
    receipts=[]
    for record in records:
        package=Path(record['package']);manifest=read_json(package/'manifest.json')
        rows=[];pictures={}
        for f in manifest['frames']:
            path=package/f['path'];assert file_sha256(path)==f['sha256']
            im=read_image(path);found=anchor(im)
            assert len(found)==1, ('ambiguous_raw_anchor',record['id'],f['index'],found)
            row={'index':f['index'],'time_s':f['time_s'],'image_sha256':f['sha256'],**found[0]}
            rows.append(row)
            pictures.setdefault(f['sha256'], (row,im))
        baseline=float(np.median([r['y'] for r in rows[20:30]]))
        airborne=[r for r in rows if r['index']>=30 and r['y']<baseline]
        takeoff=airborne[0]['index'] if airborne else None
        apex=min(airborne,key=lambda r:r['y'])['index'] if airborne else None
        landing=next((r['index'] for r in rows if takeoff is not None and r['index']>takeoff and r['y']==baseline),None)
        tiles=[]
        for n,(row,im) in enumerate(pictures.values()):
            x,y=int(row['x']),int(row['y']);l,t=max(0,x-10),max(0,y-7)
            crop=im[t:t+24,l:l+24]
            enlarged=cv2.resize(crop,None,fx=5,fy=5,interpolation=cv2.INTER_NEAREST)
            tile=np.full((160,170,3),28,np.uint8)
            tile[32:32+enlarged.shape[0],5:5+enlarged.shape[1]]=enlarged
            cv2.putText(tile,f"f{row['index']} x{row['x']:g} y{row['y']:g}",(3,13),cv2.FONT_HERSHEY_SIMPLEX,.34,(240,240,240),1)
            cv2.putText(tile,record['role'],(3,27),cv2.FONT_HERSHEY_SIMPLEX,.34,(240,240,240),1)
            tiles.append(tile)
        sheets=[]
        for page,start in enumerate(range(0,len(tiles),24)):
            group=tiles[start:start+24]
            while len(group)%4:group.append(np.zeros_like(group[0]))
            board=np.vstack([np.hstack(group[i:i+4]) for i in range(0,len(group),4)])
            path=output/record['id']/f'raw-{page:02d}.png';path.parent.mkdir(parents=True,exist_ok=True)
            cv2.imencode('.png',board)[1].tofile(path)
            sheets.append({'file':path.relative_to(output).as_posix(),'sha256':file_sha256(path)})
        # Error comparison only, after independent raw coordinates exist.
        tracked=record['observations']['objects'][0]['frames']
        errors=[float(np.hypot(r['x']-f['x'],r['y']-f['y'])) for r,f in zip(rows,tracked) if f.get('valid')]
        write_json(output/record['id']/'raw-points.json',rows)
        receipts.append({'id':record['id'],'role':record['role'],'package_sha256':record['package_sha256'],
            'observations_sha256':record['observations_sha256'],'frames':len(rows),
            'distinct_image_sha256s':len(pictures),'median_error_px':float(np.median(errors)),
            'max_error_px':max(errors),'raw_takeoff_index':takeoff,'raw_apex_index':apex,'raw_landing_index':landing,
            'sheets':sheets,'full_20_distinct_gate_satisfied':len(pictures)>=20})
    result={'kind':'LimitedGameRawControls','status':'unsigned_not_inspected','records':receipts,
        'candidate_sha256':candidate['source_sha256'],'fingerprint':fingerprint,'native_evidence_hashes':native,
        'full_review_accepted':False,'thresholds_changed':False,'external_human_review':False,
        'limitation':'A clip with fewer than20 distinct raw images cannot satisfy the frozen formal review request; all unique images are inspected without inventing samples.',
        'producer_sha256':file_sha256(__file__)}
    write_json(output/'result.json',result)
    print([{k:v for k,v in r.items() if k!='sheets'} for r in receipts],flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--session',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();run(a.session,a.output)
