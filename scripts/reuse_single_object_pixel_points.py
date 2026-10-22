"""Reuse signed positions across packages only for identical single-object pixels.

This does not reuse events, source reviews, candidate acceptance or timestamps.
The old package-inclusive inspection attestation remains mandatory and is replayed
unchanged. Only the new pixel lookup omits package identity; it adds the exact
template frame and image dimensions. Multi-object/lifecycle selections are out
of scope. Real use must wait until all timed capture work has ended.
"""
from pathlib import Path
import argparse
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256,safe_path,VisualError
from kiroclops.local_engine.review import evaluate_review


def need(ok,message):
    if not ok:raise VisualError('pixel_reuse_rejected',message)


def helper():
    path=ROOT/'artifacts/phase3a/tools/reuse_local_final_points.py'
    spec=importlib.util.spec_from_file_location('bound_prior_pixel_review',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def pixel_key(identity,record,selection,seed_sha):
    """Pure lookup key; callers must first verify both raw/seed bytes and bindings."""
    objects=selection.get('objects',[])
    need(len(objects)==1 and objects[0].get('method')=='template',
        'Only one explicitly selected template object is supported')
    need(not selection.get('lifecycle_bindings') and not selection.get('connections'),
        'Lifecycle and multi-object identity are not transferable here')
    obj=objects[0]
    need(obj.get('id')==identity[2] and digest(obj)==identity[3] and digest(selection)==identity[4],
        'Object or complete selection differs from verified identity')
    need(record['role']==identity[0] and record['package_sha256']==identity[1],
        'Original role/package binding differs')
    need(len(seed_sha)==64 and len(identity[5])==64,'Missing full pixel digest')
    size=record.get('image_size')
    need(isinstance(size,list) and len(size)==2 and all(type(v) is int and v>0 for v in size),
        'Invalid image dimensions')
    # Both current image and initial template image must match. Input clocks and
    # source hashes may differ, but no time/event/physics inference is transferred.
    return (identity[0],identity[2],identity[3],identity[4],identity[5],tuple(size),seed_sha)


def verified_key(task,records,directory,files,h):
    original=h.task_identity(task,records,directory,files)
    record,objects,selection,manifest=records[task['record_id']]
    obj=objects[task['object_id']];index=obj.get('frame_index')
    need(type(index) is int and 0<=index<len(manifest['frames']),'Invalid template frame')
    frame=manifest['frames'][index];need(frame['index']==index,'Template frame index differs')
    package=safe_path(directory,record['package'],must_exist=False)
    image=safe_path(package,frame['path']);sha=file_sha256(image)
    need(sha==frame['sha256'],'Template frame bytes changed')
    files[str(image)]=sha
    return original,pixel_key(original,record,selection,sha)


def reuse(prior,current,marks,inspection,output):
    prior,current,marks,inspection,output=map(lambda p:Path(p).resolve(),(prior,current,marks,inspection,output))
    need(prior!=current and not output.exists(),'Use different requests and fresh output')
    for p in (prior,current):need(not output.is_relative_to(p),'Do not modify review groups')
    # Keep this explicit boundary separate from individual candidate finalization.
    progress=read_json(ROOT/'artifacts/phase3a/completion-paired-cohort-01/progress.json')
    need(len(progress['rows'])==18 and all(r['status'] in ('completed','failed','interrupted')
        for r in progress['rows']),'Wait until all registered trials are terminal')
    queued=read_json(ROOT/'artifacts/phase3a/completion-postcohort-recorder-diagnostics-01/pipeline.json')
    need(queued['status']=='diagnostics_finished_interpretation_required','Wait for recorder probes to finish')
    bound=helper();h=bound._helper()
    old,old_records,files=h.load_group(prior);new,new_records,new_files=h.load_group(current);files.update(new_files)
    annotation=read_json(marks)
    need(annotation.get('status')!='unsigned' and annotation.get('request_sha256')==digest(old),'Unsigned or unrelated old marks')
    checked_old=evaluate_review(prior,annotation,persist=False);need(checked_old['status']=='pass','Old positions did not pass')
    files[str(marks)]=file_sha256(marks)
    for path in (Path(__file__),Path(bound.__file__),Path(h.__file__),ROOT/'kiroclops/local_engine/review.py'):
        files[str(path.resolve())]=file_sha256(path)
    tasks={(t['record_id'],t['object_id'],t['index']):t for t in old['tasks']}
    need(len(tasks)==len(old['tasks']),'Duplicate old task')
    identities,pool={},{}
    for point in annotation['points']:
        index=(point['record_id'],point['object_id'],point['index']);need(index in tasks,'Unknown old point')
        identity,key=verified_key(tasks[index],old_records,prior,files,h);identities[index]=identity
        if key in pool:need([pool[key][0]['x'],pool[key][0]['y']]==[point['x'],point['y']],
            'Conflicting positions for identical raw pixels and anchor')
        pool[key]=(point,identity)
    # This verifies the original package-inclusive identity; it is not replaced
    # by the weaker cross-package lookup key or by a PASS badge.
    replay=bound.validate_inspection(inspection,prior,old,annotation,marks,tasks,identities,files)
    points,missing,mapping=[],[],[]
    for task in new['tasks']:
        identity,key=verified_key(task,new_records,current,files,h);match=pool.get(key)
        if match is None:missing.append(task);continue
        point,old_identity=match
        copy={k:task[k] for k in ('record_id','object_id','index','image_sha256')}
        copy.update(x=point['x'],y=point['y'],evidence=f'Identical raw and template PNGs; prior signed position {marks}; actual inspection {inspection}')
        points.append(copy);mapping.append({'new':[task['record_id'],task['object_id'],task['index']],
            'prior':[point['record_id'],point['object_id'],point['index']],
            'old_identity_sha256':digest(old_identity),'new_identity_sha256':digest(identity),
            'pixel_lookup_sha256':digest(key),'old_package_sha256':old_identity[1],
            'new_package_sha256':identity[1],'coordinates_unchanged':True})
    result={'request_sha256':digest(new),'reviewer':'Exact single-object pixel reuse of '+annotation['reviewer'],
        'method':'independent_raw_image_marking','status':'partial' if missing else 'complete_exact_pixel_reuse',
        'fresh_visual_review':False,'points':points,'reuse':{'prior_marks_sha256':file_sha256(marks),
            'prior_inspection_sha256':file_sha256(inspection),'prior_review_sha256':digest(checked_old),
            'inspection_binding':replay,'event_or_source_judgments_reused':False,
            'match_policy':'Same role/object/full selection, image dimensions, exact raw PNG and exact initial template PNG. Package identity may differ; time and event evidence are not transferred.'}}
    checked=evaluate_review(current,result,persist=False)
    need(all(Path(p).is_file() and file_sha256(p)==sha for p,sha in files.items()),'Evidence changed during reuse')
    write_json(output/'annotation.json',result);write_json(output/'mapping.json',mapping)
    write_json(output/'missing-tasks.json',missing);write_json(output/'prior-position-replay.json',checked_old)
    write_json(output/'current-position-replay.json',checked)
    summary={'status':checked['status'],'matched':len(points),'missing':len(missing),'evidence_hashes':files,
        'inspection_binding':replay,'fresh_visual_review':False,'events_and_sources':'not_reused',
        'candidate_or_motion_acceptance_granted':False}
    write_json(output/'result.json',summary)
    return {k:v for k,v in summary.items() if k!='evidence_hashes'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('prior','current','marks','inspection','output'):p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();print(reuse(a.prior,a.current,a.marks,a.inspection,a.output))
