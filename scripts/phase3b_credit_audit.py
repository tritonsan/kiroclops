"""Read-only AWS CLI billing evidence; never change credit or subscription state."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import subprocess
import os


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cli-python', type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    base = [str(args.cli_python), '-m', 'awscli'] if args.cli_python else ['aws']
    def query(name, *command):
        try:
            proc = subprocess.run(base + list(command) + ['--region', 'us-east-1', '--output', 'json',
                                  '--cli-connect-timeout', '5', '--cli-read-timeout', '25'] + ([] if args.cli_python else ['--no-cli-pager']),
                                  env={**os.environ, 'AWS_MAX_ATTEMPTS':'2'},
                                  capture_output=True, text=True, encoding='utf-8', timeout=65)
            value = {'error': proc.stderr.strip(), 'returncode': proc.returncode} if proc.returncode else json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            value = {'error':'AWS CLI request timed out; no conclusion inferred', 'timeout_seconds':65}
        (args.output / (name + '.json')).write_text(json.dumps(value, indent=2), encoding='utf-8')
        return value
    identity = query('identity', 'sts', 'get-caller-identity')
    account = identity['Account']
    start = '2025-09-30T00:00:00Z'
    credits = query('credits', 'billing', 'get-credits', '--account-id', account, '--start-date', start)
    history = query('allocation-history', 'billing', 'get-credit-allocation-history', '--account-id', account,
                    '--start-date', '2026-08-01T00:00:00Z', '--end-date', datetime.now(timezone.utc).isoformat())
    costs = query('costs', 'ce', 'get-cost-and-usage', '--time-period', 'Start=2025-10-01,End=2026-09-30',
                  '--granularity', 'MONTHLY', '--metrics', 'UnblendedCost', '--group-by',
                  'Type=DIMENSION,Key=SERVICE', 'Type=DIMENSION,Key=RECORD_TYPE')
    months = []
    for month in costs.get('ResultsByTime', []):
        services = {}
        for group in month.get('Groups', []):
            service, kind = group['Keys']
            amount = Decimal(group['Metrics']['UnblendedCost']['Amount'])
            if amount:
                services.setdefault(service, {})[kind] = str(amount)
        months.append({'month': month['TimePeriod']['Start'][:7], 'estimated': month['Estimated'],
                       'services': services,
                       'net_usd': str(sum((Decimal(v) for r in services.values() for v in r.values()), Decimal(0)))})
    credit_summary = [{k:c.get(k) for k in ('description','initialAmount','remainingAmount','estimatedAmount','endDate','creditStatus')}
                      | {'ec2_eligible':any('Elastic Compute' in x for x in c.get('applicableProductNames',[])),
                         'vpc_eligible':'Amazon Virtual Private Cloud' in c.get('applicableProductNames',[]),
                         'lambda_eligible':'AWS Lambda' in c.get('applicableProductNames',[])} for c in credits.get('credits',[])]
    summary = {'checked_at': datetime.now(timezone.utc).isoformat(), 'account': account,
               'credits': credit_summary, 'credit_query_error':credits.get('error'), 'allocation_history_status': 'error' if 'error' in history else 'available',
               'monthly_costs': months, 'scope': 'Account-wide costs, not project-only. Current month is provisional.'}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    for m in months:
        if m['services']:
            wanted = {s:r for s,r in m['services'].items() if any(x in s.lower() for x in ('compute', 'ec2', 'lambda', 'private cloud'))}
            print(json.dumps({'month':m['month'],'estimated':m['estimated'],'net_usd':m['net_usd'],'server_services':wanted}))
    if 'error' in credits:
        print(json.dumps({'credit_error':credits['error']}))
    else:
        print(json.dumps({'credits':credit_summary}))


if __name__ == '__main__':
    main()
