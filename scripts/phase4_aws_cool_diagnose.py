"""Boot a bounded COOL worker that runs the job-path diagnostic before its worker service starts.

Phase 4 AWS addition after the V6 freeze; see ``phase4_aws_cool_diag_worker.py`` for
what runs on the instance. This side starts the COOL deployment (experiment mode:
60-minute shutdown, 75-minute independent guard), uploads the diagnostic script and
one V6 package exactly as the service client would stage it, waits for the report and
downloads it. Clean up afterwards with ``services.analysis_api.deploy cleanup``.

    python scripts/phase4_aws_cool_diagnose.py --output artifacts/phase4-aws/v6-cool-diagnostic-01
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.corpus import rebind  # noqa: E402
from kiroclops.cloud.results import request_binding  # noqa: E402
from kiroclops.cloud.transport import pack  # noqa: E402
from kiroclops.visual.package import load_package  # noqa: E402
from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from scripts.phase4_aws_deploy import start_cool  # noqa: E402
from services.analysis_api.deploy import aws  # noqa: E402

RECORD = Path('C:/kc3d/p/v6p01-cal/reference/realistic-suite-panel-calibration-reverse-w960-r1/record.json')
WAIT_S = 20*60

BEFORE_WORKER = '''APT_PACKAGES
mkdir -p /srv/kiroclops-diag
chmod 711 /srv/kiroclops-diag
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  venv/bin/python -c "import boto3; s=boto3.client('s3'); [s.download_file('BUCKET', 'service/diagnostic/'+n, '/srv/kiroclops-diag/'+n) for n in ('diag.py', 'input.zip', 'request.json')]" && break
  sleep 20
done
PYTHONPATH=/opt/kiroclops-service/code timeout 900 venv/bin/python /srv/kiroclops-diag/diag.py /srv/kiroclops-diag BUCKET > /srv/kiroclops-diag/diag.log 2>&1 || true
venv/bin/python -c "import boto3; boto3.client('s3').upload_file('/srv/kiroclops-diag/diag.log', 'BUCKET', 'service/diagnostic/diag.log')" || true
'''


def stage(output):
    """The package archive and remote request, built as RemoteAnalysisBackend._submit builds them."""
    record = read_json(RECORD)
    manifest = load_package(record['package'])
    original = request_binding(manifest['package_sha256'], record['selection'], record['suite_event_contract'], '3'*64)
    receipt = pack(record['package'], output/'input.zip')
    write_json(output/'request.json', rebind(original, original['package_sha256'], receipt['package_sha256']))
    write_json(output/'package-receipt.json', receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True)
    parser.add_argument('--diag-script', default='scripts/phase4_aws_cool_diag_worker.py')
    parser.add_argument('--apt', nargs='*', default=[], help='extra Ubuntu packages, e.g. strace')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    staging = output.with_name(output.name+'-staging')
    staging.mkdir(parents=True, exist_ok=False)
    stage(staging)
    apt = 'apt-get install -y -qq '+' '.join(args.apt)+' || true' if args.apt else ''
    start_cool(output, before_worker=lambda bucket: BEFORE_WORKER.replace('BUCKET', bucket).replace('APT_PACKAGES', apt))
    state = read_json(output/'state.json')
    for local, name in ((ROOT/args.diag_script, 'diag.py'),
                        (staging/'input.zip', 'input.zip'), (staging/'request.json', 'request.json')):
        aws('s3api', 'put-object', bucket=state['bucket'], key='service/diagnostic/'+name, body=str(local))
    print(json.dumps({'stack': state['stack'], 'staged': True}), flush=True)
    deadline = time.monotonic()+WAIT_S
    while time.monotonic() < deadline:
        try:
            aws('s3api', 'head-object', bucket=state['bucket'], key='service/diagnostic/diag.log')
            break
        except RuntimeError:
            time.sleep(20)
    else:
        raise SystemExit('No diagnostic log within the wait limit; the partial report may still exist')
    for name in ('report.json', 'diag.log'):
        subprocess.run(['aws', 's3', 'cp', f"s3://{state['bucket']}/service/diagnostic/{name}", str(output/name),
                        '--region', 'us-east-1', '--only-show-errors'], check=False, timeout=120)
    if not (output/'report.json').exists():
        raise SystemExit('No report.json; see diag.log')
    report = read_json(output/'report.json')
    for name, row in report.get('steps', {}).items():
        print(name, 'ok' if row['ok'] else 'FAILED', row.get('seconds'), flush=True)
    for name, row in report.get('variants', {}).items():
        lines = (row.get('stdout') or '').strip().splitlines()+(row.get('stderr') or '').strip().splitlines()
        print(name, 'returncode', row.get('returncode'), '|', lines[-1][:160] if lines else '', flush=True)


if __name__ == '__main__':
    main()
