"""Clean up an experiment deployment whose worker already left EC2's instance list.

Phase 4 AWS addition after the V6 freeze. ``services.analysis_api.deploy cleanup``
expects ``describe-instances`` to return the worker; a worker terminated by the
75-minute guard disappears from that list after about an hour, and the command then
stops with IndexError before deleting anything. This script applies the same scope
checks and deletions (stack, owned bucket objects and bucket, local tokens), keeps the
worker budget record as evidence, and verifies the result. It does not rewrite the
Phase 3B budget ledger, because the worker's launch time is no longer available.

    python scripts/phase4_aws_cleanup.py --output artifacts/phase4-aws/v6-service-01
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.visual.schema import read_json, write_json  # noqa: E402
from services.analysis_api.deploy import aws  # noqa: E402


def cleanup(output):
    output = Path(output).resolve()
    state = read_json(output/'state.json')
    account = aws('sts', 'get-caller-identity')['Account']
    if (not state['run'].startswith('phase3b-service-') or state['stack'] != state['run']
            or state['bucket'] != f"kiroclops-probe-{account}-{state['run']}"):
        raise ValueError('Cleanup scope does not match this account and creation receipt')
    receipt = {'checked_at': datetime.now(timezone.utc).isoformat(), 'stack': state['stack'], 'bucket': state['bucket']}
    if state.get('Table'):
        try:
            item = aws('dynamodb', 'get-item', table_name=state['Table'], consistent_read=True,
                       key={'pk': {'S': 'system'}, 'sk': {'S': 'system#budget#worker'}}).get('Item')
            if item:
                write_json(output/'worker-budget-before-cleanup.json', json.loads(item['value']['S']))
        except RuntimeError as exc:
            receipt['budget_read_error'] = str(exc)[:300]
    instance = state.get('InstanceId')
    if instance:
        reservations = aws('ec2', 'describe-instances', instance_ids=[instance]).get('Reservations', [])
        if reservations:
            actual = reservations[0]['Instances'][0]
            if {p['Key']: p['Value'] for p in actual.get('Tags', [])}.get('Run') != state['run']:
                raise ValueError('Worker ownership differs')
            write_json(output/'instance-before-cleanup.json', actual)
            receipt['instance_state'] = actual['State']['Name']
            if actual['State']['Name'] not in ('terminated', 'shutting-down'):
                aws('ec2', 'terminate-instances', instance_ids=[instance])
        else:
            receipt['instance_state'] = 'not_listed'  # terminated long enough ago to leave the list
    aws('cloudformation', 'delete-stack', stack_name=state['stack'])
    keys, token = [], None
    while True:
        page = aws('s3api', 'list-objects-v2', bucket=state['bucket'], **({'continuation_token': token} if token else {}))
        keys.extend({'Key': item['Key']} for item in page.get('Contents', []))
        token = page.get('NextContinuationToken')
        if not token:
            break
    for start in range(0, len(keys), 1000):
        result = aws('s3api', 'delete-objects', bucket=state['bucket'], delete={'Objects': keys[start:start+1000], 'Quiet': True})
        if result.get('Errors'):
            raise RuntimeError('Some owned objects could not be deleted')
    aws('s3api', 'delete-bucket', bucket=state['bucket'])
    removed = 0
    for secret in list(output.glob('capability-*.private.json'))+list(output.glob('*-client/.kiroclops-private/*.credential')):
        if not secret.resolve().is_relative_to(output):
            raise ValueError('Secret path escaped the deployment directory')
        secret.unlink()
        removed += 1
    waited = subprocess.run(['aws', 'cloudformation', 'wait', 'stack-delete-complete', '--stack-name', state['stack'],
                             '--region', 'us-east-1'], capture_output=True, text=True, timeout=900)
    bucket = subprocess.run(['aws', 's3api', 'head-bucket', '--bucket', state['bucket'], '--region', 'us-east-1'],
                            capture_output=True, text=True, timeout=60)
    receipt.update(objects_deleted=len(keys), local_secrets_removed=removed,
                   stack_delete_complete=waited.returncode == 0,
                   bucket_absent=bucket.returncode != 0 and ('404' in bucket.stderr or 'Not Found' in bucket.stderr))
    write_json(output/'cleanup-verification.json', receipt)
    state.update(cleanup_requested_at=receipt['checked_at'], deleted_object_count=len(keys))
    write_json(output/'state.json', state)
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True)
    cleanup(parser.parse_args().output)
