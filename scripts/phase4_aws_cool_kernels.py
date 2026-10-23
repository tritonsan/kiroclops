"""Boot a bounded COOL worker that times OpenCV kernels and the measurement pipeline, COOL against the standard wheel.

Phase 4 AWS addition after the V6 freeze: a post-hoc measurement that changes no recorded
result. Like ``phase4_aws_cool_diagnose.py``, this starts a separate COOL deployment
(experiment mode: 60-minute shutdown, 75-minute independent guard); the live judge
deployment is not touched. During boot, before the worker service starts, the instance
creates a second virtual environment with the standard ``opencv-python`` 5.0.0 wheel and
runs ``phase4_aws_cool_kernels_worker.py`` in three alternating rounds (COOL first, then
standard first, then COOL first) on the same frames and recordings. Clean up afterwards with
``python -m services.analysis_api.deploy cleanup --output <output>``.

    python scripts/phase4_aws_cool_kernels.py --output artifacts/phase4-aws/cool-kernels-01
    python scripts/phase4_aws_cool_kernels.py --output <scratch folder> --stage-only
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import read_json, write_json  # noqa: E402

PANEL = Path('C:/kc3d/p/v6p01-cal/reference/realistic-suite-panel-calibration-reverse-w960-r1/record.json')
SAMPLES = ROOT/'submission'/'sample-corpus'
FRAMES = 10
WAIT_S = 45*60

BEFORE_WORKER = '''WORKER_PY="$(pwd)/venv/bin/python"
BENCH=/srv/kiroclops-bench
mkdir -p $BENCH
chmod 711 $BENCH
for attempt in $(seq 1 30); do
  $WORKER_PY -c "import boto3; s=boto3.client('s3'); [s.download_file('BUCKET', 'service/benchmark/'+n, '/srv/kiroclops-bench/'+n) for n in ('bench.py', 'input.zip')]" && break
  sleep 20
done
$WORKER_PY -c "import zipfile; zipfile.ZipFile('/srv/kiroclops-bench/input.zip').extractall('/srv/kiroclops-bench/input')" > $BENCH/bench.log 2>&1 || true
BASE_PY="$($WORKER_PY -c 'import sys; print(sys._base_executable)')"
$BASE_PY -m venv $BENCH/stock >> $BENCH/bench.log 2>&1 || true
$BENCH/stock/bin/pip install -q --disable-pip-version-check opencv-python==5.0.0.93 numpy==2.2.6 >> $BENCH/bench.log 2>&1 || true
for r in 1 2 3; do
  if [ $((r % 2)) -eq 1 ]; then ORDER="cool stock"; else ORDER="stock cool"; fi
  for e in $ORDER; do
    if [ "$e" = cool ]; then PY=$WORKER_PY; else PY=$BENCH/stock/bin/python; fi
    PYTHONPATH=/opt/kiroclops-service/code timeout 900 $PY $BENCH/bench.py --engine $e --round $r --input $BENCH/input --out $BENCH/result-$e-$r.json >> $BENCH/bench.log 2>&1 || echo "engine $e round $r failed" >> $BENCH/bench.log
  done
done
$WORKER_PY -c "import boto3, glob, os; s=boto3.client('s3'); [s.upload_file(p, 'BUCKET', 'service/benchmark/out/'+os.path.basename(p)) for p in glob.glob('/srv/kiroclops-bench/*.json')]" >> $BENCH/bench.log 2>&1 || true
$WORKER_PY -c "import boto3; boto3.client('s3').upload_file('/srv/kiroclops-bench/bench.log', 'BUCKET', 'service/benchmark/out/bench.log'); boto3.client('s3').put_object(Bucket='BUCKET', Key='service/benchmark/done', Body=b'done')" || true
'''


def stage(staging):
    """input.zip: frames for the kernel timings and three recordings with their selection and contract."""
    record = read_json(PANEL)
    package = Path(record['package'])
    root = staging/'input'
    (root/'frames').mkdir(parents=True)
    for frame in read_json(package/'manifest.json')['frames'][:FRAMES]:
        shutil.copy2(package/frame['path'], root/'frames'/Path(frame['path']).name)
    items = []
    shutil.copytree(package, root/'panel'/'package')
    write_json(root/'panel'/'selection.json', record['selection'])
    write_json(root/'panel'/'contract.json', record['suite_event_contract'])
    items.append({'name': 'panel-calibration-reverse-w960', 'package': 'panel/package', 'selection': 'panel/selection.json',
                  'contract': 'panel/contract.json'})
    for name in ('game-reference-h03', 'svg-three-to-four-w960-r1'):
        shutil.copytree(SAMPLES/name/'package', root/name/'package')
        shutil.copy2(SAMPLES/name/'selection.json', root/name/'selection.json')
        shutil.copy2(SAMPLES/name/'contract.json', root/name/'contract.json')
        items.append({'name': name, 'package': f'{name}/package', 'selection': f'{name}/selection.json', 'contract': f'{name}/contract.json'})
    write_json(root/'packages.json', items)
    with zipfile.ZipFile(staging/'input.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                bundle.write(path, path.relative_to(root).as_posix())
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True)
    parser.add_argument('--stage-only', action='store_true', help='build the input folder and zip, start nothing')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    staging = output.with_name(output.name+'-staging')
    staging.mkdir(parents=True, exist_ok=False)
    stage(staging)
    if args.stage_only:
        print(json.dumps({'staging': str(staging), 'zip_bytes': (staging/'input.zip').stat().st_size}))
        return
    from scripts.phase4_aws_deploy import start_cool
    from services.analysis_api.deploy import aws
    start_cool(output, before_worker=lambda bucket: BEFORE_WORKER.replace('BUCKET', bucket))
    state = read_json(output/'state.json')
    for local, name in ((ROOT/'scripts'/'phase4_aws_cool_kernels_worker.py', 'bench.py'), (staging/'input.zip', 'input.zip')):
        aws('s3api', 'put-object', bucket=state['bucket'], key='service/benchmark/'+name, body=str(local))
    print(json.dumps({'stack': state['stack'], 'staged': True}), flush=True)
    deadline = time.monotonic()+WAIT_S
    while time.monotonic() < deadline:
        try:
            aws('s3api', 'head-object', bucket=state['bucket'], key='service/benchmark/done')
            break
        except RuntimeError:
            time.sleep(30)
    else:
        raise SystemExit('No completion marker within the wait limit; partial results may exist under service/benchmark/out/')
    subprocess.run(['aws', 's3', 'cp', f"s3://{state['bucket']}/service/benchmark/out/", str(output/'runs'), '--recursive',
                    '--region', 'us-east-1', '--only-show-errors'], check=False, timeout=300)
    runs = sorted((output/'runs').glob('result-*.json'))
    print(json.dumps({'runs': [p.name for p in runs]}), flush=True)


if __name__ == '__main__':
    main()
