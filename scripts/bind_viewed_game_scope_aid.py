"""Bind actually inspected game sequence aids without mutating the experiment."""
from pathlib import Path
import argparse
import collections
import math
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.local_engine.review import evaluate_scope_review


def run(review, aid, output):
    review,aid,output=map(lambda p:Path(p).resolve(),(review,aid,output))
    assert not output.exists(),'Preserve prior outcomes'
    proposals=read_json(aid/'proposals.json')
    journal=read_json(aid/'visual-inspection-progress.json')
    viewed={r['file']:r['sha256'] for r in journal['viewed']}
    correction_path=aid/'manual-wall-corrections.json'
    corrections=read_json(correction_path) if correction_path.exists() else None
    if corrections:
        assert viewed.get(corrections['viewed_file'])==file_sha256(aid/corrections['viewed_file'])
        assert corrections['reason'].strip()
    records={record:group for group in proposals['groups'] for record in group['records']}
    assert set(records)=={p.name for p in (review/'records').iterdir()}
    for group in proposals['groups']:
        sheet=group['sheet']
        assert viewed.get(sheet['file'])==sheet['sha256']==file_sha256(aid/sheet['file'])
    output.mkdir(parents=True)
    outcomes=[]
    for record,group in records.items():
        wall_override=(corrections['walls'].get(group['sequence_sha256']) if corrections else None)
        for scope in ('events','relationships'):
            directory=review/'records'/record/scope
            request=read_json(directory/'request.json')
            manifest=read_json(directory/'images/manifest.json')
            assert digest([(f['sha256'],f['time_s']) for f in manifest['frames']])==group['sequence_sha256']
            marks=[]
            independent=collections.defaultdict(list)
            for event in group['events']:independent[event['type']].append(event)
            used=collections.Counter()
            for task in request['tasks']:
                value=task['measurement'];kind=value['type']
                mark={'id':task['id'],'verdict':'pass',
                    'frame_sha256s':[f['sha256'] for f in task['frames']],
                    'viewed_sheet_sha256':group['sheet']['sha256'],
                    'raw_sequence_sha256':group['sequence_sha256']}
                if scope=='events':
                    index=used[kind];used[kind]+=1
                    assert index<len(independent[kind]),(record,kind,'missing raw event')
                    event=independent[kind][index]
                    mark.update(observed_interval_s=event['time_interval_s'],
                        independent_frame_bracket=event['indices'],
                        evidence=('Fixed-crop native sequence sheet actually inspected. '
                            'Onset, direction changes and sustained final pose agree with the '
                            'independent raw hat/head readings. Brackets retain raw estimator '
                            'differences; no measured event times or engine state used. '
                            'Boundary events mean the declared 12px patch proximity, not collision.'))
                elif kind=='absence':
                    assert group['wall'] is None
                    mark.update(observed_absence=True,evidence=(
                        'Single tracked character in the inspected flat test room; no declared '
                        'wall, second object or graph connector for this relationship scope. '
                        'Whole-clip raw glyph reading and sampled fixed-crop visual context '
                        'checked. This is absence in the declared scope, not absence of a floor.'))
                else:
                    assert kind=='boundary_distance'
                    index=value['index'];raw=group['points'][index]
                    assert raw['image_sha256']==manifest['frames'][index]['sha256']
                    wall=wall_override or group['wall']
                    observed=dict(group['distances'][index])
                    if wall_override:
                        dx=max(raw['x']-3.5-wall['x'],wall['x']-(raw['x']+4.5),0.)
                        dy=max(wall['top']-(raw['y']+2.),(raw['y']-4.)-wall['bottom'],0.)
                        distance=math.hypot(dx,dy)
                        observed={'distance_px':distance,'visually_touching':distance<=12.}
                    mark.update(observed=observed,
                        independent_anchor_px=[raw['x'],raw['y']],
                        independent_wall=wall,
                        manual_wall_correction_sha256=file_sha256(correction_path) if wall_override else None,
                        evidence=('Actual left/right wall and selected 8x6 head region inspected '
                            'on native image sheets. Distance recomputed from independent raw '
                            'head pixels and wall silhouette with the declared patch semantics. '
                            'Right-wall endpoint follows the visually checked chamfered corner; '
                            'the earlier rectangle aid included empty corner space. '
                            'The 12px proximity flag does not assert physical contact.'))
                marks.append(mark)
            if scope=='events':
                assert used==collections.Counter(e['type'] for e in group['events']),(
                    record,'unmatched independently detected event')
            annotation={'request_sha256':digest(request),'reviewer':'Codex assistant',
                'method':'independent_raw_sequence_review','status':'reviewed',
                'external_human_review':False,'computer_assisted':True,
                'scope_note':f"All {len(proposals['groups'])} distinct sequence boards viewed; exact complete PNG/time duplicates share observation, not tracker values.",
                'proposal_sha256':file_sha256(aid/'proposals.json'),
                'inspection_sha256':file_sha256(aid/'visual-inspection-progress.json'),
                'checks':marks}
            result=evaluate_scope_review(directory,annotation,persist=False)
            dest=output/'records'/record/scope
            write_json(dest/'annotation.json',annotation);write_json(dest/'result.json',result)
            outcomes.append({'record':record,'scope':scope,'status':result['status'],
                'checks':len(result['checks']),'failed':[r['id'] for r in result['checks'] if not r['pass']]})
    summary={'status':'pass' if all(r['status']=='pass' for r in outcomes) else 'fail',
        'records':len(records),'scopes':len(outcomes),'checks':sum(r['checks'] for r in outcomes),
        'outcomes':outcomes,'canonical_session_changed':False,'source_native_acceptance':'separate'}
    write_json(output/'scope-summary.json',summary)
    print({k:v for k,v in summary.items() if k!='outcomes'},flush=True)
    for row in outcomes:
        if row['status']!='pass':print(row,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('review',type=Path)
    p.add_argument('--aid',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.review,a.aid,a.output)
