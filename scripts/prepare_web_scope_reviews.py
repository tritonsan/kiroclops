"""Freeze event/relation review tasks; reuse image marks only by exact identity.

No event or relation verdict is produced by this command. Position annotations
can be rebound only when their original raw-frame SHA, reference selection
anchor definition, and object identity are unchanged.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256,VisualError
from kiroclops.local_engine.review import prepare_scope_review,prepare_review_group,evaluate_review


def records(corpus,domain):
    corpus=Path(corpus);index=read_json(corpus/'capture-index.json');result=[]
    for row in index['references']:
        if row['domain']!=domain:continue
        analysis=read_json(corpus/f'analysis/references/{domain}/{row["scenario_index"]}/analysis.json')
        result.append({'id':f'reference-{row["scenario_index"]}','package':row['record']['package'],
            'observations':analysis['observations'],'events':analysis['events'],'role':'reference'})
    for row in index['cases']:
        if row['case']['domain']!=domain:continue
        analysis=read_json(corpus/f'analysis/{row["case"]["id"]}/analysis.json')
        result.append({'id':row['case']['id'],'package':row['record']['package'],'observations':analysis['observations'],
            'events':analysis['events'],'role':'baseline' if row['case']['mutation']=='baseline' else 'qualification_candidate'})
    return result


def rebind_positions(old_dir,items,output):
    old_dir=Path(old_dir);old=read_json(old_dir/'request.json');marks=read_json(old_dir/'annotation.json')
    # First replay the actual earlier signed review, never trust a summary flag.
    if evaluate_review(old_dir,marks,persist=False)['status']!='pass':
        raise VisualError('invalid_review','Previous independent marks did not replay')
    old_by_id={r['id']:r for r in old['records']}
    old_objects={r['id']:{o['id']:o for o in read_json(old_dir/r['observations'])['selection']['objects']} for r in old['records']}
    new_items=[{k:r[k] for k in ('package','observations','role')} for r in items]
    prepare_review_group(new_items,output,tolerance_px=old['tolerance_px'])
    request=read_json(Path(output)/'request.json');new_by_id={r['id']:r for r in request['records']}
    # Observations may now include independently measured outer geometry. The
    # visible identity point must retain the exact original selection ROI/anchor.
    def identity(obj):
        selection=obj.get('selection',obj)
        return {k:selection.get(k) for k in ('id','roi','anchor','frame_index','method')}
    for i,item in enumerate(items):
        rid=f'record-{i:03}'
        if (old_by_id[rid]['package_sha256'],old_by_id[rid]['role'])!=(new_by_id[rid]['package_sha256'],new_by_id[rid]['role']):
            raise VisualError('stale_review','Recording identity or role differs')
        for obj in item['observations']['selection']['objects']:
            if identity(obj)!=identity(old_objects[rid][obj['id']]):
                raise VisualError('stale_review','Visible identity anchor definition changed')
    original={(p['record_id'],p['object_id'],p['image_sha256']):p for p in marks['points']}
    points=[]
    for task in request['tasks']:
        key=task['record_id'],task['object_id'],task['image_sha256']
        if key not in original:raise VisualError('incomplete_review','A newly selected image has not been independently marked')
        point=deepcopy(original[key]);point['index']=task['index'];points.append(point)
    annotation={**deepcopy(marks),'request_sha256':digest(request),'points':points,
        'reuse_provenance':{'request_sha256':digest(old),'annotation_sha256':digest(marks),
            'review_result_sha256':file_sha256(old_dir/'review-result.json'),
            'rule':'Exact raw image SHA, object identity, role and selected identity-point semantics; measurements are reevaluated.'}}
    result=evaluate_review(output,annotation)
    return {'status':result['status'],'points':len(points)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);parser.add_argument('--domain',choices=['panel','svg'],required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--position-review',type=Path)
    args=parser.parse_args();items=records(args.corpus,args.domain)
    if args.output.exists():raise VisualError('output_exists','Preserve prior review evidence')
    results=[]
    if args.position_review:
        print('positions',rebind_positions(args.position_review,items,args.output/'positions'),flush=True)
    for item in items:
        row={'id':item['id'],'role':item['role'],'package':str(item['package']),
            'package_sha256':item['observations']['package_sha256'],'observations_sha256':digest(item['observations'])}
        for scope in ('events','relationships'):
            directory=args.output/item['id']/scope
            row[scope]=prepare_scope_review(item['package'],item['observations'],item['events'],directory,scope=scope)
        results.append(row);print(item['id'],row['events']['tasks'],row['relationships']['tasks'],flush=True)
    write_json(args.output/'inventory.json',{'corpus':str(args.corpus),'records':results,'status':'pending_independent_scope_marks'})


if __name__=='__main__':main()
