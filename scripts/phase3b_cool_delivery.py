"""Add the internal COOL evidence to the curated delivery, preserving its predecessor."""
from datetime import datetime, timezone
from pathlib import Path
from kiroclops.visual.schema import read_json, write_json, file_sha256


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'artifacts/phase3b'


def main():
    predecessor = BASE/'DELIVERY_MANIFEST_20260929.json'
    if not predecessor.exists():
        raise ValueError('Preserved predecessor required')
    record = read_json(predecessor)
    summary = read_json(BASE/'cool-reference-audit-01/summary.json')
    names = set(record['files'])
    names.update('scripts/'+name for name in (
        'phase3b_credit_audit.py', 'phase3b_credit_report.py', 'phase3b_cool_probe.py',
        'phase3b_cool_watch.py', 'phase3b_cool_validation.py', 'phase3b_paired_benchmark.py',
        'phase3b_cool_report.py', 'phase3b_cool_diagnostics.py', 'phase3b_cool_cleanup_audit.py',
        'phase3b_cool_closeout.py', 'phase3b_cool_delivery.py', 'phase3b_cool_reference_audit.py',
        'phase3b_cool_reassessment_closeout.py', 'phase3b_upstream_lookup.py'))
    names.update(('kiroclops/cloud/benchmark_reference.py','tests/test_benchmark_reference.py'))
    names.update('artifacts/phase3b/'+name for name in (
        'COOL_REPORT.md', 'billing-credit-check-02/REPORT.md', 'billing-credit-check-02/summary.json',
        'cool-activation-01/receipt.json', 'cool-probe-05/summary.json', 'cool-probe-05/diagnostics.json',
        'cool-probe-05/download/cool-validation/result.json', 'cool-probe-05/download/cool-validation/engine.json',
        'cool-cleanup-01/verification.json', 'cool-cleanup-02/verification.json',
        'cool-reference-audit-01/summary.json','cool-reference-audit-01/result.json',
        'cool-reference-audit-01/REPORT.md','cool-reference-audit-01/unit-tests.json',
        'cool-reference-audit-01/portable-reference/reference.json',
        'cool-probe-06/download/cool-detected.json','cool-probe-06/download/matching-blocker.json',
        'cool-probe-06/upstream-lookup.json','COOL_REPORT-before-reference-audit.md',
        'DELIVERY_MANIFEST-before-reference-audit.json','DELIVERY_MANIFEST_20260929.json'))
    record.update(schema_version=3, created_at=datetime.now(timezone.utc).isoformat(),
                  predecessor={'path': predecessor.relative_to(ROOT).as_posix(), 'sha256': file_sha256(predecessor)},
                  files={name: file_sha256(ROOT/name) for name in sorted(names)},
                  registered_source_files=summary['core_source_files_unchanged'],
                  registered_sources_still_match=True,
                  cool_executed=True, cool_equivalent_packages=12, cool_total_packages=12,
                  cool_original_historical_only_equivalent_packages=8,
                  cool_source_reference_error_corrected=True,
                  cool_internal_measurement_eligible=True,
                  cool_service_eligible=False, cool_speedup_claim=summary['pure_cool_speedup_claim'],
                  reference_repair_unit_tests=39,
                  aws_test_resources_cleaned=summary['resources_cleaned'])
    record['remaining'] = [
        'COOL matched upstream base for a fair performance comparison',
        'COOL hosted-use written confirmation', 'Panel native MCP startup reliability',
        'Three real new-user acceptance sessions',
        'Supplied-clip reports remain partial; original Phase3A acceptance is not changed']
    write_json(BASE/'DELIVERY_MANIFEST.json', record)
    mismatches = [name for name, sha in record['files'].items() if file_sha256(ROOT/name) != sha]
    if mismatches:
        raise ValueError(mismatches)
    print({'curated_files': len(record['files']), 'all_hashes_match': True, 'status': record['status']})


if __name__ == '__main__':
    main()
