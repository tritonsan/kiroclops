"""Start the KiroClops analysis service on AWS with the standard or the COOL OpenCV engine.

Phase 4 AWS addition after the V6 freeze. ``services.analysis_api.deploy start``
always boots the worker from Ubuntu and installs the standard ``opencv-python``
wheel. With ``--engine cool`` this wrapper runs the same ``start`` (same stack,
bucket, budget seed, API archive and code bundle) but boots the worker from the
COOL Marketplace AMI (Cloud Optimized OpenCV for AWS Graviton). The worker's own
virtual environment then imports COOL's OpenCV and NumPy through a ``.pth`` file
and COOL's library folders are registered with ldconfig, so the sandboxed analysis
process, which receives a clean environment, uses COOL as well. Boot fails unless
``cv2`` resolves under ``/opt/cool/``. No code file of the service is changed.

Every other command (status, login-probe, relogin-probe, cleanup, update-api,
replace-worker) is ``services.analysis_api.deploy`` on the same output folder.

    python scripts/phase4_aws_deploy.py start --engine cool --output artifacts/phase4-aws/v6-cool-service-01
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from services.analysis_api import deploy  # noqa: E402

# Newest image of the subscribed product (Graviton5-COOL-v3, OpenCV 5.1.0-dev); Phase 3B validated it on c8g.xlarge.
COOL_AMI = 'ami-033e481a24f94c8cb'
COOL_PRODUCT = 'aajkmdd4qo3r7yhqg61a7aah9'
STANDARD_SETUP = '''python3 -m venv venv
venv/bin/pip -q install --only-binary=:all: boto3==1.42.79 opencv-python==5.0.0.93 numpy==2.2.6
'''
# CloudFormation Fn::Sub owns every "${...}" in UserData, so this shell text uses only plain $NAME forms.
COOL_SETUP = r'''test -x /opt/cool/venvs/python_3.12/bin/python
python3 -m venv venv
venv/bin/pip -q install --only-binary=:all: boto3==1.42.79
cat > cool-env.sh <<'SH'
set +u
source /opt/cool/venvs/python_3.12/bin/activate >/dev/null 2>&1
printf '%s\n%s\n' "$PYTHONPATH" "$LD_LIBRARY_PATH"
SH
bash cool-env.sh > cool-env.txt
SITE=$(venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
COOL_SITE=$(/opt/cool/venvs/python_3.12/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
sed -n 1p cool-env.txt | tr ':' '\n' | grep -v '^$' > "$SITE/cool.pth" || true
echo "import site; site.addsitedir('$COOL_SITE')" >> "$SITE/cool.pth"
sed -n 2p cool-env.txt | tr ':' '\n' | grep -v '^$' > /etc/ld.so.conf.d/kiroclops-cool.conf || true
ldconfig
env -i PATH=/usr/bin:/bin venv/bin/python -c 'import cv2, numpy; assert cv2.__file__.startswith("/opt/cool/"), cv2.__file__; print(cv2.__version__, numpy.__version__, cv2.__file__)' > cool-import.txt
'''
# COOL's environment check at cv2 import connects to the EC2 instance metadata service; the worker's
# sandbox (PrivateNetwork, AF_UNIX only) made it exit with "Non-AWS Environment Detected" (diagnostics
# v6-cool-diagnostic-01/02). This drop-in matches every "kiro-analysis-<job>" unit the worker starts and
# allows that one address only. Boot then imports cv2 under the worker's exact sandbox properties and
# stops, so the worker never starts, unless the import succeeds; the self-test output goes to S3 first.
COOL_SANDBOX = r'''mkdir -p /etc/systemd/system/kiro-analysis-.service.d
cat > /etc/systemd/system/kiro-analysis-.service.d/cool-imds-only.conf <<'CONF'
[Service]
PrivateNetwork=no
RestrictAddressFamilies=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=169.254.169.254/32
CONF
systemctl daemon-reload
systemd-run --quiet --wait --collect --pipe --unit=kiro-analysis-cool-selftest -p User=kiroanalysis -p Group=kiroanalysis -p PrivateNetwork=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p NoNewPrivileges=yes -p RestrictSUIDSGID=yes -p ProtectKernelTunables=yes -p ProtectKernelModules=yes -p ProtectControlGroups=yes -p MemoryMax=2G -p CPUQuota=400% -p RuntimeMaxSec=120 -p TasksMax=64 -p RestrictAddressFamilies=AF_UNIX /opt/kiroclops-service/venv/bin/python -c 'import cv2; print("cool-sandbox", cv2.__version__, cv2.__file__)' > cool-sandbox-selftest.txt 2>&1 || true
venv/bin/python -c "import boto3; boto3.client('s3').upload_file('/opt/kiroclops-service/cool-sandbox-selftest.txt', '@BUCKET@', 'service/cool-sandbox-selftest.txt')" || true
grep -q '^cool-sandbox ' cool-sandbox-selftest.txt
'''


UNIT_MARKER = "cat > /etc/systemd/system/kiroclops-worker.service <<'UNIT'\n"
# Up to ten minutes for boot-time apt jobs (apt-daily, unattended-upgrades) to release the apt and dpkg locks.
APT_WAIT = r'''for wait in $(seq 1 120); do
  if ! pgrep -f 'apt.systemd.daily|unattended-upgr' >/dev/null && ! fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1; then break; fi
  sleep 5
done
'''


def start_cool(output, *, invited_beta=False, before_worker=None):
    """``before_worker(bucket)`` may return shell text that boot runs after the code bundle is unpacked."""
    image = deploy.aws('ec2', 'describe-images', image_ids=[COOL_AMI])['Images'][0]
    if not any(p['ProductCodeId'] == COOL_PRODUCT for p in image.get('ProductCodes', [])):
        raise ValueError('Unexpected Marketplace AMI product')
    # The COOL root snapshot is larger than the 16 GB Ubuntu volume of the template (attempt 01 failed on it).
    root = next(m for m in image['BlockDeviceMappings'] if m['DeviceName'] == image['RootDeviceName'])
    real_aws, real_template = deploy.aws, deploy.template

    def aws(service, operation, **arguments):
        if (service, operation) == ('ssm', 'get-parameter'):
            return {'Parameter': {'Value': COOL_AMI}}  # replaces the Ubuntu AMI lookup only
        return real_aws(service, operation, **arguments)

    def template(bucket, ami, subnet, vpc, prefix, *, invited_beta=False):
        spec = real_template(bucket, ami, subnet, vpc, prefix, invited_beta=invited_beta)
        user_data = spec['Resources']['Worker']['Properties']['UserData']['Fn::Base64']
        if user_data['Fn::Sub'].count(STANDARD_SETUP) != 1:
            raise ValueError('The standard worker setup lines were not found exactly once')
        user_data['Fn::Sub'] = user_data['Fn::Sub'].replace(STANDARD_SETUP, COOL_SETUP+COOL_SANDBOX.replace('@BUCKET@', bucket))
        # live-cool-01 failed at boot: unattended-upgrades held the dpkg lock during apt-get install. Wait for it.
        for command in ('apt-get update -qq\n', 'apt-get install -y -qq '):
            if user_data['Fn::Sub'].count(command) != 1:
                raise ValueError('Expected one '+command.strip())
            waiting = APT_WAIT if command.startswith('apt-get update') else ''
            user_data['Fn::Sub'] = user_data['Fn::Sub'].replace(
                command, waiting+command.replace('apt-get ', 'apt-get -o DPkg::Lock::Timeout=600 ', 1))
        if before_worker:
            extra = before_worker(bucket)
            if '${' in extra or user_data['Fn::Sub'].count(UNIT_MARKER) != 1:
                raise ValueError('Extra boot text must avoid ${...} and the unit marker must exist once')
            user_data['Fn::Sub'] = user_data['Fn::Sub'].replace(UNIT_MARKER, extra+UNIT_MARKER)
        volume = next(m for m in spec['Resources']['Worker']['Properties']['BlockDeviceMappings']
                      if m['DeviceName'] == image['RootDeviceName'])
        volume['Ebs']['VolumeSize'] = max(volume['Ebs']['VolumeSize'], root['Ebs']['VolumeSize'])
        spec['Description'] += '; COOL Marketplace OpenCV worker'
        return spec

    deploy.aws, deploy.template = aws, template
    try:
        deploy.start(output, invited_beta=invited_beta)
    finally:
        deploy.aws, deploy.template = real_aws, real_template
    state_path = Path(output).resolve()/'state.json'
    state = read_json(state_path)
    state.update(engine='cool-marketplace', ami_name=image['Name'], ami_product_code=COOL_PRODUCT,
                 purpose='invited-beta-cool' if invited_beta else state['purpose']+'-cool')
    write_json(state_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=['start'])
    parser.add_argument('--engine', choices=['standard', 'cool'], required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--invited-beta', action='store_true')
    args = parser.parse_args()
    if args.engine == 'cool':
        start_cool(args.output, invited_beta=args.invited_beta)
    else:
        deploy.start(args.output, invited_beta=args.invited_beta)


if __name__ == '__main__':
    main()
