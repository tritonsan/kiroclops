"""Native qualification for a NEW seeded air contract, not old trial acceptance."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from kiroclops.integration import airborne_regression_v1 as air
from kiroclops.integration.autonomous_workflow_v3 import verify_protocol
from kiroclops.integration.storage import project_hashes,read_json,sha256,write_json


def qualify(output):
    output=Path(output).resolve()
    if output.parent!=ROOT/'artifacts/model-trials/preflight' or output.exists():raise ValueError('New preflight output required')
    session=ROOT/'sessions/cli-autonomous-v3-01'
    state=read_json(session/'session.json');launch=read_json(session/'kiro/launch.json')
    if state['status']!='finished' or state.get('active_job_id') is not None or launch['status']!='completed':
        raise ValueError('Historical model must be completed and all jobs terminal')
    policy=air.protocol_record(); old=verify_protocol(session)
    sources={'baseline_a':session/'baseline','baseline_b':session/'baseline',
             'preserved_candidate01':session/'candidates/candidate-01/project',
             'extra_update_candidate02':session/'candidates/candidate-02/project'}
    expected={'baseline_a':state['baseline_hashes'],'baseline_b':state['baseline_hashes'],
              'preserved_candidate01':read_json(session/'candidates/candidate-01/candidate.json')['source_hashes'],
              'extra_update_candidate02':read_json(session/'candidates/candidate-02/candidate.json')['source_hashes']}
    if any(project_hashes(source)!=expected[name] for name,source in sources.items()):raise ValueError('Historical immutable source differs')
    output.mkdir(); started=time.perf_counter()
    result={'status':'running','protocol':policy,'started_at':datetime.now(timezone.utc).isoformat(),
            'script_sha256':sha256(Path(__file__)),'old_protocol_source_hashes':old['source_hashes'],
            'model_acceptance':False,'suites':{},'comparisons':{}}
    write_json(output/'result.json',result)
    try:
        for name,source in sources.items():
            print(json.dumps({'stage':'suite_start','name':name}),flush=True)
            start=time.perf_counter()
            suite=air.run_suite(source,output/name,expected_source_hashes=expected[name],asset_cache=old['asset_cache'])
            checked=air.verify_suite(suite,expected_source_hashes=expected[name])
            if checked['status']!='pass':raise ValueError('Native suite did not validate: '+name+' '+str(checked))
            result['suites'][name]={'output':str(suite),'suite_sha256':checked['suite_sha256'],
                'source_hashes':expected[name],'full_call_seconds':time.perf_counter()-start,
                'timing_includes':'copy, exacttextureseed, normalimport, freshprivate240framecapture, evidence and full verification'}
            write_json(output/'result.json',result)
            print(json.dumps({'stage':'suite_complete','name':name,'full_call_seconds':result['suites'][name]['full_call_seconds']}),flush=True)
        a=result['suites']['baseline_a']['output']
        result['comparison_timing_seconds']={}
        for label,other in [('self','baseline_b'),('ground_only','preserved_candidate01'),('extra_air_update','extra_update_candidate02')]:
            t=time.perf_counter()
            result['comparisons'][label]=air.compare_suites(a,result['suites'][other]['output'])
            result['comparison_timing_seconds'][label]=time.perf_counter()-t
        expected_status={'self':'pass','ground_only':'pass','extra_air_update':'fail'}
        checks={name:value['status']==expected_status[name] for name,value in result['comparisons'].items()}
        negative=result['comparisons']['extra_air_update']['mismatches']
        checks['decisive_idle_left_right']={'idle-rising-left','idle-rising-right'}<={r['case_id'] for r in negative if r['step_index']==0}
        checks['old_protocol_unchanged']=verify_protocol(session)['source_hashes']==old['source_hashes']
        checks['harness_unchanged']=air.protocol_record()==policy and sha256(Path(__file__))==result['script_sha256']
        result['checks']=checks;result['status']='pass' if all(checks.values()) else 'fail'
        result['limits']=['Seeded controller contract, not a normal-map reachability demonstration.',
            'Eighteen fresh actors in one private process per suite; canonical motion fresh-process captures unchanged.',
            'Positive baseline-self and prior air-preserved candidate; negative known extra-air-update candidate. No current-source edits.',
            'Only horizontalfloatbit digests and predicates exported; no engine kinematics supplied to image fitters.',
            'One suite per source except two independent baseline copies; timing observations are not repeated performance guarantees.']
        if result['status']=='pass':
            result['finished_at']=datetime.now(timezone.utc).isoformat();result['elapsed_seconds']=time.perf_counter()-started
            air._write_new(output/'qualification.json',result)
            replay=air.verify_qualification(output)
            if replay['status']!='pass':raise ValueError('Final native qualification replay failed: '+str(replay))
            result['qualification_sha256']=replay['sha256']
    except BaseException as error:
        result.update(status='error',error={'type':type(error).__name__,'message':str(error)})
        raise
    finally:
        result.update(finished_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.perf_counter()-started)
        write_json(output/'result.json',result)
    print(json.dumps({'stage':'complete','status':result['status'],'comparisons':{k:v['status'] for k,v in result['comparisons'].items()},'elapsed_seconds':result['elapsed_seconds']}),flush=True)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=qualify(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
