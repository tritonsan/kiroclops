"""Re-measure a sealed Kiro candidate after recorder qualification, without edits.

This is a new diagnostic calibration, not a replacement of historical final or
held-out verdicts. No model runs or solution patches are issued by this script.
"""
from pathlib import Path
from copy import deepcopy
import argparse,html,json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.evaluation import _capture_web
from kiroclops.local_engine.media_session import _compare,LIMITS
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.review import audit_native_run


def run(qualification,output):
    root=Path(__file__).resolve().parents[1];qualification=Path(qualification).resolve();output=Path(output).resolve()
    gate=read_json(qualification/'result.json');qp=read_json(qualification/'protocol.json')
    if gate['status']!='pass' or gate['passed']!=7 or qp['limits']!=LIMITS:
        raise ValueError('Full reference qualification is required before candidate reassessment')
    if output.exists():raise ValueError('Fresh output required; preserve every old failure')
    adapters={p.name:file_sha256(p) for p in (root/'kiroclops/web_local').glob('*.py')}
    if adapters!=qp['adapter_hashes']:raise ValueError('Recorder changed after qualification')
    session=root/'artifacts/phase3a/panel-opus55-media-05';state=read_json(session/'session.json')
    assert state['status']=='finalized' and not state['active_job_id']
    selected=next(c for c in state['candidates'] if c['candidate_id']==state['selected_candidate_id'])
    candidate=session/'candidates'/selected['candidate_id']/'source';baseline=Path(state['baseline'])
    assert digest(source_hashes(candidate))==selected['source_sha256']
    assert source_hashes(baseline)==state['baseline_hashes']
    native=audit_native_run(session)
    if native['status']!='pass':raise ValueError('Kiro native trace must pass')
    scenario=deepcopy(qp['scenarios'][0]);sid=scenario['id']
    reference=read_json(qualification/'records'/sid/'1.json')
    if reference['source_sha256']!=digest(qp['source_hashes']):raise ValueError('Wrong reference source')
    snapshot={p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}
    output.mkdir(parents=True)
    write_json(output/'protocol.json',{'kind':'SealedCandidateRecorderReassessment','created_at':time.time(),
        'qualification_sha256':file_sha256(qualification/'result.json'),'scenario':scenario,'limits':LIMITS,
        'candidate_source_sha256':selected['source_sha256'],'adapter_hashes':adapters,'model_started':False,
        'candidate_edited':False,'retries':0,'historical_verdicts_replaced':False,'heldout_claim':False})
    write_json(output/'original-files.json',snapshot)
    records={'reference':reference};errors=[];configure_environment()
    for role,source in (('before',baseline),('candidate',candidate)):
        copy_project(source,output/'sources'/role);expected=source_hashes(source)
        def check():
            if source_hashes(output/'sources'/role)!=expected:raise ValueError('Candidate source changed')
            if adapters!={p.name:file_sha256(p) for p in (root/'kiroclops/web_local').glob('*.py')}:
                raise ValueError('Recorder changed')
        try:
            print(role,flush=True)
            with _lock(Path(state['capture_lock']),timeout=30):
                records[role]=_capture_web(output/'sources'/role,output/'captures'/role,scenario,
                    check,lambda *a:None,lambda:False,role,reference_selection=reference['selection'])
            write_json(output/'records'/(role+'.json'),records[role])
        except Exception as exc:errors.append({'role':role,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
    result={'status':'error','errors':errors,'certified_phase_success':False,'model_started':False,
        'historical_verdicts_changed':False,'candidate_source_sha256':selected['source_sha256'],
        'native_audit_status':native['status'],'independent_visual_review':'pending'}
    if not errors:
        binding=read_json(Path(state['reference'])/'capture-binding.json');binding['scenario']=scenario
        comparison=_compare(reference,records['before'],records['candidate'],binding,require_improvement=True)
        functional=all(r['functional']['status']=='pass' for r in records.values())
        result.update(status='pass' if comparison['pass'] and functional else 'fail',comparison=comparison,functional_pass=functional)
    actual={p.relative_to(session).as_posix():file_sha256(p) for p in session.rglob('*') if p.is_file()}
    assert actual==snapshot,'Original session changed'
    result['original_files_unchanged']=len(actual);write_json(output/'result.json',result)
    (output/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>KiroClops · After the recording fix</title>'
        '<style>body{font:16px/1.6 system-ui;max-width:1100px;margin:40px auto;padding:20px;background:#101827;color:#e4eafa}pre{white-space:pre-wrap}a{color:#8ecbff}</style>'
        '<h1>Measurement of the sealed Kiro code with the new recording</h1><p>candidate-02, written earlier by Opus 5.5/max, was not changed. There is no new model run or operator patch. This additional calibration does not change the old results; it is not phase acceptance or held-out validation acceptance.</p>'
        f'<h2>{html.escape(result["status"].upper())}</h2><p><a href="result.json">Full measurement</a> · <a href="protocol.json">Pre-registered protocol</a></p><pre>'
        +html.escape(json.dumps({k:v for k,v in result.items() if k!='comparison'},indent=2,ensure_ascii=False))+'</pre>',encoding='utf8')
    print({'status':result['status'],'source_unchanged':True,'old_files_preserved':len(actual)},flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--qualification',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.qualification,args.output)
