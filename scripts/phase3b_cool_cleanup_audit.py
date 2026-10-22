"""Verify only the recorded internal COOL resources and estimate a cost bound."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import subprocess

from scripts.phase3b_aws_probe import aws
from kiroclops.visual.schema import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'artifacts/phase3b'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through',type=int,default=5)
    parser.add_argument('--output',type=Path,default=BASE/'cool-cleanup-01/verification.json')
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a fresh cleanup audit path; preserve previous receipts')
    checked = datetime.now(timezone.utc)
    rows = []
    for number in range(1, args.through+1):
        directory = BASE/f'cool-probe-{number:02}'
        state = read_json(directory/'state.json')
        if not state['bucket'].startswith('kiroclops-probe-'):
            raise ValueError('Unexpected receipt scope')
        row = {'probe': directory.name, 'bucket': state['bucket'], 'stack_status': 'not_created'}
        check = subprocess.run(['aws', 's3api', 'head-bucket', '--bucket', state['bucket'],
                                '--region', 'us-east-1', '--no-cli-pager'],
                               capture_output=True, text=True, timeout=30)
        row['bucket_absent'] = check.returncode != 0 and '(404)' in check.stderr
        if state.get('stack_id'):
            stack = aws('cloudformation', 'describe-stacks', stack_name=state['stack_id'])['Stacks'][0]
            row['stack_status'] = stack['StackStatus']
            resources = aws('cloudformation', 'describe-stack-resources', stack_name=state['stack_id'])['StackResources']
            row['resources'] = [{k: r[k] for k in ('LogicalResourceId', 'PhysicalResourceId', 'ResourceType', 'ResourceStatus') if k in r}
                                for r in resources]
            row['all_stack_resources_deleted'] = all(r['ResourceStatus'] == 'DELETE_COMPLETE' for r in resources)
            groups = aws('ec2', 'describe-instances', instance_ids=[state['InstanceId']])['Reservations']
            instances = [i for group in groups for i in group['Instances']]
            row['instance_state'] = instances[0]['State']['Name'] if instances else 'absent'
            row['instance_id'] = state['InstanceId']
            snapshot = read_json(directory/'instance.json')
            volumes = [b['Ebs']['VolumeId'] for b in snapshot.get('BlockDeviceMappings', []) if 'Ebs' in b]
            if (directory/'observed-volumes.json').exists():
                volumes = sorted(set(volumes) | set(read_json(directory/'observed-volumes.json')['volume_ids']))
            row['recorded_volume_ids'] = volumes
            row['recorded_volumes_absent'] = True
            for volume in volumes:
                found = aws('ec2', 'describe-volumes', filters=[{'Name': 'volume-id', 'Values': [volume]}])['Volumes']
                row['recorded_volumes_absent'] &= not found
            row['older_volume_limit'] = 'Terminated snapshots contain no volume IDs; encrypted root DeleteOnTermination was registered.' if not volumes else None
            events = aws('cloudformation', 'describe-stack-events', stack_name=state['stack_id'])['StackEvents']
            worker_events = [e for e in events if e['LogicalResourceId'] == 'Worker']
            row['worker_events'] = [{k: e[k] for k in ('Timestamp', 'ResourceStatus')} for e in worker_events]
            start = datetime.fromisoformat(snapshot['LaunchTime'])
            deleted = [datetime.fromisoformat(e['Timestamp']) for e in worker_events if e['ResourceStatus'] == 'DELETE_COMPLETE']
            end = min(deleted) if deleted else checked
            row['worker_seconds_upper_bound'] = max(60, (end-start).total_seconds())
        row['clean'] = (row['bucket_absent'] and row['stack_status'] in ('not_created', 'DELETE_COMPLETE')
                        and row.get('all_stack_resources_deleted', True)
                        and row.get('instance_state', 'absent') in ('terminated', 'absent')
                        and row.get('recorded_volumes_absent', True))
        rows.append(row)
    active = aws('ec2', 'describe-instances', filters=[
        {'Name': 'tag:Project', 'Values': ['KiroClops']},
        {'Name': 'tag:Purpose', 'Values': ['phase3b-feasibility']},
        {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped']}])
    active_ids = [i['InstanceId'] for r in active['Reservations'] for i in r['Instances']]
    seconds = sum(r.get('worker_seconds_upper_bound', 0) for r in rows)
    prior = read_json(BASE/'cost-estimate-final-01.json')
    hours = seconds/3600
    total = prior['worker_hours']+hours
    record = {'kind': 'CoolCleanupAndCostAudit', 'schema_version': 1,
              'checked_at': checked.isoformat(), 'rows': rows,
              'active_project_feasibility_instances': active_ids,
              'status': 'pass' if all(r['clean'] for r in rows) and not active_ids else 'incomplete',
              'cost': {'cool_probe_worker_hours_upper_bound': hours,
                       'all_phase3b_worker_hours_upper_bound': total,
                       'cool_probes_ec2_ipv4_usd_upper_bound': hours*(.15952+.005),
                       'cool_probes_60gb_gp3_usd_upper_bound': hours*60*.08/730,
                       'total_ec2_ipv4_usd_upper_bound': total*(.15952+.005),
                       'software_trial': 'P7D offer does not establish a zero charge. Marketplace usage must be reconciled with account billing separately; the Oct 1 audit recorded USD 0.02 for Sep 29 COOL usage.',
                       'billing_status': 'estimate_not_invoiced',
                       'noncompute_reserve_usd': 5,
                       'daily_worker_limit_hours': 4,
                       'feasibility_limit_usd': 10,
                       'method': 'Instance LaunchTime to CloudFormation Worker DELETE_COMPLETE is a conservative upper bound including cleanup delay; older Phase3B uses its frozen state ledger.'}}
    write_json(args.output, record)
    print({'status': record['status'], 'worker_hours_upper_bound': total, 'active_instances': active_ids})


if __name__ == '__main__':
    main()
