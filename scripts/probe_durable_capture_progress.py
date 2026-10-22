"""Declared fixture-only isolation of durable progress I/O.

The previous ten probes omit the session progress callback. These four probes
alternate that condition with the actual lock/read/fsync/replace write pattern.
They do not tune or rerun a selected candidate. One attempt per condition.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256


def child(output, mode):
    from kiroclops.local_engine import backend
    from kiroclops.web_local import capture
    import diagnose_capture_stalls as probe
    output = Path(output).resolve()
    assert mode in ('none', 'durable') and not output.exists()
    sink = output.parent/(output.name+'-sink')
    assert not sink.exists()
    sink.mkdir()
    # Small job metadata, comparable to the failed session's1113-byte JSON.
    job_path = sink/'job.json'
    backend._atomic(job_path, {'status': 'running', 'job_id': 'fixture-progress-probe',
        'metadata': 'x'*800, 'progress': {'phase': 'start', 'completed': 0, 'total': 1}})
    rows = []
    lock = threading.RLock()

    @contextmanager
    def timed(operation):
        start, epoch = time.perf_counter(), time.time()
        try:
            yield
        finally:
            rows.append({'operation': operation, 'start_epoch_s': epoch,
                'end_epoch_s': time.time(), 'duration_s': time.perf_counter()-start})

    def progress(phase):
        with timed('progress_callback'), lock:
            with timed('job_lock'):
                lease = backend._lock(sink/'job.lock')
                lease.__enter__()
            try:
                with timed('job_read'):
                    current = backend._read(job_path)
                current['progress'] = {'phase': str(phase), 'completed': 0, 'total': 1}
                with timed('job_atomic_fsync_replace'):
                    backend._atomic(job_path, current)
            finally:
                lease.__exit__(None, None, None)

    original = capture.capture_run

    def run(*args, **kwargs):
        assert kwargs.get('progress') is None
        kwargs['progress'] = progress if mode == 'durable' else None
        return original(*args, **kwargs)

    result = None
    try:
        with patch.object(capture, 'capture_run', run):
            result = probe.run('svg', output, static_policy='frozen',
                               scenario_id='three-to-five-w1280-r1')
    finally:
        write_json(sink/'progress-io.json', {'mode': mode, 'operations': rows,
            'callback_scope': 'Same persistence pattern; no native polling process or concurrent model workload.',
            'source_hashes': {str(p.relative_to(ROOT)): file_sha256(p) for p in
                (Path(__file__), Path(backend.__file__), Path(capture.__file__), Path(probe.__file__))},
            'causality_established': False, 'model_run': False, 'selected_candidate_used': False})
    print(result, flush=True)


def queue(output):
    from prepare_review_between_trials import start_owned_review, stop_owned_review
    output = Path(output).resolve()
    assert not output.exists()
    output.mkdir()
    prerequisite = ROOT/'artifacts/phase3a/completion-final-svg-recorder-diagnostics-01/pipeline.json'
    producer = ROOT/'scripts/diagnose_capture_stalls.py'
    source = Path(__file__).resolve()
    hashes = {str(p): file_sha256(p) for p in (source, producer,
        ROOT/'kiroclops/local_engine/backend.py', ROOT/'kiroclops/web_local/capture.py')}
    state = {'kind': 'DeclaredDurableProgressProbe', 'status': 'waiting_for_ten_probes',
        'created_at': time.time(), 'conditions': ['none', 'durable', 'durable', 'none'],
        'scenario': 'three-to-five-w1280-r1', 'policy': 'frozen', 'rows': [],
        'source_hashes': hashes, 'attempts_per_condition_slot': 1,
        'model_run': False, 'selected_candidate_used': False, 'thresholds_changed': False,
        'primary_outcomes_modified': False, 'scope': 'Diagnostic only; no reliability or old-trial repair claim.'}

    def save(**values):
        state.update(values)
        write_json(output/'pipeline.json', state)

    save()
    deadline = time.monotonic()+24*3600
    while True:
        prior = read_json(prerequisite)
        if prior['status'] == 'diagnostics_finished_interpretation_required':
            break
        if prior['status'] in ('wait_expired_no_capture', 'not_run_predecessor_incomplete') or time.monotonic() > deadline:
            save(status='not_run_predecessor_incomplete', finished_at=time.time())
            return 2
        time.sleep(15)
    assert all(file_sha256(p) == sha for p, sha in hashes.items())
    save(status='capturing_fixture_only', prerequisite_sha256=file_sha256(prerequisite))
    for i, mode in enumerate(state['conditions']):
        folder = output/f'{i+1:02}-{mode}'
        row = {'mode': mode, 'status': 'running', 'started_at': time.time()}
        state['rows'].append(row)
        save()
        command = [sys.executable, '-B', '-X', 'utf8', str(source), '--child', mode, '--output', str(folder)]
        with (output/f'{i+1:02}-{mode}.log').open('w', encoding='utf-8') as stream:
            process, owner = start_owned_review(command, stream)
            limit = time.monotonic()+300
            try:
                while process.poll() is None and time.monotonic() < limit:
                    time.sleep(1)
            finally:
                stop_owned_review(process, owner)
        receipt = folder/'timing-probe.json'
        row.update(status='recorded' if receipt.exists() else 'incomplete',
                   exit_code=process.returncode, finished_at=time.time(), owned_tree_closed=True)
        if receipt.exists():
            value = read_json(receipt)
            row.update(timing=value.get('timing'), error=value.get('error'), receipt_sha256=file_sha256(receipt))
        save()
        print(i+1, mode, row['status'], flush=True)
    save(status='diagnostics_finished_interpretation_required', finished_at=time.time())
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=['none', 'durable'])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.child:
        child(args.output, args.child)
    else:
        raise SystemExit(queue(args.output))
