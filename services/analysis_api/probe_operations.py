"""Real operational checks against our bounded AWS deployment; no model calls."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

from kiroclops.cloud.__main__ import human_backend
from kiroclops.cloud.remote import RemoteAnalysisBackend
from kiroclops.visual.schema import read_json,write_json,VisualError
from scripts.phase3b_aws_probe import aws
from .probe import configure


def run(deployment,output):
    deployment,output=Path(deployment),Path(output)
    if output.exists():raise ValueError('Fresh evidence path required')
    output.mkdir(parents=True)
    state=read_json(deployment/'state.json')
    accounts={label:read_json(deployment/('account-'+label+'.json')) for label in ('owner','other')}
    configs={label:configure(deployment,label) for label in accounts}
    clients={label:RemoteAnalysisBackend(output/label,config) for label,config in configs.items()}
    human={label:human_backend(config,output/(label+'-human')) for label,config in configs.items()}
    checks=[]
    def record(name,passed,**evidence):
        checks.append({'name':name,'passed':bool(passed),**evidence})
        write_json(output/'partial.json',checks)
        print(name+': '+('pass' if passed else 'fail'),flush=True)
        if not passed:raise RuntimeError('Operational check failed: '+name)
    def denied(call,code):
        try:call();return False
        except VisualError as exc:return exc.code==code
    def db(label,kind,identity=''):
        tenant=accounts[label]['tenant']
        item=aws('dynamodb','get-item',table_name=state['Table'],consistent_read=True,
            key={'pk':{'S':'user#'+tenant},'sk':{'S':f'user#{tenant}#{kind}#{identity}'}}).get('Item')
        return json.loads(item['value']['S']) if item else None
    def request(name,candidate='3'*64,retention='reference',variant=None):
        root=Path('artifacts/phase3b/calibration-corpus-02')/name
        contract=read_json(root/'contract.json')
        if variant:contract={**contract,'event_options':{**contract.get('event_options',{}),'rest_duration_s':variant}}
        return {'package_path':str(root/'package'),'selection':read_json(root/'selection.json'),
            'contract':contract,'candidate_sha256':candidate,'retention_class':retention}
    def wait(client,job_id,seconds=300):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            job=client.get_job(job_id,wait_seconds=1)
            if job['status'] in ('succeeded','failed','cancelled','interrupted'):return job
        raise RuntimeError('Operational job deadline exceeded')
    try:
        owner,other=clients['owner'],clients['other']
        record('agent_cannot_delete_or_mint_sessions',all(denied(lambda m=m,p=p:owner._call(m,p,{}),'cloud_access_denied')
            for m,p in [('DELETE','/data'),('POST','/sessions'),('POST','/sessions/revoke')]))
        no_cap=RemoteAnalysisBackend(output/'no-cap',{**configs['owner'],'scoped_agent':False})
        record('agent_jwt_without_capability_is_denied',denied(lambda:no_cap._call('GET','/usage'),'cloud_access_denied'))
        # Stop only the instance explicitly owned by this test deployment.
        instance=aws('ec2','describe-instances',instance_ids=[state['InstanceId']])['Reservations'][0]['Instances'][0]
        if {p['Key']:p['Value'] for p in instance['Tags']}.get('Run')!=state['run']:raise ValueError('Wrong worker owner')
        stopped=aws('ec2','stop-instances',instance_ids=[state['InstanceId']])
        write_json(output/'stop-request.json',stopped)
        first=request('game-before-h03',variant=.31)
        submitted=owner.submit(first,idempotency_key='cancel-'+uuid.uuid4().hex)
        record('one_active_analysis_enforced',denied(lambda:owner.submit(request('game-before-h03',variant=.32),idempotency_key='second-'+uuid.uuid4().hex),'cloud_quota_exceeded'))
        owner.cancel_job(submitted['job_id']);cancelled=wait(owner,submitted['job_id'])
        record('explicit_queued_cancel_releases_slot',cancelled['status']=='cancelled' and human['owner']._call('GET','/usage')['active_job'] is None,job_id=submitted['job_id'])
        before=human['owner']._call('GET','/usage')['daily_compute']
        second=request('game-before-h03',variant=.33);logical='wake-'+uuid.uuid4().hex
        submitted=owner.submit(second,idempotency_key=logical)
        repeated=owner.submit(second,idempotency_key=logical)
        wake=wait(owner,submitted['job_id'])
        after=human['owner']._call('GET','/usage')['daily_compute']
        write_json(output/'wake-job.json',wake)
        record('stopping_worker_restarts_and_completes',wake['status']=='succeeded',job_id=submitted['job_id'],
            initial_state=stopped['StoppingInstances'][0]['CurrentState']['Name'])
        record('repeat_post_charges_one_compute_job',repeated['job_id']==submitted['job_id'] and after['compute_jobs']==before['compute_jobs']+1,
            before=before,after=after)
        # A fresh package has candidate retention; delivery acknowledgement
        # must remove its cloud bytes only after evidence exists locally.
        submitted=owner.submit(request('panel-open-close-w960-r2',retention='candidate'),idempotency_key='retire-'+uuid.uuid4().hex)
        candidate=wait(owner,submitted['job_id']);write_json(output/'candidate-job.json',candidate)
        record('candidate_measurement_delivered',candidate['status']=='succeeded',job_id=submitted['job_id'])
        saved=db('owner','job',submitted['job_id']);package=db('owner','package',saved['request']['package_sha256'])
        objects=aws('s3api','list-objects-v2',bucket=state['bucket'],prefix='users/'+accounts['owner']['tenant']+'/').get('Contents',[])
        present={row['Key'] for row in objects}
        record('acknowledged_candidate_cloud_package_removed',package['status']=='deleted' and package.get('storage_released')
            and package.get('object_key') not in present and package.get('staging_key') not in present)
        # The second tenant uploads identical game pixels and then deletes its
        # own data. This is a synthetic isolation test, not a new-user trial.
        submitted=other.submit(request('game-before-h03'),idempotency_key='other-'+uuid.uuid4().hex)
        other_job=wait(other,submitted['job_id']);write_json(output/'other-job.json',other_job)
        record('second_tenant_computes_own_result',other_job['status']=='succeeded',job_id=submitted['job_id'])
        deletion=human['other']._call('DELETE','/data')
        for _ in range(20):
            if deletion['status']=='deleted':break
            time.sleep(1);deletion=human['other']._call('DELETE','/data')
        remaining=aws('s3api','list-objects-v2',bucket=state['bucket'],prefix='users/'+accounts['other']['tenant']+'/').get('KeyCount',0)
        record('tenant_deletion_verified',deletion['status']=='deleted' and remaining==0,
            deletion=deletion,remaining_objects=remaining)
        record('deleted_data_not_returned',denied(lambda:human['other']._call('GET','/jobs/'+submitted['job_id']),'cloud_data_deleted'))
        record('other_tenant_deletion_does_not_touch_owner',human['owner']._call('GET','/usage')['data_state']=='active')
        human['owner']._call('POST','/sessions/revoke',{})
        record('logout_revokes_still_valid_agent_jwt',denied(lambda:owner._call('GET','/usage'),'cloud_access_denied'))
        result={'status':'pass','kind':'RealAwsOperationalProbe','checks':checks,'model_started':False,'external_users':0,
                'limitations':['Synthetic accounts only. Not proof of all adversarial cases or an ongoing 24-hour SLA.']}
        write_json(output/'result.json',result)
    except Exception as exc:
        write_json(output/'failure.json',{'status':'failed','code':getattr(exc,'code',type(exc).__name__),
            'message':str(exc),'checks':checks,'model_started':False})
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--deployment',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();run(args.deployment,args.output)
