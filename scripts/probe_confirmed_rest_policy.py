"""Read-only engineering prototype for the stale rest-anchor defect.

The candidate detector is compiled from a copy of the source. Neither the
running implementation nor any historical evidence is changed. The independent
forward-scanning raw-pixel aid is separately adapted to the declared new rule.
No visual-review approval is produced.
"""
from pathlib import Path
from collections import defaultdict
import argparse
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256


def module_copy(path,name,replacements):
    source=path.read_text(encoding='utf-8')
    for old,new in replacements:
        if source.count(old)!=1:raise ValueError(f'Prototype source changed: {old}')
        source=source.replace(old,new)
    spec=importlib.util.spec_from_loader(name,loader=None)
    module=importlib.util.module_from_spec(spec);module.__file__=str(path)
    exec(compile(source,str(path), 'exec'),module.__dict__)
    return module,source


def run(output):
    output=Path(output)
    if output.exists():raise ValueError('Fresh output required')
    producer,producer_text=module_copy(ROOT/'kiroclops/visual/events.py','kiroclops.visual.events_precision_prototype',[
        ('active=False;episode=[];direction=0;extreme=None;turn=None\n        previous=frame',
         'active=False;episode=[];direction=0;extreme=None;turn=None;quiet=[frame]\n        previous=frame')])
    raw,raw_text=module_copy(ROOT/'artifacts/phase3a/tools/raw_motion_event_proposals_v3.py','raw_confirmed_rest_prototype',[
        ('cursor=quiet;search_from=quiet_end+1','cursor=quiet_end;search_from=quiet_end+1')])
    output.mkdir(parents=True)
    (output/'producer-prototype.py.txt').write_text(producer_text,encoding='utf-8')
    (output/'independent-prototype.py.txt').write_text(raw_text,encoding='utf-8')
    review=ROOT/'artifacts/phase3a/panel-recorder-independent-review-01'
    aids=ROOT/'artifacts/phase3a/panel-recorder-scope-aids-01/raw'
    results=[]
    for entry in read_json(review/'inventory.json')['records']:
        record=read_json(entry['record_path']);data=read_json(aids/entry['id']/'raw-proposals.json')
        eventset=producer.extract_events(record['observations'],record['selection'],
                                        {'event_options':{'rest_duration_s':.25}})
        expected=defaultdict(list);actual=defaultdict(list)
        for event in eventset['events']:
            if event['type'] in ('motion_start','stop','settled','reversal'):
                expected[(tuple(event['object_ids']),event['type'])].append(event)
        lifetimes={o['id']:o['active_intervals_s'] for o in record['observations']['objects']}
        for ids in (['bookmark'],['handle'],['star'],['bookmark','handle'],['star','handle']):
            for event in raw.candidates(data['rows'],ids,active_intervals_s=lifetimes):
                if event['type'] in ('motion_start','stop','settled','reversal'):
                    actual[(tuple(event['object_ids']),event['type'])].append(event)
        failures=[];count=0
        for key in sorted(set(expected)|set(actual)):
            a,b=expected[key],actual[key]
            if len(a)!=len(b):failures.append({'objects':key[0],'type':key[1],'count':[len(a),len(b)]});continue
            for index,(x,y) in enumerate(zip(a,b)):
                count+=1
                delta=max(abs(p-q) for p,q in zip(x['time_interval_s'],y['observed_interval_s']))
                if delta>.05:failures.append({'objects':key[0],'type':key[1],'occurrence':index,
                    'difference_s':delta,'produced':x['time_interval_s'],'independent':y['observed_interval_s']})
        result={'record':entry['id'],'checked_events':count,'failures':failures}
        results.append(result)
        write_json(output/'events'/(entry['id']+'.json'),eventset)
    summary={'kind':'RestAnchorEngineeringPrototype','status':'diagnostic_only_not_acceptance',
        'records':len(results),'checked_events':sum(r['checked_events'] for r in results),
        'disagreement_count':sum(len(r['failures']) for r in results),'rows':results,
        'rule':'After confirmed bounded rest, the next departure uses the confirmation image; never the stale beginning of the previous quiet window.',
        'thresholds_changed':False,'raw_pixel_proposals_reused':True,'visual_review_approved':False}
    write_json(output/'result.json',summary)
    print({k:summary[k] for k in ('records','checked_events','disagreement_count')},flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args().output)
