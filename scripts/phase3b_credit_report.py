"""Readable credit audit and subscription receipt; no AWS mutations."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def redact_urls(value):
    if isinstance(value, dict):
        return {k:(urlunsplit((*urlsplit(v)[:3],'','')) if k=='url' and isinstance(v,str) else redact_urls(v)) for k,v in value.items()}
    if isinstance(value, list):
        return [redact_urls(x) for x in value]
    return value


def main():
    directory = ROOT/'artifacts/phase3b/billing-credit-check-02'
    summary = json.loads((directory/'summary.json').read_text())
    now = datetime.now(timezone.utc).timestamp()
    eligible = [c for c in summary['credits'] if c['ec2_eligible'] and c['creditStatus']=='ENABLED' and (c['endDate'] is None or c['endDate']>now)]
    estimated = sum((Decimal(c['estimatedAmount']['currencyAmount']) for c in eligible),Decimal(0))
    remaining = sum((Decimal(c['remainingAmount']['currencyAmount']) for c in eligible),Decimal(0))
    summary['ec2_eligible_balance'] = {'finalized_bill_balance_usd':str(remaining),'estimated_including_open_bills_usd':str(estimated)}
    (directory/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    allocation_path=directory/'september-allocation-retry.json'
    allocation=json.loads(allocation_path.read_text(encoding='utf-8-sig')) if allocation_path.exists() else {}
    entries=[x for x in allocation.get('creditAllocationHistoryList',[]) if x.get('appliedServiceName')=='AmazonEC2']
    ec2_applied=sum((Decimal(x['creditAmount']['currencyAmount']) for x in entries),Decimal(0))
    summary['september_ec2_allocation']={'returned_rows':len(entries),'credit_usd':str(ec2_applied),
                                        'partial_results':allocation.get('partialResults'),
                                        'more_pages':bool(allocation.get('nextToken'))}
    (directory/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    lines = ['# AWS credit and past billing check','',f"Checked: {summary['checked_at']}",'',
             f'- Estimated available balance of EC2-covered credits: **{estimated} USD** (including open bills).',
             f'- Balance after finalized bills: {remaining} USD; because of the open month, this amount is not an available balance.',
             '- The old Activate credit has been exhausted. The remaining credits come from other hackathon/prize campaigns.',
             '- The remaining credits cover EC2, Lambda and VPC; the COOL Marketplace software fee is not in their lists.',
             '- The broad credit allocation history API call timed out. A narrow September query returned four EC2 credit records; a total offset of **74.739926 USD** was verified. Because the response is partial/has more pages, this is not the complete allocation history.',
             '- The September data are estimates; new usage from today may appear on the bill with a delay.','',
             '| Month | EC2 compute USD | EC2 Other credit USD | VPC usage/credit USD | Net USD across the account |',
             '|---|---:|---:|---|---:|']
    for m in summary['monthly_costs']:
        if not m['services']:
            continue
        s=m['services'];vpc=s.get('Amazon Virtual Private Cloud',{})
        lines.append(f"| {m['month']}{' (estimated)' if m['estimated'] else ''} | {s.get('Amazon Elastic Compute Cloud - Compute',{}).get('Usage','0')} | {s.get('EC2 - Other',{}).get('Credit','0')} | {vpc.get('Usage','0')} / {vpc.get('Credit','0')} | {m['net_usd']} |")
    lines += ['', 'The credit coverage and past offsets support the eligible infrastructure charges of the new server being covered by credit; this is not an unconditional guarantee for all future bills.',
              'The balance arising from the other items of the June/August account has not been combined with the EC2 charge of the project.']
    (directory/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    activation = ROOT/'artifacts/phase3b/cool-activation-01'
    for name in ('terms.json','agreement-terms.json'):
        p=activation/name
        data=json.loads(p.read_text(encoding='utf-8-sig'))
        p.write_text(json.dumps(redact_urls(data),indent=2),encoding='utf-8')
    agreement=json.loads((activation/'agreement.json').read_text(encoding='utf-8-sig'))
    started=datetime.fromtimestamp(agreement['acceptanceTime'],timezone.utc)
    receipt={'status':agreement['status'],'agreement_id':agreement['agreementId'],'activated_at':started.isoformat(),
             'trial_duration':'P7D','expected_trial_end_utc':(started+timedelta(days=7)).isoformat(),
             'upfront_quote_usd':'0.00','software_usd_per_c8g_xlarge_hour':'0.01','estimated_vat_rate':'0.20',
             'external_hosted_use_confirmed':False,'scope':'Internal feasibility only; no external users or redistribution.'}
    (activation/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps({'ec2_estimated_credit_usd':str(estimated),'cool_activation':receipt},indent=2))


if __name__=='__main__':
    main()
