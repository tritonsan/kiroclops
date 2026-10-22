"""Prepare unsigned game image aids after the fourteen isolated captures.

Only raw image preparation is queued. Nothing signs annotations, changes a
candidate, launches a model, or changes acceptance. Existing evidence is kept.
"""
import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256
from prepare_review_between_trials import start_owned_review, stop_owned_review


def run(output, *, scopes_only=False):
    output = Path(output).resolve()
    assert not output.exists(), 'Preserve earlier preparation'
    output.mkdir(parents=True)
    base = ROOT / 'artifacts/phase3a'
    raw = ROOT / 'scripts/prepare_game_raw_review_aid.py'
    scopes = ROOT / 'scripts/prepare_game_scope_pixel_aid.py'
    helpers = [Path(__file__), raw, scopes,
               ROOT / 'scripts/prepare_review_between_trials.py']
    pinned = {str(p): file_sha256(p) for p in helpers}
    state = {'kind': 'UnsignedRemainingGameAidPreparation',
             'status': 'waiting_for_fourteen_isolated_probes',
             'created_at': time.time(), 'rows': [], 'helper_hashes': pinned,
             'model_started': False, 'acceptance_granted': False,
             'visual_inspection_claimed': False, 'scopes_only': scopes_only}

    def save(**values):
        state.update(values)
        write_json(output / 'pipeline.json', state)

    save()
    gate = base / 'completion-durable-progress-diagnostics-01/pipeline.json'
    deadline = time.monotonic() + 24 * 3600
    while True:
        status = read_json(gate)['status']
        if status == 'diagnostics_finished_interpretation_required':
            break
        if status in ('not_run_predecessor_incomplete', 'wait_expired_no_capture') or time.monotonic() > deadline:
            save(status='not_run_predecessor_incomplete')
            return 2
        time.sleep(15)
    save(status='preparing_unsigned', diagnostic_pipeline_sha256=file_sha256(gate))
    for name in ('game-tools-02', 'game-tools-03', 'game-baseline-02', 'game-baseline-03'):
        kit = base / 'completion-postcohort-reviews-01' / (name + '-jump')
        assert (kit / 'preparation-binding.json').is_file()
        tasks = [] if scopes_only else [(kind, raw, kit / 'positions' / kind) for kind in ('jump', 'boundary')]
        tasks.append(('scopes', scopes, kit))
        for kind, helper, source in tasks:
            assert all(file_sha256(p) == sha for p, sha in pinned.items())
            destination = output / name / kind
            assert not destination.exists()
            row = {'id': name + '-' + kind, 'status': 'running',
                   'output': str(destination), 'started_at': time.time()}
            state['rows'].append(row)
            save()
            command = [sys.executable, '-B', '-X', 'utf8', str(helper),
                       str(source), '--output', str(destination)]
            with (output / (row['id'] + '.log')).open('w', encoding='utf-8') as stream:
                process, owner = start_owned_review(command, stream)
                row['owned_helper_pid'] = process.pid
                save()
                try:
                    limit = time.monotonic() + 7200
                    while process.poll() is None and time.monotonic() < limit:
                        time.sleep(2)
                finally:
                    stop_owned_review(process, owner)
            row.update(status='prepared_unsigned' if process.returncode == 0 else 'failed_retained',
                       exit_code=process.returncode, owned_tree_closed=True,
                       finished_at=time.time())
            assert all(file_sha256(p) == sha for p, sha in pinned.items())
            save()
            print(row['id'], row['status'], flush=True)
    save(status='unsigned_preparation_finished_actual_inspection_required', finished_at=time.time())
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--scopes-only', action='store_true',
                        help='Position aids already exist; prepare only sequence/event/relationship boards.')
    args = parser.parse_args()
    raise SystemExit(run(args.output, scopes_only=args.scopes_only))
