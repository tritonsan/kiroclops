"""Compact read-only terminal trial evidence; never grants review acceptance.

Print only whitelisted scalar metrics, not full observations or native prose.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, file_sha256


def select(value, fields):
    return {k: value[k] for k in fields if k in value}


def summarize(session):
    session = Path(session).resolve()
    state = read_json(session/'session.json')
    launch = read_json(session/'kiro/launch.json')
    assert launch.get('finished_at') and not state.get('active_job_id'), 'Require terminal receipts'
    job_path = session/'jobs'/state['final_job_id']/'job.json' if state.get('final_job_id') else None
    job = read_json(job_path) if job_path else {}
    result_path = job_path.with_name('result.json') if job_path else None
    out = {'session': str(session), 'domain': state['domain'], 'mode': state['mode'],
        'session_status': state['status'], 'selected': state.get('selected_candidate_id'),
        'launch': select(launch, ['status', 'native_turn_status', 'model', 'effort']),
        'authoring_and_tools_s': (launch['response_received_at']-launch['prompt_started_at']
            if launch.get('response_received_at') and launch.get('prompt_started_at') else None),
        'final_job': select(job, ['job_id', 'status', 'source_sha256', 'error']),
        'candidates': [select(c, ['candidate_id', 'source_sha256', 'status', 'eligible', 'score', 'job_id', 'error'])
                       for c in state.get('candidates', [])],
        'complete_result_available': bool(result_path and result_path.is_file()),
        'evidence_sha256': {'session': file_sha256(session/'session.json'),
                           'launch': file_sha256(session/'kiro/launch.json')},
        'scope': 'Read-only compact summary; no source/image review or primary acceptance override.'}
    if not out['complete_result_available']:
        return out
    result = read_json(result_path)
    out['evidence_sha256']['result'] = file_sha256(result_path)
    out['numeric'] = select(result, ['pass', 'passed', 'required', 'motion_status', 'overall_acceptance'])
    if 'comparison' in result:
        out['game'] = {
            'comparison': select(result['comparison'], ['mean_rmse_body', 'measurement_status', 'numerical_status']),
            'preservation': select(result.get('preservation') or {}, ['status', 'failed_cases', 'mismatches']),
            'visual_wall': select(result.get('visual_wall') or {}, ['status', 'passed', 'required']),
        }
    if 'comparisons' in result:
        out['comparisons'] = []
        for c in result['comparisons']:
            pixel = c.get('pixel_comparison') or {}
            row = select(c, ['scenario_id', 'pass', 'score', 'measurement_pass',
                             'baseline_measurement_pass', 'failure_class'])
            row['functional'] = (c.get('functional') or {}).get('status')
            row['geometry'] = select(c.get('svg_geometry_preservation') or {}, ['pass', 'status'])
            row['failed_windows'] = [select(w, ['window_id', 'reference_id', 'candidate_id',
                'position_rmse', 'size_rmse', 'endpoint_error', 'scale_px', 'gates', 'limits'])
                for w in pixel.get('object_windows', []) if w.get('pass') is False]
            row['failed_improvement'] = [select(v, ['object_id', 'after_equal_window_rmse',
                'before_equal_window_rmse', 'improvement_fraction', 'pass'])
                for v in c.get('improvement', []) if v.get('pass') is False]
            row['failed_observable_dynamics'] = [select(v, ['window_id', 'object_id', 'gates',
                'reference_settling_s', 'candidate_settling_s', 'candidate_overshoot_px', 'overshoot_limit_px'])
                for v in c.get('dynamics', []) if v.get('observable') is True and v.get('pass') is False]
            out['comparisons'].append(row)
    return out


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', required=True, type=Path)
    print(json.dumps(summarize(p.parse_args().session), ensure_ascii=False))
