"""Bind the supplemental transfer result without rewriting its trial evidence."""
from pathlib import Path
import argparse
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine.review import validate_source_review, validate_independent_review


def run(session, validation, qualification_review, regression, output):
    session, validation, qualification_review, regression, output = [Path(p).resolve() for p in
        (session, validation, qualification_review, regression, output)]
    result = read_json(validation / 'validation.json')
    state = read_json(session / 'session.json')
    launch = read_json(session / 'kiro/launch.json')
    native = read_json(validation / 'native-audit.json')
    qualification = read_json(qualification_review / 'review-summary.json')
    tests = read_json(regression / 'tests.json')
    validate_source_review(output / 'source-review.json', directory=session,
        candidate_sha256=result['candidate_source_sha256'])
    validate_independent_review(qualification_review / 'independent-review.json')
    final = read_json(session / 'jobs' / state['final']['job_id'] / 'result.json')
    records = [read_json(p) for p in sorted((validation / 'records').glob('*/*.json'))]
    groups = {}
    for row in result['results']:
        group = row['scenario_id'].split('-w')[0]
        entry = groups.setdefault(group, {'passed': 0, 'required': 0, 'scores': [], 'improvements': []})
        entry['required'] += 1
        entry['passed'] += bool(row['pass'])
        if row['score'] is not None:
            entry['scores'].append(row['score'])
        if row['improvement'] is not None:
            entry['improvements'].append(row['improvement'])
    scores = [r['score'] for r in result['results'] if r['score'] is not None]
    gains = [r['improvement'] for r in result['results'] if r['improvement'] is not None]
    windows = []
    for path in (validation / 'results').glob('*.json'):
        comparison = read_json(path).get('comparison', {})
        windows += comparison.get('pixel_comparison', {}).get('object_windows', [])
    paths = {
        'validation': validation / 'validation.json', 'native_audit': validation / 'native-audit.json',
        'source_review': output / 'source-review.json', 'qualification_review': qualification_review / 'independent-review.json',
        'regression': regression / 'tests.json', 'launch': session / 'kiro/launch.json',
        'protocol': validation / 'protocol.json', 'session': session / 'session.json',
    }
    calibration = {'pass': final['calibration_pass'], 'comparisons': [
        {k: c[k] for k in ('pass', 'score', 'before_score', 'improvement_fraction')}
        for c in final['comparisons']]}
    summary = {
        'kind': 'SupplementalSVGAutonomousValidation',
        'status': 'numerical_pass_visual_pending' if result['status'] == 'pass' else 'numerical_fail',
        'overall_phase_acceptance': 'incomplete', 'paired_benchmark_cohort': False,
        'model': launch['model'], 'effort': launch['effort'], 'approval_mode': launch['approval_mode'],
        'native_run': launch['status'], 'native_audit': native['status'], 'prompt_count': native['prompt_count'],
        'tool_calls': len(native['tool_outcomes']['calls']),
        'denied_tool_calls': sum(not c['decision_allow'] for c in native['tool_outcomes']['calls']),
        'authoring_seconds': launch['response_received_at'] - launch['prompt_started_at'],
        'candidate_count': len(state['candidates']), 'selected_candidate': state['selected_candidate_id'],
        'candidate_sha256': result['candidate_source_sha256'], 'source_review': 'pass',
        'independent_final_visual_review': 'pending',
        'qualification_review': {k: qualification[k] for k in ('status', 'records', 'position_points', 'events', 'relationships', 'lifetime_checks', 'reviewer', 'external_human_review')},
        'calibration': calibration,
        'heldout': {'status': result['status'], 'passed': result['passed'], 'required': result['required'],
            'groups': groups, 'mean_score': statistics.mean(scores) if scores else None,
            'score_range': [min(scores), max(scores)] if scores else None,
            'mean_improvement': statistics.mean(gains) if gains else None,
            'improvement_range': [min(gains), max(gains)] if gains else None},
        'recordings': {'count': len(records), 'timing_passed': sum(r['timing']['status'] == 'pass' for r in records),
            'functional_passed': sum(r['functional']['status'] == 'pass' for r in records),
            'frames': sum(r['timing']['frame_count'] for r in records),
            'minimum_object_valid_ratio': min(o['valid_ratio'] for r in records for o in r['observations']['objects']),
            'minimum_compared_window_valid_time_ratio': min(w['valid_time_ratio'] for w in windows),
            'worst_clock_uncertainty_s': max(r['timing']['clock_uncertainty_s'] for r in records)},
        'original_files_unchanged': result['original_files_unchanged'],
        'previous_failed_trial_retained': True,
        'regression': {k: tests[k] for k in ('status', 'tests_run', 'failures', 'errors', 'skipped')},
        'evidence': {k: {'path': p.relative_to(ROOT).as_posix(), 'sha256': file_sha256(p)} for k, p in paths.items()},
        'limitations': ['The 16 qualification recordings were visually reviewed; the 36 fresh holdout recordings require their own independent visual review.',
            'One repaired supplemental run does not establish three-repeat or cross-domain comparative success.',
            'The native audit is a recorded-execution review, not an operating-system sandbox attestation.'],
    }
    write_json(output / 'summary.json', summary)
    attempts = read_json(output / 'attempts.json')
    attempts['attempts'][1].update(status=summary['status'], heldout_passed=result['passed'], heldout_required=result['required'],
        independent_visual_review='pending', validation_sha256=file_sha256(validation / 'validation.json'))
    write_json(output / 'attempts.json', attempts)
    print({k: summary[k] for k in ('status', 'heldout', 'recordings')}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('session', 'validation', 'qualification-review', 'regression', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    run(args.session, args.validation, args.qualification_review, args.regression, args.output)
