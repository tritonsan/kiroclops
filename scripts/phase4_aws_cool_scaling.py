"""Boot one bounded worker of a chosen instance type and run the scaling benchmark on it.

Phase 4 AWS addition after the V6 freeze: a post-hoc measurement that changes no recorded
result. It extends ``phase4_aws_cool_kernels.py`` to other instance sizes and to x86. Like
that script it starts a separate deployment (experiment mode: 60-minute shutdown, 75-minute
independent guard); the live judge deployment is not touched. With ``--engines cool,stock``
the worker boots from the COOL Marketplace image and the benchmark alternates COOL and the
standard ``opencv-python`` 5.0.0 wheel; with ``--engines stock`` it boots the standard
Ubuntu 24.04 worker (``--arch amd64`` for x86) and times the standard wheel alone. Each
engine runs three times, in alternating order. Every result is uploaded as soon as it
exists, copied to ``<output>/runs``, and the deployment is then deleted with
``services.analysis_api.deploy.cleanup``. With ``--lightglue-repro`` and ``--lightglue-model``
the worker afterwards runs that reproduction script once per engine (evidence for an
upstream OpenCV bug report; it runs after every timing, so it cannot affect them).

    python scripts/phase4_aws_cool_scaling.py --stage-only --staging <scratch folder>
    python scripts/phase4_aws_cool_scaling.py --instance-type c8g.4xlarge --engines cool,stock \
        --input-zip <scratch folder>/input.zip --output artifacts/phase4-aws/cool-scaling-01/c8g.4xlarge
    python scripts/phase4_aws_cool_scaling.py --instance-type c8i.xlarge --engines stock --arch amd64 \
        --input-zip <scratch folder>/input.zip --output artifacts/phase4-aws/cool-scaling-01/c8i.xlarge
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import file_sha256, read_json, write_json  # noqa: E402

ROUNDS = 3
WAIT_S = 58*60
TILES = {'1080p': ((2, 2), (1080, 1920)), '2160p': ((4, 4), (2160, 3840))}

BENCH = '''WORKER_PY="$(pwd)/venv/bin/python"
BENCH=/srv/kiroclops-bench
mkdir -p $BENCH
chmod 711 $BENCH
for attempt in $(seq 1 30); do
  $WORKER_PY -c "import boto3; s=boto3.client('s3'); [s.download_file('BUCKET', 'service/benchmark/'+n, '/srv/kiroclops-bench/'+n) for n in ('bench.py', 'input.zip')]" && break
  sleep 20
done
$WORKER_PY -c "import zipfile; zipfile.ZipFile('/srv/kiroclops-bench/input.zip').extractall('/srv/kiroclops-bench/input')" > $BENCH/bench.log 2>&1 || true
$WORKER_PY -c "import boto3; s=boto3.client('s3'); [s.download_file('BUCKET', 'service/benchmark/'+n, '/srv/kiroclops-bench/'+n) for n in ('lightglue_repro.py', 'aliked_lightglue.onnx')]" >> $BENCH/bench.log 2>&1 || true
lscpu > $BENCH/lscpu.txt 2>&1 || true
STOCK_SETUP
for r in ROUND_LIST; do
  if [ $((r % 2)) -eq 1 ]; then ORDER="FIRST_ORDER"; else ORDER="SECOND_ORDER"; fi
  for e in $ORDER; do
    if [ "$e" = cool ]; then PY=$WORKER_PY; else PY=STOCK_PY; fi
    PYTHONPATH=/opt/kiroclops-service/code timeout 1200 $PY $BENCH/bench.py --engine $e --round $r --input $BENCH/input --out $BENCH/result-$e-$r.json >> $BENCH/bench.log 2>&1 || echo "engine $e round $r failed" >> $BENCH/bench.log
    UPLOAD
  done
done
if [ -f $BENCH/lightglue_repro.py ] && [ -f $BENCH/aliked_lightglue.onnx ]; then
  for e in ENGINE_LIST; do
    if [ "$e" = cool ]; then PY=$WORKER_PY; else PY=STOCK_PY; fi
    timeout 600 $PY $BENCH/lightglue_repro.py $BENCH/aliked_lightglue.onnx > $BENCH/lightglue-$e.txt 2>&1 || echo "repro exit status $?" >> $BENCH/lightglue-$e.txt
  done
  UPLOAD
fi
$WORKER_PY -c "import boto3; boto3.client('s3').put_object(Bucket='BUCKET', Key='service/benchmark/done', Body=b'done')" || true
'''
UPLOAD = '''$WORKER_PY -c "import boto3, glob, os; s=boto3.client('s3'); [s.upload_file(p, 'BUCKET', 'service/benchmark/out/'+os.path.basename(p)) for p in glob.glob('/srv/kiroclops-bench/*.json')+glob.glob('/srv/kiroclops-bench/*.txt')+['/srv/kiroclops-bench/bench.log']]" || true'''
STOCK_VENV = '''BASE_PY="$($WORKER_PY -c 'import sys; print(sys._base_executable)')"
$BASE_PY -m venv $BENCH/stock >> $BENCH/bench.log 2>&1 || true
$BENCH/stock/bin/pip install -q --disable-pip-version-check opencv-python==5.0.0.93 numpy==2.2.6 >> $BENCH/bench.log 2>&1 || true'''


def bench_text(engines):
    both = 'cool' in engines
    text = (BENCH.replace('STOCK_SETUP', STOCK_VENV if both else '').replace('UPLOAD', UPLOAD)
            .replace('ENGINE_LIST', 'cool stock' if both else 'stock')
            .replace('ROUND_LIST', ' '.join(str(r) for r in range(1, ROUNDS+1)))
            .replace('FIRST_ORDER', 'cool stock' if both else 'stock')
            .replace('SECOND_ORDER', 'stock cool' if both else 'stock')
            .replace('STOCK_PY', '$BENCH/stock/bin/python' if both else '$WORKER_PY'))
    if '${' in text:
        raise ValueError('CloudFormation Fn::Sub owns every ${...}')
    return text


def stage(staging):
    """The input of ``phase4_aws_cool_kernels.py`` plus 1080p and 2160p frames tiled from its first two frames."""
    import cv2
    import numpy as np
    from scripts.phase4_aws_cool_kernels import stage as stage_kernels
    root = stage_kernels(staging)
    frames = sorted((root/'frames').glob('*.png'))[:2]
    (root/'tiles').mkdir()
    for size, ((rows, columns), (height, width)) in TILES.items():
        for i, path in enumerate(frames):
            image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            tiled = np.tile(image, (rows, columns, 1))[:height, :width]
            if tiled.shape[:2] != (height, width):
                raise ValueError(f'Tiles do not cover {width} x {height}')
            ok, encoded = cv2.imencode('.png', tiled)
            if not ok:
                raise ValueError('PNG encoding failed')
            (root/'tiles'/f'{size}-{i}.png').write_bytes(encoded.tobytes())
    with zipfile.ZipFile(staging/'input.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                bundle.write(path, path.relative_to(root).as_posix())
    return staging/'input.zip'


def boot(output, instance_type, engines, arch, bucket_text):
    from scripts.phase4_aws_deploy import APT_WAIT, UNIT_MARKER, start_cool
    from services.analysis_api import deploy
    real_aws, real_template = deploy.aws, deploy.template

    def aws(service, operation, **arguments):
        if (service, operation) == ('ssm', 'get-parameter') and arch == 'amd64':
            arguments['name'] = arguments['name'].replace('/arm64/', '/amd64/')
        return real_aws(service, operation, **arguments)

    def template(bucket, ami, subnet, vpc, prefix, *, invited_beta=False):
        spec = real_template(bucket, ami, subnet, vpc, prefix, invited_beta=invited_beta)
        worker = spec['Resources']['Worker']['Properties']
        if worker.get('InstanceType') != 'c8g.xlarge':
            raise ValueError('Unexpected worker instance type in the template')
        worker['InstanceType'] = instance_type
        if 'cool' not in engines:
            # The standard path gets the boot fixes and the benchmark text that start_cool adds for COOL.
            user_data = worker['UserData']['Fn::Base64']
            for command in ('apt-get update -qq\n', 'apt-get install -y -qq '):
                if user_data['Fn::Sub'].count(command) != 1:
                    raise ValueError('Expected one '+command.strip())
                waiting = APT_WAIT if command.startswith('apt-get update') else ''
                user_data['Fn::Sub'] = user_data['Fn::Sub'].replace(
                    command, waiting+command.replace('apt-get ', 'apt-get -o DPkg::Lock::Timeout=600 ', 1))
            if user_data['Fn::Sub'].count(UNIT_MARKER) != 1:
                raise ValueError('The worker unit marker must exist once')
            user_data['Fn::Sub'] = user_data['Fn::Sub'].replace(UNIT_MARKER, bucket_text(bucket)+UNIT_MARKER)
        spec['Description'] += f'; post-hoc scaling benchmark on {instance_type}'
        return spec

    deploy.aws, deploy.template = aws, template
    try:
        if 'cool' in engines:
            start_cool(output, before_worker=bucket_text)
        else:
            deploy.start(output)
    finally:
        deploy.aws, deploy.template = real_aws, real_template


def instance_record(state):
    """Type, image, zone and times of the worker, without network details."""
    from services.analysis_api.deploy import aws
    resources = aws('cloudformation', 'describe-stack-resources', stack_name=state['stack'])['StackResources']
    worker = next(r['PhysicalResourceId'] for r in resources if r['LogicalResourceId'] == 'Worker')
    instance = aws('ec2', 'describe-instances', instance_ids=[worker])['Reservations'][0]['Instances'][0]
    return {'instance_type': instance['InstanceType'], 'image_id': instance['ImageId'], 'architecture': instance['Architecture'],
            'availability_zone': instance['Placement']['AvailabilityZone'], 'launch_time': instance['LaunchTime'],
            'state': instance['State']['Name'], 'cpu_options': instance.get('CpuOptions'),
            'product_codes': [p['ProductCodeId'] for p in instance.get('ProductCodes', [])],
            'recorded_at': datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output')
    parser.add_argument('--instance-type')
    parser.add_argument('--engines', choices=('cool,stock', 'stock'))
    parser.add_argument('--arch', choices=('arm64', 'amd64'), default='arm64')
    parser.add_argument('--input-zip', type=Path)
    parser.add_argument('--lightglue-repro', type=Path, help='optional reproduction script, run once per engine at the end')
    parser.add_argument('--lightglue-model', type=Path, help='aliked_lightglue.onnx for --lightglue-repro')
    parser.add_argument('--stage-only', action='store_true', help='build the input folder and zip, start nothing')
    parser.add_argument('--staging', type=Path, help='folder for --stage-only')
    args = parser.parse_args()
    if args.stage_only:
        args.staging.mkdir(parents=True, exist_ok=False)
        bundle = stage(args.staging)
        print(json.dumps({'input_zip': str(bundle), 'bytes': bundle.stat().st_size, 'sha256': file_sha256(bundle)}))
        return
    if not (args.output and args.instance_type and args.engines and args.input_zip):
        parser.error('--output, --instance-type, --engines and --input-zip are required')
    if 'cool' in args.engines and args.arch != 'arm64':
        parser.error('COOL runs on Graviton (arm64) only')
    output = Path(args.output).resolve()
    engines = args.engines.split(',')
    from services.analysis_api.deploy import aws, cleanup
    boot(output, args.instance_type, engines, args.arch, lambda bucket: bench_text(engines).replace('BUCKET', bucket))
    state = read_json(output/'state.json')
    state.update(purpose='post-hoc-scaling-benchmark', instance_type=args.instance_type, engines=engines, arch=args.arch,
                 input_zip_sha256=file_sha256(args.input_zip))
    write_json(output/'state.json', state)
    try:
        uploads = [(ROOT/'scripts'/'phase4_aws_cool_scaling_worker.py', 'bench.py'), (args.input_zip, 'input.zip')]
        if args.lightglue_repro and args.lightglue_model:
            # Uploaded first: boot fetches them once, right after it finds bench.py and input.zip.
            uploads[:0] = [(args.lightglue_repro, 'lightglue_repro.py'), (args.lightglue_model, 'aliked_lightglue.onnx')]
        for local, name in uploads:
            aws('s3api', 'put-object', bucket=state['bucket'], key='service/benchmark/'+name, body=str(local))
        print(json.dumps({'stack': state['stack'], 'instance_type': args.instance_type, 'staged': True}), flush=True)
        deadline = time.monotonic()+WAIT_S
        done = False
        recorded = False
        while time.monotonic() < deadline and not done:
            if not recorded:
                try:
                    write_json(output/'instance.json', instance_record(state))
                    recorded = True
                except (RuntimeError, StopIteration, KeyError, IndexError):
                    pass
            try:
                aws('s3api', 'head-object', bucket=state['bucket'], key='service/benchmark/done')
                done = True
            except RuntimeError:
                time.sleep(30)
        subprocess.run(['aws', 's3', 'cp', f"s3://{state['bucket']}/service/benchmark/out/", str(output/'runs'), '--recursive',
                        '--region', 'us-east-1', '--only-show-errors'], check=False, timeout=300)
        runs = sorted(p.name for p in (output/'runs').glob('result-*.json')) if (output/'runs').exists() else []
        print(json.dumps({'instance_type': args.instance_type, 'done_marker': done, 'runs': runs}), flush=True)
    finally:
        cleanup(output)
        print(json.dumps({'instance_type': args.instance_type, 'cleanup': 'requested'}), flush=True)


if __name__ == '__main__':
    main()
