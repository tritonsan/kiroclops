"""One-command verification of the live KiroClops analysis service for judges.

After ``python -m kiroclops.cloud --config service.json login --username <judge>``
this script submits one bundled sample recording (``submission/sample-corpus``)
to the service, waits for the Graviton4 worker (a stopped worker wakes in about
a minute), and prints what the worker's signed delivery receipt says: the engine
that measured the recording (OpenCV version, architecture, the COOL library
names), the worker wall time, and the measurement summary. With
``--compare-local`` the same recording is also measured in-process with the local
OpenCV 5 wheel and compared with the service's cross-engine policy; the only
expected differences are inside ``observations.opencv_runtime`` (the build
record), which the script reports separately.

    python scripts/judge_check.py --config service.json --sample svg-three-to-four-w960-r1 --output judge-check-01
    python scripts/judge_check.py --config service.json --sample game-reference-h03 --output judge-check-02 --compare-local

Nothing is stored on the service beyond the job itself; ``logout`` afterwards
revokes the session capability.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.remote import RemoteAnalysisBackend  # noqa: E402
from kiroclops.visual.schema import VisualError, read_json, write_json  # noqa: E402

SAMPLES = ROOT/'submission'/'sample-corpus'
TERMINAL = ('succeeded', 'failed', 'interrupted', 'cancelled')
DEADLINE_S = 420


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, required=True, help='service.json after login')
    parser.add_argument('--sample', default='svg-three-to-four-w960-r1',
                        choices=sorted(p.name for p in SAMPLES.iterdir() if p.is_dir()))
    parser.add_argument('--output', type=Path, required=True, help='fresh folder for the client state and the receipt')
    parser.add_argument('--compare-local', action='store_true', help='also measure locally and compare (needs opencv-python 5)')
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'Use a fresh output folder: {args.output}')
    config = read_json(args.config)
    if not config.get('capability_file'):
        raise SystemExit('Log in first: python -m kiroclops.cloud --config service.json login --username <judge>')
    sample = SAMPLES/args.sample
    selection, contract = read_json(sample/'selection.json'), read_json(sample/'contract.json')
    contract = {**contract, 'judge_check': {'run': uuid.uuid4().hex[:8], 'at': datetime.now(timezone.utc).isoformat()}}
    args.output.mkdir(parents=True)
    backend = RemoteAnalysisBackend(args.output/'client', config)
    request = {'package_path': str(sample/'package'), 'selection': selection, 'contract': contract,
               'candidate_sha256': '7'*64, 'retention_class': 'reference'}
    print(f'submitting {args.sample} to {config["api_url"]} ...', flush=True)
    started = time.monotonic()
    try:
        submitted = backend.submit(request, idempotency_key=uuid.uuid4().hex)
    except VisualError as exc:
        hints = {'cloud_login_required': 'log in again (capabilities last one hour)',
                 'cloud_quota_exceeded': 'the account already has an active job or reached its daily quota; wait a minute or use the other judge account'}
        raise SystemExit(f'service refused the job: {exc.code}: {exc}. ' + hints.get(exc.code, ''))
    print(f'job {submitted["job_id"]} queued; waiting for the worker (a stopped worker wakes in about a minute)', flush=True)
    deadline = time.monotonic()+DEADLINE_S
    job = None
    while time.monotonic() < deadline:
        job = backend.get_job(submitted['job_id'], wait_seconds=5)
        if job['status'] in TERMINAL:
            break
        print(f'  {job["status"]} ... {time.monotonic()-started:.0f} s', flush=True)
    if job is None or job['status'] not in TERMINAL:
        raise SystemExit('The service did not finish within the deadline; try again (the worker may still be booting).')
    elapsed = time.monotonic()-started
    receipt_path = args.output/'client'/'jobs'/job['job_id']/'delivery-v2.json'
    summary = {'kind': 'JudgeCheck', 'checked_at': datetime.now(timezone.utc).isoformat(), 'sample': args.sample,
               'api_url': config['api_url'], 'job_id': job['job_id'], 'status': job['status'], 'end_to_end_seconds': round(elapsed, 1)}
    if job['status'] != 'succeeded':
        summary['error'] = job.get('error')
        write_json(args.output/'judge-check.json', summary)
        print(json.dumps(summary, indent=2))
        return 1
    receipt = read_json(receipt_path)['receipt']
    identity = receipt['engine']['identity']
    libraries = sorted(identity.get('mapped_libraries', {}))
    summary.update({
        'engine': {'opencv_version': identity.get('opencv_version'), 'numpy_version': identity.get('numpy_version'),
                   'architecture': identity.get('architecture'), 'system': identity.get('system'),
                   'build_information_sha256': identity.get('build_information_sha256'),
                   'opencv_libraries': libraries, 'cool_build': any('.so.5.1' in name for name in libraries)},
        'worker_wall_seconds': receipt.get('timings', {}).get('worker_wall_seconds'),
        'origin': receipt.get('origin', {}).get('kind'),
        'measurement_sha256': receipt.get('measurement_sha256'),
        'measurement_status': job['result'].get('measurement_status'),
        'objects': [{'id': o['id'], 'valid_ratio': o.get('valid_ratio'), 'frames': len(o.get('frames', []))}
                    for o in job['result']['observations'].get('objects', [])],
        'events': len(job['result'].get('events', {}).get('events', [])) if isinstance(job['result'].get('events'), dict) else None,
        'remote_opencv_runtime': job['result']['observations'].get('opencv_runtime'),
    })
    if args.compare_local:
        from kiroclops.cloud.equivalence import compare
        from kiroclops.cloud.execution import measure_direct
        local_started = time.monotonic()
        expected = measure_direct(sample/'package', selection, contract)
        local_seconds = time.monotonic()-local_started
        remote = {k: job['result'][k] for k in ('observations', 'events')}
        verdict = compare(expected, remote)
        def strip(result):
            observations = {k: v for k, v in result['observations'].items() if k != 'opencv_runtime'}
            observations['camera'] = [{k: v for k, v in row.items() if k != 'phase_shifts_px'} for row in observations.get('camera', [])]
            return {**result, 'observations': observations}
        posthoc = compare(strip(expected), strip(remote))
        remaining = sorted({d.get('path', '') for d in posthoc.get('differences', [])})
        summary['local'] = {'seconds': round(local_seconds, 2), 'opencv_version': expected['observations'].get('opencv_version'),
                            'policy_equivalent': verdict.get('equivalent'), 'policy_differences': len(verdict.get('differences', [])),
                            'difference_fields': sorted({d.get('path', '').split('/')[-3] if 'phase_shifts_px' in d.get('path', '') else d.get('path', '').split('/')[2] for d in verdict.get('differences', [])}),
                            'equivalent_excluding_build_record_and_phase_shifts': posthoc.get('equivalent'),
                            'remaining_difference_paths': remaining[:20],
                            'note': ('opencv_runtime is the engine build record (version, modules, wheel flags) and camera.phase_shifts_px is a '
                                     'report-only phaseCorrelate diagnostic whose last digits differ between x86 and Arm FFTs; neither enters any '
                                     'decision. Every tracked position, validity flag and event is compared by the policy.')}
    write_json(args.output/'judge-check.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'remote_opencv_runtime'}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
