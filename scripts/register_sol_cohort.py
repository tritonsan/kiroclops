"""Register exactly three fresh Sol CLI trials after independent preflights."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration.background_capture_v1 import verify_background_session
from kiroclops.integration.experiments import verify_ground_lock
from kiroclops.integration.model_cli_v1 import launcher_binding, load_model_config
from kiroclops.integration.model_ground_benchmark_v1 import reporter_source_hashes, _background_preflight, _control_preflight
from kiroclops.integration.storage import project_hashes, read_json, sha256, utc_now


def register(background, controls):
    destination = ROOT / 'config/model-trials/sol-v1.json'
    if destination.exists():
        raise ValueError('The registered cohort is immutable; it will not be replaced')
    identifiers = [f'cli-sol-ground-{n:02d}' for n in (1, 2, 3)]
    lock = verify_ground_lock(root=ROOT)
    baseline = None
    instruction_hash = sha256(ROOT / 'config/model-trials/sol-instructions.md')
    sessions = {}
    for name in identifiers:
        directory = ROOT / 'sessions' / name
        state = read_json(directory / 'session.json')
        config = load_model_config(directory)
        if (state['status'] != 'new' or state.get('started_at') is not None
                or state.get('deadline_at') is not None or state.get('candidates')
                or state.get('active_job_id') or state.get('selected_candidate_id')
                or (directory / 'kiro/launch.json').exists()):
            raise ValueError(f'The cohort must be registered before any model run: {name}')
        verify_background_session(directory)
        actual = project_hashes(directory / 'baseline')
        if actual != state['baseline_hashes'] or project_hashes(directory / 'target') != actual:
            raise ValueError('A fresh source snapshot changed before registration')
        if baseline is not None and baseline != actual:
            raise ValueError('The three starting source maps differ')
        baseline = actual
        if config.get('supplemental_instructions', {}).get('sha256') != instruction_hash:
            raise ValueError('Supplemental instructions differ between trials')
        sessions[name] = {'state_sha256': sha256(directory / 'session.json'),
                          'config_sha256': sha256(directory / 'target/.kiro/kiroclops-session.json'),
                          'background_receipt_sha256': sha256(directory / 'background-capture-v1.json')}
    ready = read_json(ROOT / 'sessions/cli-sol-ground-01/kiro/preflight/result.json')
    if ready.get('prompt_sent') is not False or ready.get('selection_echo_verified') is not True:
        raise ValueError('A no-prompt live model/effort preflight is required')
    def bound(path):
        path = Path(path).resolve()
        return {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha256(path)}
    record = {'schema_version': 1, 'cohort_id': 'phase2b-sol-v1',
              'experiment_id': 'ground-motion-v1', 'model': 'gpt-5.6-sol', 'effort': 'high',
              'registered_at': utc_now(), 'cli_session_ids': identifiers,
              'ide_session_ids': [], 'baseline_session_ids': [], 'replacement_runs': False,
              'max_candidates': 3, 'max_seconds': 1800,
              'cli_version': ready['cli_version'], 'kas_version': ready['kas_version'],
              'launcher': launcher_binding(), 'reporter_source_hashes': reporter_source_hashes(),
              'supplemental_instructions_sha256': instruction_hash,
              'background_implementation_sha256': sha256(ROOT / 'kiroclops/integration/background_capture_v1.py'),
              'background_preflight': bound(background), 'control_preflight': bound(controls),
              'experiment_locks': {key: lock[key] for key in ('tracking_lock_sha256', 'reference_lock_sha256')},
              'starting_source_hashes': baseline, 'prepared_sessions': sessions,
              'registrar_sha256': sha256(Path(__file__)),
              'note': 'Separate Sol CLI cohort; historical Opus failures remain unchanged.'}
    for name, verifier in (('background', _background_preflight), ('controls', _control_preflight)):
        result = verifier(record)
        if result.get('status') != 'pass':
            raise ValueError(f'{name} preflight did not independently verify: {result}')
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--background-preflight', required=True, type=Path)
    parser.add_argument('--control-preflight', required=True, type=Path)
    args = parser.parse_args()
    register(args.background_preflight, args.control_preflight)
