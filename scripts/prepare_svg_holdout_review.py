"""Freeze independent tasks for all sealed SVG holdout recordings."""
from pathlib import Path
import argparse
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.local_engine.review import prepare_review_group,prepare_scope_review

def run(validation,output):
    validation,output=Path(validation).resolve(),Path(output).resolve()
    if output.exists():raise ValueError('Fresh review directory required')
    result=read_json(validation/'validation.json')
    if result['passed']!=12 or result['status']!='pass':raise ValueError('Require finished holdout validation')
    items=[];inventory=[]
    for case in result['results']:
        for role in ('reference','before','candidate'):
            path=validation/'records'/case['scenario_id']/(role+'.json');record=read_json(path)
            items.append({**record,'role':role})
            inventory.append({'id':case['scenario_id']+'-'+role,'record_id':f'record-{len(items)-1:03}',
                'role':role,'record_path':str(path),'record_sha256':file_sha256(path),'package':record['package'],
                'package_sha256':record['observations']['package_sha256']})
    summary=prepare_review_group(items,output/'positions',tolerance_px=1.5,candidate_sha256=result['candidate_source_sha256'])
    write_json(output/'inventory.json',{'kind':'SVGHoldoutIndependentReviewInventory','status':'pending',
        'candidate_sha256':result['candidate_source_sha256'],'validation':str(validation),
        'validation_sha256':file_sha256(validation/'validation.json'),'records':inventory,'position_tasks':summary['control_points']})
    print(summary,flush=True)
    for record,row in zip(items,inventory):
        for scope in ('events','relationships'):
            receipt=prepare_scope_review(record['package'],record['observations'],record['events'],output/'records'/row['id']/scope,
                scope=scope,candidate_sha256=result['candidate_source_sha256'])
            row[scope+'_tasks']=receipt['tasks']
        print(row['id'],row['events_tasks'],row['relationships_tasks'],flush=True)
    inv=read_json(output/'inventory.json');inv['records']=inventory;inv['task_preparation_complete']=True
    write_json(output/'inventory.json',inv)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--validation',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.validation,a.output)
