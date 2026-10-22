"""Post-hoc scale sensitivity; never changes the declared experiment's units.

The ordinary Kiro helper chose an8px reference body, while the experiment
registered7px. This diagnostic quantifies that ambiguity on saved pixels.
It is not a new acceptance decision or a substitute successful trial.
"""
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.portable_v2.measurement import _jump_row, _reliable


def main():
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/game-baseline-02/jump'
    output = base / 'completion-20260927/game-baseline-02-scale-sensitivity.json'
    assert not output.exists()
    state = read_json(session / 'session.json')
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    final = state['final_job_id']
    result_path = session / 'jobs' / final / 'result.json'
    result = read_json(result_path)
    hashes = {str(result_path): file_sha256(result_path)}
    groups = []
    key = lambda r: (r['scenario']['hold_ticks'], r['scenario']['spawn_offset'], r['scenario']['repeat'])
    for role in ('reference', 'before', 'candidate'):
        listing = session / 'measurements' / final / f'{role}.json'
        hashes[str(listing)] = file_sha256(listing)
        group = {}
        for record in read_json(listing):
            path = Path(record['analysis'])
            hashes[str(path)] = file_sha256(path)
            report = read_json(path)
            _reliable(report)
            assert key(report) not in group
            group[key(report)] = report
        assert len(group) == 12
        groups.append(group)
    saved = {key(row): row for row in result['comparison']['scenario_checks']}
    rows = []
    for k in sorted(saved):
        reference, before, after = (g[k] for g in groups)
        assert reference['body_height_px'] == 7
        assert before['body_height_px'] == after['body_height_px'] == 16
        original = _jump_row(reference, before, after, True)
        assert original['after_rmse_body'] == saved[k]['after_rmse_body']
        alternate = deepcopy(reference)
        alternate['body_height_px'] = 8
        alternate['metrics']['height_body'] *= 7 / 8
        for frame in alternate['frames']:
            if frame['height_body'] is not None:
                frame['height_body'] *= 7 / 8
        changed = _jump_row(alternate, before, after, True)
        rows.append({'scenario': list(k),
            'registered_rmse_body': original['after_rmse_body'],
            'hypothetical_8px_reference_rmse_body': changed['after_rmse_body'],
            'registered_height_relative_error': original['height_relative_error'],
            'hypothetical_8px_reference_height_relative_error': changed['height_relative_error'],
            'registered_improvement': original['improvement_fraction'],
            'hypothetical_8px_reference_improvement': changed['improvement_fraction']})
    assert all(file_sha256(path) == sha for path, sha in hashes.items())
    value = {'kind': 'PostHocScaleSensitivity', 'rows': rows,
        'registered_reference_height_px': 7, 'model_chosen_reference_height_px': 8,
        'target_height_px': 16, 'preservation_still': result['preservation']['status'],
        'registered_outcome_still': f"{result['passed']}/{result['required']}",
        'evidence_hashes': hashes, 'producer_sha256': file_sha256(__file__),
        'candidate_edited': False, 'recaptured': False, 'model_run': False,
        'primary_outcome_modified': False,
        'scope': 'Only the reference normalization is varied in memory. Actual pixels/times/source stay unchanged. The8px alternative came from the model-authored helper, not a redefinition of the registered7px anchor. This is sensitivity evidence, not acceptance, proof of correct mechanics, or a repaired comparison.'}
    write_json(output, value)
    print({'registered': value['registered_outcome_still'],
           'preservation': value['preservation_still'], 'examples': [rows[0], rows[6]]})


if __name__ == '__main__':
    main()
