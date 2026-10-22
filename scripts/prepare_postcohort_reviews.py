"""Prepare unsigned review kits after all registered timed work has ended.

No model starts, capture, candidate edit, acceptance signature or retry occurs.
Each helper has an owned process tree. Existing reviews and failed kits remain.
"""
from pathlib import Path
import argparse
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from prepare_review_between_trials import start_owned_review, stop_owned_review

TERMINAL_ROWS = {'completed', 'failed', 'interrupted'}
TERMINAL_STAGES = TERMINAL_ROWS | {'not_run_predecessor_failed'}


def readiness(progress, pipeline):
    if progress.get('continuation_blocked') or pipeline.get('status') in {'blocked', 'interrupted'}:
        return 'blocked'
    if (len(progress.get('rows', [])) != 18 or
            any(r.get('status') not in TERMINAL_ROWS or
                any(s.get('status') not in TERMINAL_STAGES for s in r.get('stages', []))
                for r in progress['rows'])):
        return 'waiting'
    # run_cohort's final evidence replay must also finish, so preparation does
    # not contend with the trial process's concluding full integrity check.
    if pipeline.get('status') != 'cohort_execution_finished_review_required' or not pipeline.get('finished_at'):
        return 'waiting'
    return 'ready'


def run(output):
    output = Path(output).resolve()
    assert not output.exists(), 'Use a new review destination'
    output.mkdir(parents=True)
    cohort = ROOT / 'artifacts/phase3a/completion-paired-cohort-01'
    pipeline_path = ROOT / 'artifacts/phase3a/completion-20260927/paired-pipeline.json'
    progress_path = cohort / 'progress.json'
    registration = cohort / 'registration.json'
    registry_sha = file_sha256(registration)
    receipt = {'kind': 'PostCohortUnsignedReviewPreparation', 'status': 'waiting_for_cohort',
        'created_at': time.time(), 'registration_sha256': registry_sha, 'rows': [],
        'acceptance_granted': False, 'new_model_run': False, 'candidate_edited': False,
        'primary_outcomes_modified': False, 'source': str(Path(__file__).resolve())}

    def save(**fields):
        receipt.update(fields)
        write_json(output / 'pipeline.json', receipt)

    save()
    deadline = time.monotonic() + 24 * 3600
    while True:
        progress = read_json(progress_path)
        pipeline = read_json(pipeline_path)
        assert file_sha256(registration) == registry_sha
        assert progress['registration_sha256'] == registry_sha
        state = readiness(progress, pipeline)
        if state == 'blocked':
            save(status='blocked_before_preparation', finished_at=time.time())
            return 2
        if state == 'ready':
            break
        if time.monotonic() >= deadline:
            save(status='cohort_wait_expired', finished_at=time.time())
            return 2
        time.sleep(15)
    frozen_progress_sha = file_sha256(progress_path)
    from kiroclops.local_engine.cohort import verify_registration
    verify_registration(registration, full=False)
    save(status='preparing_unsigned_kits', progress_sha256=frozen_progress_sha, started_preparation_at=time.time())
    for trial in progress['rows']:
        for stage in trial['stages']:
            name = trial['id'] + '-' + stage['stage']
            row = {'id': name, 'status': 'pending'}
            receipt['rows'].append(row)
            session = Path(stage['session']).resolve()
            if stage['status'] == 'not_run_predecessor_failed' or not (session / 'session.json').is_file():
                row.update(status='no_completed_session', review_claim=False)
                save()
                continue
            state = read_json(session / 'session.json')
            assert not state.get('active_job_id'), 'A session still owns work'
            if (session / 'independent_review.json').is_file():
                row.update(status='prior_review_present_not_repeated',
                    prior_review_sha256=file_sha256(session / 'independent_review.json'),
                    review_replayed_here=False)
                save()
                continue
            if not state.get('final_job_id') or (state.get('final') or {}).get('status') != 'succeeded':
                row.update(status='complete_final_measurement_unavailable', review_claim=False)
                save()
                continue
            helper = (ROOT / 'scripts/review_browser_with_staged_policy.py' if state['domain'] != 'game'
                      else ROOT / 'artifacts/phase3a/tools/prepare_local_final_review.py')
            destination = output / name
            log = output / (name + '.log')
            command = [sys.executable, '-B', '-X', 'utf8', str(helper), 'prepare',
                       '--session', str(session), '--output', str(destination)]
            row.update(status='preparing', session=str(session), output=str(destination),
                       started_at=time.time(), helper_sha256=file_sha256(helper),
                       staged_browser_policy=state['domain'] != 'game')
            save()
            with log.open('w', encoding='utf-8') as stream:
                child, owner = start_owned_review(command, stream)
                row['owned_helper_pid'] = child.pid
                save()
                limit = time.monotonic() + 3600
                try:
                    while child.poll() is None:
                        if (file_sha256(progress_path) != frozen_progress_sha or
                                file_sha256(registration) != registry_sha):
                            raise RuntimeError('Cohort evidence changed during review preparation')
                        if time.monotonic() >= limit:
                            row['time_limit_reached'] = True
                            break
                        time.sleep(2)
                finally:
                    stop_owned_review(child, owner)
            complete = child.returncode == 0 and (destination / 'preparation-binding.json').is_file()
            row.update(status='prepared_unsigned' if complete else 'preparation_failed_retained',
                       exit_code=child.returncode, owned_tree_closed=True, finished_at=time.time())
            if complete:
                row['preparation_sha256'] = file_sha256(destination / 'preparation.json')
            save()
            print(name, row['status'], flush=True)
    save(status='preparation_finished_independent_inspection_required', finished_at=time.time())
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
