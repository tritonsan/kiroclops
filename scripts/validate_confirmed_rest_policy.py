"""Independent raw-pixel regression, not a signed visual acceptance receipt."""
from pathlib import Path
from collections import defaultdict
import argparse
import importlib.util
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.visual.events import extract_events


def run(output):
    output=Path(output)
    if output.exists():raise ValueError('Fresh output required')
    helper=ROOT/'artifacts/phase3a/tools/raw_motion_event_proposals_v4.py'
    spec=importlib.util.spec_from_file_location('independent_raw_events',helper)
    raw=importlib.util.module_from_spec(spec);spec.loader.exec_module(raw)
    inventory=ROOT/'artifacts/phase3a/panel-recorder-independent-review-01/inventory.json'
    summaries=[]
    for entry in read_json(inventory)['records']:
        path=Path(entry['record_path'])
        if file_sha256(path)!=entry['record_sha256']:raise ValueError('Historical record changed')
        record=read_json(path)
        raw_path=ROOT/'artifacts/phase3a/panel-recorder-scope-aids-01/raw'/entry['id']/'raw-proposals.json'
        data=read_json(raw_path)
        events=extract_events(record['observations'],record['selection'],{'event_options':{'rest_duration_s':.25}})
        actual=defaultdict(list);expected=defaultdict(list)
        lifetimes={o['id']:o['active_intervals_s'] for o in record['observations']['objects']}
        for ids in (['bookmark'],['handle'],['star'],['bookmark','handle'],['star','handle']):
            for event in raw.candidates(data['rows'],ids,active_intervals_s=lifetimes):
                actual[tuple(ids),event['type']].append(event)
        types={'motion_start','stop','settled','reversal'}
        for event in events['events']:
            if event['type'] in types:expected[tuple(event['object_ids']),event['type']].append(event)
        checks=[]
        for key in sorted(set(expected)|{key for key in actual if key[1] in types}):
            a,b=expected[key],actual[key]
            checks.append({'kind':'event_count','objects':key[0],'type':key[1],
                           'counts':[len(a),len(b)],'pass':len(a)==len(b)})
            for i,(e,observed) in enumerate(zip(a,b)):
                difference=max(abs(x-y) for x,y in zip(e['time_interval_s'],observed['observed_interval_s']))
                checks.append({'kind':'event_time','objects':key[0],'type':key[1],'occurrence':i,
                    'difference_s':difference,'pass':difference<=.05,
                    'measured_interval_s':e['time_interval_s'],
                    'independent_interval_s':observed['observed_interval_s']})
        cadence=statistics.median(b['time_s']-a['time_s'] for a,b in zip(data['rows'],data['rows'][1:]))
        resolution=min(.1,max(.05,1.5*cadence))
        for relation in events['relations']:
            if relation['type']!='relative_event_timing':continue
            parent=relation.get('relative_to_object_id');times=[]
            for oid in relation['object_ids']:
                key=tuple([oid]+([parent] if parent else [])),relation['event_type']
                pool=actual[key];ordinal=relation['occurrence']
                times.append(pool[ordinal]['observed_interval_s'] if ordinal<len(pool) else None)
            row={'kind':'relative_event_timing','objects':relation['object_ids'],
                 'relative_to':parent,'event_type':relation['event_type'],'occurrence':relation['occurrence'],'pass':False}
            if all(times):
                (aa,ab),(ba,bb)=times;lag=[ba-ab,bb-aa]
                order='before' if lag[0]>resolution else 'after' if lag[1]<-resolution else 'overlapping_uncertainty'
                difference=max(abs(a-b) for a,b in zip(lag,relation['lag_interval_s']))
                row.update(independent_lag_s=lag,independent_order=order,difference_s=difference,
                           pass_=difference<=.05 and order==relation['order'])
                row['pass']=row.pop('pass_')
            checks.append(row)
        failures=[c for c in checks if not c['pass']]
        result={'record':entry['id'],'record_sha256':file_sha256(path),'raw_sha256':file_sha256(raw_path),
            'observation_sha256':digest(record['observations']),
            'event_checks':sum(c['kind']=='event_time' for c in checks),
            'order_checks':sum(c['kind']=='relative_event_timing' for c in checks),
            'failures':failures,'checks':checks}
        write_json(output/'records'/(entry['id']+'.json'),result)
        write_json(output/'events'/(entry['id']+'.json'),events)
        summaries.append({k:v for k,v in result.items() if k!='checks'})
    value={'kind':'IndependentPixelEventRegression','status':'pass' if all(not r['failures'] for r in summaries) else 'fail',
        'records':len(summaries),'event_checks':sum(r['event_checks'] for r in summaries),
        'order_checks':sum(r['order_checks'] for r in summaries),'failures':sum(len(r['failures']) for r in summaries),
        'rows':summaries,'producer_sha256':file_sha256(ROOT/'kiroclops/visual/events.py'),
        'independent_helper_sha256':file_sha256(helper),'runner_sha256':file_sha256(__file__),
        'position_measurements_reused':True,'visual_acceptance':'pending',
        'scope':'Primary motion events and their temporal relations; no automated signature of independent visual review.'}
    write_json(output/'result.json',value)
    print({k:value[k] for k in ('status','records','event_checks','order_checks','failures')})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args().output)
