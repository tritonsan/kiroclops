"""Prepare unsigned independent values and disagreements for a panel reviewer.

Raw HSV/outline proposals are measured separately from the implementation under
review. Expected values are used only for association and displaying differences,
never to manufacture an observation. No acceptance receipt is written here.
"""
from pathlib import Path
import argparse
import math
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest


def pair(a,b):
    al,at,aw,ah=a['box'];bl,bt,bw,bh=b['box'];ar,ab=al+aw,at+ah;br,bb=bl+bw,bt+bh
    area=max(0,min(ar,br)-max(al,bl))*max(0,min(ab,bb)-max(at,bt))
    return {'anchor_distance_px':math.dist(a['anchor'],b['anchor']),
            'edge_distance_px':math.hypot(max(al-br,bl-ar,0),max(at-bb,bt-ab,0)),
            'overlap_area_px2':area,'intersection_over_union':area/(aw*ah+bw*bh-area),'visually_overlapping':area>0}


def run(directory,raw):
    directory=Path(directory);raw=Path(raw)
    proposal_name='mass-event-proposals.json' if (raw/'mass-event-proposals.json').exists() else 'event-proposals.json'
    proposals=read_json(raw/proposal_name)['events']
    if (raw/'manual-event-marks.json').exists():
        marks=read_json(raw/'manual-event-marks.json')
        proposals=marks['events']
    geometry=read_json(raw/'geometry-proposals.json')
    objects={r['index']:r['objects'] for r in geometry['rows']}
    matched={};drafts={}
    request=read_json(directory/'events/request.json')
    for task in request['tasks']:
        e=task['measurement'];pool=[p for p in proposals if (p['type'],p['object_ids'])==(e['type'],e['object_ids'])]
        choice=min(pool,key=lambda p:abs(sum(p['observed_interval_s'])-sum(e['time_interval_s']))) if pool else None
        if choice and max(abs(a-b) for a,b in zip(choice['observed_interval_s'],e['time_interval_s']))>.4:choice=None
        key=(e['type'],tuple(e['object_ids']),tuple(e['time_interval_s']));matched[key]=choice
    for scope in ('events','relationships'):
        request=read_json(directory/scope/'request.json');rows=[]
        for task in request['tasks']:
            e=task['measurement'];observed=None;difference=None;reason=None
            if scope=='events':
                proposal=matched[e['type'],tuple(e['object_ids']),tuple(e['time_interval_s'])]
                if proposal:
                    observed=proposal['observed_interval_s'];difference=max(abs(a-b) for a,b in zip(observed,e['time_interval_s']))
                else:reason='No independently proposed matching event; raw sequence review needed.'
            elif e['type']=='relative_anchor':
                a,b=[objects[e['index']][oid]['anchor'] for oid in e['object_ids']]
                observed={'dx_px':a[0]-b[0],'dy_px':a[1]-b[1]}
            elif e['type']=='object_pair_geometry':observed=pair(*[objects[e['index']][oid] for oid in e['object_ids']])
            elif e['type']=='relative_event_timing':
                intervals=[]
                for oid in e['object_ids']:
                    ids=[oid]+([e['relative_to_object_id']] if e.get('relative_to_object_id') else [])
                    expected=sorted([(key[2],value) for key,value in matched.items() if key[0]==e['event_type'] and key[1]==tuple(ids)])
                    ordinal=e['occurrence'];value=expected[ordinal][1] if ordinal<len(expected) else None
                    intervals.append(value['observed_interval_s'] if value else None)
                if all(intervals):
                    (aa,ab),(ba,bb)=intervals
                    observed={'lag_interval_s':[ba-ab,bb-aa],
                        'order':'before' if ab<ba else 'after' if bb<aa else 'overlapping_uncertainty'}
                else:reason='One or more independent motion-event marks missing.'
            else:reason='Unsupported independent relationship review aid.'
            if scope=='relationships' and observed is not None:
                deltas={}
                for key,value in observed.items():
                    expected=e[key]
                    deltas[key]=(value==expected) if isinstance(value,(str,bool)) else (
                        max(abs(a-b) for a,b in zip(value,expected)) if isinstance(value,list) else abs(value-expected))
                difference=deltas
            rows.append({'id':task['id'],'type':e['type'],'objects':e.get('object_ids'),
                'source_frames':[f['index'] for f in task['frames']], 'frame_sha256s':[f['sha256'] for f in task['frames']],
                'independent_observation':observed,'difference':difference,'needs_manual_review':reason})
        draft={'kind':'UnsignedIndependentScopeDraft','status':'pending_reviewer','request_sha256':digest(request),
            'raw_geometry_sha256':file_sha256(raw/'geometry-proposals.json'),
            'raw_event_proposals_sha256':file_sha256(raw/('manual-event-marks.json' if (raw/'manual-event-marks.json').exists() else proposal_name)),
            'checks':rows}
        write_json(directory/scope/'draft.json',draft);drafts[scope]=draft
    return drafts


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('scopes',type=Path);parser.add_argument('raw',type=Path)
    args=parser.parse_args()
    for row in read_json(args.scopes/'inventory.json')['records']:
        result=run(args.scopes/row['id'],args.raw/row['id'])
        issues=[]
        for scope,value in result.items():
            for check in value['checks']:
                bad=check['independent_observation'] is None
                if scope=='events' and check['difference'] is not None:bad|=check['difference']>.05
                if scope=='relationships' and check['difference'] is not None:
                    bad|=any((not d if isinstance(d,bool) else d>(.05 if k=='lag_interval_s' else .02 if k=='intersection_over_union' else 2)) for k,d in check['difference'].items())
                if bad:issues.append({**check,'scope':scope})
        write_json(args.scopes/row['id']/'disagreements.json',issues)
        print(row['id'],len(issues),'disagreements',flush=True)
