"""Post-hoc: the unchanged app as a no-agent baseline for the V6 panel run, on the recorded score formula.

Each recorded evaluation stores, per object window, the error of the unchanged app
(``before_rmse``) and of the candidate (``after_rmse``) against the reference. A case score
is the mean of the candidate errors and a job score is the mean of the case scores
(``kiroclops/local_engine/evaluation.py``). This script recomputes the recorded scores from
those rows (they must match exactly) and applies the same formula to the unchanged app.
It reads the frozen session and changes nothing.

    python scripts/v6_agent_baseline_posthoc.py --output artifacts/phase4-v6/agent-baseline-posthoc-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

SESSION = Path('C:/kc3d/s/v6p01')
JOBS = {'candidate-01 (calibration)': 'ef4d46e4d3c44a018197c738f83db961',
        'candidate-02 (calibration)': '6dbff94f50f541f493a135b4f871d716'}
FINAL = 'd19d9b76a015463fbf9708cb4fbde348'


def cases(comparisons):
    rows = []
    for c in comparisons:
        after = [g['after_rmse'] for g in c['improvement'] if g.get('after_rmse') is not None]
        before = [g['before_rmse'] for g in c['improvement'] if g.get('before_rmse') is not None]
        rows.append({'case_id': c['case_id'], 'role': c.get('role'), 'pass': c.get('pass'),
                     'recorded_score': c.get('score'), 'candidate': statistics.mean(after) if after else None,
                     'unchanged_app': statistics.mean(before) if before else None})
    return rows


def summary(rows, recorded_job_score=None):
    usable = [r for r in rows if r['candidate'] is not None and r['unchanged_app'] is not None]
    candidate = statistics.mean(r['candidate'] for r in usable)
    unchanged = statistics.mean(r['unchanged_app'] for r in usable)
    out = {'cases': len(rows), 'cases_with_both': len(usable), 'candidate_score': candidate, 'unchanged_app_score': unchanged,
           'ratio_unchanged_over_candidate': unchanged/candidate if candidate else None,
           'cases_candidate_better': sum(r['candidate'] < r['unchanged_app'] for r in usable),
           'recomputed_case_scores_match': all(r['recorded_score'] is None or abs(r['recorded_score']-r['candidate']) < 1e-12 for r in rows)}
    if recorded_job_score is not None:
        out['recorded_job_score'] = recorded_job_score
        out['recomputed_job_score_matches'] = abs(recorded_job_score-candidate) < 1e-12
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f'Keep prior evidence; choose a new folder: {args.output}')
    report = {'kind': 'V6AgentBaselinePosthoc', 'schema_version': 1, 'session': str(SESSION),
              'label': 'post-hoc analysis of recorded evaluations; no measurement was repeated', 'jobs': {}}
    for name, job in JOBS.items():
        path = SESSION/'jobs'/job/'result.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        rows = cases(result['comparisons'])
        report['jobs'][name] = {'job_id': job, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                'summary': summary(rows, result.get('score')), 'cases': rows}
    path = SESSION/'measurements'/FINAL/'comparison.json'
    rows = cases(json.loads(path.read_text(encoding='utf-8')))
    held = {'job_id': FINAL, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'summary': summary(rows), 'by_role': {}}
    for role in sorted({r['role'] for r in rows}):
        held['by_role'][role] = summary([r for r in rows if r['role'] == role])
    held['cases'] = rows
    report['jobs']['candidate-02 (held-out, after sealing)'] = held
    args.output.mkdir(parents=True)
    (args.output/'result.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    for name, job in report['jobs'].items():
        s = job['summary']
        print(f"{name}: unchanged app {s['unchanged_app_score']:.4f} -> candidate {s['candidate_score']:.4f} "
              f"({s['ratio_unchanged_over_candidate']:.1f}x lower), better in {s['cases_candidate_better']}/{s['cases_with_both']}, "
              f"recorded scores reproduced: {s['recomputed_case_scores_match']}")
    for role, s in held['by_role'].items():
        print(f"  held-out {role}: unchanged {s['unchanged_app_score']:.4f} -> candidate {s['candidate_score']:.4f}, "
              f"better in {s['cases_candidate_better']}/{s['cases_with_both']}")


if __name__ == '__main__':
    main()
