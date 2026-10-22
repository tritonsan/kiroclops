"""Close the source-reference repair and a bounded matched-base attempt."""
from datetime import datetime, timezone
from pathlib import Path

from kiroclops.visual.schema import read_json, write_json, file_sha256


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'artifacts/phase3b'


def main():
    audit = read_json(BASE/'cool-reference-audit-01/result.json')
    first = read_json(BASE/'cool-probe-05/download/cool-validation/result.json')
    cleanup = read_json(BASE/'cool-cleanup-02/verification.json')
    probe = BASE/'cool-probe-06'
    if audit['status'] != 'pass' or len(audit['rows']) != 12 or cleanup['status'] != 'pass':
        raise ValueError('Source-bound audit and complete resource cleanup are required')
    registration = read_json(BASE/'cool-reference-audit-01/registration.json')
    if registration['input_results']['cool'] != file_sha256(BASE/'cool-probe-05/download/cool-validation/result.json'):
        raise ValueError('Preserved original COOL result changed')
    verified = read_json(BASE/'regression-05/start.json')['source_hashes']
    if any(file_sha256(ROOT/rel) != sha for rel, sha in verified.items()):
        raise ValueError('Previously tested core sources changed')
    detected = read_json(probe/'download/cool-detected.json') if (probe/'download/cool-detected.json').exists() else None
    blocker = read_json(probe/'download/matching-blocker.json') if (probe/'download/matching-blocker.json').exists() else None
    paired = read_json(probe/'download/paired/result.json') if (probe/'download/paired/result.json').exists() else None
    paired_pass = paired is not None and paired['status'] == 'pass' and len(paired['rows']) == 12
    summary = {'kind': 'CoolReassessedFeasibility', 'schema_version': 2,
               'created_at': datetime.now(timezone.utc).isoformat(),
               'status': 'measurement_equivalence_pass' if not paired_pass else 'matched_performance_pass',
               'equivalent_packages': 12, 'total_packages': 12, 'cross_engine_comparisons_passed': 36,
               'original_historical_only_equivalent_packages': sum(r['valid'] for r in first['rows']),
               'failure_cause': 'benchmark used pre-NCC-fix historical outputs against corrected analysis code',
               'original_failed_record_preserved': True, 'core_source_files_unchanged': len(verified),
               'measurement_suitable_for_internal_tests': True, 'external_hosted_use_confirmed': False,
               'matched_speed_comparison': 'pass' if paired_pass else 'blocked_or_failed',
               'pure_cool_speedup_claim': paired_pass, 'v2_detected': detected, 'v2_blocker': blocker,
               'resources_cleaned': True, 'cost': cleanup['cost'],
               'independent_visual_or_movement_acceptance_claim': False}
    if paired_pass:
        summary['paired_speedup'] = paired['overall_speedup']
    write_json(BASE/'cool-reference-audit-01/summary.json', summary)
    lines = ['# COOL, credit coverage and source-matched measurement', '',
             'September 30, 2026. **COOL and the local and standard ARM measurements are equivalent for 12/12 on the same current code.** The earlier panel failure was caused by the evaluation tool using an old reference version.', '',
             '## Fees covered by credit', '',
             '- In the AWS CLI credit records, the estimated available balance with EC2 and VPC coverage is **7,831.86 USD**. The old Activate balance is zero; the remaining credits are from other campaigns.',
             '- **74.739926 USD** was seen in the September EC2 credit allocation, and **2.297802785 / -2.297802785 USD** in the VPC usage/credit groups. This history and service coverage support the new EC2 + public IPv4 usage being deducted from credit.',
             '- The server amounts in the report are gross estimates before credits. The exact bill and offset record for the new usage have not been generated yet; no unconditional zero-bill promise is made for all future AWS items.',
             '- The COOL Marketplace software fee is not in the credit lists. The upfront quote is 0 USD; the seven-day software trial ends around **00:22 Turkey time on October 7**. After that, c8g.xlarge costs **0.01 USD/hour + tax** (estimated 20% VAT in the quote). These small tests fall within the free software trial.',
             '- [Credit/billing records](billing-credit-check-02/REPORT.md) · [COOL activation](cool-activation-01/receipt.json) · [Official Marketplace price](https://aws.amazon.com/marketplace/pp/prodview-fdvbfiewzuehs).', '',
             '## Cause and fix of the panel difference', '',
             'The first check had treated the historical outputs in the corpus as the success reference. These outputs had been produced with the code from before the NCC equal-peak fix. Since standard ARM and the real COOL used the same corrected code, one-pixel box/distance differences were wrongly interpreted as an engine failure. The acceptance tolerance was neither narrowed nor widened; the correct source version was bound.', '',
             'The current code was re-run locally. In the 12 calibration packages, **36/36** of the local–ARM, local–COOL and ARM–COOL comparisons passed. All observations and events were compared. The five repeats and separate profiles in the first COOL run were stable for 12/12.', '',
             'The reference is now bound to the corpus, code, engine and measurement digests; outputs of a different tracking code are rejected before the engine is started. Historical differences are shown as diagnostics. The portable reference does not carry local installation paths. **39 relevant tests passed**; the previous 206 source files did not change. The new helper module and tests are delivered separately.', '',
             '[Audit explanation](cool-reference-audit-01/REPORT.md) · [36 comparisons](cool-reference-audit-01/result.json) · [First raw failed record](cool-probe-05/download/cool-validation/result.json) · [Preserved first report](COOL_REPORT-before-reference-audit.md).', '',
             '## Matched-base attempt for speed', '',
             'The real COOL engine in the new image is OpenCV 5.1.0-dev / NumPy 2.5.1; standard ARM is 5.0.0 / NumPy 2.2.6. A pure COOL speed gain cannot be derived from these different versions. Because the source revision is `unknown`, no arbitrary standard 5.x revision was chosen.', '',
             'In addition, the earlier Graviton4 v2 AMI `ami-01db31139bc5615d8` of the same product was tested on a single c8g.xlarge. If there was a stable OpenCV base, an alternating comparison with the same version, NumPy, Python and four threads was run; a development version was run so as to produce an explicit blocker result.']
    if detected:
        lines += ['', f"v2 real detection: OpenCV **{detected['opencv']}**, NumPy **{detected['numpy']}**; engine path `{detected['module']}`."]
    if blocker:
        lines += ['', f"Result: **{blocker['status']}**. The development version does not state the exact upstream revision; a matched standard base could not be determined. No speedup percentage was calculated."]
        lines += ['', 'The v2 build carries the information `4.13.0-153-g9a6e0d1bff-dirty`. The official `opencv/opencv` commit API query returned HTTP 422 / `No commit found` for this prefix. We did not match the modified vendor tree to an arbitrary 4.x commit. [Source query](cool-probe-06/upstream-lookup.json).']
    elif paired_pass:
        lines += ['', f"On the matched base, 12 packages passed; the overall ratio of the direct analysis medians is **{paired['overall_speedup']:.3f}×**. [Detailed same-machine comparison](cool-probe-06/REPORT.md). This ratio does not apply to recording, upload, queue and the user session."]
    else:
        lines += ['', 'The matched comparison was not completed or did not pass acceptance; the bootstrap/raw record is preserved. No COOL speedup is claimed.']
    cost = cleanup['cost']
    lines += ['', '## Resources and remaining work', '',
              f"The conservative total time of all Phase3B workers is **{cost['all_phase3b_worker_hours_upper_bound']:.3f} hours**, and the gross upper estimate for EC2+IPv4 is **{cost['total_ec2_ipv4_usd_upper_bound']:.4f} USD**. This is not a bill; credits have not been deducted. The 5 USD reserve for side expenses is not an amount spent.", '',
              'The temporary S3 buckets and created stacks of the six attempts were cleaned up; there is no active feasibility worker. The recorded volumes were also checked with an absence query. The COOL subscription is ACTIVE; no open server was left. [Current cleanup/cost](cool-cleanup-02/verification.json).', '',
              'The COOL license scope of the service for third parties awaits written confirmation. The panel Kiro/MCP startup reliability, three real new users and the full 3A motion acceptance are separate open topics. This measurement fix is not counted as their success or as evidence of additional benefit over normal Kiro.']
    (BASE/'COOL_REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print({'equivalent_packages': 12, 'matched_performance': summary['matched_speed_comparison'],
           'resources_cleaned': True, 'core_source_files_unchanged': len(verified)})


if __name__ == '__main__':
    main()
