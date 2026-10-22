"""On-instance diagnostic, round 2: what COOL's environment check needs inside the worker sandbox.

Phase 4 AWS, after the V6 freeze. Round 1 (``phase4_aws_cool_diag_worker.py``,
``artifacts/phase4-aws/v6-cool-diagnostic-01``) showed that importing COOL's cv2
inside the worker's systemd-run sandbox prints
``[FATAL] Security Violation: Non-AWS Environment Detected.`` and exits 1, while the
same import as root without the sandbox works. The sandbox differs in two ways: an
unprivileged user and no network (PrivateNetwork, AF_UNIX only). This round imports
cv2 under single-property variations and traces the baseline import with strace.

    PYTHONPATH=/opt/kiroclops-service/code venv/bin/python diag.py DIAG_DIR BUCKET
"""
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

CLEAN = {'PATH': '/usr/sbin:/usr/bin:/bin'}
COMMON = ['-p', 'ProtectSystem=strict', '-p', 'ProtectHome=yes', '-p', 'PrivateTmp=yes',
          '-p', 'NoNewPrivileges=yes', '-p', 'RestrictSUIDSGID=yes', '-p', 'ProtectKernelTunables=yes',
          '-p', 'ProtectKernelModules=yes', '-p', 'ProtectControlGroups=yes', '-p', 'MemoryMax=2G',
          '-p', 'CPUQuota=400%', '-p', 'RuntimeMaxSec=120', '-p', 'TasksMax=64']
USER = ['-p', 'User=kiroanalysis', '-p', 'Group=kiroanalysis']
NO_NETWORK = ['-p', 'PrivateNetwork=yes', '-p', 'RestrictAddressFamilies=AF_UNIX']
IMPORT = 'import cv2; print("cv2", cv2.__version__)'
# The analysis user may read these only if their modes allow; root always can.
DMI = ['/sys/class/dmi/id/sys_vendor', '/sys/class/dmi/id/product_name', '/sys/class/dmi/id/product_uuid',
       '/sys/class/dmi/id/board_asset_tag', '/sys/class/dmi/id/board_vendor', '/sys/class/dmi/id/chassis_asset_tag',
       '/sys/hypervisor/uuid', '/sys/devices/virtual/dmi/id/product_uuid']


def run(command, timeout=240):
    started = time.monotonic()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=CLEAN)
        return {'returncode': done.returncode, 'seconds': round(time.monotonic()-started, 3),
                'stdout': done.stdout[-3000:], 'stderr': done.stderr[-12000:]}
    except BaseException:
        return {'exception': traceback.format_exc()[-3000:]}


def main():
    base, bucket = Path(sys.argv[1]), sys.argv[2]
    report = {'kind': 'CoolSandboxEnvironmentCheckDiagnostic', 'started_at': time.time(), 'variants': {}}
    systemd_run = shutil.which('systemd-run', path=CLEAN['PATH'])
    strace = shutil.which('strace', path=CLEAN['PATH'])

    def save():
        (base/'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        try:
            import boto3
            boto3.client('s3').upload_file(str(base/'report.json'), bucket, 'service/diagnostic/report.json')
        except BaseException:
            report['upload_error'] = traceback.format_exc()[-2000:]

    def variant(name, properties, command=None):
        unit = 'kiro-diag2-'+name
        report['variants'][name] = {'properties': properties,
                                    **run([systemd_run, '--quiet', '--wait', '--collect', '--pipe', '--unit='+unit,
                                           *properties, *(command or [sys.executable, '-c', IMPORT])])}
        save()

    report['dmi_modes'] = run(['ls', '-la', *DMI])
    report['dmi_as_analysis_user'] = run(['setpriv', '--reuid=kiroanalysis', '--regid=kiroanalysis', '--init-groups',
                                          'cat', *DMI])
    save()
    variant('baseline_user_no_network', COMMON+USER+NO_NETWORK)
    variant('root_no_network', COMMON+NO_NETWORK)
    variant('user_with_network', COMMON+USER)
    variant('user_network_without_imds', COMMON+USER+['-p', 'IPAddressDeny=169.254.169.254/32'])
    variant('user_network_imds_only', COMMON+USER+['-p', 'IPAddressAllow=169.254.169.254/32', '-p', 'IPAddressDeny=any',
                                                   '-p', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6'])
    if strace:
        variant('strace_baseline', COMMON+USER+NO_NETWORK,
                [strace, '-f', '-qq', '-e', 'trace=openat,open,access,faccessat,faccessat2,statx,newfstatat,readlink,readlinkat,socket,connect',
                 '-e', 'signal=none', sys.executable, '-c', IMPORT])
        variant('strace_user_with_network', COMMON+USER,
                [strace, '-f', '-qq', '-e', 'trace=openat,socket,connect,sendto', '-e', 'signal=none',
                 sys.executable, '-c', IMPORT])
    report['finished_at'] = time.time()
    save()


if __name__ == '__main__':
    main()
