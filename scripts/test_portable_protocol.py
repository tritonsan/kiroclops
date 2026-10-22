"""Run all local unit/contract tests and bind the result to portable source bytes."""
from pathlib import Path
import sys
import unittest
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.portable.specs import protocol_sources, verify_legacy
from kiroclops.integration.storage import sha256,write_json


def main():
    output=ROOT/'artifacts/phase2c/setup'/('software-tests-'+str(time.time_ns()))
    output.mkdir(parents=True)
    before=protocol_sources(); legacy=verify_legacy()
    test_sources={p.relative_to(ROOT).as_posix():sha256(p) for p in (ROOT/'tests').glob('test_*.py')}
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    with (output/'tests.log').open('w',encoding='utf-8') as log:
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    same=(before==protocol_sources() and legacy==verify_legacy()
          and test_sources=={p.relative_to(ROOT).as_posix():sha256(p) for p in (ROOT/'tests').glob('test_*.py')})
    record={'status':'pass' if result.wasSuccessful() and same else 'fail','tests_run':result.testsRun,
        'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'protocol_sources':before,'legacy_sources_count':len(legacy),
        'test_sources':test_sources,
        'evidence_files':{(output/'tests.log').relative_to(ROOT).as_posix():sha256(output/'tests.log')}}
    write_json(output/'result.json',record)
    print(str(output/'result.json'),flush=True)
    print({k:record[k] for k in ('status','tests_run','failures','errors','skipped')},flush=True)
    return 0 if record['status']=='pass' else 1


if __name__=='__main__': raise SystemExit(main())
