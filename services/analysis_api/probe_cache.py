"""Exact-repeat integration check after EC2 has been stopped/terminated."""
from pathlib import Path
import argparse
import time
import uuid

from scripts.phase3b_aws_probe import aws
from kiroclops.cloud.remote import RemoteAnalysisBackend
from kiroclops.visual.schema import read_json,write_json,digest


def run(deployment,fresh,output):
    deployment,fresh,output=Path(deployment),Path(fresh),Path(output)
    if output.exists():raise ValueError('Fresh cache evidence directory required')
    output.mkdir(parents=True)
    state=read_json(deployment/'state.json')
    worker=aws('ec2','describe-instances',instance_ids=[state['InstanceId']])['Reservations'][0]['Instances'][0]
    if worker['State']['Name'] not in ('stopped','terminated','shutting-down'):
        raise ValueError('Stop the owned worker first to prove reuse needs no compute')
    config=read_json(deployment/'config-owner.json')
    backend=RemoteAnalysisBackend(output/'client',config)
    backend._upload=lambda *args: (_ for _ in ()).throw(AssertionError('Verified package should skip upload'))
    corpus=Path('artifacts/phase3b/calibration-corpus-02')
    rows=[]
    for old in read_json(fresh/'result.json')['rows']:
        name=old['id']
        source=backend._call('GET','/jobs/'+old['job_id'])  # validated service-side enrollment
        request={'package_path':str(corpus/name/'package'),'selection':read_json(corpus/name/'selection.json'),
                 'contract':read_json(corpus/name/'contract.json'),'candidate_sha256':'2'*64,'retention_class':'reference'}
        started=time.monotonic()
        submitted=backend.submit(request,idempotency_key=uuid.uuid4().hex)
        result=backend.get_job(submitted['job_id'])
        elapsed=time.monotonic()-started
        delivery=read_json(output/'client/jobs'/submitted['job_id']/'delivery-v2.json')
        source_delivery=source['delivery']
        checks={'immediate_completion':submitted['status']=='succeeded','different_job':submitted['job_id']!=old['job_id'],
                'measurement_exact':delivery['measurement']==source_delivery['measurement'],
                'new_candidate_bound':delivery['receipt']['candidate_sha256']=='2'*64,
                'receipt_changed':delivery['receipt_sha256']!=source_delivery['receipt_sha256'],
                'exact_reuse_origin':delivery['receipt']['origin']['kind']=='exact_reuse',
                'not_indexed':delivery['indexed_for_similarity'] is False,'no_recommendations':delivery['advice'] is None}
        rows.append({'id':name,'job_id':submitted['job_id'],'checks':checks,'end_to_end_seconds':elapsed,
                     'fresh_end_to_end_seconds':old['end_to_end_seconds'],'saved_seconds':old['end_to_end_seconds']-elapsed,
                     'upload_skipped':True,'worker_state':worker['State']['Name'],'receipt':delivery['receipt']})
    value={'kind':'ExactReuseAwsIntegration','status':'pass' if all(all(r['checks'].values()) for r in rows) else 'fail',
           'rows':rows,'scope':'Three deliberately repeated inputs; this is not a natural-user hit-rate estimate.',
           'forced_hits':3,'requests':3,'external_users':0,'model_started':False}
    write_json(output/'result.json',value);print(value['status'],flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--deployment',required=True);p.add_argument('--fresh',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    run(args.deployment,args.fresh,args.output)
