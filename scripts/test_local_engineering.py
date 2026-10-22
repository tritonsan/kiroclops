"""Run the complete inherited/local suite and bind exact sources to its result."""
from pathlib import Path
import argparse
import importlib.metadata
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.local_engine.cohort import protocol_sources
from kiroclops.portable_v2.specs import verify_legacy,protocol_sources as old_protocol_sources
from kiroclops.visual.schema import file_sha256,write_json,VisualError


def test_sources():
    return {p.relative_to(ROOT).as_posix():file_sha256(p) for p in sorted((ROOT/'tests').rglob('*.py'))}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.resolve()
    if output.exists():raise VisualError('output_exists','Keep prior test evidence')
    output.mkdir(parents=True)
    before=protocol_sources();tests=test_sources();legacy=verify_legacy();old=old_protocol_sources();started=time.time()
    if len(legacy)!=81 or len(old)!=25:raise VisualError('source_changed','Expected preserved legacy/Heart source boundaries')
    write_json(output/'start.json',{'source_hashes':before,'test_hashes':tests,'legacy_hashes':legacy,'heart_v2_hashes':old,
                                  'started_at':started,'runner_sha256':file_sha256(__file__)})
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    with (output/'tests.log').open('x',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    same=before==protocol_sources() and tests==test_sources() and legacy==verify_legacy() and old==old_protocol_sources()
    summary={'kind':'LocalEngineeringFullSuite','status':'pass' if result.wasSuccessful() and same else 'fail',
        'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'sources_unchanged':same,'legacy_count':len(legacy),'heart_v2_count':len(old),
        'elapsed_seconds':time.time()-started,'python':sys.version,'model_started':False,
        'versions':{p:importlib.metadata.version(p) for p in ('opencv-python','numpy','mcp','playwright')},
        'evidence':{'start.json':file_sha256(output/'start.json'),'tests.log':file_sha256(output/'tests.log')}}
    write_json(output/'result.json',summary);print(summary,flush=True)
    return 0 if summary['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
