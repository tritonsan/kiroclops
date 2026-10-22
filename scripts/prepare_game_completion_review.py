"""Prepare current game-event review while exactly reusing earlier pixel marks."""
from pathlib import Path
import argparse
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256
from kiroclops.visual.package import load_package,read_image
from kiroclops.local_engine.review import prepare_scope_review,validate_independent_review

def run(corpus,prior,output):
    import cv2
    import numpy as np
    corpus,prior,output=map(lambda p:Path(p).resolve(),(corpus,prior,output))
    if output.exists():raise ValueError('Fresh review required')
    receipt=validate_independent_review(prior);old={}
    for entry in receipt['events_reviews']:
        folder=Path(entry['directory']);q=read_json(folder/'request.json')
        old[q['package_sha256'],q['observations_sha256']]=folder.parent
    seen=set();rows=[]
    for path in sorted(corpus.glob('game-*/*/observations.json')):
        obs=read_json(path);key=obs['package_sha256'],digest(obs)
        if key in seen:continue
        if key not in old:raise ValueError('No unchanged prior position evidence')
        seen.add(key);events=read_json(path.with_name('events.json'));name=path.parent.parent.name+'-'+path.parent.name
        package=path.parent/'package';manifest=load_package(package);out=output/name
        row={'id':name,'package':str(package),'measurement':str(path.parent),'old_scope':str(old[key]),
            'package_sha256':key[0],'observations_sha256':key[1],'stage':read_json(path.parent.parent/'contract.json')['stage']}
        for scope in ('events','relationships'):
            prepare_scope_review(package,obs,events,out/scope,scope=scope)
            q=read_json(out/scope/'request.json');previous=read_json(old[key]/scope/'request.json')
            write_json(out/scope/'task-delta.json',{'tasks':[{'id':t['id'],'type':t['measurement']['type'],
                'exact_old_tasks':[p['id'] for p in previous['tasks'] if p['measurement']==t['measurement'] and p['frames']==t['frames']]}
                for t in q['tasks']],'status':'unsigned'})
        q=read_json(out/'events/request.json');tiles=[];bindings=[]
        for task in q['tasks']:
            e=task['measurement'];indices=[f['index'] for f in task['frames']]
            indices+=e.get('support_frame_indices',[])
            indices+=[e[k] for k in ('rest_anchor_frame_index','rest_confirmed_frame_index') if k in e]
            nearest=[max(0,min(len(manifest['frames'])-1,i)) for i in (min(indices)-2,max(indices)+2)]
            indices=sorted(set(indices+nearest))
            tile=np.full((320*((len(indices)+3)//4),4*500,3),235,np.uint8)
            for j,i in enumerate(indices):
                frame=manifest['frames'][i];im=read_image(package/frame['path']);scale=min(480/im.shape[1],265/im.shape[0])
                image=cv2.resize(im,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
                x=(j%4)*500;y=(j//4)*320
                cv2.putText(tile,f'{task["id"]} {e["type"]} f{i} {frame["time_s"]:.4f}s',(x+2,y+18),cv2.FONT_HERSHEY_SIMPLEX,.42,(0,0,0),1)
                tile[y+32:y+32+image.shape[0],x+5:x+5+image.shape[1]]=image
            fn='raw-'+task['id']+'.png';cv2.imencode('.png',tile)[1].tofile(out/fn)
            bindings.append({'file':fn,'sha256':file_sha256(out/fn),'indices':indices,
                'frames':[manifest['frames'][i]['sha256'] for i in indices]})
        # Whole-clip absence/extra-event context, uniformly sampled; no expected
        # track overlay or predicted coordinate enters any displayed image.
        indices=[int(i) for i in np.linspace(0,len(manifest['frames'])-1,20)]
        tiles=[]
        for i in indices:
            frame=manifest['frames'][i];im=read_image(package/frame['path']);tile=np.full((220,400,3),235,np.uint8)
            scale=min(390/im.shape[1],190/im.shape[0]);im=cv2.resize(im,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
            tile[25:25+im.shape[0],4:4+im.shape[1]]=im
            cv2.putText(tile,f'f{i} {frame["time_s"]:.4f}s',(3,18),cv2.FONT_HERSHEY_SIMPLEX,.45,(0,0,0),1);tiles.append(tile)
        image=np.vstack([np.hstack(tiles[i:i+4]) for i in range(0,20,4)])
        cv2.imencode('.png',image)[1].tofile(out/'raw-whole-clip.png')
        row['boards']=bindings+[{'file':'raw-whole-clip.png','sha256':file_sha256(out/'raw-whole-clip.png'),'indices':indices}]
        rows.append(row);print(name,len(q['tasks']),flush=True)
    if seen!={tuple(v) for v in receipt['required_observations']}:raise ValueError('Review coverage changed')
    write_json(output/'index.json',{'status':'pending','records':rows,'position_reviews':receipt['position_reviews'],
        'required_observations':receipt['required_observations'],'prior_review_sha256':file_sha256(prior),
        'position_reuse':'Exact raw package, full observations and previous signed review replay; event approval remains pending.'})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--corpus',required=True,type=Path);p.add_argument('--prior',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();run(a.corpus,a.prior,a.output)
