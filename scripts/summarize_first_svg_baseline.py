"""Source-bound numeric diagnosis, without changing the sealed candidate."""
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/svg-baseline-01/svg'
    output = base / 'completion-20260927/svg-baseline-01-motion-diagnostic.json'
    assert not output.exists()
    state = read_json(session / 'session.json')
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    job = read_json(session / 'jobs' / state['final_job_id'] / 'job.json')
    path = session / 'jobs' / state['final_job_id'] / 'result.json'
    result = read_json(path)
    assert job['status'] == 'succeeded' and result['passed'] == 8 and result['required'] == 12
    assert state['selected_candidate_id'] == job['candidate_id'] == 'candidate-02'
    rows = []
    for comparison in result['comparisons']:
        pixel = comparison['pixel_comparison']
        failed = [w for w in pixel['object_windows'] if w.get('pass') is False]
        rows.append({'scenario': comparison['scenario_id'], 'pass': comparison['pass'],
            'measurement_pass': comparison['measurement_pass'],
            'functional': comparison['functional']['status'],
            'geometry_pass': comparison['svg_geometry_preservation']['pass'],
            'score_fraction': comparison['score'],
            'failed_windows': [{**{k: w[k] for k in (
                'reference_id', 'candidate_id', 'window_id', 'position_rmse', 'size_rmse',
                'scale_px', 'gates', 'limits', 'time_window_s', 'valid_time_ratio')},
                'position_rmse_px': w['position_rmse'] * w['scale_px'],
                'size_rmse_px': w['size_rmse'] * w['scale_px']} for w in failed],
            'failed_preservation': [v for v in pixel['preservation_checks'] if v.get('pass') is False],
            'failed_lifecycle': [v for v in pixel['lifecycle_checks'] if v.get('pass') is False],
            'failed_improvement': [v for v in comparison['improvement'] if v.get('pass') is False],
            'failed_observable_dynamics': [v for v in comparison['dynamics']
                                           if v.get('observable') and v.get('pass') is False]})
    write_json(output, {'kind': 'SealedSvgNumericalDiagnosis', 'passed': 8, 'required': 12,
        'candidate_sha256': job['source_sha256'], 'final_job_id': state['final_job_id'],
        'mean_score_fraction': statistics.mean(r['score_fraction'] for r in rows),
        'measurement_passed': sum(r['measurement_pass'] for r in rows),
        'functional_passed': sum(r['functional'] == 'pass' for r in rows),
        'geometry_passed': sum(r['geometry_pass'] for r in rows), 'comparisons': rows,
        'result_sha256': file_sha256(path), 'job_sha256': file_sha256(path.with_name('job.json')),
        'source_sha256': file_sha256(__file__), 'primary_outcome_modified': False,
        'interpretation': 'All four drag-scenario failures are in the newly added f node before dragging starts. Normalized position error is6.5–8.1%; narrow-width size error also exceeds3%. Mean score alone would hide these local failures. Independent pixel accuracy and the exact source-level cause remain unverified.'})
    print({'passed': 8, 'required': 12,
           'failed_scenarios': [r['scenario'] for r in rows if not r['pass']],
           'independent_image_review': 'pending'})


if __name__ == '__main__':
    main()
