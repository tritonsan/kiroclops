"""Run eight declared fixture captures after all timed work and kit preparation.

No model call or candidate code is used. Original trials and results are never
replaced. Each capture is attempted once, under an owned process tree.
"""
from pathlib import Path
import argparse
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from prepare_review_between_trials import start_owned_review, stop_owned_review

CASES = [
    ('panel', 'open-w960-r1', ('frozen', 'earlier-real-png')),
    ('panel', 'open-close-w960-r1', ('earlier-real-png', 'frozen')),
    ('panel', 'reverse-w1280-r2', ('frozen', 'earlier-real-png')),
    ('svg', 'three-to-four-w960-r1', ('earlier-real-png', 'frozen')),
]


def run(output):
    output = Path(output).resolve()
    assert not output.exists()
    output.mkdir()
    registration = ROOT/'artifacts/phase3a/completion-paired-cohort-01/registration.json'
    preparation = ROOT/'artifacts/phase3a/completion-postcohort-reviews-01/pipeline.json'
    producer = ROOT/'scripts/diagnose_capture_stalls.py'
    proposal = ROOT/'artifacts/phase3a/static-sampling-compatibility-01/result.json'
    inputs = {str(p): file_sha256(p) for p in (registration, producer, proposal, Path(__file__))}
    state = {'kind': 'DeclaredRecorderDiagnostics', 'status': 'waiting_for_cohort_and_review_preparation',
        'created_at': time.time(), 'cases': CASES, 'rows': [], 'input_hashes': inputs,
        'new_model_run': False, 'selected_candidate_used': False, 'acceptance_run': False,
        'primary_outcomes_modified': False, 'thresholds_changed': False, 'retries_per_case': 0,
        'scope': 'One before/after scheduling diagnostic per declared fixture input. No statistical reliability or repaired old-trial claim.'}
    def save(**fields):
        state.update(fields)
        write_json(output/'pipeline.json', state)
    save()
    deadline = time.monotonic()+24*3600
    while True:
        current = read_json(preparation)
        if current['status'] == 'preparation_finished_independent_inspection_required':
            break
        if current['status'] in ('blocked_before_preparation', 'cohort_wait_expired'):
            save(status='not_run_predecessor_incomplete', finished_at=time.time())
            return 2
        if time.monotonic() >= deadline:
            save(status='wait_expired_no_capture', finished_at=time.time())
            return 2
        time.sleep(15)
    assert all(file_sha256(p) == sha for p, sha in inputs.items()), 'Declared diagnostic inputs changed'
    from diagnose_capture_stalls import require_no_running_trials, require_review_preparation_finished
    require_no_running_trials(registration.with_name('progress.json'))
    require_review_preparation_finished(preparation)
    save(status='capturing_fixture_diagnostics', review_preparation_sha256=file_sha256(preparation))
    for domain, scenario, order in CASES:
        for policy in order:
            name = f'{domain}-{scenario}-{policy}'
            folder = output/name
            row = {'id': name, 'domain': domain, 'scenario_id': scenario,
                   'policy': policy, 'status': 'running', 'started_at': time.time()}
            state['rows'].append(row)
            save()
            command = [sys.executable, '-B', '-X', 'utf8', str(producer), '--domain', domain,
                       '--scenario', scenario, '--static-policy', policy, '--output', str(folder)]
            with (output/(name+'.log')).open('w', encoding='utf-8') as log:
                child, owner = start_owned_review(command, log)
                limit = time.monotonic()+300
                try:
                    while child.poll() is None and time.monotonic() < limit:
                        time.sleep(1)
                finally:
                    stop_owned_review(child, owner)
            result_path = folder/'timing-probe.json'
            row.update(status='recorded' if result_path.exists() else 'probe_incomplete',
                       exit_code=child.returncode, finished_at=time.time(), owned_process_tree_closed=True)
            if result_path.exists():
                result = read_json(result_path)
                row.update(timing=result.get('timing'), error=result.get('error'),
                           source_unchanged=result['source_unchanged'], receipt_sha256=file_sha256(result_path))
            save()
            print(name, row['status'], (row.get('timing') or {}).get('status'), flush=True)
    save(status='diagnostics_finished_interpretation_required', finished_at=time.time())
    return 0


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    raise SystemExit(run(p.parse_args().output))
