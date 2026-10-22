"""Read terminal evidence without launching, retrying, or changing a trial."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/panel-baseline-01/panel'
    output = base / 'completion-20260927/panel-baseline-01-motion-diagnostic.json'
    if output.exists():
        raise ValueError('Preserve prior diagnosis')
    state = read_json(session / 'session.json')
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    launch = read_json(session / 'kiro/launch.json')
    assert launch['status'] == 'completed'
    result_path = session / 'jobs' / state['final_job_id'] / 'result.json'
    result = read_json(result_path)
    rows = []
    for c in result['comparisons']:
        rows.append({
            'scenario': c['scenario_id'], 'pass': c['pass'], 'score': c['score'],
            'measurement_pass': c['measurement_pass'],
            'functional_status': c['functional']['status'],
            'position_failures': [{
                'object': w['reference_id'], 'window': w['window_id'],
                'gates': w['gates'], 'position_rmse': w['position_rmse'],
                'rmse_limit': w['limits']['rmse_limit'], 'scale_px': w['scale_px'],
            } for w in c['pixel_comparison']['object_windows'] if not w['pass']],
            'observable_dynamics_failures': [{
                k: v for k, v in r.items()
                if k in ('object_id', 'window_id', 'reason', 'gates')
            } for r in c['dynamics'] if r.get('observable') and not r['pass']],
            'improvement_failures': [r for r in c['improvement'] if not r['pass']],
            'relative_timing_failures': [r for r in c['relative_timing'] if not r['pass']],
        })
    assert sum(r['pass'] for r in rows) == result['passed'] == 3
    assert all(r['measurement_pass'] and r['functional_status'] == 'pass' for r in rows)
    files = ['session.json', 'kiro/launch.json', 'kiro/config.json',
             f"jobs/{state['final_job_id']}/result.json"]
    write_json(output, {
        'kind': 'ReadOnlyTerminalDiagnostic', 'classification': 'motion_mismatch',
        'original_registered_failure_unchanged': True, 'acceptance_override': False,
        'selected_candidate_id': state['selected_candidate_id'],
        'candidate_sha256': launch['postturn_finalization']['source_sha256'],
        'model': launch['model'], 'effort': launch['effort'],
        'generation_configuration_verified': launch['stream_configuration']['generation_configuration_verified'],
        'passed': result['passed'], 'required': result['required'], 'scenarios': rows,
        'independent_visual_review': 'pending', 'source_review': 'pending',
        'limitation': 'Numerical diagnostic, not independent visual or full source acceptance. Interrupted reference windows are not misclassified as terminal settling failures.',
        'evidence_hashes': {n: file_sha256(session / n) for n in files},
        'diagnostic_source_sha256': file_sha256(__file__),
    })
    print({'classification': 'motion_mismatch', 'passed': 3, 'required': 12})


if __name__ == '__main__':
    main()
