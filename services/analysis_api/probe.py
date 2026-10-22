"""Real authenticated service smoke test; never launches a Kiro/model run."""
from __future__ import annotations

import argparse
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid

from kiroclops.cloud.remote import RemoteAnalysisBackend
from kiroclops.cloud.equivalence import compare
from kiroclops.cloud.execution import measure_direct
from kiroclops.cloud.identity import engine_identity
from kiroclops.visual.schema import read_json,write_json,VisualError,digest


def configure(root,label):
    state=read_json(root/'state.json')
    account=read_json(root/('account-'+label+'.json'))
    if account.get('agent_config_path'):
        config=read_json(account['agent_config_path'])
        write_json(root/('config-'+label+'.json'),config)
        return config
    token=read_json(account['capability_file'])['access_token']
    import json
    request=Request(state['ApiUrl']+'/service',headers={'Authorization':'Bearer '+token})
    with urlopen(request,timeout=30) as response:descriptor=json.load(response)
    config={**descriptor,'api_url':state['ApiUrl'],'capability_file':account['capability_file']}
    write_json(root/('config-'+label+'.json'),config)
    return config


def run(root,output,resume=None,other_label='other',rest_duration=None):
    root,output=Path(root),Path(output)
    if output.exists():raise ValueError('Use a fresh result directory')
    config=configure(root,'owner');other=configure(root,other_label)
    output.mkdir(parents=True)
    client_root=Path(resume)/'client' if resume else output/'client'
    backend=RemoteAnalysisBackend(client_root,config)
    timings=[]
    original_call=backend._call
    fail_once={'enabled':True}
    def call(method,path,body=None,key=None):
        start=time.monotonic()
        try:
            result=original_call(method,path,body,key)
            if method=='POST' and path=='/jobs' and fail_once['enabled']:
                fail_once['enabled']=False
                raise VisualError('analysis_connection_lost','Injected lost POST acknowledgement after real AWS commit')
            return result
        finally:
            timings.append({'operation':method+' '+path.split('?')[0],'seconds':time.monotonic()-start})
    backend._call=call
    other_backend=RemoteAnalysisBackend(output/'other-client',other)
    corpus=Path('artifacts/phase3b/calibration-corpus-02')
    rows=[]
    engine_identity(4)
    for name in ('game-before-h03','panel-open-w960-r1','svg-three-to-four-w960-r1'):
        selection=read_json(corpus/name/'selection.json');contract=read_json(corpus/name/'contract.json')
        if rest_duration is not None:
            contract={**contract,'event_options':{**contract.get('event_options',{}),'rest_duration_s':rest_duration}}
        request={'package_path':str(corpus/name/'package'),'selection':selection,'contract':contract,'candidate_sha256':'1'*64,'retention_class':'reference'}
        logical=uuid.uuid4().hex
        if resume and name=='game-before-h03':
            saved=list((client_root/'operations').glob('*.json'))
            if len(saved)!=1:raise ValueError('Resume requires exactly one prepared operation')
            logical=read_json(saved[0])['logical_job_id']
        started=time.monotonic();reconnected=False
        try:submitted=backend.submit(request,idempotency_key=logical)
        except VisualError as exc:
            if exc.code!='analysis_connection_lost':raise
            # Fresh client process semantics: use disk ledger, not in-memory
            # state from the failed submit call.
            backend=RemoteAnalysisBackend(client_root,config)
            submitted=backend.submit(request,idempotency_key=logical)
            reconnected=True
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            job=backend.get_job(submitted['job_id'],wait_seconds=1)
            if job['status'] in ('succeeded','failed','interrupted','cancelled'):break
        else:raise RuntimeError('Service test deadline exceeded')
        duration=time.monotonic()-started
        write_json(output/(name+'-job.json'),job)
        if job['status']!='succeeded':raise RuntimeError('Service analysis failed: '+str(job.get('error')))
        expected=measure_direct(corpus/name/'package',selection,contract)
        equivalence=compare(expected,{k:job['result'][k] for k in ('observations','events')})
        repeated=backend.submit(request,idempotency_key=logical)
        try:
            other_backend._call('GET','/jobs/'+submitted['job_id'])
            private=False
        except VisualError as exc:private=exc.code=='cloud_not_found'
        rows.append({'id':name,'job_id':job['job_id'],'status':job['status'],'end_to_end_seconds':duration,
                     'equivalence':equivalence,'reconnected_after_lost_ack':reconnected,
                     'same_job_on_repeat':repeated['job_id']==job['job_id'],'other_tenant_denied':private})
        write_json(output/'partial.json',rows)
        print(name+': '+('pass' if equivalence['equivalent'] and private else 'fail'),flush=True)
    state=read_json(root/'state.json')
    try:
        urlopen(state['ApiUrl']+'/service',timeout=30)
        unauth=False
    except HTTPError as exc:unauth=exc.code in (401,403)
    result={'kind':'AuthenticatedAwsSinglePackageProbe','model_started':False,'external_users':0,'synthetic_accounts':2,
            'status':'pass' if unauth and all(r['equivalence']['equivalent'] and r['same_job_on_repeat'] and r['other_tenant_denied'] for r in rows) else 'fail',
            'unauthenticated_denied':unauth,'rows':rows,'initial_request_timings':timings,
            'rest_duration_override_s':rest_duration,
            'scope':'Internal service equivalence only. No new-user or Kiro acceptance claim.'}
    write_json(output/'result.json',result)
    print(result['status'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--deployment',required=True);parser.add_argument('--output',required=True);parser.add_argument('--resume');parser.add_argument('--other-label',default='other');parser.add_argument('--rest-duration',type=float);args=parser.parse_args()
    run(args.deployment,args.output,args.resume,args.other_label,args.rest_duration)
