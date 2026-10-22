"""Bounded internal ARM benchmark, not the beta service.

Provision only tagged resources. The instance shuts down after 60 minutes and
an independent Lambda terminates it after 75 minutes. No inbound ports, no NAT,
no interface endpoints. Delete the stack and the private input bucket after
downloading evidence. Never accepts Marketplace terms.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def aws(service, operation, **arguments):
    command = ['aws', service, operation, '--region', 'us-east-1', '--output', 'json', '--no-cli-pager']
    for key, value in arguments.items():
        command.append('--' + key.replace('_', '-'))
        if isinstance(value, bool):
            if not value:
                command[-1] = '--no-' + key.replace('_', '-')
        else:
            command.append(json.dumps(value) if isinstance(value, (list, dict)) else str(value))
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=180)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def template(bucket, ami, subnet, vpc, prefix, *, cool=False, code_variant='baseline'):
    bootstrap = '''#!/bin/bash
set -euo pipefail
shutdown -h +60
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip unzip curl libgl1 libglib2.0-0
mkdir -p /opt/kiroclops-probe
cd /opt/kiroclops-probe
python3 -m venv control
control/bin/pip -q install boto3
cat > transfer.py <<'PY'
import boto3, pathlib, sys
s=boto3.client('s3'); root=pathlib.Path('/opt/kiroclops-probe')
if sys.argv[1]=='get': s.download_file('BUCKET','PREFIX/bundle.tar',str(root/'bundle.tar'))
else:
 for p in (root/'results').rglob('*'):
  if p.is_file(): s.upload_file(str(p),'BUCKET','PREFIX/results/'+p.relative_to(root/'results').as_posix())
PY
control/bin/python transfer.py get
trap 'control/bin/python transfer.py put || true' EXIT
tar -xf bundle.tar
python3 -m venv standard
standard/bin/pip -q install --only-binary=:all: opencv-python==5.0.0.93 numpy==2.2.6
mkdir -p results
standard/bin/pip freeze > results/requirements-standard.txt
export PYTHONPATH=/opt/kiroclops-probe/code
set +e
standard/bin/python -u -m kiroclops.cloud.benchmark local --corpus corpus --output results/standard --repeats 5 --threads 4 --no-worker > results/standard.log 2>&1
echo $? > results/standard.exit
set -e
control/bin/python transfer.py put
shutdown -h now
'''.replace('BUCKET', bucket).replace('PREFIX', prefix)
    bootstrap = bootstrap.replace('--repeats 5 --threads 4', '--repeats 5 --threads 4 --code-variant ' + code_variant)
    # COOL cannot be benchmarked by silently using the pip environment.
    if cool:
        raise ValueError('COOL requires an identified compatible environment and paired runner before launch')
    trust = lambda service: {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':service},'Action':'sts:AssumeRole'}]}
    policy = lambda statements: {'Version':'2012-10-17','Statement': statements}
    tags = [{'Key':'Project','Value':'KiroClops'}, {'Key':'Purpose','Value':'phase3b-feasibility'}, {'Key':'Run','Value':prefix}]
    guard_code = '''import boto3, datetime
def handler(event, context):
 ec2=boto3.client('ec2')
 now=datetime.datetime.now(datetime.timezone.utc)
 response=ec2.describe_instances(Filters=[{'Name':'tag:Run','Values':['PREFIX']},{'Name':'tag:Purpose','Values':['phase3b-feasibility']},{'Name':'instance-state-name','Values':['pending','running','stopping','stopped']}])
 stopped=[]
 for group in response['Reservations']:
  for i in group['Instances']:
   if (now-i['LaunchTime']).total_seconds()>4500 or i['State']['Name']=='stopped':
    ec2.terminate_instances(InstanceIds=[i['InstanceId']]); stopped.append(i['InstanceId'])
 return {'terminated':stopped}
'''.replace('PREFIX',prefix)
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'KiroClops bounded internal ARM feasibility; no public API',
      'Resources': {
        'WorkerRole': {'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':trust('ec2.amazonaws.com'),
          'Policies':[{'PolicyName':'OwnBenchmarkObjects','PolicyDocument':policy([
            {'Effect':'Allow','Action':['s3:GetObject'],'Resource':f'arn:aws:s3:::{bucket}/{prefix}/bundle.tar'},
            {'Effect':'Allow','Action':['s3:PutObject'],'Resource':f'arn:aws:s3:::{bucket}/{prefix}/results/*'}])}]}},
        'WorkerProfile':{'Type':'AWS::IAM::InstanceProfile','Properties':{'Roles':[{'Ref':'WorkerRole'}]}},
        'WorkerSecurity':{'Type':'AWS::EC2::SecurityGroup','Properties':{'GroupDescription':'No inbound access; outgoing package download only',
          'VpcId':vpc,'SecurityGroupIngress':[], 'SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIp':'0.0.0.0/0'},
           {'IpProtocol':'tcp','FromPort':80,'ToPort':80,'CidrIp':'0.0.0.0/0'}], 'Tags':tags}},
        'GuardRole':{'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':trust('lambda.amazonaws.com'),
          'Policies':[{'PolicyName':'StopOnlyThisProbe','PolicyDocument':policy([
            {'Effect':'Allow','Action':'ec2:DescribeInstances','Resource':'*'},
            {'Effect':'Allow','Action':'ec2:TerminateInstances','Resource':'arn:aws:ec2:us-east-1:*:instance/*',
             'Condition':{'StringEquals':{'ec2:ResourceTag/Run':prefix,'ec2:ResourceTag/Purpose':'phase3b-feasibility'}}}])}]}},
        'Guard':{'Type':'AWS::Lambda::Function','Properties':{'Runtime':'python3.12','Handler':'index.handler','Timeout':30,
          'Role':{'Fn::GetAtt':['GuardRole','Arn']},'Code':{'ZipFile':guard_code},'Tags':tags}},
        'GuardRule':{'Type':'AWS::Events::Rule','Properties':{'ScheduleExpression':'rate(5 minutes)','State':'ENABLED',
          'Targets':[{'Arn':{'Fn::GetAtt':['Guard','Arn']},'Id':'guard'}]}},
        'GuardPermission':{'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':{'Ref':'Guard'},'Action':'lambda:InvokeFunction',
          'Principal':'events.amazonaws.com','SourceArn':{'Fn::GetAtt':['GuardRule','Arn']}}},
        'Worker':{'Type':'AWS::EC2::Instance','DependsOn':['GuardPermission'],'Properties':{
          'ImageId':ami,'InstanceType':'c8g.xlarge','IamInstanceProfile':{'Ref':'WorkerProfile'},
          'InstanceInitiatedShutdownBehavior':'terminate','MetadataOptions':{'HttpTokens':'required','HttpPutResponseHopLimit':1},
          'NetworkInterfaces':[{'DeviceIndex':'0','AssociatePublicIpAddress':True,'SubnetId':subnet,'GroupSet':[{'Ref':'WorkerSecurity'}]}],
          'BlockDeviceMappings':[{'DeviceName':'/dev/sda1','Ebs':{'VolumeSize':16,'VolumeType':'gp3','Encrypted':True,'DeleteOnTermination':True}}],
          'UserData':{'Fn::Base64':bootstrap},'Tags':tags}},
      },'Outputs':{'InstanceId':{'Value':{'Ref':'Worker'}},'GuardName':{'Value':{'Ref':'Guard'}}}}


def start(corpus, output, runtime=None):
    corpus, output = Path(corpus).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use a fresh evidence directory; never relaunch an existing probe')
    if not (corpus/'manifest.json').exists():
        raise ValueError('Frozen corpus is required')
    output.mkdir(parents=True)
    account = aws('sts','get-caller-identity')['Account']
    run = 'phase3b-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    bucket = f'kiroclops-probe-{account}-{run}'
    state = {'schema_version':1,'region':'us-east-1','run':run,'stack':run,'bucket':bucket,'status':'preparing',
             'budget_usd':10, 'compute_hourly_usd':.15952, 'maximum_instance_hours':1.34,
             'engine':'standard-arm-only','cool_status':'blocked_marketplace_opt_in',
             'corpus_sha256':read_json(corpus/'manifest.json')['corpus_sha256']}
    write_json(output/'state.json',state)
    ami = aws('ssm','get-parameter',name='/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id')['Parameter']['Value']
    vpcs=aws('ec2','describe-vpcs',filters=[{'Name':'is-default','Values':['true']}])['Vpcs']
    if len(vpcs)!=1:raise ValueError('One default VPC required')
    vpc=vpcs[0]['VpcId']
    subnets=aws('ec2','describe-subnets',filters=[{'Name':'vpc-id','Values':[vpc]},{'Name':'default-for-az','Values':['true']}])['Subnets']
    subnet=next(s['SubnetId'] for s in subnets if s['MapPublicIpOnLaunch'])
    state.update(ami=ami,subnet=subnet,vpc=vpc)
    aws('s3api','create-bucket',bucket=bucket)
    aws('s3api','put-public-access-block',bucket=bucket,public_access_block_configuration={k:True for k in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')})
    aws('s3api','put-bucket-encryption',bucket=bucket,server_side_encryption_configuration={'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})
    aws('s3api','put-bucket-lifecycle-configuration',bucket=bucket,lifecycle_configuration={'Rules':[{'ID':'ProbeTemporary','Status':'Enabled','Filter':{'Prefix':run+'/'},'Expiration':{'Days':1},'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]})
    aws('s3api','put-bucket-policy',bucket=bucket,policy=json.dumps({'Version':'2012-10-17','Statement':[{'Sid':'TLSOnly','Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[f'arn:aws:s3:::{bucket}',f'arn:aws:s3:::{bucket}/*'],'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}))
    archive=output/'bundle.tar'
    with tarfile.open(archive,'w') as tar:
        for item in read_json(corpus/'manifest.json')['items']:
            tar.add(corpus/item['id'], arcname='corpus/'+item['id'])
        tar.add(corpus/'manifest.json',arcname='corpus/manifest.json')
        # Only the frozen pixel-analysis core and benchmark harness go to AWS.
        code_root = Path(runtime).resolve() if runtime else corpus/'source'
        for path in code_root.rglob('*.py'):
            tar.add(path,arcname='code/'+path.relative_to(code_root).as_posix())
        if not runtime:
            for path in (ROOT/'kiroclops/cloud').glob('*.py'):
                tar.add(path,arcname='code/kiroclops/cloud/'+path.name)
    state['bundle_sha256']=file_sha256(archive)
    aws('s3api','put-object',bucket=bucket,key=run+'/bundle.tar',body=str(archive))
    spec=template(bucket,ami,subnet,vpc,run,code_variant='local-optimization' if runtime else 'baseline')
    write_json(output/'stack.json',spec)
    write_json(output/'state.json',state)
    response=aws('cloudformation','create-stack',stack_name=run,template_body='file://'+str(output/'stack.json'),
                 capabilities=['CAPABILITY_IAM'],timeout_in_minutes=15,on_failure='DELETE',
                 tags=[{'Key':'Project','Value':'KiroClops'},{'Key':'Purpose','Value':'phase3b-feasibility'}])
    state.update(status='creating',stack_id=response['StackId'],created_at=datetime.now(timezone.utc).isoformat())
    write_json(output/'state.json',state)
    print(json.dumps({'status':state['status'],'stack':run,'evidence':str(output)}))


def status(output):
    output=Path(output); state=read_json(output/'state.json')
    stack=aws('cloudformation','describe-stacks',stack_name=state['stack'])['Stacks'][0]
    state['stack_status']=stack['StackStatus']
    for row in stack.get('Outputs',[]):state[row['OutputKey']]=row['OutputValue']
    objects=aws('s3api','list-objects-v2',bucket=state['bucket'],prefix=state['run']+'/results/').get('Contents',[])
    for obj in objects:
        relative=obj['Key'].removeprefix(state['run']+'/results/')
        from kiroclops.visual.schema import safe_path
        target=safe_path(output/'download',relative,must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
    if objects:
        p=subprocess.run(['aws','s3','sync',f"s3://{state['bucket']}/{state['run']}/results/",str(output/'download'),
                          '--region','us-east-1','--only-show-errors'],capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr)
    if state.get('InstanceId'):
        reservation=aws('ec2','describe-instances',instance_ids=[state['InstanceId']])['Reservations']
        state['instance_state']=reservation[0]['Instances'][0]['State']['Name']
        write_json(output/'instance.json',reservation[0]['Instances'][0])
    write_json(output/'state.json',state)
    print(json.dumps({'stack_status':state['stack_status'],'instance_state':state.get('instance_state'),'downloaded_objects':len(objects)}))


def cleanup(output):
    output=Path(output);state=read_json(output/'state.json')
    if not state['stack'].startswith('phase3b-') or not state['bucket'].startswith('kiroclops-probe-'):
        raise ValueError('Unexpected cleanup scope')
    # Exact names from the creation receipt; never delete any unrelated AWS data.
    aws('cloudformation','delete-stack',stack_name=state['stack'])
    objects=aws('s3api','list-objects-v2',bucket=state['bucket'],prefix=state['run']+'/').get('Contents',[])
    if objects:aws('s3api','delete-objects',bucket=state['bucket'],delete={'Objects':[{'Key':o['Key']} for o in objects],'Quiet':True})
    aws('s3api','delete-bucket',bucket=state['bucket'])
    state['cleanup_requested_at']=datetime.now(timezone.utc).isoformat()
    write_json(output/'state.json',state)
    print('Cleanup requested; verify stack deletion and EC2 termination separately.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','status','cleanup'])
    parser.add_argument('--corpus',type=Path);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime',type=Path)
    args=parser.parse_args()
    if args.command=='start':start(args.corpus,args.output,args.runtime)
    elif args.command=='status':status(args.output)
    else:cleanup(args.output)
