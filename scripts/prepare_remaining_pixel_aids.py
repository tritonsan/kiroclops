"""Prepare unsigned final browser aids after the isolated recorder diagnostics.

No model, candidate edits, acceptance claims, or retries. A named reviewer must
inspect the generated raw images before any annotation can become a receipt.
"""
import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from prepare_review_between_trials import start_owned_review, stop_owned_review


def run(output):
    output = Path(output).resolve()
    assert not output.exists()
    output.mkdir()
    base = ROOT/'artifacts/phase3a'
    cases = [('svg-baseline-02', 'svg'), ('svg-tools-02', 'svg'),
             ('panel-baseline-03', 'panel'), ('svg-tools-03', 'svg')]
    producers = {domain: ROOT/f'scripts/prepare_final_{domain}_raw_aids.py'
                 for domain in ('svg', 'panel')}
    pinned = {str(p): file_sha256(p) for p in [Path(__file__), *producers.values()]}
    state = {'kind': 'UnsignedRemainingPixelAidPreparation', 'status': 'waiting_for_isolated_probes',
             'created_at': time.time(), 'rows': [], 'producer_hashes': pinned,
             'new_model_run': False, 'acceptance_granted': False, 'visual_inspection_claimed': False}

    def save(**values):
        state.update(values)
        write_json(output/'pipeline.json', state)

    save()
    gate = base/'completion-durable-progress-diagnostics-01/pipeline.json'
    deadline = time.monotonic()+24*3600
    while True:
        status = read_json(gate)['status']
        if status == 'diagnostics_finished_interpretation_required':
            break
        if status in ('not_run_predecessor_incomplete', 'wait_expired_no_capture') or time.monotonic() > deadline:
            save(status='not_run_predecessor_incomplete')
            return 2
        time.sleep(15)
    assert all(file_sha256(p) == sha for p, sha in pinned.items())
    save(status='preparing_unsigned', diagnostic_pipeline_sha256=file_sha256(gate))
    for name, domain in cases:
        review = base/'completion-postcohort-reviews-01'/f'{name}-{domain}'
        target = base/f'completion-{name}-raw-review-01'
        assert (review/'preparation-binding.json').is_file() and not target.exists()
        row = {'id': name, 'status': 'running', 'output': str(target), 'started_at': time.time()}
        state['rows'].append(row)
        save()
        command = [sys.executable, '-B', '-X', 'utf8', str(producers[domain]),
                   '--review', str(review), '--output', str(target)]
        with (output/f'{name}.log').open('w', encoding='utf-8') as stream:
            process, owner = start_owned_review(command, stream)
            limit = time.monotonic()+7200
            try:
                while process.poll() is None and time.monotonic() < limit:
                    time.sleep(2)
            finally:
                stop_owned_review(process, owner)
        row.update(status='prepared_unsigned' if process.returncode == 0 else 'failed_retained',
                   exit_code=process.returncode, owned_tree_closed=True, finished_at=time.time())
        save()
        print(name, row['status'], flush=True)
    save(status='unsigned_preparation_finished_actual_inspection_required', finished_at=time.time())
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    raise SystemExit(run(parser.parse_args().output))
