"""Run prior coverage and new repair guards against applied production files."""
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    base = ROOT/'artifacts/phase3a'
    repair = base/'completion-postcohort-repairs-01'
    application = read_json(repair/'application.json')
    assert application['status']=='applied_regression_required'
    old = read_json(base/'completion-input-gap-regression-01/tests.json')
    assert old['tests_run']==464 and old['status']=='pass'
    modules = list(old['modules'])
    modules += ['tests.'+Path(n).stem for n in application['test_modules']]
    modules += ['tests.test_local_review', 'tests.test_local_evidence',
                'tests.test_local_evidence_sources', 'tests.test_local_evidence_report_failure',
                'tests.test_local_postturn_native_audit', 'tests.test_acceptance_axes',
                'tests.test_portable_v2_measurement']
    modules = list(dict.fromkeys(modules))
    paths = [ROOT/item['target'] for item in application['files']]
    paths += [ROOT/(module.replace('.', '/')+'.py') for module in modules]
    paths += [base/'completion-paired-cohort-01/registration.json',
              base/'completion-paired-cohort-01/progress.json']
    hashes = {p.relative_to(ROOT).as_posix(): file_sha256(p) for p in paths}
    for item in application['files']:
        assert hashes[item['target']]==item['proposed_sha256']
    out = base/'completion-applied-repairs-regression-01'
    assert not out.exists()
    out.mkdir()
    started = time.monotonic()
    with (out/'tests.log').open('w', encoding='utf-8') as stream:
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    unchanged = all(file_sha256(ROOT/name)==digest for name,digest in hashes.items())
    receipt = {'status': 'pass' if result.wasSuccessful() and unchanged else 'fail',
        'tests_run': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'duration_s': time.monotonic()-started, 'modules': modules,
        'input_hashes': hashes, 'source_unchanged_during_tests': unchanged,
        'application_sha256': file_sha256(repair/'application.json'),
        'output_sha256': file_sha256(out/'tests.log'), 'new_model_run': False,
        'primary_outcomes_modified': False}
    write_json(out/'tests.json', receipt)
    print({k:receipt[k] for k in ('status','tests_run','failures','errors','skipped','duration_s')})
    return 0 if receipt['status']=='pass' else 1


if __name__ == '__main__': raise SystemExit(main())
