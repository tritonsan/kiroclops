"""Prepare a terminal review kit only before another trial's native launcher.

No experiment is paused, restarted or edited. A newly appearing launch receipt
immediately stops this owned, image-only Python helper; incomplete kits remain
unaccepted. This avoids archive work during timed authoring/capture.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / 'artifacts/phase3a/completion-paired-cohort-01'
sys.path.insert(0, str(ROOT))


def start_owned_review(command, stream):
    """Contain the Windows redirector before it can spawn the real interpreter."""
    owner = None
    child = None
    try:
        if os.name == 'nt':
            from kiroclops.integration.kiro import _WindowsJob
            owner = _WindowsJob()
            child = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4)  # CREATE_SUSPENDED
            owner.assign_and_resume(child)
        else:
            child = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        return child, owner
    except BaseException:
        if owner is not None:
            owner.close()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        raise


def stop_owned_review(child, owner):
    if owner is not None:
        owner.close()  # KILL_ON_JOB_CLOSE covers the real Python and descendants.
    elif os.name != 'nt':
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif child.poll() is None:
        raise RuntimeError('Windows review worker lacks required job ownership')
    child.wait(timeout=10)


def read(path):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None


def gate():
    progress = read(COHORT / 'progress.json')
    if progress is None or progress.get('continuation_blocked'):
        return None
    active = [s for r in progress['rows'] if r['status'] == 'running'
              for s in r['stages'] if s['status'] == 'running']
    if len(active) != 1:
        return None
    stage = active[0]
    session = Path(stage['session'])
    state = read(session / 'session.json')
    if state is None or (session / 'kiro/launch.json').exists():
        return None
    if state.get('started_at') is not None or state.get('active_job_id'):
        return None
    return stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--staged-browser-policy', action='store_true',
                        help='Use the separately tested post-hoc reader; leave production frozen.')
    parser.add_argument('--current-prelaunch-window', action='store_true',
                        help='Use the current prelaunch window, still stopping when launch.json appears.')
    args = parser.parse_args()
    assert not args.output.exists() and not args.receipt.exists()
    state = read(args.session / 'session.json')
    assert state and state.get('status') == 'finalized' and not state.get('active_job_id')
    record = {'status': 'waiting_for_prelaunch_window', 'created_at': time.time(),
              'session': str(args.session.resolve()), 'output': str(args.output.resolve()),
              'experiment_edited_or_paused': False, 'native_model_started_by_helper': False}

    def save(**values):
        record.update(values)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(record, indent=2) + '\n', 'utf-8')

    save()
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        stage = gate()
        if stage and (args.current_prelaunch_window or 0 <= time.time() - stage['started_at'] < 90):
            break
        time.sleep(2)
    else:
        save(status='window_unavailable', finished_at=time.time())
        return 2
    owner = Path(stage['session'])
    log = args.receipt.with_suffix('.log')
    helper = (ROOT / 'scripts/review_browser_with_staged_policy.py' if args.staged_browser_policy
              else ROOT / 'artifacts/phase3a/tools/prepare_local_final_review.py')
    command = [sys.executable, '-B', '-X', 'utf8', str(helper),
               'prepare', '--session', str(args.session.resolve()), '--output', str(args.output.resolve())]
    with log.open('w', encoding='utf-8') as stream:
        child, child_owner = start_owned_review(command, stream)
        save(status='preparing_before_native_launch', helper_pid=child.pid,
             helper_started_at=time.time(), gate_session=str(owner),
             descendant_containment='suspended_start_windows_job' if child_owner else 'owned_process_group',
             staged_browser_policy=args.staged_browser_policy,
             helper_script=str(helper))
        stop_at = time.monotonic() + 200
        try:
            while child.poll() is None:
                reason = ('native_launch_started' if (owner / 'kiro/launch.json').exists()
                          else 'preparation_time_limit' if time.monotonic() >= stop_at else None)
                if reason:
                    stop_owned_review(child, child_owner)
                    save(status='incomplete_unaccepted', reason=reason, helper_exit_code=child.returncode,
                         helper_finished_at=time.time(), owned_process_tree_closed=True)
                    return 2
                time.sleep(.05)
        finally:
            stop_owned_review(child, child_owner)
    complete = child.returncode == 0 and (args.output / 'preparation-binding.json').is_file()
    save(status='prepared_unsigned' if complete else 'preparation_failed',
         helper_exit_code=child.returncode, helper_finished_at=time.time(),
         owned_process_tree_closed=True,
         native_launch_present_at_finish=(owner / 'kiro/launch.json').exists())
    print(record['status'], flush=True)
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
