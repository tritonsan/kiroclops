"""Bind preregistered initial calibration pixels before a Kiro prompt."""
import argparse
from pathlib import Path
import json
import os

from kiroclops.local_engine.backend import _lock
from kiroclops.local_engine.media_suite import load_suite,record_from_suite,create_suite,create_suite_session
from kiroclops.local_engine.reliability import prepare_runtime
from kiroclops.visual.schema import VisualError,read_json,write_json,digest


def run(previous,identifier,root=Path('C:/kc3d')):
    previous=Path(previous);state=read_json(previous/'session.json')
    if state['started_at'] is not None or state['candidates']:
        raise VisualError('session_started','Initial evidence cannot be added after model work.')
    # Read-only preparation revision: it cannot launch a model or certify
    # behavior. The new executable revision freezes current analysis sources.
    suite=load_suite(state['reference'],verify_analysis_code=False);old_ready=read_json(previous/'environment-readiness.json')
    runtime=prepare_runtime(root);os.environ.update(runtime['environment']);os.environ['KIROCLOPS_ANALYSIS_BACKEND']='local'
    output=root/'p'/identifier
    if output.exists():raise VisualError('output_exists','Keep prior baseline captures unchanged.')
    output.mkdir(parents=True)
    initial=[]
    def progress(phase,completed=0,total=1):
        print(phase,flush=True);write_json(output/'progress.json',{'phase':phase,'completed':completed,'total':total,'model_started':False})
    with _lock(Path(runtime['capture_lock']),timeout=60):
        for case in (c for c in suite['cases'] if c['role']=='calibration'):
            reference=record_from_suite(state['reference'],case)
            for role,project in (('before',state['baseline']),('original',Path(state['reference'])/suite['original'])):
                progress(role+' '+case['case_id'])
                if case['stage'] in ('jump','ground'):
                    from kiroclops.local_engine.suite_game import capture_target
                    record=capture_target(project,case['scenario'],output/role/case['case_id'],progress=progress,variant='before')
                else:
                    from kiroclops.local_engine.suite_web import capture_record
                    record=capture_record(project,output/role/case['case_id'],case['scenario'],lambda:None,progress,None,'before',
                                          reference_selection=reference['selection'])
                if record.get('measurement_status')!='ok':raise VisualError('tracking_failure','Initial calibration does not qualify.')
                write_json(output/role/case['case_id']/'record.json',record)
                initial.append({'case_id':case['case_id'],'role':role,'record':record})
        new_suite=create_suite(output/'suite',domain=suite['domain'],stage=suite['stage'],
            records=[{**c,'record':record_from_suite(state['reference'],c)} for c in suite['cases']],
            required_cases=suite['required_cases'],original=Path(state['reference'])/suite['original'],
            authoring_parent=state['baseline'],goal=suite['goal'],event_contract=suite['event_contract'],
            preservation=suite['preservation_contract'],initial_records=initial)
        session=Path(runtime['session_root'])/identifier
        create_suite_session(session,output/'suite',state['baseline'],runtime=runtime)
        write_json(session/'environment-readiness.json',{**old_ready,'suite_sha256':digest(new_suite),
            'initial_record_count':len(initial),'reference_revision':str(previous),'status':'pending_raw_review'})
        result={'status':'ready_for_raw_review','session':str(session),'initial_cases':len(initial),
                'model_started':False,'reference_pool_changed':False}
        write_json(output/'result.json',result)
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--previous',required=True,type=Path);p.add_argument('--id',required=True)
    args=p.parse_args()
    try:result=run(args.previous,args.id)
    except (VisualError,OSError,KeyError,ValueError) as exc:
        print(json.dumps({'status':'failed','code':getattr(exc,'code','initial_capture_failed'),'message':str(exc)}));return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
