"""Wake test of a persistent (invited-beta) deployment: one job sent while the worker is stopped.

Phase 4 AWS addition after the V6 freeze. The worker of an ``--invited-beta``
deployment stops itself after five idle minutes; a new job must start it again
through the controller. This script waits until the worker is stopped, submits one
frozen V6 recording, records the job status and the EC2 state over time, and compares
the delivered measurement with the local one (unchanged policy and, labelled post-hoc,
without ``observations.opencv_runtime``). Renew the synthetic account first with
``services.analysis_api.deploy relogin-probe``; agent capabilities last at most 60 minutes.

    python scripts/phase4_aws_wake_test.py --deployment artifacts/phase4-aws/live-cool-02 \
        --output artifacts/phase4-aws/live-cool-wake-01
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.cloud.equivalence import compare  # noqa: E402
from kiroclops.cloud.execution import measure_direct  # noqa: E402
from kiroclops.cloud.identity import engine_identity  # noqa: E402
from kiroclops.cloud.remote import RemoteAnalysisBackend  # noqa: E402
from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from scripts.phase4_aws_runtime_excluded_check import without  # noqa: E402
from scripts.phase4_aws_v6_equivalence import recordings  # noqa: E402
from services.analysis_api.deploy import aws  # noqa: E402
from services.analysis_api.probe import configure  # noqa: E402

STOP_WAIT_S = 20*60
JOB_DEADLINE_S = 15*60


def instance_state(instance):
    return aws('ec2', 'describe-instances', instance_ids=[instance])['Reservations'][0]['Instances'][0]['State']['Name']


def chosen(record=None):
    """A recording the deployment has not measured yet; otherwise the exact-replay cache answers without the worker."""
    if record is None:
        return recordings()[0]
    record = Path(record)
    value = read_json(record)
    return {'id': record.parent.parent.name+'/'+record.parent.name, 'package': Path(value['package']),
            'selection': value['selection'], 'contract': value['suite_event_contract']}


def run(deployment, output, record=None):
    deployment, output = Path(deployment), Path(output)
    if output.exists():
        raise ValueError('Use a fresh result directory')
    state = read_json(deployment/'state.json')
    deadline = time.monotonic()+STOP_WAIT_S
    while instance_state(state['InstanceId']) != 'stopped':
        if time.monotonic() > deadline:
            raise SystemExit('The worker did not stop within the wait limit')
        time.sleep(20)
    config = configure(deployment, 'owner')
    output.mkdir(parents=True)
    item = chosen(record)
    backend = RemoteAnalysisBackend(output/'client', config)
    timeline = [{'t_s': 0.0, 'instance': 'stopped', 'job': None}]
    started = time.monotonic()
    submitted = backend.submit({'package_path': str(item['package']), 'selection': item['selection'],
                                'contract': item['contract'], 'candidate_sha256': '4'*64,
                                'retention_class': 'reference'}, idempotency_key=uuid.uuid4().hex)
    last = None
    deadline = time.monotonic()+JOB_DEADLINE_S
    while time.monotonic() < deadline:
        job = backend.get_job(submitted['job_id'], wait_seconds=5)
        current = (instance_state(state['InstanceId']), job['status'])
        if current != last:
            timeline.append({'t_s': round(time.monotonic()-started, 1), 'instance': current[0], 'job': current[1]})
            last = current
        if job['status'] in ('succeeded', 'failed', 'interrupted', 'cancelled'):
            break
    else:
        raise SystemExit('The job did not finish within the wait limit')
    total = time.monotonic()-started
    write_json(output/'job.json', job)
    result = {'kind': 'InvitedBetaWakeTest', 'checked_at': datetime.now(timezone.utc).isoformat(),
              'deployment': state['stack'], 'recording': item['id'], 'job_id': job['job_id'], 'status': job['status'],
              'wake_to_result_seconds': round(total, 1), 'timeline': timeline}
    if job['status'] == 'succeeded':
        engine_identity(4)
        local = measure_direct(item['package'], item['selection'], item['contract'])
        remote = {k: job['result'][k] for k in ('observations', 'events')}
        full = compare(local, remote)
        posthoc = compare(without(local), without(remote))
        result.update(remote_opencv_version=remote['observations'].get('opencv_version'),
                      policy_equivalent=full['equivalent'], policy_differences=len(full['differences']),
                      posthoc_runtime_excluded_equivalent=posthoc['equivalent'],
                      posthoc_label='POST-HOC: observations.opencv_runtime excluded, see COOL_V6_REPORT.md')
    write_json(output/'result.json', result)
    print({k: result.get(k) for k in ('status', 'wake_to_result_seconds', 'remote_opencv_version', 'policy_equivalent',
                                      'posthoc_runtime_excluded_equivalent')}, flush=True)
    for row in timeline:
        print(row, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--record', help='record.json of a recording not yet measured on this deployment')
    args = parser.parse_args()
    run(args.deployment, args.output, args.record)
