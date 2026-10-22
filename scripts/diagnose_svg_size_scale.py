"""Read an old terminal SVG result's size gate without rescoring the trial.

Fixed source geometry is a functional fact, not a substitute for pixel review.
Subpixel acceptance floors must be investigated, never silently relaxed here.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def run():
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/svg-baseline-01/svg'
    state = read_json(session/'session.json')
    source = session/'candidates'/state['selected_candidate_id']/'source'
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    result = read_json(result_path)
    assert result['passed'] == 8 and result['required'] == 12
    ref = ROOT/'fixtures/phase3a-svg/reference'
    files = {str(p.relative_to(ROOT)): file_sha256(p)
             for p in (source/'src/app.js', source/'style.css', ref/'src/app.js', ref/'style.css',
                       result_path, ROOT/'kiroclops/local_engine/evaluation.py',
                       ROOT/'kiroclops/visual/comparison.py')}
    # The recorded target CSS adds one trailing blank line. Do not claim its
    # whole-file hash equals the reference; compare text except end whitespace.
    source_geometry_equal = all((source/name).read_text('utf-8').rstrip() ==
                                (ref/name).read_text('utf-8').rstrip()
                                for name in ('src/app.js', 'style.css'))
    assert source_geometry_equal
    rows = []
    for c in result['comparisons']:
        for w in c['pixel_comparison']['object_windows']:
            if w.get('gates', {}).get('size') is not False:
                continue
            scale = w['scale_px']
            rows.append({'scenario_id': c['scenario_id'], 'window_id': w['window_id'],
                'object_id': w['reference_id'], 'reference_movement_px': scale,
                'size_rmse_px': w['size_rmse']*scale,
                'registered_size_limit_px': w['limits']['size_rmse_limit']*scale,
                'position_rmse_px': w['position_rmse']*scale,
                'position_gate_pass': w['gates']['position'],
                'functional_status': c['functional']['status'],
                'visible_geometry_status': c['svg_geometry_preservation']['status']})
    assert len(rows) == 2 and all(r['position_gate_pass'] is False for r in rows)
    dest = base/'completion-20260927/svg-baseline-01-size-gate-diagnostic.json'
    assert not dest.exists()
    value = {'kind': 'ReadOnlySizeGateScaleDiagnostic',
        'status': 'subpixel_size_gate_requires_separate_measurement_investigation',
        'source_geometry_text_equal_except_trailing_whitespace': source_geometry_equal,
        'source_fact': 'Both source renderers declare a fixed138x58 SVG rect and identical styles; candidate controller changes translations and velocities only.',
        'rows': rows, 'input_hashes': files, 'producer_sha256': file_sha256(__file__),
        'primary_outcome_modified': False, 'threshold_changed': False,
        'interpretation': 'Size RMSE is normalized by travel distance, so these short f-node movements impose a below-one-pixel width/height gate. Fixed source dimensions alone cannot prove measured raster dimensions or point accuracy. Both rows also fail position, so removing this gate would not repair their original comparison.',
        'next_evidence_needed': 'Inspect raw rectangle edges and size estimates independently before attributing residual size error to rendering or measurement. Any future contract revision must be prospective, shared by both arms and kept separate from this frozen cohort.'}
    write_json(dest, value)
    print({'size_gate_rows': rows, 'primary_outcome_modified': False})


if __name__ == '__main__':
    run()
