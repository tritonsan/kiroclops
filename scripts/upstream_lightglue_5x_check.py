"""Build the current OpenCV 5.x branch on a bounded AWS worker and run a LightGlue reproduction on it.

Support for an upstream OpenCV bug report about ``cv2.LightGlueMatcher`` in 5.0.0 (see
``kiroclops/visual/features5.py``); no KiroClops result depends on it. The worker boots like
``phase4_aws_cool_scaling.py --engines stock`` (standard Ubuntu 24.04, experiment mode:
60-minute shutdown, 75-minute independent guard; the live judge deployment is not touched).
During boot it clones the 5.x branch (depth 1), builds core, imgproc, geometry, flann, dnn,
features and the Python bindings (with ``-g1`` line tables), and runs the reproduction script
twice: with that build and with the ``opencv-python`` 5.0.0.93 wheel of the worker's own
environment; then it runs the two failing calls and the small-then-large call sequence under
gdb for a backtrace. The outputs, the
5.x commit and the build log are copied to ``<output>/runs``; the deployment is then deleted.

    python scripts/upstream_lightglue_5x_check.py --instance-type c8g.4xlarge --repro <lightglue_repro.py> \
        --model .tools/models/aliked_lightglue.onnx --output artifacts/phase4-v6/lightglue-5x-check-01
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import file_sha256, read_json, write_json  # noqa: E402

WAIT_S = 58*60
BUILD = '''WORKER_PY="$(pwd)/venv/bin/python"
OCV=/srv/opencv5x
mkdir -p $OCV
for attempt in $(seq 1 30); do
  $WORKER_PY -c "import boto3; s=boto3.client('s3'); [s.download_file('BUCKET', 'service/benchmark/'+n, '/srv/opencv5x/'+n) for n in ('lightglue_repro.py', 'aliked_lightglue.onnx')]" && break
  sleep 20
done
apt-get -o DPkg::Lock::Timeout=600 install -y -qq build-essential cmake ninja-build git python3-dev gdb > $OCV/build.log 2>&1 || true
git clone -q --depth 1 --branch 5.x https://github.com/opencv/opencv.git $OCV/src >> $OCV/build.log 2>&1 || true
git -C $OCV/src log -1 --format='%H %cI %s' > $OCV/commit.txt 2>&1 || true
cmake -S $OCV/src -B $OCV/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS=-g1 -DCMAKE_CXX_FLAGS=-g1 -DBUILD_SHARED_LIBS=OFF -DBUILD_LIST=core,imgproc,geometry,flann,dnn,features,python3 -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DOCS=OFF -DBUILD_opencv_apps=OFF -DWITH_IPP=OFF -DWITH_OPENCL=OFF -DPYTHON3_EXECUTABLE=$WORKER_PY -DOPENCV_PYTHON3_INSTALL_PATH=$OCV/python -DCMAKE_INSTALL_PREFIX=$OCV/install > $OCV/cmake.log 2>&1 || true
cmake --build $OCV/build -j $(nproc) >> $OCV/build.log 2>&1 || echo "build failed" >> $OCV/build.log
cmake --install $OCV/build >> $OCV/build.log 2>&1 || true
PYTHONPATH=$OCV/python timeout 900 $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx > $OCV/lightglue-5x.txt 2>&1 || echo "repro exit status $?" >> $OCV/lightglue-5x.txt
timeout 900 $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx > $OCV/lightglue-5.0.0-wheel.txt 2>&1 || echo "repro exit status $?" >> $OCV/lightglue-5.0.0-wheel.txt
for steps in 2x64 33x2; do
  PYTHONPATH=$OCV/python timeout 600 gdb -q -batch -ex run -ex 'bt 30' --args $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx $steps > $OCV/gdb-5x-$steps.txt 2>&1 || true
  timeout 600 gdb -q -batch -ex run -ex 'bt 30' --args $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx $steps > $OCV/gdb-5.0.0-wheel-$steps.txt 2>&1 || true
done
PYTHONPATH=$OCV/python timeout 600 gdb -q -batch -ex run -ex 'bt 30' --args $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx 8x8 50x200 > $OCV/gdb-5x-8x8-50x200.txt 2>&1 || true
timeout 600 gdb -q -batch -ex run -ex 'bt 30' --args $WORKER_PY $OCV/lightglue_repro.py $OCV/aliked_lightglue.onnx 8x8 50x200 > $OCV/gdb-5.0.0-wheel-8x8-50x200.txt 2>&1 || true
grep -m1 -i flags /proc/cpuinfo > $OCV/cpu-flags.txt 2>&1 || true
tail -c 200000 $OCV/build.log > $OCV/build-tail.log || true
$WORKER_PY -c "import boto3, glob, os; s=boto3.client('s3'); [s.upload_file(p, 'BUCKET', 'service/benchmark/out/'+os.path.basename(p)) for p in glob.glob('/srv/opencv5x/*.txt')+['/srv/opencv5x/cmake.log', '/srv/opencv5x/build-tail.log']]" || true
$WORKER_PY -c "import boto3; boto3.client('s3').put_object(Bucket='BUCKET', Key='service/benchmark/done', Body=b'done')" || true
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True)
    parser.add_argument('--instance-type', required=True)
    parser.add_argument('--arch', choices=('arm64', 'amd64'), default='arm64')
    parser.add_argument('--repro', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    if '${' in BUILD:
        raise ValueError('CloudFormation Fn::Sub owns every ${...}')
    output = Path(args.output).resolve()
    from scripts.phase4_aws_cool_scaling import boot, instance_record
    from services.analysis_api.deploy import aws, cleanup
    boot(output, args.instance_type, ['stock'], args.arch, lambda bucket: BUILD.replace('BUCKET', bucket))
    state = read_json(output/'state.json')
    state.update(purpose='upstream-lightglue-5x-check', instance_type=args.instance_type, arch=args.arch,
                 repro_sha256=file_sha256(args.repro), model_sha256=file_sha256(args.model))
    write_json(output/'state.json', state)
    try:
        for local, name in ((args.repro, 'lightglue_repro.py'), (args.model, 'aliked_lightglue.onnx')):
            aws('s3api', 'put-object', bucket=state['bucket'], key='service/benchmark/'+name, body=str(local))
        print(json.dumps({'stack': state['stack'], 'instance_type': args.instance_type, 'staged': True}), flush=True)
        deadline = time.monotonic()+WAIT_S
        done = recorded = False
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
        files = sorted(p.name for p in (output/'runs').glob('*')) if (output/'runs').exists() else []
        print(json.dumps({'done_marker': done, 'files': files}), flush=True)
    finally:
        cleanup(output)
        print(json.dumps({'cleanup': 'requested'}), flush=True)


if __name__ == '__main__':
    main()
