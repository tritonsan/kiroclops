"""Qualify an unwired private-desktop renderer on disposable baseline copies.

No model is invoked. The registered sessions and implementations are not edited.
Only real Godot GUI launches are routed through the private-desktop runner.
"""
from pathlib import Path
import argparse
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration import ground_runner, runner
from kiroclops.integration.private_desktop_v1 import run_private_desktop
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json


def check(output, repeats=1):
    output = Path(output).resolve()
    parent = (ROOT / 'artifacts/model-trials/preflight').resolve()
    if output.parent != parent or output.exists() or repeats not in (1, 2, 3):
        raise ValueError('Use a fresh immediate preflight child and one to three repetitions')
    source = ROOT / 'sessions/cli-ground-01/baseline'
    expected = read_json(ROOT / 'sessions/cli-ground-01/session.json')['baseline_hashes']
    if project_hashes(source) != expected:
        raise ValueError('Historical baseline source changed')
    gui = (ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe').resolve()
    if not gui.is_file() or gui.stat().st_size < 10_000_000:
        raise ValueError('The direct pinned GUI engine is required')
    previous_root = ROOT / 'sessions/cli-ground-01/baseline-runs/calibration'
    previous = {name: previous_root / f'{name}-x0-r1' for name in ('r06', 'r48')}
    for old in previous.values():
        provenance = read_json(old / 'manifest.json')['provenance']
        if provenance['full_source_hashes_before'] != expected or provenance['full_source_hashes_after'] != expected:
            raise ValueError('Historical frames do not bind the chosen unchanged baseline')
    output.mkdir()
    project = copy_project(source, output / 'project')
    result = {'schema_version': 1, 'status': 'running', 'started_at': utc_now(),
              'scope': 'Separate renderer qualification only; not model-cohort acceptance',
              'source': str(source), 'source_hashes': expected, 'checks': [],
              'code_hashes': {str(p.relative_to(ROOT)): sha256(p) for p in (
                  Path(__file__).resolve(), ROOT / 'kiroclops/integration/private_desktop_v1.py',
                  ROOT / 'kiroclops/integration/runner.py', ROOT / 'kiroclops/integration/ground_runner.py')},
              'direct_gui_engine': str(gui), 'direct_gui_sha256': sha256(gui)}
    write_json(output / 'result.json', result)
    original = runner._run_process
    launches = []
    started = time.monotonic()

    def isolated(args, *, cwd, env, log_path, cancel=None, timeout=120):
        # This is a local function substitution in this disposable qualification
        # process, not a mutation of the registered worker's source or sessions.
        if Path(args[0]).resolve() not in {gui, gui.with_name(gui.stem + '_console.exe')} or '--headless' in args or '--version' in args:
            return original(args, cwd=cwd, env=env, log_path=log_path, cancel=cancel, timeout=timeout)
        log_path = Path(log_path)
        destination = log_path.parent / 'private-desktop'
        argv = [str(gui), *map(str, args[1:])]
        try:
            record = run_private_desktop(argv, cwd=Path(cwd), env=env,
                                         output_dir=destination, timeout=timeout, cancel=cancel)
        finally:
            chunks = [p.read_bytes() for p in (destination / 'stdout.log', destination / 'stderr.log') if p.is_file()]
            log_path.write_bytes(b'\n'.join(chunks))
        launches.append({'path': str(destination / 'process.json'), 'record': record})
        if record.get('status') not in {'completed', 'failed'} or record.get('cleanup_errors') or not record.get('owned_process_termination_confirmed'):
            raise runner.RunnerError('isolation_error', 'Private-desktop process did not complete and clean up', {'receipt': str(destination / 'process.json')})
        samples = record.get('desktop_samples', [])
        if not samples or any(s.get('receives_input') is not False for s in samples):
            raise runner.RunnerError('isolation_error', 'Missing non-input desktop proof')
        return record['exit_code']

    runner._run_process = isolated
    try:
        ground_runner.import_project(project, game='slay', runtime_dir=output / 'runtime/import')
        if project_hashes(project) != expected:
            raise ValueError('Import changed source bytes')
        for name, old in previous.items():
            for repetition in range(1, repeats + 1):
                scenario = dict(read_json(old / 'manifest.json')['scenario'], repeat=repetition)
                run = ground_runner.capture_project(project, output / f'{name}-repeat-{repetition}',
                                                    game='slay', scenario=scenario, variant='before')
                capture_manifest = read_json(run / 'manifest.json')
                mismatched, frames = [], {}
                for index in range(240):
                    relative = f'frames/{index:06d}.png'
                    old_hash, new_hash = sha256(old / relative), sha256(run / relative)
                    frames[relative] = {'reference': old_hash, 'private_desktop': new_hash}
                    if old_hash != new_hash:
                        mismatched.append(index)
                receipt = run / 'private-desktop/process.json'
                process = read_json(receipt)
                item = {'scenario': name, 'repetition': repetition, 'run': str(run), 'reference': str(old),
                        'reference_manifest_sha256': sha256(old / 'manifest.json'),
                        'capture_manifest_sha256': sha256(run / 'manifest.json'),
                        'process_receipt_sha256': sha256(receipt), 'frame_hashes': frames,
                        'different_frames': mismatched, 'desktop_samples': len(process['desktop_samples']),
                        'saw_owned_window': process.get('saw_owned_window', False),
                        'capture_wall_seconds': capture_manifest['provenance']['wall_seconds']}
                item['status'] = 'pass' if (not mismatched and item['saw_owned_window']
                    and capture_manifest['viewport'] == [640, 360] and capture_manifest['frame_count'] == 240
                    and capture_manifest['fps'] == 60 and project_hashes(project) == expected
                    and project_hashes(source) == expected) else 'fail'
                result['checks'].append(item)
                write_json(output / 'result.json', result)
                print(json.dumps({k: item[k] for k in ('scenario', 'repetition', 'status', 'different_frames', 'saw_owned_window', 'capture_wall_seconds')}), flush=True)
                if item['status'] != 'pass':
                    raise ValueError('Renderer proof failed; do not start model trials')
        result['status'] = 'pass'
    except BaseException as error:
        result.update(status='fail', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        runner._run_process = original
        result.update(finished_at=utc_now(), elapsed_seconds=time.monotonic() - started,
                      source_preserved=project_hashes(source) == expected,
                      project_preserved=project_hashes(project) == expected,
                      private_launch_receipts=[{'path': row['path'], 'sha256': sha256(row['path'])} for row in launches])
        if not result['source_preserved'] or not result['project_preserved']:
            result['status'] = 'fail'
        write_json(output / 'result.json', result)
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    raise SystemExit(check(args.output, args.repeats))
