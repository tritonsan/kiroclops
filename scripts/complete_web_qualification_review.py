"""Unsigned numerical diagnosis, then explicit image-journal-bound completion.

The complete operation is only called after the assistant actually views the
listed images. Independent raw pixel estimators are computational assistance,
not evidence of a human clicking every point.
"""
from pathlib import Path
import argparse,sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256
from kiroclops.local_engine.review import evaluate_review,evaluate_scope_review
import prepare_fresh_web_review_v2 as binder

def annotations(root,domain):
    directory=root/domain;req=read_json(directory/'positions/request.json')
    if domain=='svg':points=read_json(directory/'raw/position-proposals.json')['points']
    else:
        rows={}
        for rec in read_json(directory/'inventory.json')['records']:
            raw=read_json(directory/'raw'/rec['id']/'raw-proposals.json')
            for row in raw['rows']:rows[raw['record_id'],row['index']]=row
        points=[]
        for t in req['tasks']:
            row=rows[t['record_id'],t['index']];assert row['image_sha256']==t['image_sha256']
            xy=row['points'][t['object_id']]
            points.append({k:t[k] for k in ('record_id','object_id','index','image_sha256')}|{'x':xy['x'],'y':xy['y']})
    yield f'{domain}/positions',{'request_sha256':digest(req),'reviewer':'Codex assistant','status':'unsigned',
        'method':'independent_raw_image_marking','external_human_review':False,'points':points,
        'method_detail':'Independent raw foreground color-mass estimates. No tracker/DOM coordinate is a marking input. Completion requires actual original crop-sheet inspection; not manual clicking of every point.'},evaluate_review
    for rec in read_json(directory/'inventory.json')['records']:
        for scope in ('events','relationships'):
            path=directory/'records'/rec['id']/scope
            draft=read_json(directory/'drafts'/rec['id']/(scope+'.json')) if domain=='panel' else read_json(path/'draft.json')
            checks=[]
            for c in draft['checks']:
                value=c.get('independent_observation') if domain=='panel' else c.get('observed')
                checks.append({'id':c['id'],'verdict':'pass' if value is not None else 'fail',
                    'frame_sha256s':c['frame_sha256s'],'evidence':'Independent full raw sequence estimates; actual event and geometry sheet inspection is separately required before completion.',
                    ('observed_interval_s' if scope=='events' else 'observed'):value})
            yield path.relative_to(root).as_posix(),{'request_sha256':draft['request_sha256'],'reviewer':'Codex assistant',
                'method':'independent_raw_sequence_review','status':'unsigned','external_human_review':False,'checks':checks},evaluate_scope_review

def required_images(root):
    result=list((root/'panel/raw').glob('position-*.png'))+list((root/'panel/event-boards').glob('*.png'))
    result+=list((root/'svg/raw').glob('points-*.png'))+list((root/'svg/event-boards').glob('*/events-*.png'))
    for domain in ('panel','svg'):
        for rec in read_json(root/domain/'inventory.json')['records']:
            result.append(root/domain/'raw'/rec['id']/'geometry-01.png')
    return result

def run(root,complete=False):
    root=Path(root).resolve();items=[];failures=[]
    for domain in ('panel','svg'):
        for path,annotation,method in annotations(root,domain):
            result=method(root/path,annotation,persist=False)
            items.append((path,annotation,result))
            if result['status']!='pass':
                failures.append({'directory':path,'failed':result.get('objects') if 'objects' in result else [c for c in result['checks'] if not c['pass']]})
    # A matching subset of event times must not hide an extra/missing raw event.
    for rec in read_json(root/'svg/inventory.json')['records']:
        differences=read_json(root/'svg/records'/rec['id']/'disagreements.json')
        counts=[d for d in differences if d['scope']=='event_count']
        if counts:failures.append({'directory':'svg/records/'+rec['id'],'event_count_disagreements':counts})
    for rec in read_json(root/'panel/drafts/summary.json')['records']:
        counts=[d for d in rec['event_counts'] if d['required']!=d['independent']]
        if counts:failures.append({'directory':'panel/records/'+rec['record'],'event_count_disagreements':counts})
    if not complete:
        write_json(root/'unsigned-numeric-diagnosis.json',{'status':'unsigned','numeric_agreement':'fail' if failures else 'pass',
            'failures':failures,'scope_results':{p:r for p,a,r in items},'visual_review_status':'pending',
            'required_visual_files':[p.relative_to(root).as_posix() for p in required_images(root)]})
        print({'numeric_disagreeing_scopes':len(failures),'visual_review':'pending'},flush=True);return
    assert not failures,failures
    journal=read_json(root/'visual-inspection-progress.json');viewed={r['file']:r['sha256'] for r in journal['viewed']}
    for p in required_images(root):assert viewed.get(p.relative_to(root).as_posix())==file_sha256(p),str(p)
    out=root/'completed-annotations';assert not out.exists()
    for path,annotation,result in items:
        annotation.update(status='completed',inspection_journal_sha256=file_sha256(root/'visual-inspection-progress.json'))
        write_json(out/path/'annotation.json',annotation)
    result=binder.bind(root,out);print(result,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('--complete',action='store_true');a=p.parse_args();run(a.root,a.complete)
