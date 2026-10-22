"""Bounded, private end-to-end service probe. No self-registration or emails.

Reuses the independent EC2 terminator from the measured ARM probe. Every
resource belongs to one CloudFormation stack and an explicitly named bucket.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import secrets
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.phase3b_aws_probe import aws, template as probe_template
from kiroclops.visual.schema import write_json,read_json,file_sha256,digest


def api_archive(path):
    with zipfile.ZipFile(path,'w') as archive:
        for name in ('__init__.py','api.py','store.py','cache.py','quota.py','storage.py','lifecycle.py','capabilities.py','operations.py','maintenance.py'):
            archive.write(ROOT/'services/analysis_api'/name,'services/analysis_api/'+name)
        # Pure Python contracts only; Lambda never imports OpenCV/NumPy.
        for name in ('kiroclops/__init__.py','kiroclops/cloud/__init__.py','kiroclops/cloud/results.py',
                     'kiroclops/cloud/identity.py','kiroclops/visual/__init__.py','kiroclops/visual/schema.py'):
            archive.write(ROOT/name,name)


def template(bucket,ami,subnet,vpc,prefix,*,invited_beta=False):
    value = probe_template(bucket,ami,subnet,vpc,prefix)
    value['Description']='KiroClops JWT single-package service probe; manually started bounded worker'
    resources=value['Resources']
    # The independent probe guard must permit an idle stop followed by a
    # controlled restart. Its absolute 75-minute termination remains in place
    # for these internal deployments; this is not an unattended public beta.
    guard=resources['Guard']['Properties']['Code']['ZipFile']
    resources['Guard']['Properties']['Code']['ZipFile']=guard.replace(" or i['State']['Name']=='stopped'",'')
    if invited_beta:
        # Keep the independent runaway guard in a reusable deployment. A
        # stopped worker can wake for a later authorized, budgeted job.
        resources['Guard']['Properties']['Code']['ZipFile']=resources['Guard']['Properties']['Code']['ZipFile'].replace(
            'ec2.terminate_instances(', 'ec2.stop_instances(')
        for policy in resources['GuardRole']['Properties']['Policies']:
            for statement in policy['PolicyDocument']['Statement']:
                action=statement['Action']
                if isinstance(action,list):statement['Action']=[a.replace('ec2:TerminateInstances','ec2:StopInstances') for a in action]
                else:statement['Action']=action.replace('ec2:TerminateInstances','ec2:StopInstances')
    ref=lambda name:{'Ref':name}
    att=lambda name,attribute:{'Fn::GetAtt':[name,attribute]}
    sub=lambda text:{'Fn::Sub':text}
    resources['Table']={'Type':'AWS::DynamoDB::Table','Properties':{'BillingMode':'PAY_PER_REQUEST','AttributeDefinitions':[{'AttributeName':'pk','AttributeType':'S'},{'AttributeName':'sk','AttributeType':'S'}],
        'KeySchema':[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'sk','KeyType':'RANGE'}],'SSESpecification':{'SSEEnabled':True}}}
    resources['Queue']={'Type':'AWS::SQS::Queue','Properties':{'VisibilityTimeout':45,'MessageRetentionPeriod':3600,'SqsManagedSseEnabled':True}}
    resources['Pool']={'Type':'AWS::Cognito::UserPool','Properties':{'AdminCreateUserConfig':{'AllowAdminCreateUserOnly':True},
        'UserPoolName':prefix,'Policies':{'PasswordPolicy':{'MinimumLength':20,'RequireLowercase':True,'RequireUppercase':True,'RequireNumbers':True,'RequireSymbols':True}},'DeletionProtection':'INACTIVE'}}
    resources['Client']={'Type':'AWS::Cognito::UserPoolClient','Properties':{'UserPoolId':ref('Pool'),'GenerateSecret':False,
        'ExplicitAuthFlows':['ALLOW_USER_PASSWORD_AUTH','ALLOW_REFRESH_TOKEN_AUTH'],'PreventUserExistenceErrors':'ENABLED',
        'AccessTokenValidity':60,'IdTokenValidity':60,'RefreshTokenValidity':1,
        'TokenValidityUnits':{'AccessToken':'minutes','IdToken':'minutes','RefreshToken':'days'},'EnableTokenRevocation':True}}
    from copy import deepcopy
    resources['AgentClient']=deepcopy(resources['Client'])
    resources['ApiRole']={'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},
        'Policies':[{'PolicyName':'PrivateAnalysisControl','PolicyDocument':{'Version':'2012-10-17','Statement':[
          {'Effect':'Allow','Action':['dynamodb:GetItem','dynamodb:PutItem','dynamodb:DeleteItem','dynamodb:Query'],'Resource':att('Table','Arn')},
          {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject'],'Resource':[f'arn:aws:s3:::{bucket}/users/*',f'arn:aws:s3:::{bucket}/service/*']},
          {'Effect':'Allow','Action':['s3:ListBucket'],'Resource':f'arn:aws:s3:::{bucket}','Condition':{'StringLike':{'s3:prefix':'users/*'}}},
          {'Effect':'Allow','Action':['sqs:SendMessage'],'Resource':att('Queue','Arn')} ]}}]}}
    resources['ApiFunction']={'Type':'AWS::Lambda::Function','Properties':{'Runtime':'python3.12','Handler':'services.analysis_api.api.handler',
        'Role':att('ApiRole','Arn'),'Timeout':25,'MemorySize':256,'ReservedConcurrentExecutions':2,
        'Code':{'S3Bucket':bucket,'S3Key':prefix+'/api.zip'},
        'Environment':{'Variables':{'TABLE':ref('Table'),'BUCKET':bucket,'QUEUE':ref('Queue'),
                                   'HUMAN_CLIENT':ref('Client'),'AGENT_CLIENT':ref('AgentClient'),
                                   'CONTROLLER':ref('Controller'),'REQUIRE_BUDGET':'1'}}}}
    resources['Api']={'Type':'AWS::ApiGatewayV2::Api','Properties':{'Name':prefix,'ProtocolType':'HTTP'}}
    resources['Integration']={'Type':'AWS::ApiGatewayV2::Integration','Properties':{'ApiId':ref('Api'),'IntegrationType':'AWS_PROXY','IntegrationUri':att('ApiFunction','Arn'),'PayloadFormatVersion':'2.0'}}
    resources['Authorizer']={'Type':'AWS::ApiGatewayV2::Authorizer','Properties':{'ApiId':ref('Api'),'AuthorizerType':'JWT','Name':'CognitoAccessTokens',
        'IdentitySource':['$request.header.Authorization'],'JwtConfiguration':{'Audience':[ref('Client'),ref('AgentClient')],'Issuer':sub('https://cognito-idp.${AWS::Region}.amazonaws.com/${Pool}')}}}
    resources['Route']={'Type':'AWS::ApiGatewayV2::Route','Properties':{'ApiId':ref('Api'),'RouteKey':'$default','Target':sub('integrations/${Integration}'),
        'AuthorizationType':'JWT','AuthorizerId':ref('Authorizer'),'AuthorizationScopes':['aws.cognito.signin.user.admin']}}
    resources['Stage']={'Type':'AWS::ApiGatewayV2::Stage','Properties':{'ApiId':ref('Api'),'StageName':'$default','AutoDeploy':True,
        'DefaultRouteSettings':{'ThrottlingBurstLimit':10,'ThrottlingRateLimit':5}}}
    resources['ApiPermission']={'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':ref('ApiFunction'),'Action':'lambda:InvokeFunction','Principal':'apigateway.amazonaws.com',
        'SourceArn':sub('arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${Api}/*')}}
    statements=resources['WorkerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    statements[:]=[
        {'Effect':'Allow','Action':['s3:GetObject'],'Resource':f'arn:aws:s3:::{bucket}/{prefix}/bundle.tar'},
        {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject'],'Resource':[f'arn:aws:s3:::{bucket}/users/*',f'arn:aws:s3:::{bucket}/service/*']},
        {'Effect':'Allow','Action':['dynamodb:GetItem','dynamodb:PutItem','dynamodb:DeleteItem','dynamodb:Query'],'Resource':att('Table','Arn')},
        {'Effect':'Allow','Action':['sqs:ReceiveMessage','sqs:SendMessage','sqs:DeleteMessage','sqs:ChangeMessageVisibility'],'Resource':att('Queue','Arn')}]
    # The controller uses EC2 state, not analysis durations, as its meter.
    resources['ControllerRole']={'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},
        'Policies':[{'PolicyName':'LifecycleAndWorkerBudget','PolicyDocument':{'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['ec2:DescribeInstances'],'Resource':'*'},
            {'Effect':'Allow','Action':['ec2:StartInstances','ec2:StopInstances'],'Resource':'arn:aws:ec2:us-east-1:*:instance/*','Condition':{'StringEquals':{'ec2:ResourceTag/Run':prefix}}},
            {'Effect':'Allow','Action':['dynamodb:GetItem','dynamodb:PutItem','dynamodb:DeleteItem','dynamodb:Query'],'Resource':att('Table','Arn')},
            {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject'],'Resource':[f'arn:aws:s3:::{bucket}/users/*',f'arn:aws:s3:::{bucket}/service/*']},
            {'Effect':'Allow','Action':'s3:ListBucket','Resource':f'arn:aws:s3:::{bucket}','Condition':{'StringLike':{'s3:prefix':'users/*'}}},
            {'Effect':'Allow','Action':'sqs:SendMessage','Resource':att('Queue','Arn')} ]}}]}}
    resources['Controller']={'Type':'AWS::Lambda::Function','Properties':{'Runtime':'python3.12','Handler':'services.analysis_api.maintenance.handler',
        'Role':att('ControllerRole','Arn'),'Timeout':90,'MemorySize':256,'ReservedConcurrentExecutions':1,
        'Code':{'S3Bucket':bucket,'S3Key':prefix+'/api.zip'},'Environment':{'Variables':{
            'TABLE':ref('Table'),'BUCKET':bucket,'QUEUE':ref('Queue'),'WORKER_ID':ref('Worker')}}}}
    resources['ApiRole']['Properties']['Policies'][0]['PolicyDocument']['Statement'].append(
        {'Effect':'Allow','Action':'lambda:InvokeFunction','Resource':att('Controller','Arn')})
    resources['ControllerSchedule']={'Type':'AWS::Events::Rule','Properties':{'ScheduleExpression':'rate(5 minutes)','State':'ENABLED',
        'Targets':[{'Arn':att('Controller','Arn'),'Id':'controller'}]}}
    resources['ControllerStates']={'Type':'AWS::Events::Rule','Properties':{'EventPattern':{'source':['aws.ec2'],'detail-type':['EC2 Instance State-change Notification'],
        'detail':{'instance-id':[ref('Worker')]}},'State':'ENABLED','Targets':[{'Arn':att('Controller','Arn'),'Id':'controller'}]}}
    for logical in ('ControllerSchedule','ControllerStates'):
        resources[logical+'Permission']={'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':ref('Controller'),'Action':'lambda:InvokeFunction',
            'Principal':'events.amazonaws.com','SourceArn':att(logical,'Arn')}}
    bootstrap='''#!/bin/bash
set -euo pipefail
shutdown -h +60
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip libgl1 libglib2.0-0
useradd --system --no-create-home --shell /usr/sbin/nologin kiroanalysis
mkdir -p /opt/kiroclops-service /srv/kiroclops-jobs
chmod 711 /srv/kiroclops-jobs
cd /opt/kiroclops-service
python3 -m venv venv
venv/bin/pip -q install --only-binary=:all: boto3==1.42.79 opencv-python==5.0.0.93 numpy==2.2.6
venv/bin/python -c "import boto3; boto3.client('s3').download_file('@BUCKET@','@PREFIX@/bundle.tar','bundle.tar')"
tar -xf bundle.tar
cat > /etc/systemd/system/kiroclops-worker.service <<'UNIT'
[Unit]
Description=KiroClops bounded analysis control process
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=PYTHONPATH=/opt/kiroclops-service/code
Environment=TABLE=TABLE_NAME
Environment=BUCKET=@BUCKET@
Environment=QUEUE=QUEUE_URL
Environment=AWS_DEFAULT_REGION=us-east-1
Environment=AMI_ID=@AMI@
ExecStartPre=/usr/sbin/shutdown -h +60
ExecStart=/opt/kiroclops-service/venv/bin/python -u -m services.analysis_api.worker
TimeoutStopSec=20
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now kiroclops-worker.service
'''.replace('@BUCKET@',bucket).replace('@PREFIX@',prefix).replace('@AMI@',ami).replace('TABLE_NAME','${Table}').replace('QUEUE_URL','${Queue}')
    resources['Worker']['Properties']['UserData']={'Fn::Base64':sub(bootstrap)}
    resources['Worker']['Properties']['InstanceInitiatedShutdownBehavior']='stop'
    value['Outputs'].update({name:{'Value':ref(name)} for name in ('Table','Queue','Pool','Client','AgentClient')})
    value['Outputs']['ApiUrl']={'Value':att('Api','ApiEndpoint')}
    value['Outputs']['Controller']={'Value':ref('Controller')}
    return value


def start(output,*,invited_beta=False):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Fresh evidence directory required')
    output.mkdir(parents=True)
    account=aws('sts','get-caller-identity')['Account']
    run='phase3b-service-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    bucket=f'kiroclops-probe-{account}-{run}'
    state={'schema_version':1,'run':run,'stack':run,'bucket':bucket,'region':'us-east-1','purpose':'internal-jwt-e2e','created_at':datetime.now(timezone.utc).isoformat()}
    if invited_beta:state['purpose']='invited-beta-standard-opencv'
    write_json(output/'state.json',state)
    ami=aws('ssm','get-parameter',name='/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id')['Parameter']['Value']
    vpc=aws('ec2','describe-vpcs',filters=[{'Name':'is-default','Values':['true']}])['Vpcs'][0]['VpcId']
    subnet=next(s['SubnetId'] for s in aws('ec2','describe-subnets',filters=[{'Name':'vpc-id','Values':[vpc]},{'Name':'default-for-az','Values':['true']}])['Subnets'] if s['MapPublicIpOnLaunch'])
    aws('s3api','create-bucket',bucket=bucket)
    aws('s3api','put-public-access-block',bucket=bucket,public_access_block_configuration={k:True for k in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')})
    aws('s3api','put-bucket-encryption',bucket=bucket,server_side_encryption_configuration={'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})
    aws('s3api','put-bucket-lifecycle-configuration',bucket=bucket,lifecycle_configuration={'Rules':[
        {'ID':'BoundUserObjectsIncludingOrphans','Status':'Enabled','Filter':{'Prefix':'users/'},'Expiration':{'Days':30}}]})
    aws('s3api','put-bucket-policy',bucket=bucket,policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[f'arn:aws:s3:::{bucket}',f'arn:aws:s3:::{bucket}/*'],'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}))
    seed_path=ROOT/'artifacts/phase3b/operations-latest-budget.json'
    if seed_path.exists():
        seed=read_json(seed_path)
    else:
        from services.analysis_api.operations import account_interval
        seed={};seen=set();inputs=[]
        for folder in sorted((ROOT/'artifacts/phase3b').iterdir()):
            if not folder.is_dir() or not folder.name.startswith(('arm-probe-','service-probe-')):continue
            for name in ('instance-after-cache.json','instance-before-cleanup.json','instance.json'):
                saved=folder/name
                if not saved.exists():continue
                instance=read_json(saved);identity=instance.get('InstanceId')
                proof_path=folder/'cleanup-verification.json'
                proof=read_json(proof_path) if proof_path.exists() else {}
                terminated=instance.get('State',{}).get('Name')=='terminated' or proof.get('instance_status')=='terminated' or proof.get('instance_state')=='terminated'
                if identity in seen or not terminated:continue
                launch=datetime.fromisoformat(instance['LaunchTime']).timestamp()
                end=datetime.fromisoformat(proof['checked_at']).timestamp() if proof.get('checked_at') else max(saved.stat().st_mtime,proof_path.stat().st_mtime if proof_path.exists() else 0)
                seed=account_interval(seed,launch,max(launch,end))
                seen.add(identity);inputs.append({'instance_id':identity,'evidence':str(saved.relative_to(ROOT)),'sha256':file_sha256(saved)})
                break
        seed['prior_probe_evidence']=inputs
    write_json(output/'budget-seed.json',seed)
    aws('s3api','put-object',bucket=bucket,key='service/budget-seed.json',body=str(output/'budget-seed.json'))
    api_archive(output/'api.zip')
    with tarfile.open(output/'bundle.tar','w') as archive:
        for folder in ('kiroclops/visual','kiroclops/cloud','services/analysis_api'):
            for path in sorted((ROOT/folder).glob('*.py')):
                archive.add(path,'code/'+path.relative_to(ROOT).as_posix())
        archive.add(ROOT/'kiroclops/__init__.py','code/kiroclops/__init__.py')
    for name in ('api.zip','bundle.tar'):
        aws('s3api','put-object',bucket=bucket,key=run+'/'+name,body=str(output/name))
    spec=template(bucket,ami,subnet,vpc,run,invited_beta=invited_beta)
    write_json(output/'stack.json',spec)
    aws('cloudformation','validate-template',template_body='file://'+str(output/'stack.json'))
    response=aws('cloudformation','create-stack',stack_name=run,template_body='file://'+str(output/'stack.json'),capabilities=['CAPABILITY_IAM'],timeout_in_minutes=15,on_failure='DELETE')
    state.update(stack_id=response['StackId'],ami=ami,status='creating',bundle_sha256=file_sha256(output/'bundle.tar'),api_sha256=file_sha256(output/'api.zip'))
    write_json(output/'state.json',state)
    print(json.dumps({'stack':run,'status':'creating'}))


def status(output):
    output=Path(output)
    state=read_json(output/'state.json')
    stack=aws('cloudformation','describe-stacks',stack_name=state['stack'])['Stacks'][0]
    for row in stack.get('Outputs',[]):state[row['OutputKey']]=row['OutputValue']
    state['stack_status']=stack['StackStatus']
    write_json(output/'state.json',state)
    print(json.dumps({'status':state['stack_status'],'worker':state.get('InstanceId')}))


def login_probe(output, label):
    import time
    import re
    output=Path(output)
    state=read_json(output/'state.json')
    if not re.fullmatch('[a-z][a-z0-9-]{0,30}',label) or (output/('account-'+label+'.json')).exists():
        raise ValueError('Use a new short account label; never replace credentials silently')
    enrolled=aws('dynamodb','query',table_name=state['Table'],key_condition_expression='pk = :p AND begins_with(sk, :s)',
        expression_attribute_values={':p':{'S':'system'},':s':{'S':'system#tenant#'}},consistent_read=True)
    if len(enrolled.get('Items',[]))>=5:raise ValueError('The invitation limit is five accounts')
    # Synthetic internal accounts; these are not counted as external testers.
    username='internal-'+label+'-'+secrets.token_hex(4)
    password='aA9!'+secrets.token_urlsafe(24)
    user=aws('cognito-idp','admin-create-user',user_pool_id=state['Pool'],username=username,message_action='SUPPRESS')['User']
    aws('cognito-idp','admin-set-user-password',user_pool_id=state['Pool'],username=username,password=password,permanent=True)
    result=aws('cognito-idp','initiate-auth',client_id=state['Client'],auth_flow='USER_PASSWORD_AUTH',auth_parameters={'USERNAME':username,'PASSWORD':password})['AuthenticationResult']
    sub=next(p['Value'] for p in user['Attributes'] if p['Name']=='sub')
    tenant=digest(sub)[:32]
    account={'enabled':True,'storage_bytes':0,'kind':'synthetic_internal_probe'}
    aws('dynamodb','put-item',table_name=state['Table'],item={'pk':{'S':f'user#{tenant}'},'sk':{'S':f'user#{tenant}#account#'},'value':{'S':json.dumps(account,sort_keys=True,separators=(',',':'))}})
    aws('dynamodb','put-item',table_name=state['Table'],item={'pk':{'S':'system'},'sk':{'S':f'system#tenant#{tenant}'},'value':{'S':json.dumps({'tenant':tenant},sort_keys=True,separators=(',',':'))}})
    secret=output/('capability-'+label+'.private.json')
    write_json(secret,{'access_token':result['AccessToken'],'expires_at':time.time()+result['ExpiresIn']})
    client_config=output/(label+'-client')/'service.json'
    write_json(client_config,{'api_url':state['ApiUrl'],'cognito_client_id':state['Client'],
                            'agent_client_id':state['AgentClient'],'region':'us-east-1'})
    from unittest.mock import patch
    from kiroclops.cloud.__main__ import login
    with patch('getpass.getpass',return_value=password):login(client_config,username)
    # No refresh token or password is retained. Same-user processes can read
    # this file; this is not advertised as strong OS credential isolation.
    write_json(output/('account-'+label+'.json'),{'tenant':tenant,'username':username,'synthetic':True,'capability_file':str(secret.resolve()),
                                              'agent_config_path':str(client_config.resolve())})
    print(json.dumps({'account':label,'created':True,'tokens_logged':False}))


def relogin_probe(output,label):
    """Explicitly renew only our synthetic account, without storing a password."""
    import time
    import re
    from unittest.mock import patch
    from kiroclops.cloud.__main__ import login
    output=Path(output);state=read_json(output/'state.json')
    if not re.fullmatch('[a-z][a-z0-9-]{0,30}',label):raise ValueError('Invalid label')
    account=read_json(output/('account-'+label+'.json'))
    if not account.get('synthetic') or not account['username'].startswith('internal-'+label+'-'):
        raise ValueError('Only synthetic probe accounts can be reset by this command')
    active=aws('dynamodb','get-item',table_name=state['Table'],key={'pk':{'S':'user#'+account['tenant']},
        'sk':{'S':'user#'+account['tenant']+'#active#'}},consistent_read=True)
    if active.get('Item'):raise ValueError('Do not replace credentials while a job is active')
    password='aA9!'+secrets.token_urlsafe(24)
    aws('cognito-idp','admin-set-user-password',user_pool_id=state['Pool'],username=account['username'],password=password,permanent=True)
    with patch('getpass.getpass',return_value=password):result=login(account['agent_config_path'],account['username'])
    write_json(output/('login-'+label+'-'+str(time.time_ns())+'.json'),{'status':result['status'],
        'synthetic':True,'expires_at':result['expires_at'],'scope':result['agent_scope'],'credentials_logged':False})
    print(json.dumps({'account':label,'reauthenticated':True,'credentials_logged':False}))


def cleanup(output):
    output=Path(output).resolve();state=read_json(output/'state.json')
    account=aws('sts','get-caller-identity')['Account']
    if (not state['run'].startswith('phase3b-service-') or state['stack']!=state['run']
            or state['bucket']!=f"kiroclops-probe-{account}-{state['run']}"):
        raise ValueError('Cleanup scope does not match this account and creation receipt')
    instance=state.get('InstanceId')
    budget=None
    # Preserve the cumulative resource ledger before deleting a disposable
    # table. A later deployment must include previous development/test hours.
    if state.get('Table'):
        try:
            item=aws('dynamodb','get-item',table_name=state['Table'],key={'pk':{'S':'system'},'sk':{'S':'system#budget#worker'}},consistent_read=True).get('Item')
            if item:
                budget=json.loads(item['value']['S'])
        except RuntimeError:
            # The earlier single-key prototype has no cumulative ledger.
            pass
    if instance:
        actual=aws('ec2','describe-instances',instance_ids=[instance])['Reservations'][0]['Instances'][0]
        if {p['Key']:p['Value'] for p in actual['Tags']}.get('Run')!=state['run']:
            raise ValueError('Worker ownership differs')
        write_json(output/'instance-before-cleanup.json',actual)
        if budget is not None:
            from services.analysis_api.operations import observe
            import time
            end=time.time()+(120 if actual['State']['Name'] in ('pending','running','stopping') else 0)
            budget=observe(budget,'stopped',end,datetime.fromisoformat(actual['LaunchTime']).timestamp())
            budget['cleanup_stop_grace_seconds']=120
            write_json(output/'worker-budget-before-cleanup.json',budget)
            write_json(ROOT/'artifacts/phase3b/operations-latest-budget.json',budget)
        if actual['State']['Name'] not in ('terminated','shutting-down'):
            aws('ec2','terminate-instances',instance_ids=[instance])
    aws('cloudformation','delete-stack',stack_name=state['stack'])
    keys=[];token=None
    while True:
        page=aws('s3api','list-objects-v2',bucket=state['bucket'],**({'continuation_token':token} if token else {}))
        keys.extend({'Key':item['Key']} for item in page.get('Contents',[]))
        token=page.get('NextContinuationToken')
        if not token:break
    if keys:
        for start in range(0,len(keys),1000):
            result=aws('s3api','delete-objects',bucket=state['bucket'],delete={'Objects':keys[start:start+1000],'Quiet':True})
            if result.get('Errors'):raise RuntimeError('Some owned probe objects could not be deleted')
    aws('s3api','delete-bucket',bucket=state['bucket'])
    for secret in output.glob('capability-*.private.json'):
        if secret.resolve().parent!=output:raise ValueError('Secret path escaped probe directory')
        secret.unlink()
    for secret in output.glob('*-client/.kiroclops-private/*.credential'):
        if not secret.resolve().is_relative_to(output):raise ValueError('Credential path escaped probe directory')
        secret.unlink()
    state.update(cleanup_requested_at=datetime.now(timezone.utc).isoformat(),deleted_object_count=len(keys))
    write_json(output/'state.json',state)
    print(json.dumps({'cleanup':'requested','objects_deleted':len(keys),'local_tokens_removed':True}))


def update_api(output):
    output=Path(output);state=read_json(output/'state.json')
    archive_path=output/('api-update-'+datetime.now(timezone.utc).strftime('%H%M%S')+'.zip')
    api_archive(archive_path)
    updates=[]
    for logical in ('ApiFunction','Controller'):
        function=aws('cloudformation','describe-stack-resource',stack_name=state['stack'],logical_resource_id=logical)['StackResourceDetail']['PhysicalResourceId']
        response=aws('lambda','update-function-code',function_name=function,zip_file='fileb://'+str(archive_path.resolve()))
        updates.append({'function':function,'status':response.get('LastUpdateStatus')})
    write_json(archive_path.with_suffix('.json'),{'updates':updates,'code_sha256':file_sha256(archive_path)})
    print(json.dumps({'updated':True,'archive':archive_path.name}))


def release_template(original, bucket, run, revision):
    """Bind boot download and its exact IAM object to the same release."""
    if not isinstance(revision,int) or revision<2:raise ValueError('Revision must be at least two')
    worker='Worker'+str(revision)
    prefix=run+'/release-'+str(revision).zfill(2)
    def replace(value):
        if value=={'Ref':'Worker'}:return {'Ref':worker}
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        if isinstance(value,list):return [replace(v) for v in value]
        if isinstance(value,str):return value.replace('${Worker}','${'+worker+'}')
        return value
    spec=replace(original)
    spec['Resources'][worker]=spec['Resources'].pop('Worker')
    properties=spec['Resources'][worker]['Properties']
    bootstrap=properties['UserData']['Fn::Base64']['Fn::Sub']
    properties['UserData']['Fn::Base64']['Fn::Sub']=bootstrap.replace(run+'/bundle.tar',prefix+'/bundle.tar').replace(
        'Environment=AWS_DEFAULT_REGION=us-east-1','Environment=AWS_DEFAULT_REGION=us-east-1\nEnvironment=DEPLOYMENT_REVISION='+str(revision))
    statements=spec['Resources']['WorkerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    for statement in statements:
        if statement.get('Resource')==f'arn:aws:s3:::{bucket}/{run}/bundle.tar':
            statement['Resource']=f'arn:aws:s3:::{bucket}/{prefix}/bundle.tar'
            break
    else:raise ValueError('Expected exact bundle policy is missing')
    for logical in ('ApiFunction','Controller'):spec['Resources'][logical]['Properties']['Code']['S3Key']=prefix+'/api.zip'
    for logical in ('Client','AgentClient'):
        spec['Resources'][logical]['Properties'].update(AccessTokenValidity=60,IdTokenValidity=60)
    return spec


def replace_worker(output, revision=2):
    """Publish one frozen service revision, with no live worker overlap."""
    output=Path(output);state=read_json(output/'state.json')
    label='release-'+str(revision).zfill(2)
    if (output/(label+'.json')).exists():raise ValueError('This release already exists')
    stack=aws('cloudformation','describe-stacks',stack_name=state['stack'])['Stacks'][0]
    if stack['StackStatus'] not in ('CREATE_COMPLETE','UPDATE_COMPLETE'):raise ValueError('Stack is still changing')
    registry=aws('dynamodb','query',table_name=state['Table'],key_condition_expression='pk = :p AND begins_with(sk, :s)',
        expression_attribute_values={':p':{'S':'system'},':s':{'S':'system#tenant#'}},consistent_read=True)
    for item in registry.get('Items',[]):
        tenant=json.loads(item['value']['S'])['tenant']
        active=aws('dynamodb','get-item',table_name=state['Table'],key={'pk':{'S':'user#'+tenant},'sk':{'S':f'user#{tenant}#active#'}},consistent_read=True)
        if active.get('Item'):raise ValueError('Do not replace a worker while a user job is active')
    old=aws('ec2','describe-instances',instance_ids=[state['InstanceId']])['Reservations'][0]['Instances'][0]
    if {p['Key']:p['Value'] for p in old['Tags']}.get('Run')!=state['run']:raise ValueError('Worker ownership differs')
    write_json(output/(label+'-previous-instance.json'),old)
    if old['State']['Name']!='terminated':
        aws('ec2','terminate-instances',instance_ids=[state['InstanceId']])
        import subprocess
        subprocess.run(['aws','ec2','wait','instance-terminated','--instance-ids',state['InstanceId'],
                        '--region','us-east-1','--no-cli-pager'],check=True,capture_output=True,timeout=180)
    # A previous boot's descriptor must not imply the replacement is ready.
    aws('s3api','delete-object',bucket=state['bucket'],key='service/descriptor.json')
    prefix=state['run']+'/'+label
    api_archive(output/('api-'+label+'.zip'))
    with tarfile.open(output/('bundle-'+label+'.tar'),'w') as archive:
        for folder in ('kiroclops/visual','kiroclops/cloud','services/analysis_api'):
            for path in sorted((ROOT/folder).glob('*.py')):archive.add(path,'code/'+path.relative_to(ROOT).as_posix())
        archive.add(ROOT/'kiroclops/__init__.py','code/kiroclops/__init__.py')
    aws('s3api','put-object',bucket=state['bucket'],key=prefix+'/bundle.tar',body=str(output/('bundle-'+label+'.tar')))
    aws('s3api','put-object',bucket=state['bucket'],key=prefix+'/api.zip',body=str(output/('api-'+label+'.zip')))
    spec=release_template(read_json(output/'stack.json'),state['bucket'],state['run'],revision)
    write_json(output/('stack-'+label+'.json'),spec)
    aws('cloudformation','validate-template',template_body='file://'+str((output/('stack-'+label+'.json')).resolve()))
    aws('cloudformation','update-stack',stack_name=state['stack'],template_body='file://'+str((output/('stack-'+label+'.json')).resolve()),capabilities=['CAPABILITY_IAM'])
    state['deployment_revision']=revision
    write_json(output/'state.json',state)
    write_json(output/(label+'.json'),{'status':'updating','previous_instance':state['InstanceId'],
        'bundle_sha256':file_sha256(output/('bundle-'+label+'.tar')),'api_sha256':file_sha256(output/('api-'+label+'.zip'))})
    print(json.dumps({'status':'updating','old_worker_terminated':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['start','status','login-probe','relogin-probe','cleanup','update-api','replace-worker']);parser.add_argument('--output',required=True);parser.add_argument('--label',default='owner');parser.add_argument('--revision',type=int,default=2);parser.add_argument('--invited-beta',action='store_true');args=parser.parse_args()
    if args.command=='start':start(args.output,invited_beta=args.invited_beta)
    elif args.command=='status':status(args.output)
    elif args.command=='cleanup':cleanup(args.output)
    elif args.command=='update-api':update_api(args.output)
    elif args.command=='replace-worker':replace_worker(args.output,args.revision)
    elif args.command=='relogin-probe':relogin_probe(args.output,args.label)
    else:login_probe(args.output,args.label)
