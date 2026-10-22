"""Reference-only prospective repeatability qualification; never edits a model."""
from pathlib import Path
from copy import deepcopy
import argparse, sys, time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,digest,file_sha256
from kiroclops.visual.package import load_package
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.session import source_hashes,copy_project,_lock
from kiroclops.local_engine.evaluation import _capture_web
from kiroclops.local_engine.media_session import _compare,LIMITS


def run(output,policy,driver='controlled_begin_frame_v1'):
    root=Path(__file__).resolve().parents[1];output=Path(output).resolve()
    if output.exists():raise ValueError('Fresh output required; keep every failed run')
    state=read_json(root/'artifacts/phase3a/panel-opus55-media-05/session.json')
    protocol=read_json(root/'artifacts/phase3a/panel-feedback-validation-01/protocol.json')
    source=root/'artifacts/phase3a/panel-feedback-validation-01/sources/reference'
    hashes=source_hashes(source)
    scenario=deepcopy(next(s for s in protocol['heldout'] if s['recipe']=='reverse' and s['viewport'][0]==1280))
    scenario.update(id='reference-release-qualification',split='qualification',click_schedule=policy,frame_driver=driver)
    binding=read_json(Path(state['reference'])/'capture-binding.json');binding['scenario']=scenario
    output.mkdir(parents=True)
    write_json(output/'protocol.json',{'created_at':time.time(),'scenario':scenario,'repeats':4,
        'comparison_pairs':[[0,1],[0,2],[0,3],[1,2],[1,3],[2,3]],'limits':LIMITS,
        'source_hashes':hashes,'candidate_used':False,'retries':0,'legacy_evidence_rewritten':False,
        'capture_sha256':file_sha256(root/'kiroclops/web_local/capture.py')})
    copy_project(source,output/'source');configure_environment();records=[];errors=[]
    def check():
        if source_hashes(output/'source')!=hashes:raise ValueError('Source changed')
    for i in range(4):
        print('reference',i+1,flush=True)
        try:
            with _lock(Path(state['capture_lock']),timeout=30):
                record=_capture_web(output/'source',output/'captures'/str(i+1),scenario,
                    check,lambda *args:None,lambda:False,'reference')
            write_json(output/'records'/f'{i+1}.json',record);records.append(record)
        except Exception as exc:
            errors.append({'repeat':i+1,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)});records.append(None)
        write_json(output/'progress.json',{'completed':i+1,'errors':errors})
    pairs=[]
    for a in range(4):
        for b in range(a+1,4):
            if records[a] is None or records[b] is None:continue
            result=_compare(records[a],records[a],records[b],binding,require_improvement=False)
            pairs.append({'pair':[a+1,b+1],'pass':result['pass'],'comparison':result})
    timing=[]
    for i,r in enumerate(records):
        if r:
            inputs=load_package(r['package'])['inputs']['events']
            actual=[x['time_s'] for x in inputs]
            timing.append({'repeat':i+1,'actual_inputs_s':actual,
                'intervals_s':[y-x for x,y in zip(actual,actual[1:])],
                'lateness_s':[x['time_s']-x['planned_time_s'] for x in inputs],
                'functional_pass':r['functional']['status']=='pass'})
    result={'status':'pass' if not errors and len(pairs)==6 and all(p['pass'] for p in pairs)
        and all(t['functional_pass'] for t in timing) else 'fail','pairs':pairs,'errors':errors,
        'input_timing':timing,'source_sha256':digest(hashes),'limits_changed':False,'scope':'reference_only'}
    write_json(output/'result.json',result);print({'status':result['status'],'passed':sum(p['pass'] for p in pairs),'total':6},flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--policy',choices=['dispatch_at_due_v1','release_at_due_v1','prepared_hover_v2'],default='release_at_due_v1')
    p.add_argument('--driver',choices=['controlled_begin_frame_v1','natural_browser_v1','natural_browser_raf_v1'],default='controlled_begin_frame_v1')
    args=p.parse_args();run(args.output,args.policy,args.driver)
