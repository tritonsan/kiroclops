"""Take over two queued unsigned reviews after all timed experiments have ended.

The older serial coordinator will stop at its no-overwrite precondition when it
reaches these outputs. This is an explicit orchestration handoff, not a failed
experiment. Neither existing evidence nor the running producer is modified.
"""
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from prepare_review_between_trials import start_owned_review, stop_owned_review


def main():
    base = ROOT/'artifacts/phase3a'
    out = base/'completion-tail-pixel-aid-handoff-01'
    assert not out.exists()
    progress = base/'completion-paired-cohort-01/progress.json'
    assert all(r['status'] in ('completed', 'failed', 'interrupted')
               for r in read_json(progress)['rows'])
    original = base/'completion-remaining-pixel-aids-01/pipeline.json'
    assert all(r['id'] in ('svg-baseline-02', 'svg-tools-02')
               for r in read_json(original)['rows'])
    cases = [('panel-baseline-03', 'panel'), ('svg-tools-03', 'svg')]
    for name, _ in cases:
        assert not (base/f'completion-{name}-raw-review-01').exists()
    out.mkdir()
    state = {'status': 'preparing_unsigned', 'created_at': time.time(),
             'progress_sha256': file_sha256(progress), 'rows': [],
             'superseded_queue_tail': [c[0] for c in cases],
             'original_coordinator': str(original),
             'expected_original_stop': 'no-overwrite precondition after svg-tools-02',
             'new_model_run': False, 'primary_outcomes_modified': False,
             'acceptance_granted': False, 'visual_inspection_claimed': False}
    def save(): write_json(out/'pipeline.json', state)
    save()
    running = []
    try:
        for name, domain in cases:
            producer = ROOT/f'scripts/prepare_final_{domain}_raw_aids.py'
            review = base/'completion-postcohort-reviews-01'/f'{name}-{domain}'
            target = base/f'completion-{name}-raw-review-01'
            stream = (out/f'{name}.log').open('w', encoding='utf-8')
            try:
                child, owner = start_owned_review([sys.executable, '-B', '-X', 'utf8',
                    str(producer), '--review', str(review), '--output', str(target)], stream)
            except BaseException:
                stream.close()
                raise
            row = {'id': name, 'status': 'running', 'output': str(target),
                   'producer_sha256': file_sha256(producer), 'started_at': time.time()}
            state['rows'].append(row)
            running.append((child, owner, stream, row))
            save()
        deadline = time.monotonic()+4*3600
        while running and time.monotonic() < deadline:
            for item in list(running):
                child, owner, stream, row = item
                if child.poll() is None:
                    continue
                stop_owned_review(child, owner)
                stream.close()
                row.update(status='prepared_unsigned' if child.returncode == 0 else 'failed_retained',
                           exit_code=child.returncode, finished_at=time.time(), owned_tree_closed=True)
                running.remove(item)
                save()
                print(row['id'], row['status'], flush=True)
            if running: time.sleep(2)
    finally:
        for child, owner, stream, row in running:
            stop_owned_review(child, owner)
            stream.close()
            row.update(status='interrupted_unaccepted', exit_code=child.returncode,
                       finished_at=time.time(), owned_tree_closed=True)
        state['status'] = ('unsigned_preparation_finished_actual_inspection_required'
            if all(r['status']=='prepared_unsigned' for r in state['rows']) else 'incomplete_retained')
        save()


if __name__ == '__main__': main()
