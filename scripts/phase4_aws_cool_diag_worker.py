"""On-instance diagnostic of the COOL worker's job path (Phase 4 AWS, after the V6 freeze).

Not part of the service. On 2026-10-19 the COOL worker (``v6-cool-service-02``)
ended every job within a second with ``worker_failure``: an exception that
``services/analysis_api/worker.py`` swallows without a traceback. This script runs
on a fresh COOL worker at boot, before the worker service starts, and repeats the
worker's job steps on one package while keeping every traceback and subprocess output:

1. environment facts (COOL paths, ldconfig registrations, systemd-run linkage),
2. the worker's S3 download path,
3. a trivial command, a cv2 import and the real runner inside the worker's exact
   systemd-run sandbox, then the worker's exact Popen call,
4. the runner steps inside the sandbox with traceback printing,
5. the worker-side delivery steps (status, measurement, envelope, validate_delivery, strict JSON),
6. measure_direct in-process as root, without the sandbox.

The report is uploaded after every step to ``service/diagnostic/report.json``.

    PYTHONPATH=/opt/kiroclops-service/code venv/bin/python diag.py DIAG_DIR BUCKET
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

CODE = Path('/opt/kiroclops-service/code')
CLEAN = {'PATH': '/usr/sbin:/usr/bin:/bin'}
# The properties services/analysis_api/worker.py passes to systemd-run (ReadWritePaths is added per run).
SANDBOX = ['-p', 'User=kiroanalysis', '-p', 'Group=kiroanalysis', '-p', 'PrivateNetwork=yes',
           '-p', 'ProtectSystem=strict', '-p', 'ProtectHome=yes', '-p', 'PrivateTmp=yes',
           '-p', 'NoNewPrivileges=yes', '-p', 'RestrictSUIDSGID=yes', '-p', 'ProtectKernelTunables=yes',
           '-p', 'ProtectKernelModules=yes', '-p', 'ProtectControlGroups=yes', '-p', 'MemoryMax=2G',
           '-p', 'CPUQuota=400%', '-p', 'RuntimeMaxSec=120', '-p', 'TasksMax=64']
TRACE = r'''
import sys, traceback
from pathlib import Path
try:
    from kiroclops.visual.schema import read_json, write_json
    from kiroclops.cloud.transport import unpack
    from kiroclops.cloud.execution import measure_direct
    from kiroclops.cloud.results import make_measurement
    from kiroclops.cloud.identity import engine_identity
    request_path, output = Path(sys.argv[1]), Path(sys.argv[2])
    request = read_json(request_path)
    engine_identity(4)
    unpack(request_path.parent/'input.zip', output/'package', request['package_sha256'])
    result = measure_direct(output/'package', request['selection'], request.get('contract'))
    write_json(output/'measurement.json', make_measurement(result['observations'], result['events']))
    print('trace-ok')
except BaseException:
    traceback.print_exc()
    raise SystemExit(3)
'''


def run(command, timeout=240, env=CLEAN):
    started = time.monotonic()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
        return {'returncode': done.returncode, 'seconds': round(time.monotonic()-started, 3),
                'stdout': done.stdout[-4000:], 'stderr': done.stderr[-6000:]}
    except BaseException:
        return {'exception': traceback.format_exc()[-4000:]}


def read(path):
    path = Path(path)
    return path.read_text(encoding='utf-8', errors='replace')[-6000:] if path.exists() else None


def main():
    base, bucket = Path(sys.argv[1]), sys.argv[2]
    report = {'kind': 'CoolWorkerJobPathDiagnostic', 'started_at': time.time(), 'python': sys.executable, 'steps': {}}

    def save():
        (base/'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        try:
            import boto3
            boto3.client('s3').upload_file(str(base/'report.json'), bucket, 'service/diagnostic/report.json')
        except BaseException:
            report['upload_error'] = traceback.format_exc()[-2000:]

    def step(name, fn):
        started = time.monotonic()
        try:
            report['steps'][name] = {'ok': True, 'value': fn()}
        except BaseException:
            report['steps'][name] = {'ok': False, 'traceback': traceback.format_exc()[-6000:]}
        report['steps'][name]['seconds'] = round(time.monotonic()-started, 3)
        save()
        return report['steps'][name]

    systemd_run = shutil.which('systemd-run', path=CLEAN['PATH']) or '/usr/bin/systemd-run'
    step('environment', lambda: {
        'cool_env': read('/opt/kiroclops-service/cool-env.txt'),
        'cool_import': read('/opt/kiroclops-service/cool-import.txt'),
        'ld_conf': read('/etc/ld.so.conf.d/kiroclops-cool.conf'),
        'pth': {str(p): read(p) for p in Path(sys.prefix).glob('lib/python3*/site-packages/*.pth')},
        'sys_path': sys.path,
        'opt_listing': run(['ls', '-la', '/opt', '/opt/cool', '/opt/arm']),
        'systemd_run': systemd_run,
        'systemd_run_version': run([systemd_run, '--version']),
        'ldd_systemd_run': run(['ldd', systemd_run]),
        'ldd_python': run(['ldd', os.path.realpath(sys.executable)]),
        'kiroanalysis': str(pwd.getpwnam('kiroanalysis')),
    })

    def download():
        from services.analysis_api.api import S3Objects
        target = base/'download-check.zip'
        S3Objects(bucket).client.download_file(bucket, 'service/diagnostic/input.zip', str(target))
        return {'bytes': target.stat().st_size, 'same_as_bootstrap_copy': target.read_bytes() == (base/'input.zip').read_bytes()}
    step('worker_s3_download', download)

    os.chmod(base, 0o711)
    account = pwd.getpwnam('kiroanalysis')

    def prepare(name):
        directory = base/name
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(mode=0o711)
        output = directory/'output'
        output.mkdir(mode=0o700)
        os.chown(output, account.pw_uid, account.pw_gid)
        for item in ('input.zip', 'request.json'):
            shutil.copy(base/item, directory/item)
            os.chmod(directory/item, 0o444)
        return directory, output

    def sandboxed(unit, output, *command):
        return [systemd_run, '--quiet', '--wait', '--collect', '--pipe', '--unit='+unit, *SANDBOX,
                '-p', 'ReadWritePaths='+str(output), '-p', 'RestrictAddressFamilies=AF_UNIX',
                '--setenv=PYTHONPATH='+str(CODE), '--setenv=OPENCV_IO_MAX_IMAGE_PIXELS=2073600', *command]

    def trivial():
        _, output = prepare('true')
        return run(sandboxed('kiro-diag-true', output, '/bin/true'))
    step('sandbox_true', trivial)

    def cv2_import():
        _, output = prepare('import')
        return run(sandboxed('kiro-diag-import', output, sys.executable, '-c',
                             'import cv2, numpy, sys; print(cv2.__version__, numpy.__version__, cv2.__file__, sys.path)'))
    step('sandbox_cv2_import', cv2_import)

    def runner():
        directory, output = prepare('runner')
        result = run(sandboxed('kiro-diag-runner', output, sys.executable, '-m', 'services.analysis_api.runner',
                               str(directory/'request.json'), str(output)))
        result['status_json'] = read(output/'status.json')
        result['output_files'] = sorted(p.name for p in output.iterdir())
        return result
    step('sandbox_runner', runner)

    def worker_popen():
        # services/analysis_api/worker.py lines 136-146, unchanged apart from paths and the unit name.
        directory, output = prepare('popen')
        command = sandboxed('kiro-diag-popen', output, sys.executable, '-m', 'services.analysis_api.runner',
                            str(directory/'request.json'), str(output))
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CLEAN)
        process.wait(timeout=240)
        return {'returncode': process.returncode, 'status_json': read(output/'status.json')}
    step('worker_exact_popen', worker_popen)

    def trace():
        directory, output = prepare('trace')
        result = run(sandboxed('kiro-diag-trace', output, sys.executable, '-c', TRACE,
                               str(directory/'request.json'), str(output)))
        result['output_files'] = sorted(p.name for p in output.iterdir())
        return result
    step('sandbox_traced_steps', trace)

    def delivery():
        from kiroclops.cloud.identity import engine_identity
        from kiroclops.cloud.results import envelope, validate_delivery
        from kiroclops.visual.schema import read_json
        from services.analysis_api.worker import sources
        directory = base/'runner'
        engine, srcs = engine_identity(4), sources()
        request = read_json(directory/'request.json')
        measurement = read_json(directory/'output'/'measurement.json')
        value = envelope(measurement, request, logical_job_id='diagnostic', engine=engine, sources=srcs,
                         timings={'worker_wall_seconds': 1.0},
                         origin={'kind': 'fresh', 'backend': 'aws-standard-arm', 'ami_id': os.environ.get('AMI_ID')})
        validate_delivery(value, request, logical_job_id='diagnostic', expected_engine_sha256=engine['engine_sha256'],
                          expected_sources=srcs)
        return {'measurement_bytes': (directory/'output'/'measurement.json').stat().st_size,
                'delivery_json_bytes': len(json.dumps(value, allow_nan=False))}
    step('worker_delivery_steps', delivery)

    def in_process():
        from kiroclops.cloud.execution import measure_direct
        from kiroclops.cloud.identity import engine_identity
        from kiroclops.cloud.results import make_measurement
        from kiroclops.cloud.transport import unpack
        from kiroclops.visual.schema import read_json
        request = read_json(base/'request.json')
        engine_identity(4)
        target = base/'root-package'
        if target.exists():
            shutil.rmtree(target)
        unpack(base/'input.zip', target, request['package_sha256'])
        result = measure_direct(target, request['selection'], request.get('contract'))
        measurement = make_measurement(result['observations'], result['events'])
        return {'objects': len(result['observations']['objects']), 'json_bytes': len(json.dumps(measurement, allow_nan=False)),
                'opencv_version': result['observations'].get('opencv_version')}
    step('root_in_process_measure', in_process)
    report['finished_at'] = time.time()
    save()


if __name__ == '__main__':
    main()
