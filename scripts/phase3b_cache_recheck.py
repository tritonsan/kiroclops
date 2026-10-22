"""Prove latest-release exact reuse with the owned worker stopped."""
from pathlib import Path
import argparse
import time
import uuid

from kiroclops.cloud.__main__ import human_backend
from kiroclops.cloud.remote import RemoteAnalysisBackend
from kiroclops.visual.schema import read_json, write_json
from scripts.phase3b_aws_probe import aws


def run(deployment, fresh, output):
    deployment, fresh, output = Path(deployment), Path(fresh), Path(output)
    if output.exists():
        raise ValueError('Preserve earlier repeat evidence')
    state, config = read_json(deployment/'state.json'), read_json(deployment/'config-owner.json')
    output.mkdir(parents=True)
    human = human_backend(config, output/'human')
    if human._call('GET', '/usage').get('active_job'):
        raise ValueError('Never stop a worker during active analysis')
    def instance():
        value=aws('ec2','describe-instances',instance_ids=[state['InstanceId']])['Reservations'][0]['Instances'][0]
        if {t['Key']:t['Value'] for t in value.get('Tags',[])}.get('Run') != state['run']:
            raise ValueError('Worker is not owned by this deployment')
        return value
    before=instance()
    if before['State']['Name'] not in ('stopped','stopping'):
        aws('ec2','stop-instances',instance_ids=[state['InstanceId']])
    deadline=time.monotonic()+300
    while instance()['State']['Name']!='stopped':
        if time.monotonic()>deadline:raise TimeoutError('Owned worker did not stop')
        time.sleep(5)
    client=RemoteAnalysisBackend(output/'client',config)
    def reject_upload(*args):
        raise AssertionError('Verified package unexpectedly requires upload')
    client._upload=reject_upload
    registered=read_json(fresh/'result.json')
    rows=[]
    for original in registered['rows']:
        source=human._call('GET','/jobs/'+original['job_id'])
        base=Path('artifacts/phase3b/calibration-corpus-02')/original['id']
        contract=read_json(base/'contract.json')
        if registered.get('rest_duration_override_s') is not None:
            contract={**contract,'event_options':{**contract.get('event_options',{}),
                'rest_duration_s':registered['rest_duration_override_s']}}
        request={'package_path':str(base/'package'),'selection':read_json(base/'selection.json'),
                 'contract':contract,'candidate_sha256':'a'*64,'retention_class':'reference'}
        started=time.monotonic()
        submitted=client.submit(request,idempotency_key='recheck-'+uuid.uuid4().hex)
        if submitted['status']!='succeeded':raise AssertionError('Expected immediate exact-reuse result')
        delivered=client.get_job(submitted['job_id'])
        elapsed=time.monotonic()-started
        receipt=read_json(output/'client/jobs'/submitted['job_id']/'delivery-v2.json')
        old=source['delivery']
        checks={'measurement_exact':receipt['measurement']==old['measurement'],
                'different_job':submitted['job_id']!=original['job_id'],
                'different_receipt':receipt['receipt_sha256']!=old['receipt_sha256'],
                'new_candidate_bound':receipt['receipt']['candidate_sha256']=='a'*64,
                'reuse_origin':receipt['receipt']['origin']['kind']=='exact_reuse',
                'not_indexed':receipt['indexed_for_similarity'] is False,
                'no_advice':receipt['advice'] is None,'delivered':delivered['status']=='succeeded'}
        rows.append({'id':original['id'],'job_id':submitted['job_id'],'checks':checks,
                     'end_to_end_seconds':elapsed,'fresh_seconds':original['end_to_end_seconds'],
                     'saved_seconds':original['end_to_end_seconds']-elapsed,'upload_skipped':True,
                     'measurement_sha256':receipt['measurement_sha256'],'receipt':receipt['receipt']})
        write_json(output/'partial.json',rows)
        print(original['id'],all(checks.values()),round(elapsed,2),flush=True)
    after=instance()
    still_stopped=after['State']['Name']=='stopped'
    write_json(output/'result.json',{'status':'pass' if still_stopped and all(all(r['checks'].values()) for r in rows) else 'fail',
        'rows':rows,'worker_before':'stopped','worker_after':after['State']['Name'],
        'worker_did_not_restart':still_stopped and before['LaunchTime']==after['LaunchTime'],
        'forced_hits':len(rows),'requests':len(rows),'external_users':0,'model_started':False,
        'scope':'Deliberate exact repeats; not a natural hit rate or a full-session speedup.'})


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('deployment','fresh','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    run(args.deployment,args.fresh,args.output)
