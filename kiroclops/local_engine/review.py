"""Independent raw-image control points, bound to exact frame and package hashes."""
from __future__ import annotations

import json
import math
import ntpath
from pathlib import Path
import re
import statistics
import hashlib
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

import numpy as np

from kiroclops.visual.package import copy_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json, finite, file_sha256, safe_path


def prepare_review(package, observations, output, *, tolerance_px, points_per_object=20, candidate_sha256=None):
    return prepare_review_group([{'package':package,'observations':observations,'role':'default'}],output,
        tolerance_px=tolerance_px,points_per_object=points_per_object,candidate_sha256=candidate_sha256)


def prepare_review_group(records, output, *, tolerance_px, points_per_object=20, candidate_sha256=None):
    """Choose twenty distinct images across clips, separately for each object/role.

    Hashes are reproducibility bindings in an operator-owned experiment, not a
    signature or a security boundary against somebody editing all evidence.
    """
    if points_per_object<20:raise VisualError('insufficient_review','At least twenty distinct control frames per object')
    tolerance_px=finite(tolerance_px,'position tolerance',minimum=0)
    output=Path(output)
    if output.exists():raise VisualError('output_exists','Keep earlier independent evidence unchanged')
    if not records:raise VisualError('insufficient_review','At least one pixel recording is required')
    bound=[]; pools={}; critical={}
    for number,record in enumerate(records):
        manifest=load_package(record['package']);observations=record['observations']
        if observations.get('package_sha256')!=manifest['package_sha256']:
            raise VisualError('wrong_result','Observations belong to a different image package')
        role=str(record.get('role','default'));rid=f'record-{number:03}'
        bound.append({'id':rid,'role':role,'package_sha256':manifest['package_sha256'],
            'package':f'images/{rid}','observations':f'observations/{rid}.json',
            'observations_sha256':digest(observations),'image_size':manifest['image_size']})
        for obj in observations['objects']:
            key=role,obj['id'];pool=pools.setdefault(key,[]);required=critical.setdefault(key,set())
            active=obj.get('active_intervals_s',[[manifest['frames'][0]['time_s'],manifest['frames'][-1]['time_s']]])
            indexes=[f['index'] for f in manifest['frames'] if any(a<=f['time_s']<=b for a,b in active)]
            if not indexes:continue
            for index in indexes:
                frame=manifest['frames'][index]
                pool.append({'record_id':rid,'role':role,'package_sha256':manifest['package_sha256'],
                    'object_id':obj['id'],'index':index,'time_s':frame['time_s'],
                    'image':f'images/{rid}/'+frame['path'],'image_sha256':frame['sha256']})
            event_times=[e['time_s'] for e in manifest['inputs']['events']]
            for event in record.get('events',{}).get('events',[]):event_times.extend(event.get('time_interval_s',[]))
            for stamp in event_times:
                near=min(indexes,key=lambda i:abs(manifest['frames'][i]['time_s']-stamp))
                required.update((rid,i) for i in (near-1,near,near+1) if i in indexes)
    tasks=[]
    for key,pool in pools.items():
        unique={}
        for task in pool:unique.setdefault(task['image_sha256'],task)
        distinct=list(unique.values())
        if len(distinct)<points_per_object:
            raise VisualError('insufficient_review',f'{key} has fewer than twenty distinct images; group additional recordings')
        chosen={(distinct[int(i)]['record_id'],distinct[int(i)]['index']) for i in np.linspace(0,len(distinct)-1,points_per_object)}
        chosen.update(critical[key]);tasks.extend(t for t in pool if (t['record_id'],t['index']) in chosen)
    if not tasks:raise VisualError('insufficient_review','No object control points')
    for record,item in zip(records,bound):
        copy_package(record['package'],output/item['package']);write_json(output/item['observations'],record['observations'])
    request={'schema_version':2,'kind':'IndependentReviewRequest','records':bound,
             'package_sha256':bound[0]['package_sha256'] if len(bound)==1 else None,
             'candidate_sha256':candidate_sha256,'tolerance_px':tolerance_px,
             'points_per_object':points_per_object,'tasks':tasks,
             'status':'pending','instruction':'Mark the same physical anchor in raw images. No predicted positions are shown.'}
    write_json(output/'request.json',request)
    write_json(output/'binding.json',{'request_sha256':digest(request)})
    script=json.dumps({**request,'request_sha256':digest(request)},ensure_ascii=False).replace('</','<\\/')
    (output/'index.html').write_text(REVIEW_HTML.replace('__DATA__',script),encoding='utf-8')
    return {'status':'pending','review':str(output/'index.html'),'request':str(output/'request.json'),'control_points':len(tasks)}


def _load_request(directory):
    directory=Path(directory);request=read_json(directory/'request.json')
    if read_json(directory/'binding.json').get('request_sha256')!=digest(request):
        raise VisualError('stale_review','The frozen review tasks or tolerance changed')
    records={};measured={}
    for record in request['records']:
        manifest=load_package(safe_path(directory,record['package'],must_exist=False));obs=read_json(safe_path(directory,record['observations']))
        if manifest['package_sha256']!=record['package_sha256'] or digest(obs)!=record['observations_sha256']:
            raise VisualError('stale_review','Review pixels or observations changed')
        records[record['id']]=(record,manifest)
        for obj in obs['objects']:measured[record['id'],obj['id']]={f['index']:f for f in obj['frames']}
    for task in request['tasks']:
        record,manifest=records[task['record_id']];frame=manifest['frames'][task['index']]
        if (task['package_sha256']!=record['package_sha256'] or task['role']!=record['role'] or
            task['image_sha256']!=frame['sha256'] or task['time_s']!=frame['time_s'] or
            task['image']!=record['package']+'/'+frame['path'] or (record['id'],task['object_id']) not in measured):
            raise VisualError('stale_review','A selected control point differs from its actual frame')
    return request,records,measured


def evaluate_review(directory, annotation, *, persist=True):
    directory=Path(directory);request,records,measured=_load_request(directory)
    if annotation.get('request_sha256')!=digest(request):raise VisualError('stale_review','Annotation belongs to another review request')
    if not annotation.get('reviewer') or annotation.get('method')!='independent_raw_image_marking':
        raise VisualError('invalid_review','Independent reviewer and method must be recorded')
    expected={(t['record_id'],t['object_id'],t['index']):t for t in request['tasks']}
    if len(expected)!=len(request['tasks']):raise VisualError('invalid_review','Duplicate frozen task')
    groups={(t['role'],t['object_id']) for t in request['tasks']}
    seen=set();errors={key:[] for key in groups};invalid={key:0 for key in groups}
    for point in annotation.get('points',[]):
        key=point.get('record_id'),point.get('object_id'),point.get('index')
        if key not in expected or key in seen:raise VisualError('invalid_review','Unknown or duplicated control point')
        seen.add(key);target=expected[key]
        if point.get('image_sha256')!=target['image_sha256']:
            raise VisualError('stale_review','A control frame was changed or substituted')
        x=finite(point.get('x'),'marked x');y=finite(point.get('y'),'marked y')
        width,height=records[key[0]][0]['image_size'];group=target['role'],target['object_id']
        if not 0<=x<width or not 0<=y<height:raise VisualError('invalid_review','Marked anchor outside image')
        frame=measured[key[:2]].get(key[2])
        if not frame or not frame.get('valid'):
            invalid[group]+=1;continue
        errors[group].append(math.hypot(x-frame['x'],y-frame['y']))
    rows=[]
    for group,values in sorted(errors.items()):
        relevant={k for k,t in expected.items() if (t['role'],t['object_id'])==group}
        required=len(relevant);count=len(seen&relevant)
        unique_images=len({expected[k]['image_sha256'] for k in seen&relevant})
        median=statistics.median(values) if values else None
        rows.append({'role':group[0],'object_id':group[1],'required':required,'marked':count,'unique_images':unique_images,'invalid_measurements':invalid[group],
                     'median_error_px':median,'tolerance_px':request['tolerance_px'],
                     'pass':count==required and unique_images>=20 and len(values)/required>=.95 and median is not None and median<=request['tolerance_px']})
    complete=seen==set(expected)
    result={'schema_version':1,'kind':'IndependentPositionReview','status':'pass' if complete and all(o['pass'] for o in rows) else 'fail',
            'reviewer':annotation['reviewer'],'method':annotation['method'],'package_sha256':request['package_sha256'],
            'annotation_sha256':digest(annotation),'request_sha256':digest(request),'records':request['records'],
            'candidate_sha256':request.get('candidate_sha256'),'objects':rows,
            'event_review_status':'pending','relationship_review_status':'pending',
            'scope':'Position controls only; event timing and relationship acceptance need separate evidence.'}
    if persist:write_json(directory/'annotation.json',annotation);write_json(directory/'review-result.json',result)
    return result


def prepare_scope_review(package, observations, events, output, *, scope, candidate_sha256=None):
    """Freeze raw-frame tasks for a separate event or visible-relation review.

    A reviewer records independently observed times or geometry. An empty event
    set still requires a whole-clip absence review; it never passes vacuously.
    """
    if scope not in ('events','relationships'):raise VisualError('invalid_review','Unknown review scope')
    output=Path(output);manifest=load_package(package)
    if output.exists():raise VisualError('output_exists','Use a fresh review directory')
    if observations.get('package_sha256')!=manifest['package_sha256'] or events.get('observation_sha256')!=digest(observations):
        raise VisualError('stale_review','Events must bind the exact observations and pixels')
    tasks=[];frames=manifest['frames']
    median=statistics.median(b['time_s']-a['time_s'] for a,b in zip(frames,frames[1:]))
    tolerance_s=min(.1,max(.05,1.5*median))
    values=events.get('events' if scope=='events' else 'relations',[])
    if scope=='relationships':
        grouped={}
        for value in values:
            key=(value.get('type'),value.get('connection_id'),value.get('object_id'),value.get('boundary_id'),
                 tuple(value.get('object_ids',[])),value.get('relative_to_object_id'),value.get('event_type'),value.get('occurrence'))
            grouped.setdefault(key,[]).append(value)
        values=[]
        for group in grouped.values():
            indexes={int(i) for i in np.linspace(0,len(group)-1,min(20,len(group)))}
            for i in range(1,len(group)):
                if group[i].get('visually_attached',group[i].get('visually_touching'))!=group[i-1].get('visually_attached',group[i-1].get('visually_touching')):indexes.update((i-1,i))
            values.extend(group[i] for i in sorted(indexes))
    for number,value in enumerate(values):
        indexes=value.get('frame_indices',value.get('indices',[value['index']] if 'index' in value else []))
        if not indexes:indexes=value.get('reference_frame_indices',[])+value.get('candidate_frame_indices',[])
        if not indexes and 'time_interval_s' in value:
            indexes=[min(range(len(frames)),key=lambda i:abs(frames[i]['time_s']-t)) for t in value['time_interval_s']]
        if not indexes:raise VisualError('invalid_review','Visual event has no source frames')
        tasks.append({'id':f'{scope}-{number:04}','measurement':value,'frames':[
            {'index':i,'time_s':frames[i]['time_s'],'sha256':frames[i]['sha256']} for i in sorted(set(indexes))]})
    if not tasks:
        tasks=[{'id':'absence','measurement':{'type':'absence','scope':scope},'frames':[
            {'index':f['index'],'time_s':f['time_s'],'sha256':f['sha256']} for f in frames]}]
    copy_package(package,output/'images');write_json(output/'observations.json',observations);write_json(output/'events.json',events)
    request={'schema_version':1,'kind':'IndependentScopeReviewRequest','scope':scope,'candidate_sha256':candidate_sha256,
        'package_sha256':manifest['package_sha256'],'observations_sha256':digest(observations),'events_sha256':digest(events),
        'event_tolerance_s':tolerance_s,'tasks':tasks,'scope_note':'Independent visible behavior review; not engine collision or graph-state proof.'}
    write_json(output/'request.json',request);write_json(output/'binding.json',{'request_sha256':digest(request)})
    return {'status':'pending','request':str(output/'request.json'),'request_sha256':digest(request),'tasks':len(tasks)}


def evaluate_scope_review(directory, annotation, *, persist=True):
    directory=Path(directory);request=read_json(directory/'request.json')
    if read_json(directory/'binding.json').get('request_sha256')!=digest(request) or annotation.get('request_sha256')!=digest(request):
        raise VisualError('stale_review','Scoped review request changed')
    manifest=load_package(directory/'images');obs=read_json(directory/'observations.json');events=read_json(directory/'events.json')
    if (manifest['package_sha256']!=request['package_sha256'] or digest(obs)!=request['observations_sha256'] or digest(events)!=request['events_sha256']):
        raise VisualError('stale_review','Scoped image or analysis evidence changed')
    if not annotation.get('reviewer') or annotation.get('method')!='independent_raw_sequence_review':
        raise VisualError('invalid_review','Record the independent raw sequence reviewer')
    marked={a.get('id'):a for a in annotation.get('checks',[])}
    if len(marked)!=len(annotation.get('checks',[])):raise VisualError('invalid_review','Duplicate scoped review item')
    rows=[]
    for task in request['tasks']:
        for f in task['frames']:
            actual=manifest['frames'][f['index']]
            if actual['sha256']!=f['sha256'] or actual['time_s']!=f['time_s']:raise VisualError('stale_review','Changed scoped frame')
        mark=marked.get(task['id'],{});value=task['measurement']
        good=(mark.get('verdict')=='pass' and isinstance(mark.get('evidence'),str) and bool(mark['evidence'].strip()) and
              mark.get('frame_sha256s')==[f['sha256'] for f in task['frames']])
        if value['type']=='absence':good &= mark.get('observed_absence') is True
        elif request['scope']=='events':
            interval=mark.get('observed_interval_s',[]);expected=value['time_interval_s']
            good &= isinstance(interval,list) and len(interval)==2
            good &= expected[1]-expected[0]<=.1000001 and value.get('timing_status','resolved')=='resolved'
            if good:
                interval=[finite(t,'independent event time') for t in interval]
                good &= interval[0]<=interval[1] and max(abs(a-b) for a,b in zip(interval,expected))<=request['event_tolerance_s']
        else:
            expected={k:value[k] for k in ('distance_px','endpoint_distances_px','visually_attached','visually_touching',
                'anchor_distance_px','edge_distance_px','overlap_area_px2','intersection_over_union','visually_overlapping',
                'dx_px','dy_px','lag_interval_s','order') if k in value}
            observed=mark.get('observed',{})
            good &= bool(expected) and set(observed)==set(expected)
            for key,wanted in expected.items():
                if isinstance(wanted,bool):good &= observed.get(key) is wanted
                elif isinstance(wanted,str):good &= observed.get(key)==wanted
                elif isinstance(wanted,list):
                    actual=observed.get(key,[]);good &= isinstance(actual,list) and len(actual)==len(wanted)
                    tolerance=request['event_tolerance_s'] if key=='lag_interval_s' else 2
                    if good:good &= all(abs(finite(a,'independent distance or time')-b)<=tolerance for a,b in zip(actual,wanted))
                elif key in observed:
                    tolerance=.02 if key=='intersection_over_union' else 2
                    good &= abs(finite(observed[key],'independent distance')-wanted)<=tolerance
            if value.get('type')=='relative_event_timing':good &= value.get('timing_status')=='resolved'
        rows.append({'id':task['id'],'pass':bool(good)})
    complete=set(marked)=={t['id'] for t in request['tasks']}
    result={'schema_version':1,'kind':'IndependentScopeReview','scope':request['scope'],
        'status':'pass' if complete and rows and all(r['pass'] for r in rows) else 'fail',
        'request_sha256':digest(request),'annotation_sha256':digest(annotation),'package_sha256':request['package_sha256'],
        'observations_sha256':request['observations_sha256'],'events_sha256':request['events_sha256'],
        'candidate_sha256':request.get('candidate_sha256'),'reviewer':annotation['reviewer'],'checks':rows}
    if persist:write_json(directory/'annotation.json',annotation);write_json(directory/'review-result.json',result)
    return result


def _reference(base, value):
    path=Path(value)
    return path if path.is_absolute() else Path(base)/path


def _final_raw_visual_review(path, receipt, *, candidate_sha256=None,
                             session_directory=None, initial_acceptance_directory=None,
                             check_declared=True):
    """Replay a distinct final aid; never synthesize legacy review tasks."""
    from . import suite_final_raw_review as final
    from .session import Session, _ordinary

    def need(value, message, code='invalid_review'):
        if not value:raise VisualError(code, message)

    need(receipt.get('kind')=='IndependentFinalRawVisualReview' and receipt.get('schema_version')==1
         and receipt.get('status')=='pass' and receipt.get('method')=='replayed_final_raw_measurement_inspection'
         and receipt.get('source_code_review_status')=='pending' and receipt.get('full_behavior_acceptance') is False
         and not any(key in receipt for key in ('position_reviews','events_reviews','relationships_reviews')),
         'Expected a final-only raw inspection receipt with separate source/behavior acceptance.')
    path=_ordinary(path)
    declared_session=receipt.get('session_directory');declared_initial=receipt.get('initial_acceptance_directory')
    need((declared_session is None)!=(declared_initial is None), 'Bind exactly one authoritative final seal.')
    need(not (session_directory is not None and initial_acceptance_directory is not None),
         'Choose one authoritative final seal.')
    # Existing callers consume session/independent_review.json without passing
    # a directory. Its actual containing Session is authoritative even when
    # another Session happened to produce identical candidate bytes.
    containing_session=path.parent if (path.parent/'session.json').is_file() else None
    authoritative_session=session_directory if session_directory is not None else containing_session
    raw_value=receipt.get('raw_review_directory')
    need(isinstance(raw_value,str) and raw_value.strip(), 'Actual final raw review directory is missing.')
    raw_directory=_ordinary(_reference(path.parent,raw_value))
    if declared_session is not None:
        need(isinstance(declared_session,str) and declared_session.strip() and initial_acceptance_directory is None,
             'A selected Session cannot use an unchanged-SVG seal.', 'wrong_final_seal')
        declared_session=_ordinary(_reference(path.parent,declared_session))
        authoritative_session=_ordinary(authoritative_session or declared_session)
        need(authoritative_session==declared_session and (containing_session is None or authoritative_session==containing_session),
             'Final review belongs to another authoritative Session.', 'wrong_final_seal')
        state=Session(authoritative_session)._load()
        selected=state.get('selected_candidate_id')
        choices=[c for c in state.get('candidates',[]) if c.get('candidate_id')==selected]
        need(selected and len(choices)==1, 'Current Session has no uniquely selected candidate.', 'stale_review')
        selected_id=selected;expected_source=choices[0]['source_sha256']
        need(receipt.get('selected_candidate_id')==selected_id, 'Final review names another selected candidate.', 'stale_review')
        proof_arguments={'session_directory':authoritative_session}
        seal_kind='SealedSessionFinalRawMatrix'
        declared_session=str(authoritative_session);declared_initial=None
    else:
        need(authoritative_session is None and isinstance(declared_initial,str) and declared_initial.strip(),
             'A Session consumer cannot accept a standalone unchanged-source seal.', 'wrong_final_seal')
        declared_initial=_ordinary(_reference(path.parent,declared_initial))
        if initial_acceptance_directory is not None:
            need(declared_initial==_ordinary(initial_acceptance_directory), 'Unchanged SVG review belongs to another initial run.', 'wrong_final_seal')
        from .suite_initial_acceptance import load_plan
        plan,_=load_plan(declared_initial)
        expected_source=plan['unchanged_source_sha256'];selected_id=None
        need(receipt.get('selected_candidate_id') is None, 'Unchanged historical SVG has no new selected candidate.', 'stale_review')
        proof_arguments={'initial_acceptance_directory':declared_initial}
        seal_kind='UnchangedHistoricalSVGFinalRawMatrix'
        declared_initial=str(declared_initial);declared_session=None
    need(isinstance(expected_source,str) and expected_source and receipt.get('candidate_sha256')==expected_source
         and (candidate_sha256 is None or candidate_sha256==expected_source), 'Raw review belongs to another full source snapshot.', 'stale_review')
    result=read_json(raw_directory/'result.json')
    if result.get('kind')=='FinalOnlyUnsignedPanelReviewAid' or result.get('domain')=='panel':
        from . import suite_final_panel_raw_review as panel
        need(result.get('kind')=='FinalOnlyUnsignedPanelReviewAid' and result.get('schema_version')==1
             and result.get('version')==panel.VERSION and result.get('domain')=='panel'
             and declared_session is not None and state.get('domain')=='panel',
             'Panel final aid requires its exact helper version and authoritative panel Session.', 'wrong_final_seal')
        proof=panel.validate_review(raw_directory,session_directory=authoritative_session)
    else:
        proof=final.validate_review(raw_directory,**proof_arguments)
    need(proof.get('kind')=='VerifiedFinalRawMeasurementInspection' and proof.get('status')=='pass'
         and proof.get('source_code_review_status')=='pending' and proof.get('full_behavior_acceptance') is False
         and proof.get('sealed_source_sha256')==expected_source and result.get('sealed_source_sha256')==expected_source
         and result.get('selected_candidate_id')==selected_id and result.get('seal_kind')==seal_kind
         and proof.get('result_sha256')==digest(result)
         and proof.get('attestation_sha256')==file_sha256(raw_directory/'attestation.json'),
         'The final raw aid did not replay against this exact immutable seal.', 'stale_review')
    observations=proof.get('required_observations')
    need(isinstance(observations,(list,tuple)) and observations and all(
         isinstance(pair,(list,tuple)) and len(pair)==2 and all(isinstance(v,str) and v for v in pair) for pair in observations),
         'Complete raw observation coverage is missing.', 'incomplete_review')
    canonical={'kind':'IndependentFinalRawVisualReview','schema_version':1,'status':'pass',
        'method':'replayed_final_raw_measurement_inspection','session_directory':declared_session,
        'initial_acceptance_directory':declared_initial,'raw_review_directory':str(raw_directory),
        'candidate_sha256':expected_source,'selected_candidate_id':selected_id,'seal_kind':seal_kind,
        'raw_result_sha256':proof['result_sha256'],'raw_attestation_sha256':proof['attestation_sha256'],
        'raw_evidence_receipt_sha256':file_sha256(raw_directory/'evidence-receipt.json'),
        'matrix_sha256':proof['matrix_sha256'],'required_observations':[list(pair) for pair in sorted({tuple(pair) for pair in observations})],
        'record_count':proof['record_count'],'source_code_review_status':'pending','full_behavior_acceptance':False}
    if check_declared:
        need(all(receipt.get(key)==value for key,value in canonical.items()),
             'The final-only receipt is stale or omits its exact replay bindings.', 'stale_review')
    return canonical, {**canonical,'verified_scopes':{scope:'pass' for scope in ('position','events','relationships')},
        'raw_validation':proof,'verification_scope':'final_raw_measurement_inspection_only',
        'new_model_generation_claimed_by_bridge':False}


def attach_final_raw_review(raw_review_directory, path, *, session_directory=None, initial_acceptance_directory=None):
    """Attach only a genuinely inspected aid; preserve all earlier receipts."""
    from .session import Session, _ordinary
    path=_ordinary(path)
    if path.exists():raise VisualError('output_exists','Preserve the earlier independent review receipt.')
    if (session_directory is None)==(initial_acceptance_directory is None):
        raise VisualError('invalid_review','Bind exactly one authoritative Session or initial SVG run.')
    if session_directory is not None:
        authority=_ordinary(session_directory);state=Session(authority)._load();selected=state.get('selected_candidate_id')
        candidates=[c for c in state.get('candidates',[]) if c.get('candidate_id')==selected]
        if not selected or len(candidates)!=1:raise VisualError('stale_review','No uniquely selected final candidate.')
        source=candidates[0]['source_sha256'];initial=None;session=str(authority)
    else:
        authority=_ordinary(initial_acceptance_directory)
        if path.is_relative_to(authority):
            raise VisualError('unsafe_path','Keep the initial unchanged-source evidence immutable; attach its review outside that directory.')
        from .suite_initial_acceptance import load_plan
        plan,_=load_plan(authority);source=plan['unchanged_source_sha256'];selected=None;session=None;initial=str(authority)
    declaration={'kind':'IndependentFinalRawVisualReview','schema_version':1,'status':'pass',
        'method':'replayed_final_raw_measurement_inspection','session_directory':session,'initial_acceptance_directory':initial,
        'raw_review_directory':str(_ordinary(raw_review_directory)),'candidate_sha256':source,
        'selected_candidate_id':selected,'source_code_review_status':'pending','full_behavior_acceptance':False}
    canonical,verified=_final_raw_visual_review(path,declaration,session_directory=session_directory,
        initial_acceptance_directory=initial_acceptance_directory,check_declared=False)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:
        json.dump(canonical,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')
    return verified


def validate_independent_review(path, *, candidate_sha256=None, session_directory=None, initial_acceptance_directory=None):
    """Replay all three independent gates, including frozen request bindings."""
    path=Path(path);receipt=read_json(path)
    if receipt.get('kind')=='IndependentRealisticVisualReview':
        from .realistic_review import validate_final_review
        return validate_final_review(path,candidate_sha256=candidate_sha256,session_directory=session_directory)
    if receipt.get('kind')=='IndependentFinalRawVisualReview':
        return _final_raw_visual_review(path,receipt,candidate_sha256=candidate_sha256,
            session_directory=session_directory,initial_acceptance_directory=initial_acceptance_directory)[1]
    if receipt.get('kind')!='IndependentVisualReview' or receipt.get('status')!='pass':
        raise VisualError('invalid_review','Expected a complete independent visual review aggregate')
    if candidate_sha256 is not None and receipt.get('candidate_sha256')!=candidate_sha256:
        raise VisualError('stale_review','Visual review belongs to a different candidate')
    coverage={};scopes={}
    for scope in ('position','events','relationships'):
        entries=receipt.get(scope+'_reviews',[])
        if not entries:raise VisualError('incomplete_review',f'Independent {scope} evidence is missing')
        covered=set()
        for entry in entries:
            directory=_reference(path.parent,entry['directory'])
            if file_sha256(directory/'review-result.json')!=entry.get('result_sha256'):
                raise VisualError('stale_review','Independent result file changed')
            saved=read_json(directory/'review-result.json');request=read_json(directory/'request.json')
            if digest(request)!=entry.get('request_sha256'):raise VisualError('stale_review','Aggregate request binding changed')
            annotation=read_json(directory/'annotation.json')
            if scope=='position':
                # evaluate_review writes identical deterministic evidence; verify
                # the original digest first so a changed annotation cannot heal it.
                result=evaluate_review(directory,annotation,persist=False)
                for record in result['records']:covered.add((record['package_sha256'],record['observations_sha256']))
            else:
                result=evaluate_scope_review(directory,annotation,persist=False)
                if result['scope']!=scope:raise VisualError('invalid_review','Wrong scoped review')
                covered.add((result['package_sha256'],result['observations_sha256']))
            if result!=saved or result['status']!='pass':raise VisualError('incomplete_review','Independent review did not replay as pass')
            if candidate_sha256 is not None and result.get('candidate_sha256')!=candidate_sha256:
                raise VisualError('stale_review','A review task is not bound to the selected candidate')
        coverage[scope]=covered;scopes[scope]='pass'
    if coverage['position']!=coverage['events'] or coverage['position']!=coverage['relationships']:
        raise VisualError('incomplete_review','Position, event and relationship reviews cover different recordings')
    required={tuple(v) for v in receipt.get('required_observations',[])}
    if not required or required!=coverage['position']:raise VisualError('incomplete_review','Review coverage differs from its frozen observation list')
    return {**receipt,'verified_scopes':scopes}


def _context_usage_housekeeping(event,session_id):
    """The one observed post-response notification shape; no arbitrary tail."""
    import math
    def keys(value,expected):return isinstance(value,dict) and set(value)==set(expected)
    def percent(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=100
    def amount(value,items=False):
        fields={'tokens','percent','items'} if items else {'tokens','percent'}
        return (keys(value,fields) and type(value['tokens']) is int and value['tokens']>=0
                and percent(value['percent']) and (not items or value['items']==[]))
    if not keys(event,{'jsonrpc','method','params'}) or event['jsonrpc']!='2.0' or event['method']!='session/update':return False
    params=event['params']
    if not keys(params,{'sessionId','update'}) or params['sessionId']!=session_id:return False
    update=params['update']
    if not keys(update,{'sessionUpdate','_meta'}) or update['sessionUpdate']!='session_info_update':return False
    meta=update['_meta']
    if not keys(meta,{'kiro'}):return False
    kiro=meta['kiro']
    if not keys(kiro,{'kind','usagePercentage','contextUsage','breakdown'}) or kiro['kind']!='context_usage':return False
    context=kiro['contextUsage'];parts=kiro['breakdown']
    if (not percent(kiro['usagePercentage']) or not keys(context,{'usagePercentage'})
            or not percent(context['usagePercentage']) or context['usagePercentage']!=kiro['usagePercentage']
            or not keys(parts,{'contextFiles','tools','kiroResponses','yourPrompts','sessionFiles'})):return False
    tools=parts['tools']
    return (amount(parts['contextFiles'],True) and amount(parts['sessionFiles'],True)
        and amount(parts['kiroResponses']) and amount(parts['yourPrompts'])
        and keys(tools,{'tokens','percent','mcp','builtin'})
        and amount({k:tools[k] for k in ('tokens','percent')})
        and amount(tools['mcp']) and amount(tools['builtin']))


def _replay_consumed_native_stream(inbound,launch,config,prompt_response):
    """Bind live consumption to its exact response; audit every buffered tail event."""
    from .launcher import MODEL,EFFORT
    from kiroclops.integration import kiro as native
    def need(value,message):
        if not value:raise VisualError('invalid_native_evidence',message)
    saved=launch.get('stream_configuration')
    need(isinstance(saved,dict),'Native live stream summary is missing')
    count=saved.get('events_seen')
    need(type(count) is int and 0<count<=len(inbound),'Native consumed prefix count is invalid')
    positions=[i for i,event in enumerate(inbound) if event==prompt_response]
    need(len(positions)==1 and positions[0]==count-1,'Live consumption must end at the uniquely bound prompt response')
    need(prompt_response.get('result')==launch.get('response') and
         prompt_response.get('result',{}).get('stopReason')=='end_turn','Native consumed prefix has no completed response')
    # The offline loader has validated the prepared configuration against its
    # exact current or explicitly pinned historical launcher. Today's default
    # model must not relabel an older recording or invalidate its true model.
    guard=native.StreamConfigurationGuard(config['agent_name'],
        model=config.get('model',MODEL),effort=config.get('effort',EFFORT));autopilot=None
    try:
        for event in inbound[:count]:
            for obj in native._stream_objects(event):
                for option in obj.get('configOptions',[]) if isinstance(obj.get('configOptions',[]),list) else []:
                    if isinstance(option,dict) and option.get('id')=='autopilot':autopilot=option.get('currentValue')
            guard.observe(event)
            if guard.generation_started:need(autopilot=='on','Autopilot was not enabled during actual generation')
        need(guard.generation_started and guard.summary()==saved,'Native consumed-prefix summary does not replay exactly')
        semantics={k:v for k,v in saved.items() if k!='events_seen'}
        sid=launch.get('preflight',{}).get('session_id')
        extra_tail=_builtin_227_housekeeping_tail(inbound,count,sid,_builtin_policy_227({
            'cli_version':launch.get('cli_version'),'kas_version':launch.get('kas_version'),
            'launcher_sha256':config.get('launcher_sha256')}),config.get('target_dir'))
        for index,event in enumerate(inbound[count:],count+1):
            need(_context_usage_housekeeping(event,sid) or index in extra_tail,
                 'Unexpected post-response event; only strict context or source-bound unchanged current metadata is supported')
            guard.observe(event)
            actual=guard.summary()
            need(actual['events_seen']==index and {k:v for k,v in actual.items() if k!='events_seen'}==semantics,
                 'Post-response housekeeping changed semantic stream configuration')
    except native.KiroError as exc:raise VisualError('invalid_native_evidence',str(exc)) from exc
    return {'kind':'ConsumedPrefixAndBufferedHousekeepingReplay','consumed_events':count,
        'recorded_events':len(inbound),'prompt_response_event_number':count,
        'housekeeping_tail_events':len(inbound)-count,'full_live_summary_verified':True,
        'tail_semantic_configuration_unchanged':True,
        'scope':'Stream accounting only; does not establish private-file/tool-boundary enforcement or source authorship.'}


def _native_tool_name(value):
    """Explicit observed transport/hook spellings, never fuzzy title matching."""
    from .launcher import MCP_TOOLS
    if not isinstance(value,str):return None
    names={'Read File':'read_file','List Directory':'list_directory','Write File':'fs_write',
        'Append to File':'fs_append','Replace in File':'str_replace',
        'Run Command':'execute_pwsh','Grep Search':'grep_search','Delete File':'delete_file',
        'Tool Load':'tool_load'}
    if value in names:return names[value]
    if value in names.values():return value
    for tool in MCP_TOOLS:
        if value in ('@kiroclops/'+tool,'mcp_kiroclops_'+tool,'kiroclops::'+tool,
                     'mcp__kiroclops__'+tool,'kiroclops___'+tool):return '@kiroclops/'+tool
    return None


_NATIVE_OUTPUT_POLICY_SOL_MAX_V4={
    'id':'kas-0.66.8-cli-2.24.0-hook-output-sol-max-v4',
    'cli_version':'kiro-cli-chat 2.24.0','kas_version':'0.66.8',
    'launcher_sha256':'a816685f811ebb1fa04f50c51bea2f126be8228163161226f092474f8f0efa32',
    'launcher_equivalence_proof_sha256':'ec4a69d38c158ae33c2bc4bca20439e211e6e54881425c50de9dc47bd173ea3a',
    'portable_client_sha256':'63f64b50790876af6fbb1eac504426dba905ab3a31c329a939c2acc0d42a4e45',
    'installed_source_sha256':'c1bc24cad8f4700013dd701f6d75e92eff82b53bfc458d80f3a14ba5bc4181b3',
    'source_proof_sha256':'fd350131186aefaafe13e925888461e09fc659a9297616b74341b8fb4cafd10a',
    'native_canary_sha256':'f3c226836e95169cb2b14b1aec4b8ac2ad584073aa8f7c9c6538ee741266421b',
    'scope':'Recorded versions, launcher, static KAS source review, and five-call live canary; not cryptographic runtime attestation.'}

_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V1={
    **_NATIVE_OUTPUT_POLICY_SOL_MAX_V4,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-v1',
    'launcher_sha256':'bf20ded67d2d527c1fec4c79e3a48b8d563fcbae45018a88b767e051d7bf8bcc',
    'launcher_equivalence_proof_sha256':'d95681ef08b7c39aaa733ff1cbc26631160de7ef30743c23c88b3c19dfbad6b3',
    'scope':'Exact model-only source equivalence to the recorded Sol launcher, inspected KAS source and historical five-call Sol canary; not cryptographic runtime attestation or a live Opus 5.5 canary.'}

_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V2={
    **_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V1,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-v2',
    'shell_correlation_source_proof_sha256':'d4141439c08a13037d1e6fd66cd01bd48ddfecf5373701202ac86017a631b7d5',
    'scope':'Pinned KAS source, model-only launcher equivalence and exact native envelopes, including provider shell IDs, optional shell inputs and pre-handle rejection; not cryptographic runtime attestation.'}

_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3={
    **_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V2,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-v3',
    'shell_cwd_source_proof_sha256':'8a8df666a4583a51f08df9f9d6c4e491454e91f19a2502ee15288aa7563a1a18',
    'scope':'Pinned KAS source and exact native envelopes, including explicit workspace-descendant shell cwd; full independent hook arguments and decisions remain required; not cryptographic runtime attestation.'}

_NATIVE_OUTPUT_POLICY_DIAGNOSTIC_V3={
    **_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-diagnostics-v3',
    'shell_offload_source_proof_sha256':'651f623eae1886fa2e8609467dfaf3b9426d26ea0cc35635eefc27be6579bd06',
    'shell_background_source_proof_sha256':'c374fd73555e5d75b54f189548ab40224f008ad47aa1ac513ab26e5248d490d5',
    'launcher_sha256':'07431f33111a78c8fc54ce5ff68526f0e87b881b67be1eb134d45e29f5a1f592',
    'launcher_equivalence_proof_sha256':None,
    'transport_compatibility_proof_sha256':'bf1a6eb53c67eac30fe94f47afb8a164e0199baff52fad9b89f579cd7f8f57bb',
    'guard_bootstrap_sha256':'6c85b3adf2958b9cc8c52dc7fdcb81f391a9f594e063748e11193940ca13d2bc',
    'scope':'Pinned native transport sections and KAS output formats; diagnostic guard and reporting changes are separately tested. Every canonical decision remains required. Diagnostics are never authorization evidence; not cryptographic runtime attestation, a live canary or whole-guard equivalence.'}

_NATIVE_OUTPUT_POLICY={
    **_NATIVE_OUTPUT_POLICY_DIAGNOSTIC_V3,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-cloud-v1',
    'launcher_sha256':'762693695c8c74448bd9494fc8acca7bfcb7914bed7be0a83d5d1a15e5bcdfe3',
    'transport_compatibility_proof_sha256':'a15047b492fb54640e35e4000a5abc26064d2c5223a6b07385154ba4a2af65e8',
    'scope':'Explicit cloud configuration only; native transport and permission decisions remain unchanged. Every canonical decision remains required; not cryptographic runtime attestation, a new canary or a transfer-success claim.'}

_NATIVE_OUTPUT_POLICY_STARTUP={
    **_NATIVE_OUTPUT_POLICY,
    'id':'kas-0.66.8-cli-2.24.0-hook-output-opus55-max-startup-v1',
    'launcher_sha256':'d4b73b41e37b8f8e55f5468416c1716b3c1cd11d940382ddb2c8a0164daf1ebe',
    'transport_compatibility_proof_sha256':'68d9bd7bb337d04f829dad9e4df2b1a8401a68f4f61b21c101f66c8080a525e0',
    'scope':'Pinned pre-prompt recovery changes only. Exact reconstruction preserves native client, guard, model, approval and post-turn code. Every decision and denial outcome is still required; not a permission expansion or a transfer-success claim.'}

_NATIVE_PROGRESSIVE_POLICY={
    'id':'kas-0.66.22-cli-2.27.0-progressive-tools-v1',
    'cli_version':'kiro-cli-chat 2.27.0','kas_version':'0.66.22',
    'source_sha256':'79a1a743ee7236a71bba9c6c68342deccfcffea4d61361eae0254288339b19c8',
    'source_proof_sha256':'637eb20f64a09d46ab77fa2697aee19ec8c4a38a0f57b08993b7374bea8baf32',
    'scope':'Pinned static KAS implementation and exact recorded envelopes; every canonical decision remains required. No launcher equivalence, runtime signature or authorization claim.'}


_NATIVE_BUILTIN_POLICY_227_V1={
    'id':'kas-0.66.22-cli-2.27.0-successful-builtins-v1',
    'cli_version':'kiro-cli-chat 2.27.0','kas_version':'0.66.22',
    'launcher_sha256':'15222b46591070ff18ed352a21bb7f13e0a1c240418a59ccb25386709d8403ac',
    'source_sha256':'79a1a743ee7236a71bba9c6c68342deccfcffea4d61361eae0254288339b19c8',
    'source_proof_sha256':'95712751d3395ef5681a52820f1f5f8e3ba321f4e21d9a51508b4318e13926b1',
    'scope':'Distinct pinned static 2.27 source and exact successful foreground/edit envelopes. Independent canonical decisions and cardinality remain required. Failed, denied, background, offloaded, clamped and unknown metadata envelopes are unsupported; no live canary, runtime signature or authorization claim.'}


_NATIVE_BUILTIN_POLICY_227_V2={
    **_NATIVE_BUILTIN_POLICY_227_V1,
    'id':'kas-0.66.22-cli-2.27.0-successful-builtins-v2',
    'source_proof_sha256':'1ff376e4fb2709621d3014d2557c96f481003f74998d2d305ddef262a5678db1',
    'scope':'Distinct pinned 2.27 source and exact successful foreground shell, write, append and literal-string replacement envelopes. Independent canonical decisions/cardinality and complete native edit checkpoints remain required. Unknown/failed/denied/background/offloaded/clamped metadata and JS replacement-token cases are unsupported; no live canary, runtime signature or authorization claim.'}


_NATIVE_BUILTIN_POLICY_227={
    **_NATIVE_BUILTIN_POLICY_227_V2,
    'id':'kas-0.66.22-cli-2.27.0-observed-outcomes-v3',
    'source_proof_sha256':'4b5cad90bdbf80ee4a386ab2e3a70342a5c4322a8ca2b2d9a44ea62508bab2ed',
    'scope':'Distinct source-pinned current-version successes plus exact code1 completed error and one immutable full p04 offload0; exact typed repeated housekeeping only. No catch/timeout/denial/background generalization, version alias or behavior acceptance.'}


def _verified_launcher_execution_extension(context):
    root=Path(__file__).resolve().parents[2]
    helper=root/'kiroclops/local_engine/launcher_policy_proof.py'
    proof=root/'artifacts/phase3d/launcher-execution-v2b/source-proof.json'
    try:
        if (file_sha256(helper)!='467d076393dce29f146406b98f5609a14e1b7ae19f6572554e1d568d739619be'
                or file_sha256(proof)!='2a566e3fc132b95716e28c956fd361c390776005a223d387b4ccb28cb8941c8e'):
            return None
        from .launcher_policy_proof import verify_extension
        return verify_extension(context)
    except (OSError,ValueError,VisualError):return None


def _builtin_policy_227(context):
    policy=_NATIVE_BUILTIN_POLICY_227
    if any(context.get(k)!=policy[k] for k in ('cli_version','kas_version','launcher_sha256')):
        if _verified_launcher_execution_extension(context) is None:return None
    saved=_builtin_227_v3_proof(policy)
    if (saved is None or saved.get('schema_version')!=1
            or any(saved.get(k)!=policy[k] for k in ('cli_version','kas_version'))):return None
    base=Path(__file__).resolve().parents[2]/'reviews/staged-native227v3/v2-baseline/source-proof.json'
    try:
        if (file_sha256(base)!=_NATIVE_BUILTIN_POLICY_227_V2['source_proof_sha256']
            or saved.get('base_v2_proof_sha256')!=_NATIVE_BUILTIN_POLICY_227_V2['source_proof_sha256']):return None
    except (OSError,ValueError,VisualError):return None
    return policy


def _progressive_policy(context):
    if any(context.get(k)!=_NATIVE_PROGRESSIVE_POLICY[k] for k in ('cli_version','kas_version')):return None
    proof=Path(__file__).resolve().parents[2]/'artifacts/phase3d/native-progressive-tool-source-proof.json'
    try:
        if file_sha256(proof)!=_NATIVE_PROGRESSIVE_POLICY['source_proof_sha256']:return None
        saved=read_json(proof)
        if (saved.get('kind')!='PinnedKASProgressiveToolSourceReview' or saved.get('status')!='pass'
                or saved.get('source_sha256')!=_NATIVE_PROGRESSIVE_POLICY['source_sha256']):return None
    except (OSError,ValueError,VisualError):return None
    return _NATIVE_PROGRESSIVE_POLICY


_TOOL_LOAD_NOTE='These tools are now loaded for the rest of this session. To RUN one, call tool_call with { "tool_id": "namespace::tool", "arguments": { ... } } (use the tool_id shown for each result, and arguments matching its input_schema). Do not call tool_load again for tools you have already loaded.'


def _progressive_native_arguments(name,identity,updates,policy):
    """Exact observed successful disclosure/deferred subset, never title-only."""
    from .launcher import MCP_TOOLS
    if policy is None or not isinstance(identity,str) or not _REJECTED_COMMAND_CALL_ID.fullmatch(identity):return None
    if name=='tool_load':
        if len(updates)!=1:return None
        u=updates[0]
        if (set(u)!={'_meta','content','rawInput','rawOutput','sessionUpdate','status','title','toolCallId'}
                or u['sessionUpdate']!='tool_call_update' or u['status']!='completed'
                or u['title']!='Tool Load' or u['toolCallId']!=identity
                or u['_meta']!={'kiro':{'toolOrigin':'default'}}
                or u['rawInput']!={'tool_ids':['kiroclops::*']}
                or not isinstance(u['rawOutput'],dict) or set(u['rawOutput'])!={'message'}):return None
        message=u['rawOutput']['message']
        if (not isinstance(message,str) or u['content']!=[{'type':'content','content':{'type':'text','text':message}}]):return None
        try:catalog=json.loads(message)
        except (ValueError,TypeError):return None
        if (not isinstance(catalog,dict) or set(catalog)!={'loaded','not_found','note'}
                or catalog['not_found']!=[] or catalog['note']!=_TOOL_LOAD_NOTE
                or not isinstance(catalog['loaded'],list) or len(catalog['loaded'])!=len(MCP_TOOLS)):return None
        ids=[]
        for entry in catalog['loaded']:
            if (not isinstance(entry,dict) or set(entry)!={'tool_id','description','input_schema'}
                    or not isinstance(entry['description'],str) or not isinstance(entry['input_schema'],dict)):return None
            ids.append(entry['tool_id'])
        if len(set(ids))!=len(ids) or set(ids)!={'kiroclops::'+tool for tool in MCP_TOOLS}:return None
        return dict(u['rawInput'])
    if not isinstance(name,str) or not name.startswith('@kiroclops/'):return None
    tool=name.split('/',1)[1]
    if tool not in MCP_TOOLS or len(updates)!=3:return None
    shapes=({'_meta','kind','rawInput','sessionUpdate','status','title','toolCallId'},
            {'_meta','rawInput','sessionUpdate','status','toolCallId'},
            {'_meta','content','rawInput','rawOutput','sessionUpdate','status','title','toolCallId'})
    if any(set(u)!=shape for u,shape in zip(updates,shapes)):return None
    first,middle,last=updates
    if ([u['sessionUpdate'] for u in updates]!=['tool_call','tool_call_update','tool_call_update']
            or [u['status'] for u in updates]!=['pending','in_progress','completed']
            or first['kind']!='other' or first['title']!=name or last['title']!=name
            or any(u['toolCallId']!=identity or u['_meta']!={'kiro':{'serverName':'kiroclops','toolOrigin':'default'}} for u in updates)):return None
    wrapper=first['rawInput']
    if (not isinstance(wrapper,dict) or set(wrapper)!={'tool_id','arguments'}
            or wrapper['tool_id']!='kiroclops::'+tool or not isinstance(wrapper['arguments'],dict)
            or any(u['rawInput']!=wrapper for u in updates)):return None
    # Current supported subset has text-only MCP output. Keep unknown images,
    # transformed/offloaded messages, extra output fields and failed envelopes closed.
    output=last['rawOutput']
    if (not isinstance(output,dict) or set(output)!={'response','imageBase64Urls','message'}
            or not isinstance(output['message'],str) or output['response']!=output['message']
            or output['imageBase64Urls']!=[]
            or last['content']!=[{'type':'content','content':{'type':'text','text':output['message']}}]):return None
    return _native_arguments(name,wrapper['arguments'])


def _native_arguments(tool,value):
    """Only inspected SyncTool/xHa/schema transforms; never path/string guessing."""
    if not isinstance(value,dict):return None
    result=dict(value)
    if '_meta' in result:
        meta=result.pop('_meta')
        if (not isinstance(meta,dict) or set(meta)!={'_isValid','_activePath','_completedPaths'}
                or meta['_isValid'] is not True or not isinstance(meta['_activePath'],list)
                or not isinstance(meta['_completedPaths'],list)):return None
        def path(v):return isinstance(v,list) and all(type(x) in (str,int) for x in v)
        if not path(meta['_activePath']) or not all(path(p) for p in meta['_completedPaths']):return None
    # xHa maps top-level null to undefined before schema validation. Limit this
    # equivalence to known optional built-in fields; arbitrary MCP null survives.
    nullable={'read_file':{'offset','limit','start_line','end_line','explanation'},
        'list_directory':{'depth','explanation'},'execute_pwsh':{'cwd','timeout','warning','ignoreWarning'},
        'grep_search':{'caseSensitive','excludePattern','explanation','includePattern','file_type','context','max_results'},
        'str_replace':set(),'fs_write':set()}.get(tool,set())
    result={k:v for k,v in result.items() if not (k in nullable and v is None)}
    # The installed delete schema has two string fields; its native location
    # uses targetFile. Preserve exact input digests and pre-handle denial.
    if tool=='delete_file' and (set(result)!={'targetFile','explanation'}
        or not all(isinstance(result[k],str) and result[k] for k in result)):return None
    if tool=='str_replace':result.setdefault('replace_all',False)
    if tool=='execute_pwsh':result.setdefault('run_in_background',False)
    return result


_NATIVE_PROVIDER_CALL_ID=r'(?:call_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|toolu_bdrk_[A-Za-z0-9]{24})'
_RUN_COMMAND_CALL_ID=re.compile('run_command_'+_NATIVE_PROVIDER_CALL_ID+r'\Z')
_REJECTED_COMMAND_CALL_ID=re.compile(_NATIVE_PROVIDER_CALL_ID+r'\Z')


def _native_pwsh_arguments(args,*,target_dir,allow_background=False,allow_external_cwd=False):
    """Validate the inspected foreground shell subset without inventing defaults.

    KAS EQa drops top-level null before schema parsing; cwd and timeout are
    optional. An omitted cwd selects the workspace and timeout selects the
    configured/default duration, but neither value is added to rawInput.
    Preserve absence versus an explicit value for the independent hook match.
    """
    if (not _native_windows_path(target_dir) or not isinstance(args,dict)
            or not {'command','description','run_in_background'}<=set(args)
            or set(args)-{'command','description','cwd','run_in_background','timeout'}):return False
    if (not isinstance(args['command'],str) or not args['command']
            or not isinstance(args['description'],str) or not args['description']
            or args['description']!=' '.join(args['description'].split())
            or len(args['description'])>120 or not args['description'].isascii()
            or not args['description'].isprintable()
            or not (args['run_in_background'] is False or allow_background and args['run_in_background'] is True)):return False
    if 'cwd' in args:
        root=_native_windows_path(target_dir);cwd=_native_windows_path(args['cwd'])
        if cwd is None:return False
        try:
            if not allow_external_cwd and ntpath.commonpath((root,cwd))!=root:return False
        except ValueError:return False
        # This only recognizes the inspected envelope. Preserve the exact cwd
        # string for independent hook matching; lexical containment is not an
        # authorization decision or filesystem/symlink attestation.
    if 'timeout' in args and (type(args['timeout']) not in (int,float)
            or not math.isfinite(args['timeout']) or args['timeout']<=0):return False
    return True


def _native_rejected_pwsh_call(identity,updates,*,target_dir,policy_supported):
    """Recognize only the pre-handle shell rejection, still requiring a deny.

    SyncTool emits the unprefixed provider action ID, static title and original
    input before executeBash can emit run_command_<actionId>. The later audit
    must independently bind the deny row, hook completions and no-output shape.
    """
    if (not policy_supported or not isinstance(identity,str)
            or not _REJECTED_COMMAND_CALL_ID.fullmatch(identity)
            or not isinstance(updates,list) or len(updates)!=1):return False
    update=updates[0]
    if (not isinstance(update,dict)
            or set(update)!={'sessionUpdate','toolCallId','status','title','rawInput','_meta'}
            or update['sessionUpdate']!='tool_call_update' or update['toolCallId']!=identity
            or update['status']!='failed' or update['title']!='Run Command'
            or update['_meta']!={'kiro':{'toolOrigin':'default'}}):return False
    # A prevented request may name an external directory. This recognizes
    # its identity, never permission: the caller still requires a matched
    # deny, a completed hook and the exact output-free rejection envelope.
    return _native_pwsh_arguments(_native_arguments('execute_pwsh',update['rawInput']),
                                  target_dir=target_dir,allow_external_cwd=True)


def _native_described_pwsh_call(identity,updates,*,target_dir,policy_supported,session_id=None,offload_supported=False,background_supported=False):
    """Recognize the pinned KAS run_command/execute_pwsh description title.

    KAS 0.66.8 exposes execute_pwsh as a Windows variant of run_command.
    Its ACP title is the normalized input description, so a non-default title
    cannot be resolved through the static tool-name table. Limit this case to
    the exact four-update envelope observed with CLI 2.24.0/Sol and Opus max.
    executeBash prefixes the provider action ID; its parsed optional cwd/timeout
    fields need not be present. The guard must still independently match every
    normalized input field and prove the tool decision. No ID is rewritten.
    """
    if not policy_supported or not isinstance(identity,str) or not _RUN_COMMAND_CALL_ID.fullmatch(identity):return False
    if not isinstance(updates,list) or len(updates)!=4:return False
    first,second,third,last=updates
    shapes=(
        {'_meta','kind','rawInput','sessionUpdate','status','title','toolCallId'},
        {'_meta','rawInput','sessionUpdate','status','toolCallId'},
        {'_meta','rawInput','sessionUpdate','status','toolCallId'},
        {'_meta','content','rawInput','rawOutput','sessionUpdate','status','title','toolCallId'},
    )
    if any(not isinstance(update,dict) or set(update)!=shape for update,shape in zip(updates,shapes)):return False
    if ([update['sessionUpdate'] for update in updates]!=['tool_call','tool_call_update','tool_call_update','tool_call_update']
            or [update['status'] for update in updates[:3]]!=['pending','in_progress','in_progress']
            or last['status'] not in ('completed','failed') or first['kind']!='execute'
            or any(update['toolCallId']!=identity for update in updates)
            or any(update['_meta']!={'kiro':{'toolOrigin':'default'}} for update in updates[:-1])):return False
    if last['_meta']!={'kiro':{'toolOrigin':'default'}}:
        from .native_offload import shell_offload_terminal
        if not offload_supported or not shell_offload_terminal(last,session_id):return False
    elif isinstance(last.get('rawOutput'),dict) and str(last['rawOutput'].get('message','')).startswith('The output from "execute_pwsh"'):
        # A preview cannot silently lose the metadata that binds its session,
        # destination and original length and fall through as ordinary output.
        return False
    args=first['rawInput']
    if isinstance(args,dict) and args.get('run_in_background') is True:
        from .native_background import background_launch_terminal
        if not background_supported or background_launch_terminal(last) is None:return False
    if (not _native_pwsh_arguments(args,target_dir=target_dir,allow_background=background_supported)
            or first['title']!=args['description'] or last['title']!=args['description']
            or any(update['rawInput']!=args for update in updates)):return False
    return True


def _native_windows_path(value, root=None):
    """Canonical lexical Windows path, resolving only plain relative paths."""
    if not isinstance(value,str) or not value or '\x00' in value:return None
    drive=ntpath.splitdrive(value)[0]
    if ntpath.isabs(value):
        if not drive:return None
    else:
        if drive or root is None:return None
        base=_native_windows_path(root)
        if base is None:return None
        value=ntpath.join(base,value)
    return ntpath.normcase(ntpath.normpath(value))


def _builtin_227_clean(value):
    """Exact R0c/iGi removed codepoint ranges, without changing hook inputs."""
    ranges=((917504,917631),(8203,8207),(8232,8239),(8287,8303),(65520,65532),(65534,65535))
    return ''.join(c for c in value if not any(low<=ord(c)<=high for low,high in ranges))


def _builtin_227_title(description):
    if description is None:return 'Run Command'
    # ECMAScript whitespace, rather than Python's broader Unicode \s.
    value=re.sub(r'[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+',
                 ' ',_builtin_227_clean(description)).strip(' ')
    return (value[:120]+'...' if len(value)>120 else value) or 'Run Command'


def _builtin_227_short(value):
    # ACP recursively clamps strings at 30000 UTF-16 units. Never treat a
    # shortened preview as exact native output or checkpoint text.
    return isinstance(value,str) and len(value.encode('utf-16-le',errors='surrogatepass'))//2<30000


def _builtin_227_arguments(tool,value):
    result=_native_arguments(tool,value)
    # qSc drops null top-level properties before the parsed shell schema.
    if tool=='execute_pwsh' and result is not None and result.get('description',False) is None:
        result.pop('description')
    return result


def _builtin_227_pwsh_v2(identity,updates,*,target_dir,policy):
    """Successful, unedited foreground text output; no 0.66.8 policy alias."""
    if (policy is None or not isinstance(identity,str) or not _RUN_COMMAND_CALL_ID.fullmatch(identity)
            or not isinstance(updates,list) or len(updates)<3):return None
    first,last=updates[0],updates[-1]
    start_fields={'_meta','kind','rawInput','sessionUpdate','status','title','toolCallId'}
    progress_fields={'_meta','rawInput','sessionUpdate','status','toolCallId'}
    end_fields={'_meta','content','rawInput','rawOutput','sessionUpdate','status','title','toolCallId'}
    if (set(first)!=start_fields or set(last)!=end_fields
            or first['sessionUpdate']!='tool_call' or first['status']!='pending' or first['kind']!='execute'
            or last['sessionUpdate']!='tool_call_update' or last['status']!='completed'
            or any(set(u)!=progress_fields or u['sessionUpdate']!='tool_call_update'
                   or u['status']!='in_progress' for u in updates[1:-1])
            or any(u['toolCallId']!=identity or u['_meta']!={'kiro':{'toolOrigin':'default'}} for u in updates)):
        return None
    args=_builtin_227_arguments('execute_pwsh',first['rawInput'])
    if (not isinstance(args,dict) or not isinstance(args.get('command'),str) or not args['command']
            or set(args)-{'command','description','run_in_background','cwd','timeout'}
            or args.get('run_in_background') is not False
            or 'description' in args and not isinstance(args['description'],str)):
        return None
    # Reuse lexical workspace cwd validation; use a temporary title solely
    # because the legacy helper deliberately recognizes only short ASCII.
    if not _native_pwsh_arguments({**args,'description':'checked'},target_dir=target_dir):return None
    if 'timeout' in args and type(args['timeout']) is not int:return None
    if (first['title']!=_builtin_227_title(args.get('description')) or last['title']!=first['title']
            or any(digest(_builtin_227_arguments('execute_pwsh',u['rawInput']))!=digest(args) for u in updates)
            or any(not isinstance(u['rawInput'],dict) or '_meta' in u['rawInput'] for u in updates)):
        return None
    output=last['rawOutput']
    if (not isinstance(output,dict) or set(output)!={'output','exitCode','message'}
            or type(output['exitCode']) is not int or output['exitCode']!=0
            or not _builtin_227_short(output['output']) or not _builtin_227_short(output['message'])
            or output['output']!=_builtin_227_clean(output['output'])
            or output['message']!='Output:\n'+output['output']+'\n\nExit Code: 0'
            or last['content']!=[{'type':'content','content':{'type':'text','text':output['message']}}]):
        return None
    return args


def _builtin_227_v3_proof(policy):
    if policy is None or policy.get('id') != 'kas-0.66.22-cli-2.27.0-observed-outcomes-v3':
        return None
    path = Path(__file__).resolve().parents[2] / 'reviews/staged-native227v3/source-proof-v3.json'
    try:
        if file_sha256(path) != policy['source_proof_sha256']:
            return None
        saved = read_json(path)
        if (saved.get('kind') != 'PinnedKAS227BuiltinSourceReview' or saved.get('policy_version') != 3
                or saved.get('status') != 'pass' or saved.get('source_sha256') != policy['source_sha256']
                or saved.get('native_canary') is not False or saved.get('live_model_run') is not False):
            return None
    except (OSError, ValueError, VisualError):
        return None
    return saved


def _builtin_227_v3_utf16_slice(value, start=None, end=None):
    raw = value.encode('utf-16-le', 'surrogatepass')
    return raw[slice(None if start is None else start * 2, None if end is None else end * 2)].decode('utf-16-le', 'surrogatepass')


def _builtin_227_v3_units(value):
    return len(value.encode('utf-16-le', 'surrogatepass')) // 2


def _builtin_227_v3_clamp(value):
    size = _builtin_227_v3_units(value)
    if size < 30000:
        return value
    start, end = 500, size - 500
    if 0xD800 <= ord(_builtin_227_v3_utf16_slice(value, start - 1, start)) <= 0xDBFF:
        start -= 1
    if 0xDC00 <= ord(_builtin_227_v3_utf16_slice(value, end, end + 1)) <= 0xDFFF:
        end += 1
    return (_builtin_227_v3_utf16_slice(value, 0, start)
            + f'\n...[truncated {end - start} chars]...\n' + _builtin_227_v3_utf16_slice(value, end))


def _builtin_227_v3_offload_message(path, full):
    size = _builtin_227_v3_units(full)
    return '\n'.join([
        f'The output from "execute_pwsh" ({size:,} chars) exceeded the 30,000-char limit; here is a preview of the tool\'s result:',
        '', '--- HEAD (first 500 chars) ---', _builtin_227_v3_utf16_slice(full, 0, 500), '',
        f'--- {size - 1000:,} chars omitted ---', '', '--- TAIL (last 500 chars) ---',
        _builtin_227_v3_utf16_slice(full, -500), '--- END ---', '', 'If more context is needed:',
        '1. Retry the tool with narrower arguments — a tighter pattern, a filter, a smaller range or page — so the result fits.',
        "2. The full output was saved to a file under '" + path.replace('\\', '/')
        + "'. Inspect it with targeted shell commands, combining them so the result stays within the size limit (e.g. head -c 2000, tail -c 5000, grep -n 'pattern' | head -c 2000).",
    ])


def _builtin_227_v3_offload_output(identity, updates, session_id, policy):
    """Only the independently inspected exact p04 output pin; no preview waiver."""
    proof = _builtin_227_v3_proof(policy)
    if proof is None:
        return None
    pins = [p for p in proof.get('observed_output_pins', []) if p.get('tool_call_id') == identity]
    if len(pins) != 1 or pins[0].get('native_updates_sha256') != digest(updates):
        return None
    pin = pins[0]
    last = updates[-1]
    meta = last.get('_meta')
    if not isinstance(meta, dict) or set(meta) != {'kiro'}:
        return None
    km = meta['kiro']
    if not isinstance(km, dict) or set(km) != {'toolOrigin', 'outputTransformation'} or km['toolOrigin'] != 'default':
        return None
    change = km['outputTransformation']
    if (not isinstance(change, dict) or set(change) != {'kind', 'absFilePath', 'totalChars'}
            or change['kind'] != 'offloaded' or type(change['totalChars']) is not int
            or not 30000 <= change['totalChars'] <= 32 * 1024 * 1024):
        return None
    name = change['absFilePath']
    if (not isinstance(name, str) or not isinstance(session_id, str) or name != pin.get('original_path')
            or '\x00' in name or ntpath.normpath(name) != name
            or not re.fullmatch(r'[A-Za-z]:\\[^\r\n<>"|?*]+', name)
            or not re.search(r'\\\.kiro\\sessions\\[0-9a-f]{16}\\' + re.escape(session_id)
                             + r'\\tool-outputs\\execute_pwsh-[0-9a-f]{8}\.txt\Z', name)):
        return None
    raw = last.get('rawOutput')
    if (last.get('status') != 'completed' or not isinstance(raw, dict)
            or set(raw) != {'output', 'exitCode', 'message'} or type(raw['exitCode']) is not int or raw['exitCode'] != 0):
        return None
    try:
        from .session import _ordinary
        # Check lexical ancestors before resolve, including Windows junctions;
        # identical pinned bytes through a redirected path are insufficient.
        external = _ordinary(Path(name))
        archive_root = Path(__file__).resolve().parents[2]
        archive = _ordinary(archive_root / pin['archive'])
        if (not archive.is_relative_to(archive_root.resolve()) or external.is_symlink()
                or external.stat().st_size > 32 * 1024 * 1024 or archive.stat().st_size > 32 * 1024 * 1024):
            return None
        data = external.read_bytes()
        if hashlib.sha256(data).hexdigest() != pin['sha256'] or archive.read_bytes() != data:
            return None
        full = data.decode('utf-8')
    except (OSError, ValueError, KeyError, VisualError):
        return None
    if (_builtin_227_v3_units(full) != change['totalChars']
            or not full.startswith('Output:\n') or not full.endswith('\n\nExit Code: 0')):
        return None
    output = full[8:-len('\n\nExit Code: 0')]
    expected = _builtin_227_v3_offload_message(name, full)
    if (output != _builtin_227_clean(output) or raw['output'] != _builtin_227_v3_clamp(output)
            or raw['message'] != expected
            or last.get('content') != [{'type': 'content', 'content': {'type': 'text', 'text': expected}}]):
        return None
    return output


def _builtin_227_pwsh(identity, updates, *, target_dir, policy, session_id=None):
    """V2 successes plus source-bound exit1 and one pinned full offload0 output."""
    normal = _builtin_227_pwsh_v2(identity, updates, target_dir=target_dir, policy=policy)
    if normal is not None:
        return normal
    if (_builtin_227_v3_proof(policy) is None or not isinstance(updates, list) or len(updates) != 4
            or any(not isinstance(u, dict) for u in updates)):
        return None
    last = updates[-1]
    output = last.get('rawOutput')
    if not isinstance(output, dict) or set(output) != {'output', 'exitCode', 'message'}:
        return None
    code = output['exitCode']
    if type(code) is not int:
        return None
    if code == 1 and last.get('status') == 'failed' and last.get('_meta') == {'kiro': {'toolOrigin': 'default'}}:
        text = output['output']
        if (not _builtin_227_short(text) or text != _builtin_227_clean(text)
                or output['message'] != 'Output:\n' + text + '\n\nExit Code: 1'
                or last.get('content') != [{'type': 'content', 'content': {'type': 'text', 'text': output['message']}}]):
            return None
    elif code == 0:
        text = _builtin_227_v3_offload_output(identity, updates, session_id, policy)
        if text is None:
            return None
    else:
        # -1 is also emitted for "command not executed" catch paths. Missing,
        # null, timeout, denied and other error codes are outside this subset.
        return None
    # Check every existing v2 identity/argument/title/progress/cardinality rule
    # on a schema projection. Preserve the actual terminal in audit outcomes.
    projected = json.loads(json.dumps(updates))
    end = projected[-1]
    end['status'] = 'completed'
    end['_meta'] = {'kiro': {'toolOrigin': 'default'}}
    check_text = '' if code == 0 else text
    message = 'Output:\n' + check_text + '\n\nExit Code: 0'
    end['rawOutput'] = {'output': check_text, 'exitCode': 0, 'message': message}
    end['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': message}}]
    return _builtin_227_pwsh_v2(identity, projected, target_dir=target_dir, policy=policy)


def _builtin_227_context_housekeeping(event, session_id):
    try:
        parts = event['params']['update']['_meta']['kiro']['breakdown']
        memory = parts.get('memory')
        if (not isinstance(memory, dict) or set(memory) != {'tokens', 'percent'}
                or type(memory['tokens']) is not int or memory['tokens'] < 0
                or type(memory['percent']) not in (int, float) or not math.isfinite(memory['percent'])
                or not 0 <= memory['percent'] <= 100):
            return False
        projected = json.loads(json.dumps(event))
        del projected['params']['update']['_meta']['kiro']['breakdown']['memory']
        return _context_usage_housekeeping(projected, session_id)
    except (KeyError, TypeError, ValueError):
        return False


def _builtin_227_metadata_shape(event, session_id):
    if (not isinstance(event, dict) or set(event) != {'jsonrpc', 'method', 'params'} or event['jsonrpc'] != '2.0'
            or not isinstance(event['params'], dict) or event['params'].get('sessionId') != session_id):
        return False
    p = event['params']
    if event['method'] == '_kiro/governance/state':
        features = p.get('features')
        return (set(p) == {'sessionId', 'isEnterprise', 'features', 'disabledReason'}
                and type(p['isEnterprise']) is bool and p['disabledReason'] == 'admin_disabled'
                and isinstance(features, dict) and set(features) == {'mcpEnabled', 'webToolsEnabled', 'usageAnalytics',
                    'contentCollection', 'promptLogging', 'codeReferenceTracker', 'autonomousAgents'}
                and all(type(v) is bool for v in features.values()))
    if event['method'] != '_kiro/mcp/status' or set(p) != {'sessionId', 'servers'}:
        return False
    servers = p['servers']
    if (not isinstance(servers, list) or len(servers) != 2 or not all(isinstance(s, dict) for s in servers)
            or not all(isinstance(s.get('name'), str) for s in servers)
            or {s.get('name') for s in servers} != {'kiroclops', 'power-kiroclops-kiroclops'}):
        return False
    names = {'inspect_session', 'prepare_experiment', 'evaluate_candidate', 'finalize_candidate', 'get_job', 'cancel_job', 'build_report'}
    for s in servers:
        if (set(s) != {'name', 'status', 'tools', 'prompts', 'resources', 'resourceTemplates', '_meta'}
                or s['status'] != 'connected' or s['prompts'] != [] or s['resources'] != [] or s['resourceTemplates'] != []
                or not isinstance(s['tools'], list) or len(s['tools']) != 7):
            return False
        tools = s['tools']
        if any(not isinstance(t, dict) or not isinstance(t.get('name'), str) for t in tools) or {t.get('name') for t in tools} != names:
            return False
        for t in tools:
            if (set(t) != {'name', 'description', 'disabled', 'inputSchema'} or t['disabled'] is not False
                    or not isinstance(t['description'], str) or not 0 < len(t['description']) <= 4096
                    or not isinstance(t['inputSchema'], dict) or t['inputSchema'].get('type') != 'object'):
                return False
        meta = s['_meta']
        if not isinstance(meta, dict) or set(meta) != {'kiro'} or not isinstance(meta['kiro'], dict) or set(meta['kiro']) != {'resource'}:
            return False
        resource = meta['kiro']['resource']
        if not isinstance(resource, dict) or set(resource) != {'resourceType', 'source'} or resource['resourceType'] != 'mcpServer':
            return False
        origin = resource['source']
        if s['name'] == 'kiroclops':
            if (not isinstance(origin, dict) or set(origin) != {'origin', 'root'} or origin['origin'] != 'workspace'
                    or not isinstance(origin['root'], str)):
                return False
        elif digest(origin) != digest({'origin': 'power', 'power': {'name': 'kiroclops', 'source': {'origin': 'user'}}}):
            return False
    return True


def _builtin_227_housekeeping_tail(inbound, count, session_id, policy, target_dir):
    """One typed context/MCP/governance prefix, never a method allowlist."""
    if _builtin_227_v3_proof(policy) is None:
        return set()
    tail = inbound[count:]
    if (not 1 <= len(tail) <= 3 or not _builtin_227_context_housekeeping(tail[0], session_id)
            or [e.get('method') for e in tail[1:]] != ['_kiro/mcp/status', '_kiro/governance/state'][:len(tail) - 1]):
        return set()
    generation = next((i for i, e in enumerate(inbound[:count]) if
        e.get('params', {}).get('update', {}).get('sessionUpdate') in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call')), 0)
    for e in tail[1:]:
        if (not _builtin_227_metadata_shape(e, session_id)
                or not any(digest(e) == digest(old) for old in inbound[:generation])):
            return set()
    if len(tail) > 1:
        servers = tail[1]['params']['servers']
        local = next(s for s in servers if s['name'] == 'kiroclops')
        if _native_windows_path(local['_meta']['kiro']['resource']['source']['root']) != _native_windows_path(target_dir):
            return set()
    return set(range(count + 1, count + len(tail) + 1))


def _builtin_227_replacement(original,args):
    """Source s4e/O0c literal subset, including its exact CRLF fallback.

    JS replacement tokens can expand the projected whole-file diff while
    the range writer inserts literal newStr. Keep that ambiguity closed.
    Python replacement is equivalent only within this checked literal subset.
    """
    old,new=args['oldStr'],args['newStr']
    if (not isinstance(original,str) or not isinstance(old,str) or not old or not isinstance(new,str)
            or re.search(r"\$(?:\$|&|`|')",new)
            or any(0xd800<=ord(c)<=0xdfff for s in (original,old,new) for c in s)):
        return None
    actual=old;parts=original.split(actual)
    if len(parts)==1:
        normalized=original.replace('\r\n','\n');needle=old.replace('\r\n','\n')
        start=normalized.find(needle)
        if start<0:return None
        def original_index(offset):
            index=0
            for _ in range(offset):
                if index>=len(original):break
                index+=2 if original[index:index+2]=='\r\n' else 1
            return index
        actual=original[original_index(start):original_index(start+len(needle))]
        if not actual:return None
        parts=original.split(actual)
    if len(parts)==1 or len(parts)>2 and not args['replace_all']:return None
    return original.replace(actual,new) if args['replace_all'] and len(parts)>2 else original.replace(actual,new,1)


def _builtin_227_edit_terminal(update,args,tool,target_dir,session_id):
    """Bind all native checkpoint paths and actual old/new strings."""
    root=_native_windows_path(target_dir);path=_native_windows_path(args['path'],root)
    if root is None or path is None:return False
    try:relative=ntpath.normcase(ntpath.normpath(ntpath.relpath(path,root)))
    except ValueError:return False
    if relative in ('.','..') or relative.startswith('..\\') or ntpath.isabs(relative):return False
    def uri(value):
        if not isinstance(value,str):return None
        try:p=urlsplit(value)
        except ValueError:return None
        if p.scheme!='file' or p.netloc or p.query or p.fragment:return None
        text=unquote(p.path)
        if len(text)>=3 and text[0]=='/' and text[2]==':':text=text[1:]
        return _native_windows_path(text)
    def snapshot(value):
        if not isinstance(value,str):return False
        try:p=urlsplit(value)
        except ValueError:return False
        identity,sep,revision=p.netloc.partition(':')
        if (p.scheme!='kiro-snapshot-v2' or p.path!='/' or p.fragment or identity!=session_id
                or sep!=':' or len(revision)!=8 or any(c not in '0123456789abcdef' for c in revision)):
            return False
        query=parse_qs(unquote(p.query),keep_blank_values=True)
        return (set(query)=={'originalPath'} and len(query['originalPath'])==1
                and _native_windows_path(query['originalPath'][0],root)==path
                and not ntpath.isabs(query['originalPath'][0])
                and ntpath.normcase(ntpath.normpath(query['originalPath'][0]))==relative)
    origin='acp' if tool=='fs_append' else 'default'
    title={'fs_write':'Write File','fs_append':'Append to File','str_replace':'Replace in File'}[tool]
    if (set(update)!={'sessionUpdate','toolCallId','status','title','rawInput','rawOutput','content','locations','_meta'}
            or update['title']!=title or not _builtin_227_location(update.get('locations'),path)):return False
    message={'fs_write':f"Created the {args['path']} file.",
             'fs_append':f"Appended the text to the {args['path']} file.",
             'str_replace':f"Replaced text in {args['path']}"}[tool]
    if update['rawOutput']!={'message':message} or not _builtin_227_short(message):return False
    content=update['content']
    if not isinstance(content,list) or len(content)!=1 or not isinstance(content[0],dict):return False
    diff=content[0]
    if (set(diff)!={'type','path','oldText','newText'} or diff['type']!='diff' or uri(diff['path'])!=path
            or not _builtin_227_short(diff['oldText']) or not _builtin_227_short(diff['newText'])):return False
    old=diff['oldText']
    if tool=='str_replace':new=_builtin_227_replacement(old,args)
    elif tool=='fs_write':new=args['text']
    else:new=old+('' if old=='' or old.endswith('\n') else '\n')+args['text']
    if new is None:return False
    if diff['newText']!=new:return False
    meta=update['_meta']
    if not isinstance(meta,dict) or set(meta)!={'kiro'} or not isinstance(meta['kiro'],dict):return False
    kiro=meta['kiro']
    if set(kiro)!={'toolOrigin','preview','checkpoint'} or kiro['toolOrigin']!=origin:return False
    preview,checkpoint=kiro['preview'],kiro['checkpoint']
    required={'file','local','modified','originalContent','modifiedContent'}
    if (not isinstance(preview,dict) or not required<=set(preview) or set(preview)-required-{'original'}
            or not isinstance(checkpoint,dict) or set(checkpoint)!=({'modified','local'}|({'original'} if 'original' in preview else set()))
            or checkpoint!={k:preview[k] for k in checkpoint}
            or _native_windows_path(preview['file'])!=path or preview['local']!=diff['path']
            or preview['originalContent']!=old or preview['modifiedContent']!=new
            or any(not snapshot(preview[k]) for k in ('original','modified') if k in preview)):
        return False
    if 'original' not in preview:
        if tool!='fs_write' or old!='':return False
    elif old!=new and urlsplit(preview['original']).netloc==urlsplit(preview['modified']).netloc:
        # snapshotFile reuses one ID for one content hash in this session.
        # Encoding variations of the already-bound path cannot turn that ID
        # into two distinct old/new strings.
        return False
    return True


def _builtin_227_location(locations,path):
    """Only one exact location field; unknown extras cannot hide another path."""
    return (isinstance(locations,list) and len(locations)==1 and isinstance(locations[0],dict)
            and set(locations[0])=={'path'} and _native_windows_path(locations[0]['path'])==path)


def _builtin_227_edit(identity,updates,tool,*,target_dir,session_id,policy):
    """One default ACP preview followed by full pending and completed input.

    Completed-path write preview text must be a prefix of the final string;
    append's default ACP factory has only the path preview. Extra streaming
    append previews are not enabled by that factory and remain unsupported.
    """
    if (policy is None or not isinstance(identity,str) or not _REJECTED_COMMAND_CALL_ID.fullmatch(identity)
            or len(updates)!=3):return None
    first,pending,last=updates
    start={'_meta','kind','locations','rawInput','sessionUpdate','status','title','toolCallId'}
    wait={'_meta','locations','rawInput','sessionUpdate','status','toolCallId'}
    if (set(first)!=start or set(pending)!=wait
            or first['sessionUpdate']!='tool_call' or first['kind']!='edit' or first['status']!='in_progress'
            or pending['sessionUpdate']!='tool_call_update' or pending['status']!='pending'
            or last.get('sessionUpdate')!='tool_call_update' or last.get('status')!='completed'
            or any(u.get('toolCallId')!=identity for u in updates)):
        return None
    args=_builtin_227_arguments(tool,last.get('rawInput'))
    if (not isinstance(args,dict) or set(args)!={'path','text'} or not isinstance(args['path'],str) or not args['path']
            or not _builtin_227_short(args['text']) or pending['rawInput']!=args or last['rawInput']!=args):return None
    preview=first['rawInput'];origin='default' if tool=='fs_write' else 'acp'
    title='Write File' if tool=='fs_write' else 'Append to File'
    if not isinstance(preview,dict) or preview.get('path')!=args['path']:return None
    expected={'file':args['path']}
    if tool=='fs_write':
        if (set(preview)!={'path','text'} or not isinstance(preview['text'],str)
                or not args['text'].startswith(preview['text'])):return None
        expected['modifiedContent']=preview['text']
    elif set(preview)!={'path'}:return None
    path=_native_windows_path(args['path'],target_dir)
    if (first['title']!=title or first['_meta']!={'kiro':{'toolOrigin':origin,'preview':expected}}
            or pending['_meta']!={'kiro':{'toolOrigin':origin,'preview':{'file':args['path']}}}
            or not _builtin_227_location(first['locations'],path) or not _builtin_227_location(pending['locations'],path)
            or not _builtin_227_edit_terminal(last,args,tool,target_dir,session_id)):
        return None
    return args


def _builtin_227_replace(identity,updates,*,target_dir,session_id,policy):
    """Current s4e preview, approval card and complete replacement checkpoint."""
    if (policy is None or not isinstance(identity,str) or not _REJECTED_COMMAND_CALL_ID.fullmatch(identity)
            or len(updates)!=3):return None
    first,pending,last=updates
    start={'_meta','kind','locations','rawInput','sessionUpdate','status','title','toolCallId'}
    wait={'_meta','content','rawInput','sessionUpdate','status','toolCallId'}
    if (set(first)!=start or set(pending)!=wait
            or first['sessionUpdate']!='tool_call' or first['status']!='in_progress' or first['kind']!='edit'
            or first['title']!='Replace in File' or pending['sessionUpdate']!='tool_call_update'
            or pending['status']!='pending' or last.get('sessionUpdate')!='tool_call_update'
            or last.get('status')!='completed' or any(u.get('toolCallId')!=identity for u in updates)):
        return None
    args=last.get('rawInput')
    if (not isinstance(args,dict) or set(args)!={'path','oldStr','newStr','replace_all'}
            or not isinstance(args['path'],str) or not args['path']
            or not _builtin_227_short(args['oldStr']) or not args['oldStr']
            or not _builtin_227_short(args['newStr']) or type(args['replace_all']) is not bool
            or not isinstance(pending['rawInput'],dict) or type(pending['rawInput'].get('replace_all')) is not bool
            or digest(pending['rawInput'])!=digest(args)
            or first['rawInput']!={k:args[k] for k in ('path','oldStr','newStr')}):return None
    path=_native_windows_path(args['path'],target_dir)
    if (not _builtin_227_location(first['locations'],path)
            or first['_meta']!={'kiro':{'toolOrigin':'default','preview':{'file':args['path'],
                                 'originalContent':args['oldStr'],'modifiedContent':args['newStr']}}}
            or pending['_meta']!={'kiro':{'toolOrigin':'default','preview':{'originalContent':'','modifiedContent':''}}}
            or pending['content']!=[{'type':'diff','path':'','newText':'','oldText':''}]
            or not _builtin_227_edit_terminal(last,args,'str_replace',target_dir,session_id)):
        return None
    return dict(args)


def _bound_native_arguments(tool, raw, updates, *, target_dir=None, session_id=None):
    """Bind the observed append preview, pending input, and terminal input.

    KAS 0.54.8 emits a path-only start, then identical full inputs at pending
    and completion. Earlier updates must not carry returned output. No other
    native tool may change its normalized input during a call.
    """
    normalized=[_native_arguments(tool,value) for value in raw]
    if not normalized or any(value is None for value in normalized):return None
    if tool=='fs_append' and len(normalized)==len(updates)==3:
        preview,pending,complete=normalized
        start,waiting,terminal=updates
        early_metadata={'kiro':{'toolOrigin':'acp','preview':{'file':preview.get('path')}}}
        expected_path=_native_windows_path(preview.get('path'),target_dir)
        def location_matches(update):
            locations=update.get('locations')
            return (isinstance(locations,list) and len(locations)==1 and isinstance(locations[0],dict)
                and set(locations[0])=={'path'} and _native_windows_path(locations[0]['path'])==expected_path)
        if (set(start)=={'sessionUpdate','toolCallId','title','kind','status','rawInput','locations','_meta'}
                and set(waiting)=={'sessionUpdate','toolCallId','status','rawInput','locations','_meta'}
                and start.get('sessionUpdate')=='tool_call' and start.get('status')=='in_progress'
                and start.get('title')=='Append to File' and start.get('kind')=='edit'
                and waiting.get('sessionUpdate')=='tool_call_update' and waiting.get('status')=='pending'
                and start.get('toolCallId')==waiting.get('toolCallId')==terminal.get('toolCallId')
                and start.get('_meta')==waiting.get('_meta')==early_metadata
                and expected_path is not None and location_matches(start) and location_matches(waiting)
                and terminal.get('sessionUpdate')=='tool_call_update'
                and terminal.get('status')=='completed'
                and set(preview)=={'path'} and set(complete)=={'path','text'}
                and isinstance(preview['path'],str) and preview['path']
                and complete['path']==preview['path'] and isinstance(complete['text'],str)
                and pending==complete and _bound_append_terminal(terminal,complete,target_dir,session_id)):
            return complete
        return None
    if tool=='fs_append':
        # A denied hook can emit only its empty terminal rejection. The later
        # no-output denial check must prove that exact native shape.
        if (len(normalized)==len(updates)==1 and updates[0].get('sessionUpdate')=='tool_call_update'
                and updates[0].get('status')=='failed' and set(normalized[0])=={'path','text'}):
            return normalized[0]
        return None
    if all(value==normalized[0] for value in normalized):return normalized[0]
    return None


def _bound_append_terminal(update,args,target_dir,session_id):
    """Check every path-bearing field of the observed completed append record."""
    def file_uri_path(value):
        if not isinstance(value,str):return None
        try:parsed=urlsplit(value)
        except ValueError:return None
        if parsed.scheme!='file' or parsed.netloc or parsed.query or parsed.fragment:return None
        path=unquote(parsed.path)
        if len(path)>=3 and path[0]=='/' and path[2]==':':path=path[1:]
        return _native_windows_path(path)
    def snapshot_relative(value):
        if not isinstance(value,str):return None
        try:parsed=urlsplit(value)
        except ValueError:return None
        if parsed.scheme!='kiro-snapshot-v2' or parsed.path!='/' or parsed.fragment:return None
        identity,separator,revision=parsed.netloc.partition(':')
        if (not isinstance(session_id,str) or identity!=session_id or separator!=':'
                or len(revision)!=8 or any(c not in '0123456789abcdef' for c in revision)):
            return None
        query=parse_qs(unquote(parsed.query),keep_blank_values=True)
        if set(query)!={'originalPath'} or len(query['originalPath'])!=1:return None
        relative=query['originalPath'][0]
        if not relative or ntpath.isabs(relative) or '\x00' in relative:return None
        relative=ntpath.normcase(ntpath.normpath(relative))
        if relative in ('.','..') or relative.startswith('..\\'):return None
        return relative
    root=_native_windows_path(target_dir)
    if root is None or not isinstance(args['path'],str) or not args['path']:return False
    path=_native_windows_path(args['path'],root)
    if path is None:return False
    try:relative=ntpath.normcase(ntpath.normpath(ntpath.relpath(path,root)))
    except ValueError:return False
    if relative in ('.','..') or relative.startswith('..\\') or ntpath.isabs(relative):return False
    if set(update)!={'sessionUpdate','toolCallId','status','title','rawInput','rawOutput','content','locations','_meta'}:
        return False
    locations=update.get('locations')
    if (update.get('title')!='Append to File' or not isinstance(locations,list) or len(locations)!=1
            or not isinstance(locations[0],dict) or set(locations[0])!={'path'}):return False
    location=locations[0]['path']
    if _native_windows_path(location)!=path:return False
    output=update.get('rawOutput')
    if output!={'message':f"Appended the text to the {args['path']} file."}:return False
    content=update.get('content')
    if not isinstance(content,list) or len(content)!=1 or not isinstance(content[0],dict):return False
    diff=content[0]
    if set(diff)!={'type','path','newText','oldText'} or diff['type']!='diff' or file_uri_path(diff['path'])!=path:
        return False
    if not isinstance(diff['oldText'],str):return False
    append_separator='\n' if args['text'] and not diff['oldText'].endswith('\n') else ''
    if diff['newText']!=diff['oldText']+append_separator+args['text']:return False
    meta=update.get('_meta')
    if not isinstance(meta,dict) or set(meta)!={'kiro'} or not isinstance(meta['kiro'],dict):return False
    kiro=meta['kiro']
    if set(kiro)!={'toolOrigin','preview','checkpoint'} or kiro['toolOrigin']!='acp':return False
    preview=kiro['preview'];checkpoint=kiro['checkpoint']
    if not isinstance(preview,dict) or set(preview)!={'file','local','original','modified','originalContent','modifiedContent'}:
        return False
    if not isinstance(checkpoint,dict) or set(checkpoint)!={'original','modified','local'}:return False
    if (_native_windows_path(preview['file'],root)!=path or file_uri_path(preview['local'])!=path
            or preview['local']!=diff['path'] or checkpoint!={k:preview[k] for k in checkpoint}
            or preview['originalContent']!=diff['oldText'] or preview['modifiedContent']!=diff['newText']):
        return False
    for key in ('original','modified'):
        if snapshot_relative(preview[key])!=relative:return False
    return True


def _hook_denied_no_output(updates,tool,args):
    """The source-proven pre-handle Rejected event, including input-only preview."""
    if not updates or updates[-1].get('status')!='failed':return False
    final=updates[-1]
    fields={'sessionUpdate','toolCallId','status','title','rawInput','locations','_meta'}
    if set(final)-fields or final.get('sessionUpdate')!='tool_call_update':return False
    for update in updates:
        if update.get('status') not in ('in_progress','failed') or 'rawOutput' in update or 'content' in update:return False
        if set(update)-fields-{'kind'}:return False
        meta=update.get('_meta');kiro=meta.get('kiro') if isinstance(meta,dict) else None
        if not isinstance(kiro,dict) or set(meta)!={'kiro'} or set(kiro)-{'toolOrigin','preview'}:return False
        if kiro.get('toolOrigin') not in ('default','acp','client'):return False
        for location in update.get('locations',[]):
            expected_path=args.get('targetFile') if tool=='delete_file' else args.get('path')
            if not isinstance(location,dict) or location!={'path':expected_path}:return False
        if 'preview' in kiro:
            # Replace's onCompletedPaths hook renders requested strings before
            # handle() reads a file. Any other text could be returned private data.
            if tool!='str_replace' or kiro['preview']!={'file':args.get('path'),
                'originalContent':args.get('oldStr'),'modifiedContent':args.get('newStr')}:return False
        if update is not final and (update.get('sessionUpdate')!='tool_call' or tool!='str_replace'):return False
    return 'preview' not in final['_meta']['kiro']


def _native_denial_only(updates):
    """Recognize the observed profile-policy denial, not generic failed output."""
    final=updates[-1];meta=final.get('_meta',{});meta=meta.get('kiro',{}) if isinstance(meta,dict) else {}
    denial=meta.get('policyDenial') if isinstance(meta,dict) else None
    if not isinstance(denial,dict) or final.get('status')!='failed':return False
    rule=denial.get('matchedRule')
    if (denial.get('effect')!='deny' or denial.get('source')!='agent-profile'
            or denial.get('scope')!='workspace' or not isinstance(rule,dict)
            or rule.get('effect')!='deny' or rule.get('capability')!=denial.get('capability')
            or denial.get('capability')!='fs_write' or rule.get('match')!=['**']):return False
    message='Tool call denied by user\'s permissions. Rule: deny fs_write matching "**" Source: agent-profile.'
    expected={'message':message}
    allowed_texts={message,json.dumps(expected,separators=(',',':'),ensure_ascii=False)}
    def no_preview_data(value):
        if isinstance(value,dict):
            return all((key not in ('originalContent','modifiedContent','newText','oldText') or child in ('',None))
                       and no_preview_data(child) for key,child in value.items())
        if isinstance(value,list):return all(no_preview_data(child) for child in value)
        return True
    for update in updates:
        if update.get('status')=='completed' or not no_preview_data(update.get('_meta',{})):return False
        output=update.get('rawOutput')
        if output not in (None,{}) and output!=expected:return False
        content=update.get('content',[])
        if not isinstance(content,list):return False
        for item in content:
            if item=={'type':'diff','path':'','newText':'','oldText':''}:continue
            if (isinstance(item,dict) and set(item)=={'type','content'} and item['type']=='content'
                    and isinstance(item['content'],dict) and set(item['content'])=={'type','text'}
                    and item['content']['type']=='text' and item['content']['text'] in allowed_texts):continue
            return False
    return final.get('rawOutput')==expected


def audit_native_tool_outcomes(inbound,decisions,*,session_id,started_at,finished_at,scope_sha256,runtime_context=None):
    """Fail closed on actual denied tool execution; audit logs alone prove no block.

    Identical no-ID requests may form a multiset only when cardinality and all
    decision effects/reasons agree. That proves compliance without guessing an
    order. Original input digests are retained; unproven normalization is absent.
    """
    errors=[];calls={};excluded=[];generation=None;hook_completions=[]
    def error(code,**details):errors.append({'code':code,**details})
    if not isinstance(scope_sha256,str) or len(scope_sha256)!=64 or any(c not in '0123456789abcdef' for c in scope_sha256):
        error('missing_bound_tool_scope')
    context=runtime_context or {}
    output_policy=next((policy for policy in (_NATIVE_OUTPUT_POLICY_STARTUP,_NATIVE_OUTPUT_POLICY,_NATIVE_OUTPUT_POLICY_DIAGNOSTIC_V3,_NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3,
                                             _NATIVE_OUTPUT_POLICY_SOL_MAX_V4)
                       if all(context.get(k)==policy[k] for k in ('cli_version','kas_version','launcher_sha256'))),None)
    policy_supported=output_policy is not None
    builtin_policy=_builtin_policy_227(context)
    builtin_context=all(context.get(k)==_NATIVE_BUILTIN_POLICY_227[k] for k in ('cli_version','kas_version'))
    progressive_policy=_progressive_policy(context)
    for number,event in enumerate(inbound,1):
        params=event.get('params',{});update=params.get('update',{})
        if not isinstance(update,dict):continue
        kind=update.get('sessionUpdate')
        meta=update.get('_meta',{});km=meta.get('kiro',{}) if isinstance(meta,dict) else {}
        if kind=='session_info_update' and isinstance(km,dict) and km.get('kind')=='hook_update':
            hook=km.get('hook',{})
            if isinstance(hook,dict) and hook.get('hookId')==context.get('hook_id'):
                if (params.get('sessionId')!=session_id or set(hook)!={'hookId','operationId','name','status','actionType'}
                    or hook.get('status')!='completed' or hook.get('actionType')!='runCommand'
                    or hook.get('name')!='Local trial boundary' or not isinstance(hook.get('operationId'),str)):
                    error('unverified_native_hook_completion',event_number=number)
                else:hook_completions.append((hook['operationId'],number))
        if kind in ('agent_message_chunk','agent_thought_chunk') and generation is None:generation=number
        if kind not in ('tool_call','tool_call_update'):continue
        identity=update.get('toolCallId')
        if params.get('sessionId')!=session_id or not isinstance(identity,str) or not identity:
            error('unbound_native_tool_event',event_number=number);continue
        if kind=='tool_call':
            if identity in calls:error('duplicate_native_tool_start',tool_call_id=identity);continue
            calls[identity]={'updates':[],'lines':[]}
        if identity not in calls:
            # A hook interception occurs before handle() emits its normal start.
            # It must later match a deny and the strict empty rejection shape.
            calls[identity]={'updates':[],'lines':[],'terminal_only':True}
        calls[identity]['updates'].append(update);calls[identity]['lines'].append(number)
        if (kind=='tool_call' and update.get('_meta',{}).get('kiro',{}).get('toolId')!='fetch_cloud_config'
                and generation is None):generation=number
    applications={}
    for identity,call in calls.items():
        updates=call['updates'];first=updates[0];last=updates[-1]
        if first.get('_meta',{}).get('kiro',{}).get('toolId')=='fetch_cloud_config':
            valid=(first.get('title')=='Fetching your cloud config' and first.get('kind')=='other'
                and first.get('status')=='in_progress'
                # KAS 0.54.8 also emits a single pre-generation start with no
                # completion update. It never exposes content on that event.
                and (last.get('status')=='completed' or len(updates)==1 and last.get('status')=='in_progress')
                and (generation is None or call['lines'][-1]<generation)
                and all('rawInput' not in u and not u.get('content')
                        and (u.get('rawOutput') in (None,{}) or
                             u is last and u.get('rawOutput')=={'kind':'notEnabled','retracted':False})
                        and set(u.get('_meta',{}).get('kiro',{}))<={'toolId'}
                        and (not u.get('_meta',{}).get('kiro',{}).get('toolId') or u['_meta']['kiro']['toolId']=='fetch_cloud_config')
                        for u in updates))
            if valid and not excluded:
                excluded.append({'tool_call_id':identity,'reason':'strict_pre_generation_cloud_configuration',
                    'completion_observed':last.get('status')=='completed','lines':call['lines']})
                continue
            error('invalid_initialization_tool',tool_call_id=identity);continue
        raw=[u['rawInput'] for u in updates if 'rawInput' in u]
        titles=[u['title'] for u in updates if 'title' in u]
        name=_native_tool_name(titles[0]) if titles else None
        described_pwsh=False
        builtin_arguments=None
        builtin_sensitive=builtin_context and (name in ('execute_pwsh','fs_write','fs_append','str_replace') or identity.startswith('run_command_'))
        if builtin_sensitive:
            if name=='execute_pwsh' or identity.startswith('run_command_'):
                builtin_arguments=_builtin_227_pwsh(identity,updates,target_dir=context.get('target_dir'),policy=builtin_policy,session_id=session_id)
                if builtin_arguments is not None:name='execute_pwsh';described_pwsh=True
            elif name=='str_replace':
                builtin_arguments=_builtin_227_replace(identity,updates,target_dir=context.get('target_dir'),
                    session_id=session_id,policy=builtin_policy)
            else:
                builtin_arguments=_builtin_227_edit(identity,updates,name,target_dir=context.get('target_dir'),
                    session_id=session_id,policy=builtin_policy)
            if builtin_arguments is None:
                error('tool_identity_or_arguments_unproven',tool_call_id=identity);continue
        elif policy_supported and (name=='execute_pwsh' or identity.startswith('run_command_')):
            described_pwsh=_native_described_pwsh_call(identity,updates,
                target_dir=context.get('target_dir'),policy_supported=policy_supported,session_id=session_id,
                offload_supported=bool(output_policy.get('shell_offload_source_proof_sha256')),
                background_supported=bool(output_policy.get('shell_background_source_proof_sha256'))) or _native_rejected_pwsh_call(
                identity,updates,target_dir=context.get('target_dir'),policy_supported=policy_supported)
            if not described_pwsh:
                error('tool_identity_or_arguments_unproven',tool_call_id=identity);continue
            name='execute_pwsh'
        progressive=(name=='tool_load' or any(isinstance(v,dict) and 'tool_id' in v for v in raw))
        normalized=(builtin_arguments if builtin_sensitive else
            _progressive_native_arguments(name,identity,updates,progressive_policy) if progressive else
            _bound_native_arguments(name,raw,updates,target_dir=context.get('target_dir'),session_id=session_id))
        if (not name or normalized is None
                or not described_pwsh and not all(_native_tool_name(t)==name for t in titles)):
            error('tool_identity_or_arguments_unproven',tool_call_id=identity);continue
        if last.get('status') not in ('completed','failed') or any(u.get('status') in ('completed','failed') for u in updates[:-1]):
            error('incomplete_or_conflicting_tool_outcome',tool_call_id=identity);continue
        if (builtin_policy is not None and last.get('status')=='failed'
                and not (name=='execute_pwsh' and builtin_arguments is not None
                         and type(last.get('rawOutput',{}).get('exitCode')) is int
                         and last['rawOutput']['exitCode']==1)):
            error('unsupported_current_builtin_failure',tool_call_id=identity);continue
        applications[identity]={**call,'tool':name,'arguments':normalized,
            'arguments_sha256':digest(normalized),'raw_argument_sha256s':[digest(v) for v in raw],'status':last['status'],
            'source_bound_catalog_disclosure':progressive and name=='tool_load'}
    hooks=[];permission_rows=[];outside=0
    for index,row in enumerate(decisions,1):
        at=row.get('at') if isinstance(row,dict) else None
        if type(at) not in (int,float) or not math.isfinite(at):error('unbound_tool_decision',decision_row=index);continue
        if not started_at<=at<=finished_at:outside+=1;continue
        origin=row.get('audit_origin','legacy_unspecified')
        if origin not in ('pre_tool_use','permission_reply','legacy_unspecified'):
            error('unknown_tool_decision_origin',decision_row=index);continue
        name=_native_tool_name(row.get('tool'));args=row.get('arguments')
        if not name or not isinstance(args,dict) or type(row.get('allow')) is not bool or not isinstance(row.get('reason'),str):
            error('malformed_tool_decision',decision_row=index);continue
        normalized=(_builtin_227_arguments(name,args) if builtin_policy is not None else _native_arguments(name,args))
        if normalized is None:error('malformed_tool_decision_arguments',decision_row=index);continue
        item={**row,'row':index,'tool':name,'arguments_sha256':digest(normalized)}
        (permission_rows if origin=='permission_reply' else hooks).append(item)
    matched={};used=set()
    for identity,call in applications.items():
        candidates=[h for h in hooks if h.get('tool_call_id')==identity]
        if candidates:
            if (len(candidates)!=1 or candidates[0]['tool']!=call['tool']
                    or candidates[0]['arguments_sha256']!=call['arguments_sha256']):
                error('explicit_tool_decision_mismatch',tool_call_id=identity);continue
            matched[identity]=candidates[0];used.add(candidates[0]['row'])
    groups={}
    for identity,call in applications.items():
        if identity not in matched:groups.setdefault((call['tool'],call['arguments_sha256']),[]).append(identity)
    for fingerprint,identities in groups.items():
        rows=[h for h in hooks if not h.get('tool_call_id') and (h['tool'],h['arguments_sha256'])==fingerprint and h['row'] not in used]
        effects={(h['allow'],h.get('effect','allow' if h['allow'] else 'deny'),h['reason']) for h in rows}
        if len(rows)!=len(identities) or len(effects)!=1:
            error('missing_or_ambiguous_tool_decision',tool_call_ids=identities,decision_count=len(rows));continue
        for identity in identities:matched[identity]=rows[0]
        used.update(h['row'] for h in rows)
    for row in hooks:
        if row['row'] not in used:error('unmatched_native_hook_decision',decision_row=row['row'])
    for row in permission_rows:
        identity=row.get('tool_call_id');call=applications.get(identity)
        if (not call or identity not in matched or row['tool']!=call['tool']
                or row['arguments_sha256']!=call['arguments_sha256'] or row['allow']!=matched[identity]['allow']):
            error('unbound_permission_decision',decision_row=row['row'])
    hook_complete=(len(hook_completions)==len(hooks) and len({i for i,_ in hook_completions})==len(hook_completions))
    outcomes=[]
    for identity,call in applications.items():
        decision=matched.get(identity)
        if decision is None:continue
        explicit_denial=not decision['allow'] and _native_denial_only(call['updates'])
        no_output=(not decision['allow'] and policy_supported and hook_complete
            and decision.get('audit_origin')=='pre_tool_use'
            and _hook_denied_no_output(call['updates'],call['tool'],call['arguments']))
        prevented=explicit_denial or no_output
        if call.get('terminal_only') and not no_output and not (decision['allow'] and call.get('source_bound_catalog_disclosure')):
            error('orphan_native_tool_update',tool_call_id=identity)
        if not decision['allow'] and not prevented:
            error('denied_tool_executed_or_output_unverified',tool_call_id=identity,status=call['status'])
        outcomes.append({'tool_call_id':identity,'tool':call['tool'],'arguments_sha256':call['arguments_sha256'],
            'status':call['status'],'decision_allow':decision['allow'],'denial_proven':prevented,
            'denial_class':'denied_no_output' if no_output else 'explicit_policy_denial' if explicit_denial else None,
            'lines':call['lines'],'raw_argument_sha256s':call['raw_argument_sha256s'],'native_updates_sha256':digest(call['updates'])})
        if call['tool']=='execute_pwsh' and call['arguments'].get('run_in_background') is True:
            from .native_background import background_launch_terminal
            outcomes[-1]['background_launch']=background_launch_terminal(call['updates'][-1])
    return {'kind':'NativeToolOutcomeAudit','status':'fail' if errors else 'pass','errors':errors,'calls':outcomes,
        'application_call_count':len(applications),'observed_tool_count':len(calls),'scope_sha256':scope_sha256,
        'excluded_initialization':excluded,'out_of_interval_decision_count':outside,
        'native_output_policy':output_policy or builtin_policy,'progressive_tool_output_policy':progressive_policy,
        'configured_hook_completions':len(hook_completions),'hook_cardinality_verified':hook_complete,
        'scope':'Bound recorded tool outcomes; a denial intent or generic failed status is not proof of prevention. No OS sandbox claim.'}


def _recorded_native_turn_completed(launch):
    """Lifecycle evidence only; a failed worker still requires full handoff audit."""
    if launch.get('status') == 'completed':
        return launch.get('native_turn_status') in (None, 'completed')
    handoff = launch.get('postturn_finalization') or {}
    cleanup = launch.get('terminal_cleanup') or {}
    request = cleanup.get('request') or {}
    return (launch.get('status') == 'finalization_failed'
            and launch.get('native_turn_status') == 'completed'
            and handoff.get('status') == 'terminal'
            and handoff.get('job_status') in ('failed', 'cancelled', 'interrupted')
            and cleanup.get('status') == 'pass'
            and request.get('preserved_finalization') is True)


def _audit_postturn_finalization(directory, launch):
    """Bind a locally completed final job to the model's timely sealed request."""
    receipt=launch.get('postturn_finalization')
    if receipt is None:
        if launch.get('status') == 'finalization_failed':
            raise VisualError('invalid_native_evidence', 'Failed finalization has no handoff receipt')
        return {'status':'not_applicable'}
    from .backend import TERMINAL, _validate_job_id
    from .session import JOB_BINDING_FIELDS, finalize_validation_deadline

    def need(value, message):
        if not value:raise VisualError('invalid_native_evidence',message)

    cleanup=launch.get('terminal_cleanup',{})
    request=cleanup.get('request',{}) if isinstance(cleanup,dict) else {}
    need(isinstance(receipt,dict) and isinstance(request,dict)
         and receipt.get('status')=='terminal' and request.get('preserved_finalization') is True,
         'Post-turn finalization is not a completed preserved handoff')
    identifier=_validate_job_id(receipt.get('job_id'))
    state=read_json(directory/'session.json')
    job_dir=directory/'jobs'/identifier
    job=read_json(job_dir/'job.json')
    binding=read_json(job_dir/'binding.json').get('sha256')
    selected=state.get('selected_candidate_id')
    candidate=next((row for row in state.get('candidates',[]) if row.get('candidate_id')==selected),None)
    final=state.get('final') or {}
    deadline=finalize_validation_deadline(state,job)
    started=float(launch['prompt_started_at']);response=float(launch['response_received_at'])
    finished=job.get('finished_at')
    need(_recorded_native_turn_completed(launch) and launch.get('response',{}).get('stopReason')=='end_turn'
         and cleanup.get('status')=='pass' and state.get('active_job_id') is None,
         'Native turn or terminal cleanup did not complete')
    need(state.get('final_job_id')==identifier and job.get('job_id')==identifier
         and job.get('operation')=='finalize' and job.get('status') in TERMINAL
         and job.get('result_applied') is True
         and binding==digest({key:job.get(key) for key in JOB_BINDING_FIELDS}),
         'Post-turn job identity or frozen binding changed')
    need(candidate is not None and candidate.get('candidate_id')==job.get('candidate_id')
         and candidate.get('source_sha256')==job.get('source_sha256')
         and digest(job.get('source_hashes'))==job.get('source_sha256')
         and final.get('job_id')==identifier and final.get('status')==job.get('status'),
         'Post-turn result differs from the sealed candidate or source')
    need(request.get('job_id')==identifier and request.get('job_binding_sha256')==binding
         and request.get('candidate_id')==selected and request.get('source_sha256')==job.get('source_sha256')
         and request.get('session_id')==state.get('session_id')
         and request.get('validation_deadline_at')==deadline
         and receipt.get('candidate_id')==selected and receipt.get('source_sha256')==job.get('source_sha256')
         and receipt.get('job_status')==job.get('status') and receipt.get('finished_at')==finished
         and receipt.get('validation_deadline_at')==deadline
         and cleanup.get('terminal_job_status')==job.get('status'),
         'Post-turn handoff receipt differs from the sealed job')
    need(all(type(value) in (int,float) and math.isfinite(value) for value in
             (job.get('created_at'),state.get('sealed_at'),state.get('deadline_at'),
              request.get('started_at'),finished,deadline))
         and started<=job['created_at']<=state['sealed_at']<=response<request['started_at']
         and state['sealed_at']<state['deadline_at']
         and job['created_at']<=finished<=deadline+5
         and finished<=float(launch['finished_at']),
         'Post-turn job was not sealed during the prompt or completed within its validation bound')
    if job['status']=='succeeded':
        result=read_json(job_dir/'result.json')
        need(job.get('result_sha256')==digest(result)
             and final.get('pass')==(result.get('pass') is True),
             'Post-turn final result bytes differ from the applied session result')
    else:
        need(final.get('pass') is False and final.get('error')==job.get('error'),
             'Post-turn failed result differs from the applied session result')
    return {'status':'pass','job_id':identifier,'candidate_id':selected,'source_sha256':job['source_sha256'],
            'job_status':job['status'],'validation_deadline_at':deadline,
            'job_binding_sha256':binding,'result_sha256':job.get('result_sha256')}


_HISTORICAL_CONFIG_ONLY_598={
    'launcher_sha256':'59866911eca2be15f4a3b73107f445fa3e4e1bd82b81f2d828b4a218958d1138',
    'archive':'artifacts/phase3d/historical-launcher-59866911/launcher.py',
    'proof':'artifacts/phase3d/historical-launcher-59866911/source-proof.json',
    'proof_sha256':'f52a297634042578fb3930e7c4ecb7bd79e6063817f51b26a86d573c60d56988'}


def _load_native_config_for_audit(directory):
    """Read a pinned historical configuration without authorizing its execution.

    Normal launcher.load_config still rejects any changed launcher. An offline
    audit may inspect explicitly reviewed older Sol/Opus launchers, provided their
    exact stored model, approval and generated control files remain unchanged.
    This does not rewrite the session, substitute a current hook, or certify
    the native outcome. The rest of the evidence replay is still mandatory.
    """
    from .launcher import load_config, MODEL, EFFORT, APPROVAL
    directory = Path(directory).resolve()
    try:
        return load_config(directory)
    except VisualError as exc:
        if exc.code not in ('launcher_changed', 'model_changed'):
            raise
        config = read_json(directory/'kiro/config.json')
        pinned = {
            _NATIVE_OUTPUT_POLICY['launcher_sha256']: ('claude-opus-5.5', 'max', APPROVAL),
            _NATIVE_OUTPUT_POLICY_STARTUP['launcher_sha256']: ('claude-opus-5.5', 'max', APPROVAL),
            _NATIVE_OUTPUT_POLICY_DIAGNOSTIC_V3['launcher_sha256']: ('claude-opus-5.5', 'max', APPROVAL),
            _NATIVE_OUTPUT_POLICY_OPUS55_MAX_V1['launcher_sha256']: ('claude-opus-5.5', 'max', APPROVAL),
            _NATIVE_OUTPUT_POLICY_SOL_MAX_V4['launcher_sha256']: ('gpt-5.6-sol', 'max', APPROVAL),
            _NATIVE_BUILTIN_POLICY_227['launcher_sha256']: ('claude-opus-5.5','max',APPROVAL),
        }
        # Reading this exact archived configuration does not recognize new
        # runtime outcomes or authorize execution of a changed launcher.
        old=_HISTORICAL_CONFIG_ONLY_598
        if config.get('launcher_sha256')==old['launcher_sha256']:
            root=Path(__file__).resolve().parents[2]
            try:
                valid=(file_sha256(root/old['archive'])==old['launcher_sha256']
                       and file_sha256(root/old['proof'])==old['proof_sha256'])
                proof=read_json(root/old['proof'])
                valid=valid and (proof.get('kind')=='HistoricalLauncherConfigurationSourceArchive'
                       and proof.get('status')=='pass' and proof.get('archive_sha256')==old['launcher_sha256']
                       and proof.get('runtime_output_policy_rebound') is False and proof.get('native_canary') is False)
            except (OSError,ValueError,VisualError):valid=False
            if not valid:raise VisualError('historical_launcher_unbound','Historical configuration archive or proof differs.')
            pinned[old['launcher_sha256']]=('claude-opus-5.5','max',APPROVAL)
        expected = pinned.get(config.get('launcher_sha256'))
        if expected is None:
            raise
        if (config.get('model'), config.get('effort'), config.get('approval_mode')) != expected:
            raise VisualError('model_changed', 'Historical audit model or approval configuration differs')
        if Path(config.get('session_dir', '')).resolve() != directory:
            raise VisualError('config_changed', 'Historical audit configuration belongs to another session')
        # Audit never connects to the old service, but its endpoint binding and
        # guard source must still be the exact files prepared for this trial.
        execution = config.get('analysis_execution', {'backend':'local'})
        if execution['backend'] == 'remote' and file_sha256(execution['config_path']) != execution['config_sha256']:
            raise VisualError('config_changed', 'Historical cloud configuration changed')
        for relative, expected in config.get('guard_sources', {}).items():
            if relative != 'guard_diagnostics.py' or file_sha256(Path(__file__).with_name(relative)) != expected:
                raise VisualError('guard_source_changed', 'Historical guard source changed')
        controls = Path(config['target_dir'])/'.kiro'
        hashes = config.get('generated_hashes')
        if not isinstance(hashes, dict) or not hashes:
            raise VisualError('config_changed', 'Historical controls are not bound')
        for relative, expected in hashes.items():
            path = controls/relative
            if not path.resolve().is_relative_to(controls.resolve()) or file_sha256(path) != expected:
                raise VisualError('config_changed', 'Historical generated control changed')
        return config


def audit_native_run(directory):
    """Replay native transport evidence; summaries alone never establish a run."""
    from .launcher import MODEL, EFFORT, APPROVAL
    from kiroclops.integration import kiro as native
    directory=Path(directory).resolve();config=_load_native_config_for_audit(directory)
    launch=read_json(directory/'kiro/launch.json')
    expected_model=config.get('model',MODEL);expected_effort=config.get('effort',EFFORT)
    expected_approval=config.get('approval_mode',APPROVAL)
    def need(value,message):
        if not value:raise VisualError('invalid_native_evidence',message)
    need(_recorded_native_turn_completed(launch) and launch.get('prompt_sent') is True and
         (launch.get('model'),launch.get('effort'),launch.get('approval_mode'))==(expected_model,expected_effort,expected_approval),
         'Native launch did not complete with the exact model and approval configuration')
    task_started=float(launch.get('prompt_started_at',launch['started_at']))
    task_finished=float(launch.get('response_received_at',launch['finished_at']))
    from .execution_policy import policy_from_state
    execution_policy=policy_from_state(read_json(directory/'session.json'))
    need(float(launch['started_at'])<=task_started<=task_finished<=float(launch['finished_at']) and
         task_finished-task_started<=execution_policy['max_seconds'],'Native task exceeded its frozen authoring time budget')
    def stream(name):
        path=directory/'kiro/run'/name
        need(path.stat().st_size<=32*1024*1024,'Native log exceeds bound')
        values=[json.loads(line) for line in path.read_text('utf-8').splitlines() if line.strip()]
        need(values and all(isinstance(v,dict) for v in values),'Native log missing')
        return values
    inbound,outbound=stream('stdout.jsonl'),stream('outbound.jsonl')
    need(len(outbound)%2==0,'Incomplete native write-ahead receipt')
    messages=[];previous=float(launch['started_at'])
    for index in range(0,len(outbound),2):
        attempt,sent=outbound[index:index+2]
        canonical=hashlib.sha256(json.dumps(attempt.get('message'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        need(attempt.get('phase')=='attempt' and sent.get('phase')=='sent' and attempt.get('sequence')==sent.get('sequence')==index//2+1
             and attempt.get('payload_sha256')==sent.get('payload_sha256')==canonical and 'message' not in sent,'Native send receipt changed')
        a=datetime.fromisoformat(attempt['at'].replace('Z','+00:00')).timestamp();b=datetime.fromisoformat(sent['at'].replace('Z','+00:00')).timestamp()
        need(previous<=a<=b<=float(launch['finished_at']),'Native send chronology differs');previous=b
        messages.append(attempt['message'])
    creates=[m for m in messages if m.get('method')=='session/new'];prompts=[m for m in messages if m.get('method')=='session/prompt']
    need(len(creates)==len(prompts)==1,'Expected one fresh native session and exactly one task prompt')
    prompt=Path(config['prompt_path']).read_text('utf-8');sid=launch.get('preflight',{}).get('session_id')
    need(sid and Path(creates[0]['params']['cwd']).resolve()==Path(config['target_dir']).resolve() and
         prompts[0].get('params')=={'sessionId':sid,'prompt':[{'type':'text','text':prompt}]},'Actual prompt or workspace differs from prepared task')
    responses=lambda m:[r for r in inbound if 'method' not in r and 'id' in r and type(r['id']) is type(m.get('id')) and r['id']==m.get('id')]
    new,final=responses(creates[0]),responses(prompts[0])
    need(len(new)==len(final)==1 and new[0].get('result',{}).get('sessionId')==sid and
         final[0].get('result',{}).get('stopReason')=='end_turn' and final[0]['result']==launch.get('response'),'Native session completion is missing')
    stream_replay=_replay_consumed_native_stream(inbound,launch,config,final[0])
    after=False;ids=set();controls={'initialize','session/new','session/set_mode','session/set_config_option','_kiro/hooks/list'}
    for message in messages:
        need(message.get('jsonrpc')=='2.0','Malformed native message')
        method=message.get('method')
        if method:
            identity=(type(message.get('id')).__name__,str(message.get('id')))
            need('id' in message and identity not in ids,'Duplicate native request');ids.add(identity)
            if method=='session/prompt':after=True
            else:need(method in controls and not after,'Unexpected request or post-task coaching')
        else:
            requests=[r for r in inbound if 'method' in r and r.get('id')==message.get('id')]
            need(len(requests)==1,'Unbound client response')
            if requests[0]['method']=='session/request_permission':
                opts=requests[0].get('params',{}).get('options',[])
                choice=next((o for o in opts if isinstance(o,dict) and o.get('kind')=='allow_always'
                             and isinstance(o.get('optionId'),str) and o['optionId']),None) if isinstance(opts,list) else None
                if not choice:
                    raise VisualError('native_approval_unavailable','Native Always Allow was unavailable; no approval fallback is accepted')
                result=message.get('result',{});outcome=result.get('outcome',{})
                if outcome.get('outcome')=='selected':
                    need(outcome.get('optionId')==choice['optionId'],'Native Always Allow was not selected')
                    need(result.get('_meta')=={'kiro':{'consent':{'scope':'session'}}},'Native consent scope differs')
                else:need(outcome=={'outcome':'cancelled'},'Malformed permission response')
    files=['kiro/launch.json','kiro/config.json','kiro/run/stdout.jsonl','kiro/run/outbound.jsonl','kiro/tool-audit.jsonl']
    audit_path=directory/'kiro/tool-audit.jsonl'
    need(audit_path.is_file(),'Tool boundary audit missing')
    need(audit_path.stat().st_size<=32*1024*1024,'Tool boundary audit exceeds bound')
    try:audit_text=audit_path.read_text('utf-8')
    except UnicodeError as exc:
        raise VisualError('invalid_native_evidence','Tool boundary audit is not UTF-8') from exc
    need(not audit_text or audit_text.endswith('\n'),'Tool boundary audit ends with an incomplete row')
    decisions=[]
    for line_number,line in enumerate(audit_text.splitlines(),1):
        try:decision=json.loads(line)
        except ValueError as exc:
            raise VisualError('invalid_native_evidence',f'Tool boundary audit has invalid JSON on line {line_number}') from exc
        need(isinstance(decision,dict),f'Tool boundary audit has a non-object row on line {line_number}')
        decisions.append(decision)
    outcomes=audit_native_tool_outcomes(inbound,decisions,session_id=sid,
        started_at=float(launch['started_at']),finished_at=float(launch['finished_at']),scope_sha256=digest(config.get('generated_hashes',{})),
        runtime_context={'cli_version':launch.get('cli_version'),'kas_version':launch.get('kas_version'),
            'launcher_sha256':config.get('launcher_sha256'),
            'target_dir':config.get('target_dir'),
            'hook_id':str(Path(config['target_dir'])/'.kiro/hooks/kiroclops-guard.json')+'#hook-0'})
    need(outcomes['status']=='pass','Native tool outcomes do not establish the required boundary: '+
         ', '.join(sorted({e['code'] for e in outcomes['errors']})))
    postturn=_audit_postturn_finalization(directory,launch)
    return {'status':'pass','model':expected_model,'effort':expected_effort,'prompt_count':1,'native_session_id':sid,'stream_replay':stream_replay,'tool_outcomes':outcomes,
        'postturn_finalization':postturn,
        'evidence_hashes':{name:file_sha256(directory/name) for name in files},
        'scope':'Recorded native CLI transport and operator-owned evidence; not an OS sandbox or third-party attestation.'}


def validate_source_review(path,directory,*,candidate_sha256):
    from .session import source_hashes
    directory=Path(directory);receipt=read_json(path);state=read_json(directory/'session.json')
    native=audit_native_run(directory)
    selected=state['selected_candidate_id'];current=source_hashes(directory/'candidates'/selected/'source')
    required={'actual_code_authorship','no_coordinate_replay','no_private_reference_access','no_operator_coaching','protected_behavior_preserved'}
    checks=receipt.get('checks',[])
    if (receipt.get('kind')!='IndependentSourceReview' or receipt.get('status')!='pass' or not receipt.get('reviewer')
        or receipt.get('method')!='independent_source_and_native_trace_audit' or receipt.get('candidate_sha256')!=candidate_sha256
        or digest(current)!=candidate_sha256 or receipt.get('source_hashes')!=current
        or receipt.get('baseline_hashes')!=state['baseline_hashes'] or receipt.get('native_evidence_hashes')!=native['evidence_hashes']
        or {c.get('id') for c in checks}!=required or len(checks)!=len(required)
        or not all(c.get('verdict')=='pass' and isinstance(c.get('evidence'),str) and c['evidence'].strip() for c in checks)):
        raise VisualError('incomplete_review','Independent source and native trace audit is incomplete or stale')
    return {**receipt,'native_validation':native}


REVIEW_HTML=r'''<!doctype html><html lang="en"><meta charset="utf-8"><title>KiroClops · independent image controls</title><style>body{font:16px system-ui;background:#101826;color:#e3eefb;margin:24px}canvas{max-width:100%;border:1px solid #7187a5;image-rendering:pixelated;cursor:crosshair}button,input{font:inherit;padding:8px;margin:6px}p{max-width:900px}</style><h1>Independent image controls</h1><p>Click the same physical anchor of the named object. The image contains no predicted tracking overlay. Review original pixels before moving on. This records position controls; it does not approve event timing or application integrity.</p><input id="reviewer" placeholder="Reviewer name"><span id="status"></span><p><button id="previous">Previous</button><button id="next">Next</button><button id="save">Save independent marks</button></p><canvas id="image"></canvas><script>
const R=__DATA__;let index=0,marks={};const canvas=document.getElementById('image'),ctx=canvas.getContext('2d'),picture=new Image();function key(t){return t.record_id+':'+t.object_id+':'+t.index}function show(){let t=R.tasks[index];document.getElementById('status').textContent=`${index+1}/${R.tasks.length} · ${t.role} / ${t.record_id} / ${t.object_id} · frame ${t.index} · ${t.time_s.toFixed(3)}s`;picture.onload=()=>{canvas.width=picture.width;canvas.height=picture.height;ctx.drawImage(picture,0,0);let m=marks[key(t)];if(m){ctx.strokeStyle='#ff4867';ctx.beginPath();ctx.arc(m.x,m.y,4,0,2*Math.PI);ctx.stroke()}};picture.src=t.image}canvas.onclick=e=>{let b=canvas.getBoundingClientRect(),t=R.tasks[index];marks[key(t)]={record_id:t.record_id,package_sha256:t.package_sha256,object_id:t.object_id,index:t.index,image_sha256:t.image_sha256,x:(e.clientX-b.left)*canvas.width/b.width,y:(e.clientY-b.top)*canvas.height/b.height};show()};document.getElementById('previous').onclick=()=>{index=Math.max(0,index-1);show()};document.getElementById('next').onclick=()=>{index=Math.min(R.tasks.length-1,index+1);show()};document.getElementById('save').onclick=()=>{let reviewer=document.getElementById('reviewer').value.trim();if(!reviewer){alert('Record the independent reviewer.');return}let data={schema_version:2,request_sha256:R.request_sha256,package_sha256:R.package_sha256,reviewer,method:'independent_raw_image_marking',points:Object.values(marks)},a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download='independent-marks.json';a.click()};show();</script></html>'''
