"""Bounded regression suite for the event and prospective recorder repairs."""
from pathlib import Path
import argparse
import io
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256
from scripts.validate_panel_recorder import implementation_hashes


def run(output):
    output=Path(output)
    if output.exists():raise ValueError('Fresh test receipt required')
    old=read_json(ROOT/'artifacts/phase3a/independent-review-engineering-01/tests.json')['modules']
    modules=list(dict.fromkeys(old+[
        'tests.test_confirmed_rest_policy','tests.test_relative_order_comparison','tests.test_panel_observed_boundary','tests.test_panel_input_checkpoint','tests.test_reference_event_observability','tests.test_visual_events','tests.test_visual_departure',
        'tests.test_observed_response','tests.test_web_natural_capture','tests.test_local_svg_preservation',
        'tests.test_web_local','tests.test_reference_selection_binding','tests.test_visual_pixel_anchor',
        'tests.test_web_capture_progress','tests.test_web_capture_tail',
        'tests.test_web_capture_first_input_schedule','tests.test_web_capture_static_coverage',
        'tests.test_web_capture_post_input_cadence','tests.test_visual_clock','tests.test_pixel_clock_probe',
        'tests.test_svg_capture_admission','tests.test_local_media_session',
        'tests.test_qualified_cohort_policy','tests.test_local_web_reference','tests.test_local_cohort',
        'tests.test_reference_cache','tests.test_reference_acceptance']))
    frozen=implementation_hashes(ROOT);start=time.perf_counter();stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestLoader().loadTestsFromNames(modules))
    same=implementation_hashes(ROOT)==frozen
    output.mkdir(parents=True);(output/'tests.log').write_text(stream.getvalue(),encoding='utf-8')
    value={'kind':'ObservabilityRepairRegression','status':'pass' if result.wasSuccessful() and same else 'fail',
        'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'duration_s':time.perf_counter()-start,'modules':modules,'implementation_hashes':frozen,
        'source_unchanged_during_tests':same,'model_started':False,'output_sha256':file_sha256(output/'tests.log')}
    write_json(output/'tests.json',value)
    print({k:value[k] for k in ('status','tests_run','failures','errors','skipped')},flush=True)
    if not result.wasSuccessful():print(stream.getvalue()[-14000:])
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    result=run(p.parse_args().output);raise SystemExit(0 if result['status']=='pass' else 1)
