"""Refresh human-facing progress from saved files; never run or resume a trial."""
from pathlib import Path
import json
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/phase3a'


def main():
    progress = json.loads((BASE / 'completion-paired-cohort-01/progress.json').read_text('utf-8'))
    path = BASE / 'completion-20260927/work.json'
    state = json.loads(path.read_text('utf-8'))
    rows = progress['rows']
    counts = {status: sum(row['status'] == status for row in rows)
              for status in sorted({row['status'] for row in rows})}
    state['steps']['paired_trials'] = {'counts': counts,
        'active': [row['id'] for row in rows if row['status'] == 'running'],
        'source': 'completion-paired-cohort-01/progress.json',
        'scope': 'Primary preregistered outcomes; no post-hoc compatibility overrides.'}
    state['steps']['native_delete_compatibility'] = {
        'status': 'staged_not_applied_during_frozen_cohort',
        'tests': 71, 'tests_pass': True,
        'isolated_real_trace_calls_bound': 42,
        'primary_trial_outcome_modified': False,
        'proposal': 'native-delete-compatibility-01/review-proposed.py',
        'reason': 'Exact installed Delete File alias and targetFile schema absent from auditor; denied operation did not execute.'}
    state['updated_at'] = time.time()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    old = '**18 paired Kiro trials were preregistered; the first trial is being started. There is no full three-domain acceptance yet.**'
    new = ('**18 paired Kiro trials are in progress. The first tool-assisted game run passed the 12/12 numerical validation; '
           'the primary run remained failed because of the recording auditor, which does not recognize a blocked `Delete File` call. '
           'A compatibility fix that passed 71 tests was prepared separately; the sources of the running experiment did not change. '
           'There is no full three-domain acceptance yet.**')
    for name in ('README.md', 'PHASE3A_STATUS.md', 'ROADMAP.md'):
        doc = ROOT / name
        text = doc.read_text('utf-8')
        if old in text:
            doc.write_text(text.replace(old, new, 1), 'utf-8')
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
