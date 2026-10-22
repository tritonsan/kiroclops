"""Opt-in CLI execution repairs; frozen experiment and old sessions stay intact.

The protocol changes isolation, verified asset/version reuse and the explicitly
registered steady-speed estimator. Physics, other gates, scenario counts,
clocks and candidate source code are preserved.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

from kiroclops.common import ROOT
from .session import Session
from .storage import IntegrationError, project_hashes, read_json, sha256, write_json, utc_now

PROTOCOL_ID = 'ground-cli-repair-v2'
MODULE = 'kiroclops.integration.repair_workflow_v2'
_SUBMIT_LOCK = threading.RLock()
NEW_SOURCES = (
    'kiroclops/integration/repair_workflow_v2.py',
    'kiroclops/integration/isolated_runtime_v2.py',
    'kiroclops/integration/private_desktop_v1.py',
    'kiroclops/integration/import_cache_v1.py',
    'kiroclops/integration/ground_speed_v2.py',
    'kiroclops/integration/ground_comparison_v2.py',
)
INSTRUCTIONS = '''These instructions belong to the ground-cli-repair-v2 execution protocol.
Use only this fresh workspace and its seven MCP tools. The curve, braking,
endpoint, jump gates, three-candidate limit and 30-minute clock are unchanged. Only calibration
r06/r48 may guide edits. Do not seek held-out evidence before finalizing.

Finalize immediately once an evaluation says eligible=true and gameplay/jump
checks pass. Three candidates is a maximum, not a requirement. Do not spend a
new candidate merely to reduce an already acceptable calibration error: final
validation needs time. get_job while an operation runs; do not prematurely end
your turn. If no candidate is eligible, fix only the reported problems within
the remaining budget. Never treat a recording/tool failure as a motion result.

Ground-specific movement must check actual floor contact at the call site;
the Idle state name alone does not prove floor contact. Preserve existing air,
double-jump, wall-jump and vertical-jump paths. Tools provide measurements, not
a controller patch. You must write the actual target GDScript yourself.

Steady-speed measurement is explicitly revised: only a final held leg of at
least 30 ticks is eligible; its fixed last two-thirds (maximum 32/minimum 20 frames)
must support constant speed within native pixel rounding. Every feasible
reference/target speed pair must meet the unchanged 10% limit. Short r06/r18
and reversal legs still require all curve/braking/endpoint gates. Uncertain
long-hold speed is not a pass. Legacy speed_body_s in raw exports is only an
eight-frame diagnostic; use the evaluation's speed_comparison as authoritative.
'''


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _source_binding():
    original = read_json(ROOT / 'config/model-trials/sol-v1.json')
    files = set(NEW_SOURCES)
    files.update(original['launcher']['source_hashes'])
    files.update(original['reporter_source_hashes'])
    files.update(read_json(ROOT / 'config/phase2b/tracking-lock.json')['files'])
    return {name: sha256(ROOT / name) for name in sorted(files)}


def verify_protocol(directory):
    session = Session(directory)
    state = session.read()
    marker = state.get('execution_protocol', {})
    path = session.directory / 'execution-protocol.json'
    if marker.get('id') != PROTOCOL_ID or not path.is_file() or sha256(path) != marker.get('sha256'):
        raise IntegrationError('execution_protocol_changed', 'A registered repair session is required')
    record = read_json(path)
    if (record.get('protocol_id') != PROTOCOL_ID or record.get('session_id') != session.directory.name
            or record.get('measurement_policy') != 'ground-speed-long-hold-v2'
            or record.get('source_hashes') != _source_binding()
            or record.get('baseline_hashes') != state.get('baseline_hashes')):
        raise IntegrationError('execution_protocol_changed', 'Repair code, baseline, or policy binding changed')
    from .experiments import get_experiment, GROUND_ID
    from .ground_speed_v2 import LONG_HOLD_POLICY
    if (state.get('schema_version') != 3 or state.get('experiment_id') != GROUND_ID
            or state.get('mode') != 'tools' or state.get('session_id') != session.directory.name
            or Path(state.get('baseline', '')).resolve() != session.directory / 'baseline'
            or Path(state.get('target', '')).resolve() != session.directory / 'target'
            or state.get('experiment_spec') != get_experiment(GROUND_ID).record()
            or state.get('prepared_baseline_hashes') != record['baseline_hashes']
            or project_hashes(state['baseline']) != record['baseline_hashes']
            or record.get('speed_policy') != LONG_HOLD_POLICY.record()
            or record.get('limits') != {'max_candidates': 3, 'max_seconds': 1800}):
        raise IntegrationError('execution_protocol_changed', 'Live baseline, experiment contract or speed policy changed')
    engine = ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe'
    if sha256(engine) != record.get('engine_sha256'):
        raise IntegrationError('execution_protocol_changed', 'The pinned engine changed')
    if not state.get('execution_controls_sha256'):
        raise IntegrationError('execution_controls_changed', 'Registered CLI controls are required')
    if state['execution_controls_sha256']:
        controls = session.directory / 'execution-controls.json'
        if not controls.is_file() or sha256(controls) != state['execution_controls_sha256']:
            raise IntegrationError('execution_controls_changed', 'CLI control registration changed')
        registered = read_json(controls)
        if registered.get('protocol') != marker:
            raise IntegrationError('execution_controls_changed', 'CLI control protocol differs')
        config_root = Path(state['target']) / '.kiro'
        config = read_json(config_root / 'kiroclops-session.json')
        if (config.get('execution_protocol') != marker
                or config.get('generated_hashes') != registered['generated_hashes']):
            raise IntegrationError('execution_controls_changed', 'CLI controls differ from registration')
        for relative, expected in registered['generated_hashes'].items():
            path = (config_root / relative).resolve()
            if not path.is_relative_to(config_root.resolve()) or not path.is_file() or sha256(path) != expected:
                raise IntegrationError('execution_controls_changed', 'A registered CLI control changed')
    return record


def _rewrite_server(value, session):
    """Change only an exact generated local MCP launch, before first use."""
    changed = 0
    if isinstance(value, dict):
        if value.get('args') == ['-m', 'kiroclops.integration.mcp', '--session', str(session)]:
            value['args'] = ['-m', MODULE, 'mcp', '--session', str(session)]
            changed += 1
        for child in value.values():
            changed += _rewrite_server(child, session)
    elif isinstance(value, list):
        for child in value:
            changed += _rewrite_server(child, session)
    return changed


def prepare(name):
    """Fresh source + precomputed asset cache. No model invocation."""
    from .ground_worker import provision_session
    from .import_cache_v1 import publish_import_cache
    from .model_cli_v1 import configure_model_cli
    directory = provision_session(name, configure=False)
    session = Session(directory)
    state = session.read()
    engine = ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe'
    cache = publish_import_cache(Path(state['baseline']), directory / 'runtime/asset-cache',
        engine=engine, expected_source_hashes=state['baseline_hashes'])
    from .ground_speed_v2 import LONG_HOLD_POLICY
    record = {'schema_version': 1, 'protocol_id': PROTOCOL_ID, 'session_id': directory.name,
              'registered_at': utc_now(), 'measurement_policy': 'ground-speed-long-hold-v2',
              'speed_policy': LONG_HOLD_POLICY.record(),
              'source_hashes': _source_binding(), 'engine_sha256': sha256(engine),
              'baseline_hashes': state['baseline_hashes'], 'asset_cache': cache,
              'limits': {'max_candidates': 3, 'max_seconds': 1800},
              'scope': 'New CLI diagnostic run, not a replacement for any old cohort slot'}
    path = write_json(directory / 'execution-protocol.json', record)
    marker = {'id': PROTOCOL_ID, 'sha256': sha256(path)}
    session.update(lambda s: s.update(execution_protocol=marker))
    config = configure_model_cli(directory, model='gpt-5.6-sol', effort='high',
                                 supplemental_instructions=INSTRUCTIONS)
    controls = Path(config['target_dir']) / '.kiro'
    rewrites = 0
    for relative in config['generated_hashes']:
        file = controls / relative
        if file.suffix == '.json':
            value = read_json(file)
            changes = _rewrite_server(value, directory)
            if changes:
                write_json(file, value)
                rewrites += changes
                config['generated_hashes'][relative] = sha256(file)
    if rewrites < 1:
        raise IntegrationError('repair_config_missing', 'No generated MCP server could be bound to repairs')
    config['execution_protocol'] = marker
    write_json(controls / 'kiroclops-session.json', config)
    control_path = write_json(directory / 'execution-controls.json', {'protocol': marker,
        'generated_hashes': config['generated_hashes'], 'registered_at': utc_now()})
    session.update(lambda s: s.update(execution_controls_sha256=sha256(control_path)))
    verify_protocol(directory)
    return directory


class _SpawnProxy:
    def __init__(self, original, directory):
        self.original, self.directory = original, Path(directory)

    def __getattr__(self, name):
        return getattr(self.original, name)

    def Popen(self, args, *positional, **kwargs):
        expected = ['-m', 'kiroclops.integration.worker', '--session', str(self.directory), '--job']
        if (not isinstance(args, (list, tuple)) or len(args) != 7
                or list(args[1:6]) != expected or Path(args[0]).resolve() != Path(sys.executable).resolve()):
            raise IntegrationError('unexpected_worker', 'Repair session may launch only its exact owned worker')
        rewritten = [args[0], '-m', MODULE, 'worker', '--session', str(self.directory), '--job', args[6]]
        return self.original.Popen(rewritten, *positional, **kwargs)


class RepairSession(Session):
    def __init__(self, directory):
        super().__init__(directory)
        verify_protocol(directory)

    def inspect(self):
        record = verify_protocol(self.directory)
        return {**super().inspect(), 'execution_protocol': record['protocol_id'],
                'measurement_policy': record['measurement_policy'],
                'next_step': 'Finalize an eligible candidate promptly; three attempts is a maximum.'}

    def submit(self, action, candidate_id=None):
        verify_protocol(self.directory)
        from . import session as session_module
        # Change this module reference only, never subprocess.Popen process-wide.
        # The lock covers the short reservation/spawn operation, not job duration.
        with _SUBMIT_LOCK:
            original = session_module.subprocess
            session_module.subprocess = _SpawnProxy(original, self.directory)
            try:
                return super().submit(action, candidate_id)
            finally:
                session_module.subprocess = original


@contextmanager
def repaired_imports(directory, record):
    from . import ground_runner
    from .import_cache_v1 import ImportCacheError, seed_import_cache
    original = ground_runner.import_project
    session = Session(directory)
    engine = ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe'

    def cached(project, game='slay', **kwargs):
        project = Path(project).resolve()
        if game != 'slay' or not project.is_relative_to(session.directory):
            raise IntegrationError('unexpected_import', 'Repair import is restricted to its session Slay copies')
        expected = project_hashes(project)
        receipt = None
        started = time.perf_counter()
        if not (project / '.godot').exists():
            cache = record['asset_cache']
            try:
                receipt = seed_import_cache(cache['cache_dir'], project, engine=engine,
                    expected_source_hashes=expected, expected_manifest_sha256=cache['manifest_sha256'],
                    cancel=kwargs.get('cancel'))
            except ImportCacheError as error:
                raise IntegrationError('import_cache_error', str(error)) from error
        result = original(project, game=game, **kwargs)
        if project_hashes(project) != expected:
            raise IntegrationError('source_changed', 'Import modified the candidate source')
        runtime = kwargs.get('runtime_dir')
        if runtime:
            write_json(Path(runtime) / 'repair-import.json', {'protocol_id': PROTOCOL_ID,
                'seed': receipt, 'editor_import_completed': True,
                'elapsed_seconds': time.perf_counter()-started, 'source_hashes': expected})
        return result

    ground_runner.import_project = cached
    try:
        yield
    finally:
        ground_runner.import_project = original


def run_worker(directory, job_id):
    from .isolated_runtime_v2 import isolated_runtime
    from .ground_comparison_v2 import repaired_measurements
    from .worker import run_job
    session = Session(directory)
    record = verify_protocol(directory)
    job = read_json(session.directory / 'jobs' / (job_id + '.json'))
    if job.get('job_id') != job_id or job.get('session_id') != session.directory.name:
        raise IntegrationError('unexpected_job', 'Job is not bound to this session')
    with isolated_runtime(project_roots=[session.directory]), repaired_imports(directory, record), repaired_measurements():
        run_job(directory, job_id)
    if job['action'] == 'report':
        completed = read_json(session.directory / 'jobs' / (job_id + '.json'))
        if completed.get('status') == 'succeeded':
            report_path = Path(completed['result']['report_path']).resolve()
            if not report_path.is_relative_to(session.directory):
                raise IntegrationError('report_path', 'Repair report escaped its session')
            _label_report(report_path)


@contextmanager
def operator_context(directory):
    """Verified v2 measurement context for this session's reviews and reports.

    Do not mix old and new sessions within this context. Old standalone review
    commands deliberately retain their original measurement policy.
    """
    from .ground_comparison_v2 import repaired_measurements
    verify_protocol(directory)
    with repaired_measurements():
        yield


def build_report(directory):
    from .ground_reporting import generate_ground_report
    with operator_context(directory):
        path = generate_ground_report(directory)
    return _label_report(path)


def _label_report(path):
    from html import escape
    path = Path(path)
    content = path.read_text(encoding='utf-8')
    banner = ('<section style="margin:1rem;padding:1rem;border:2px solid #4096ff">'
        '<strong>' + escape(PROTOCOL_ID) + '</strong> — New CLI diagnostic run. '
        'Steady speed is evaluated only on holds of at least 30 steps, with a fixed long window and '
        'pixel uncertainty; the speed threshold is 10%. '
        'Curve, braking and final-position checks for short and reversal movements are retained. '
        'It is not pooled with earlier eight-frame experiment groups.</section>')
    opening = content.find('>', content.find('<body'))
    if opening < 0 or '<body' not in content:
        raise IntegrationError('report_format', 'Expected a standalone HTML report')
    path.write_text(content[:opening+1] + banner + content[opening+1:], encoding='utf-8')
    return path


def run_mcp(directory):
    from . import mcp as original
    previous = original.Session
    original.Session = RepairSession
    try:
        server, session = original.create_server(directory)
    finally:
        original.Session = previous
    from .power_lifetime import start_launcher_watch
    stop = None
    try:
        stop = start_launcher_watch(session)
        server.run(transport='stdio')
    finally:
        if stop is not None:
            stop.set()
        session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'mcp', 'worker', 'preflight', 'run', 'report', 'review', 'verify-review'])
    parser.add_argument('--session', required=True)
    parser.add_argument('--job')
    parser.add_argument('--controls', type=Path)
    parser.add_argument('--source', choices=['human', 'assistant_visual'], default='human')
    parser.add_argument('--reuse-ground-controls', type=Path, nargs='*')
    parser.add_argument('--reuse-jump-controls', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        print(prepare(args.session))
    else:
        directory = ROOT / 'sessions' / args.session if Path(args.session).parent == Path('.') else Path(args.session)
        verify_protocol(directory)
        if args.action == 'mcp':
            run_mcp(directory)
        elif args.action == 'worker':
            if not args.job:
                parser.error('--job is required for worker')
            run_worker(directory, args.job)
        elif args.action == 'report':
            print(build_report(directory))
        elif args.action in {'review', 'verify-review'}:
            from .ground_review import review_session, verify_review_receipt
            with operator_context(directory):
                result = (verify_review_receipt(directory) if args.action == 'verify-review' else
                    review_session(directory, args.controls, source=args.source,
                        reuse_ground_controls_from=args.reuse_ground_controls,
                        reuse_jump_controls_from=args.reuse_jump_controls))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from .model_cli_v1 import preflight_model_cli, run_model_cli
            from .ground_worker import verify_session
            verify_session(Session(directory))
            result = preflight_model_cli(directory) if args.action == 'preflight' else run_model_cli(directory)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if args.action == 'preflight' or result.get('status') == 'completed' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
