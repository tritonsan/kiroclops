"""Bounded internal COOL feasibility. Marketplace subscription must already exist."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tarfile

from scripts.phase3b_aws_probe import aws, template, status, cleanup
from kiroclops.visual.schema import write_json, read_json, file_sha256

ROOT = Path(__file__).resolve().parents[1]


def bootstrap(bucket, prefix, cool_only=False):
    script = r'''#!/bin/bash
set -euo pipefail
shutdown -h +35
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip libgl1 libglib2.0-0
mkdir -p /opt/kiroclops-probe/results
cd /opt/kiroclops-probe
python3 -m venv control
control/bin/pip -q install boto3
cat > transfer.py <<'PY'
import boto3,pathlib,sys
s=boto3.client('s3');root=pathlib.Path('/opt/kiroclops-probe')
if sys.argv[1]=='get':s.download_file('BUCKET','PREFIX/bundle.tar',str(root/'bundle.tar'))
else:
 for p in (root/'results').rglob('*'):
  if p.is_file():s.upload_file(str(p),'BUCKET','PREFIX/results/'+p.relative_to(root/'results').as_posix())
PY
trap 'control/bin/python transfer.py put || true; shutdown -h now' EXIT
control/bin/python transfer.py get
tar -xf bundle.tar
exec > >(tee results/bootstrap.log) 2>&1
test -f /opt/cool/venvs/python_3.12/bin/activate
export PYTHONPATH="${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
set +u
source /opt/cool/venvs/python_3.12/bin/activate
set -u
COOL_PYTHON=/opt/cool/venvs/python_3.12/bin/python
export PYTHONPATH=/opt/kiroclops-probe/code:${PYTHONPATH:-}
"$COOL_PYTHON" -c 'import json,cv2,numpy,sys;from pathlib import Path;Path("results/cool-detected.json").write_text(json.dumps({"opencv":cv2.__version__,"numpy":numpy.__version__,"python":sys.version,"module":cv2.__file__},indent=2))'
"$COOL_PYTHON" -c 'import cv2;from pathlib import Path;Path("results/cool-build-information.txt").write_text(cv2.getBuildInformation())'
COOL_ONLY_BRANCH
env -u LD_LIBRARY_PATH -u PYTHONPATH /usr/bin/python3.12 -m venv standard
NP_VERSION=$("$COOL_PYTHON" -c 'import numpy; print(numpy.__version__)')
CV_VERSION=$("$COOL_PYTHON" -c 'import cv2; print(cv2.__version__)')
if [[ ! "$CV_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  "$COOL_PYTHON" -c 'import json,cv2;from pathlib import Path;Path("results/matching-blocker.json").write_text(json.dumps({"status":"blocked_unmatched_opencv_base","actual_version":cv2.__version__,"reason":"Development version does not identify an upstream revision; no arbitrary standard base selected."}))'
  exit 0
fi
WHEEL_VERSION=$(control/bin/python - "$CV_VERSION" <<'PY'
import json,sys,urllib.request
from pathlib import Path
version=sys.argv[1]
with urllib.request.urlopen('https://pypi.org/pypi/opencv-python/json',timeout=30) as r:data=json.load(r)
available=[v for v,files in data['releases'].items() if v.startswith(version+'.') and all(x.isdecimal() for x in v.split('.')) and any(f['filename'].endswith('aarch64.whl') for f in files)]
if not available:raise ValueError('No ARM wheel with the exact stable OpenCV base')
selected=max(available,key=lambda v:tuple(int(x) for x in v.split('.')))
Path('results/standard-wheel-selection.json').write_text(json.dumps({'pypi':'https://pypi.org/pypi/opencv-python/json','opencv_base':version,'wheel_version':selected}))
print(selected)
PY
)
env -u LD_LIBRARY_PATH -u PYTHONPATH standard/bin/pip -q install --only-binary=:all: numpy=="$NP_VERSION" opencv-python=="$WHEEL_VERSION"
standard/bin/pip freeze > results/requirements-standard.txt
"$COOL_PYTHON" -m pip freeze > results/requirements-cool.txt
export PYTHONPATH=/opt/kiroclops-probe/code:${PYTHONPATH:-}
set +e
standard/bin/python -u code/scripts/phase3b_paired_benchmark.py paired --corpus corpus --reference reference --output results/paired --standard /opt/kiroclops-probe/standard/bin/python --cool "$COOL_PYTHON" --sync-command /opt/kiroclops-probe/control/bin/python /opt/kiroclops-probe/transfer.py put
CODE=$?
set -e
echo "$CODE" > results/paired.exit
control/bin/python transfer.py put
'''.replace('BUCKET', bucket).replace('PREFIX', prefix)
    branch = r'''
set +e
"$COOL_PYTHON" -u code/scripts/phase3b_cool_validation.py --corpus corpus --reference reference --output results/cool-validation --sync-command /opt/kiroclops-probe/control/bin/python /opt/kiroclops-probe/transfer.py put
CODE=$?
set -e
echo "$CODE" > results/cool-validation.exit
control/bin/python transfer.py put
exit "$CODE"
''' if cool_only else ''
    return script.replace('COOL_ONLY_BRANCH',branch)


def start(corpus, output, reuse_bundle=None, cool_only=False, reference=None, ami='ami-033e481a24f94c8cb'):
    corpus, output = Path(corpus).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Fresh evidence directory required')
    from kiroclops.cloud.corpus import verify_corpus
    from kiroclops.cloud.identity import analysis_sources
    from kiroclops.cloud.benchmark_reference import validate_reference
    if reference is None:
        raise ValueError('Explicit source-bound current reference is required')
    previous = read_json(Path(reuse_bundle)/'state.json') if reuse_bundle else None
    manifest = read_json(corpus/'manifest.json') if previous else verify_corpus(corpus)
    if previous:
        from kiroclops.visual.schema import digest
        if digest({k:v for k,v in manifest.items() if k!='corpus_sha256'}) != previous['corpus_sha256']:
            raise ValueError('Reused archive has another registered corpus')
        if file_sha256(Path(reuse_bundle)/'bundle.tar') != previous['bundle_sha256']:
            raise ValueError('Previously verified archive changed')
        fixed_inputs={rel:sha for rel,sha in previous['analysis_bundle_sources'].items() if rel.startswith('kiroclops/visual/')}
        if fixed_inputs != analysis_sources() or any(file_sha256(ROOT/rel)!=sha for rel,sha in fixed_inputs.items()):
            raise ValueError('Visual analysis sources changed since archive creation')
    validated_reference=validate_reference(reference,manifest,analysis_sources())
    image = aws('ec2','describe-images',image_ids=[ami])['Images'][0]
    if not any(p['ProductCodeId']=='aajkmdd4qo3r7yhqg61a7aah9' for p in image['ProductCodes']):
        raise ValueError('Unexpected Marketplace AMI product')
    output.mkdir(parents=True)
    account = aws('sts','get-caller-identity')['Account']
    run = 'phase3b-cool-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    bucket = f'kiroclops-probe-{account}-{run}'
    vpc = aws('ec2','describe-vpcs',filters=[{'Name':'is-default','Values':['true']}])['Vpcs'][0]['VpcId']
    subnets = aws('ec2','describe-subnets',filters=[{'Name':'vpc-id','Values':[vpc]},{'Name':'default-for-az','Values':['true']}])['Subnets']
    subnet = next(s['SubnetId'] for s in subnets if s['MapPublicIpOnLaunch'])
    state = {'schema_version':1,'region':'us-east-1','run':run,'stack':run,'bucket':bucket,'status':'preparing',
             'ami':ami,'ami_name':image['Name'],'instance_type':'c8g.xlarge','engine':'standard-and-cool-paired',
             'corpus_sha256':manifest['corpus_sha256'],'compute_hourly_usd':.15952,'cool_hourly_usd':.01,
             'maximum_instance_hours':.92,'budget_usd':10,'internal_evaluation_only':True,
             'subnet':subnet,'vpc':vpc,'cool_only':cool_only,
             'reference_sha256':validated_reference['reference_sha256']}
    write_json(output/'state.json',state)
    aws('s3api','create-bucket',bucket=bucket)
    aws('s3api','put-public-access-block',bucket=bucket,public_access_block_configuration={k:True for k in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')})
    aws('s3api','put-bucket-encryption',bucket=bucket,server_side_encryption_configuration={'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})
    aws('s3api','put-bucket-policy',bucket=bucket,policy=json.dumps({'Version':'2012-10-17','Statement':[
        {'Sid':'TLSOnly','Effect':'Deny','Principal':'*','Action':'s3:*',
         'Resource':[f'arn:aws:s3:::{bucket}',f'arn:aws:s3:::{bucket}/*'],
         'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}))
    aws('s3api','put-bucket-lifecycle-configuration',bucket=bucket,lifecycle_configuration={'Rules':[{'ID':'TemporaryProbe','Status':'Enabled','Filter':{'Prefix':run+'/'},'Expiration':{'Days':1},'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]})
    archive = output/'bundle.tar'
    source_files = sorted((ROOT/'kiroclops/visual').glob('*.py')) + [ROOT/'kiroclops/__init__.py']
    source_files += [ROOT/'kiroclops/cloud'/n for n in ('__init__.py','identity.py','benchmark.py','corpus.py','equivalence.py','benchmark_reference.py')]
    source_files += [ROOT/'scripts/phase3b_paired_benchmark.py']
    if cool_only:
        source_files.append(ROOT/'scripts/phase3b_cool_validation.py')
    state['analysis_bundle_sources'] = {p.relative_to(ROOT).as_posix():file_sha256(p) for p in source_files}
    if previous:
        # Reuse only immutable corpus members; write current harness once.
        # The original archive's bytes and analysis inputs were checked above.
        with tarfile.open(Path(reuse_bundle)/'bundle.tar','r') as old, tarfile.open(archive,'w') as tar:
            for member in old:
                if member.name.startswith('corpus/'):
                    tar.addfile(member,old.extractfile(member) if member.isfile() else None)
            for p in source_files:
                tar.add(p,arcname='code/'+p.relative_to(ROOT).as_posix())
        state['reused_verified_archive_from']=str(Path(reuse_bundle).resolve())
        state['worker_still_verifies_corpus_before_analysis']=True
    else:
        with tarfile.open(archive,'w') as tar:
            for item in manifest['items']:
                tar.add(corpus/item['id'],arcname='corpus/'+item['id'])
            tar.add(corpus/'manifest.json',arcname='corpus/manifest.json')
            for p in source_files:
                tar.add(p,arcname='code/'+p.relative_to(ROOT).as_posix())
    with tarfile.open(archive,'a') as tar:
        reference=Path(reference)
        tar.add(reference/'reference.json',arcname='reference/reference.json')
        for row in validated_reference['items']:
            tar.add(reference/row['measurement_path'],arcname='reference/'+row['measurement_path'])
    state['bundle_sha256'] = file_sha256(archive)
    aws('s3api','put-object',bucket=bucket,key=run+'/bundle.tar',body=str(archive))
    spec = template(bucket,ami,subnet,vpc,run,code_variant='local-optimization')
    guard=spec['Resources']['Guard']['Properties']['Code']
    guard['ZipFile']=guard['ZipFile'].replace('>4500','>2700')
    worker = spec['Resources']['Worker']['Properties']
    worker['UserData'] = {'Fn::Base64':bootstrap(bucket,run,cool_only)}
    worker['BlockDeviceMappings'][0]['Ebs']['VolumeSize'] = image['BlockDeviceMappings'][0]['Ebs']['VolumeSize']
    write_json(output/'stack.json',spec)
    write_json(output/'state.json',state)
    response = aws('cloudformation','create-stack',stack_name=run,template_body='file://'+str(output/'stack.json'),
                   capabilities=['CAPABILITY_IAM'],timeout_in_minutes=15,on_failure='DELETE',
                   tags=[{'Key':'Project','Value':'KiroClops'},{'Key':'Purpose','Value':'phase3b-feasibility'}])
    state.update(status='creating',stack_id=response['StackId'],created_at=datetime.now(timezone.utc).isoformat())
    write_json(output/'state.json',state)
    print(json.dumps({'status':state['status'],'stack':run,'evidence':str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['start','status','cleanup'])
    parser.add_argument('--corpus',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-bundle',type=Path)
    parser.add_argument('--cool-only',action='store_true')
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--ami',choices=['ami-033e481a24f94c8cb','ami-01db31139bc5615d8'],default='ami-033e481a24f94c8cb')
    args = parser.parse_args()
    if args.command=='start':start(args.corpus,args.output,args.reuse_bundle,args.cool_only,args.reference,args.ami)
    elif args.command=='status':status(args.output)
    else:cleanup(args.output)
