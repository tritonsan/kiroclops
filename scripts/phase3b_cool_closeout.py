"""Publish the completed internal COOL evidence with an explicit failed gate."""
from datetime import datetime, timezone
from pathlib import Path

from kiroclops.visual.schema import read_json, write_json, file_sha256


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'artifacts/phase3b'


def main():
    probe = BASE/'cool-probe-05'
    run = read_json(probe/'download/cool-validation/result.json')
    engine = read_json(probe/'download/cool-validation/engine.json')
    diagnostic = read_json(probe/'diagnostics.json')
    cleanup = read_json(BASE/'cool-cleanup-01/verification.json')
    receipt = read_json(BASE/'cool-activation-01/receipt.json')
    if run['status'] == 'running' or len(run['rows']) != 12 or cleanup['status'] != 'pass':
        raise ValueError('Completed measurement and verified cleanup are required')
    verified = read_json(BASE/'regression-05/start.json')['source_hashes']
    changed = [name for name, sha in verified.items() if file_sha256(ROOT/name) != sha]
    if changed:
        raise ValueError('Frozen source changes: '+str(changed))
    summary = {'kind': 'CoolInternalFeasibility', 'schema_version': 1,
               'created_at': datetime.now(timezone.utc).isoformat(),
               'status': 'partial_gate_failed', 'cool_executed': True,
               'equivalent_packages': sum(r['valid'] for r in run['rows']), 'total_packages': 12,
               'repetitions_stable': all(r['profile_stable'] and all(t['stable'] for t in r['timings']) for r in run['rows']),
               'observed_anchor_coordinates_unchanged': all(r['coordinate_difference_count'] == 0 for r in diagnostic['rows']),
               'validity_decisions_unchanged': all(r['validity_difference_count'] == 0 for r in diagnostic['rows']),
               'service_eligible': False, 'matched_speed_comparison': 'blocked_unmatched_opencv_base',
               'speedup_claim': False, 'engine_sha256': engine['engine_sha256'],
               'engine_identity': engine['identity'], 'sources_unchanged': run['sources_unchanged'],
               'registered_core_source_count': len(verified), 'registered_core_sources_unchanged': True,
               'resources_cleaned': True, 'cost': cleanup['cost'],
               'external_hosted_use_confirmed': False}
    write_json(probe/'summary.json', summary)
    lines = ['# COOL activation, credit coverage and real-workload experiment', '',
             'September 30, 2026. **Activation and a real COOL analysis on AWS are complete; the engine did not pass the gate for acceptance into the service.**', '',
             '## Credit and fees', '',
             '- AWS CLI Billing.GetCredits record: estimated available balance with EC2 coverage **7,831.86 USD**. The old Activate credit is exhausted; the remaining amount comes from other campaigns.',
             '- A narrow September allocation query directly showed **74.739926 USD of EC2 credit**. The query is partial and paginated; it does not stand in for the whole history. The exact bill for the new attempt has not been generated yet.',
             '- The COOL agreement is ACTIVE. The upfront quote is **0 USD**; the seven-day software trial ends around **00:22 Turkey time on October 7**. After that, the c8g.xlarge software costs **0.01 USD/hour**; the quote includes an estimated 20% VAT.',
             '- Credit-covered infrastructure and Marketplace software are separate. The service list of the existing credits does not include the COOL software fee. A 120-hour software charge would be 1.20 USD before tax, or about 1.44 USD at the VAT rate in the quote.',
             '- [Credit/billing report](billing-credit-check-02/REPORT.md) · [Activation receipt](cool-activation-01/receipt.json).', '',
             '## The real COOL engine and accuracy', '',
             'The Marketplace AMI `ami-033e481a24f94c8cb` ran on c8g.xlarge. The real native library under `/opt/cool/` was loaded: OpenCV **5.1.0-dev**, NumPy **2.5.1**, Python **3.12.3**, aarch64, four threads, OpenCL off. The library and build digests were recorded.', '',
             f"- Recorded equivalence: **{summary['equivalent_packages']}/12**; game 4/4, SVG 4/4, panel 0/4.",
             '- Each package had one warm-up, five timing repeats and a separate profile. The repeats and profiled results of the same COOL engine were **stable for 12/12**; the corpus and analysis sources did not change.',
             '- In all packages, the measured x/y tracking points and validity decisions were preserved. In the panel, 11 box coordinates differ by one pixel; two matching margins and seven derived object distances exceeded the recorded numerical tolerance.',
             '- This diagnostic is not an independent visual accuracy review. The equivalence policy and the motion acceptance thresholds were not changed. In this state the engine is not admitted to the analysis service; the standard OpenCV path is kept.',
             '- The historical game packages carry observations only: event equivalence is not considered tested in these four packages. For panel/SVG, observations and events were compared.', '',
             '| Package | COOL direct analysis median (s) | Recorded equivalence |',
             '|---|---:|---|']
    for row in run['rows']:
        lines.append(f"| {row['id']} | {row['median_s']:.3f} | {'Passed' if row['valid'] else 'Failed'} |")
    cost = cleanup['cost']
    lines += ['', '## Limits of the speed comparison', '',
              'The standard ARM environment uses OpenCV 5.0.0 / NumPy 2.2.6. The COOL build information says `Version control: unknown`; no matching source revision could be found. For this reason, no COOL speedup percentage was calculated from the 5.1 COOL and 5.0 standard times. Only the real analysis times above are reported; they are not upload, queue, startup or user-session times.', '',
              'Because the OpenCV base, the optimization build and NumPy all differ together, it cannot be said that the panel difference stems from the COOL optimization alone. A matched standard engine and a general NCC/box stability review should be the next technical task. [Detailed diagnosis](cool-probe-05/diagnostics.json).', '',
              '## Cost and resource shutdown', '',
              f"- For the three paid COOL-image workers, the conservative upper estimate for EC2+IPv4 is **{cost['cool_probes_ec2_ipv4_usd_upper_bound']:.4f} USD**; 60 GB of gp3 adds at most about **{cost['cool_probes_60gb_gp3_usd_upper_bound']:.4f} USD** separately.",
              f"- Including all earlier Phase3B work, the upper bound on total worker time is **{cost['all_phase3b_worker_hours_upper_bound']:.3f} hours** and the EC2+IPv4 estimate is **{cost['total_ec2_ipv4_usd_upper_bound']:.4f} USD**. These are not bills; they are conservative because they include the time until CloudFormation was cleaned up.",
              '- The 5 USD reserve for other expenses is not an amount spent; the feasibility study is within the 10 USD and four-hours-per-day limits. The COOL software is covered by the active free trial; the exact offset/bill will be seen later.',
              '- The temporary S3 buckets of the five attempt directories were deleted, three stacks reached DELETE_COMPLETE and three workers were terminated. The recorded EBS volume of the new worker was verified separately by querying that it no longer exists. The records of the old terminated workers carry no volume ID; their DeleteOnTermination setting and the stack shutdown are recorded.',
              '- No active KiroClops feasibility worker remains in the account. The COOL subscription is open; no running instance was left. [Cleanup and cost receipt](cool-cleanup-01/verification.json).', '',
              '## Preserved failures and the next gate', '',
              '01: namespace-file assumption in package preparation; the server was not started. 02: unset PYTHONPATH in the vendor activate script; the server shut down before the analysis started. 03: stopped before the server was started, for the same activation fix. 04: since the engine turned out to be 5.1-dev, the unmatched 5.0 comparison was not run. 05: the real 12-package experiment completed; panel equivalence failed. These records were not deleted or replaced with a successful run.', '',
              'The hosted-license scope of COOL for third parties has not been confirmed in writing. [Draft question](../../docs/COOL_HOSTED_USE_QUESTION.md) is ready; no e-mail was sent. The full 3A acceptance, the panel Kiro startup problem and the three real external-user tests are not considered closed by this run.', '',
              '[Raw result](cool-probe-05/download/cool-validation/result.json) · [Engine identity](cool-probe-05/download/cool-validation/engine.json) · [Diagnosis](cool-probe-05/diagnostics.json).']
    (BASE/'COOL_REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print({'status': summary['status'], 'equivalent': summary['equivalent_packages'],
           'registered_core_sources_unchanged': len(verified), 'report': str(BASE/'COOL_REPORT.md')})


if __name__ == '__main__':
    main()
