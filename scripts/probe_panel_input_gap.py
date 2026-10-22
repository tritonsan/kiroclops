"""Prospective recorder-only diagnosis; no model run or acceptance substitution."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.backend import _lock
from kiroclops.web_local import natural_capture
from kiroclops.web_local.specs import capture_scenarios,fixture_project,QUALIFIED_CAPTURE_POLICY
from kiroclops.web_local.capture import capture_run
from kiroclops.local_engine.evaluation import _measure_record
from kiroclops.visual.schema import write_json,digest,file_sha256

def run(output):
    output=Path(output).resolve();output.mkdir(exist_ok=False);configure_environment()
    before=dict(natural_capture.POLICY)
    natural_capture.POLICY.update(final_input_image_check_lead_s=.010,oldest_final_input_image_before_due_s=.020)
    write_json(output/'registration.json',{'kind':'RecorderDiagnostic','old_policy':before,'prospective_policy':natural_capture.POLICY,
        'helper_sha256':file_sha256(__file__),'source_files_modified':False,'model_started':False,'thresholds_changed':False})
    records=[]
    with _lock(ROOT/'artifacts/local-engine/capture.lock',timeout=10):
        for i in (0,1,2,3):
            scenario=capture_scenarios('panel','validation',QUALIFIED_CAPTURE_POLICY)[i]
            dest=output/scenario['id'];print('capture',scenario['id'],flush=True)
            try:
                result=capture_run(fixture_project('panel','reference'),dest,scenario,variant='engineering_input_gap_probe')
                records.append({'scenario':scenario,'package':result['package_dir'],'timing':result['timing'],'functional':result['functional'],'source_sha256':digest(result['source_hashes'])})
            except Exception as exc:
                write_json(output/'capture-failure.json',{'code':getattr(exc,'code',type(exc).__name__),'message':str(exc),'scenario':scenario['id']});raise
    write_json(output/'captures.json',records);summaries=[]
    for rec in records:
        measured=_measure_record(rec,output/'analysis'/rec['scenario']['id'],lambda:None,lambda m:None)
        events=measured['events'];unresolved=[x for x in events['events']+events['relations'] if x.get('timing_status','resolved')!='resolved']
        item={'scenario':rec['scenario']['id'],'timing':rec['timing']['status'],'functional':rec['functional']['status'],
            'unresolved_timing':unresolved,'measurement_status':measured['measurement_status']}
        summaries.append(item);print(item['scenario'],item['timing'],'unresolved',len(unresolved),flush=True)
    write_json(output/'result.json',{'status':'pass' if all(x['timing']=='pass' and not x['unresolved_timing'] for x in summaries) else 'fail','records':summaries,'scope':'Diagnostic, not qualification or Kiro behavior success'})
if __name__=='__main__':run(sys.argv[1])
