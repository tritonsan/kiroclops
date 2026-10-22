"""Fresh max-effort trial with prospective airborne differential preservation.

Old sessions stay intact. This protocol adds differential airborne preservation
to explicit Sol/max, calibration diagnostics and qualified private rendering.
Existing motion gates, scenarios and budget stay fixed. Only Kiro writes the
actual candidate controller.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import threading
import time

from kiroclops.common import ROOT
from .session import Session
from .storage import IntegrationError, project_hashes, read_json, sha256, write_json, utc_now

PROTOCOL_ID = 'ground-cli-autonomous-v4'
MODULE = 'kiroclops.integration.autonomous_workflow_v4'
AIRBORNE_QUALIFICATION = 'airborne-regression-v1-02'
_SUBMIT_LOCK = threading.RLock()
_AIRBORNE_LOCK = threading.Lock()
NEW_SOURCES = (
    'kiroclops/integration/repair_workflow_v2.py',
    'kiroclops/integration/model_cli_max_v1.py',
    'kiroclops/integration/calibration_motion_feedback_v3.py',
    'kiroclops/integration/autonomous_workflow_v3.py',
    'kiroclops/integration/autonomous_workflow_v4.py',
    'kiroclops/integration/autonomous_review_v4.py',
    'kiroclops/integration/airborne_regression_v1.py',
    'kiroclops/integration/isolated_runtime_v2.py',
    'kiroclops/integration/isolated_runtime_v3.py',
    'kiroclops/integration/private_desktop_v1.py',
    'kiroclops/integration/import_cache_v1.py',
    'kiroclops/integration/ground_speed_v2.py',
    'kiroclops/integration/ground_comparison_v2.py',
    'kiroclops/integration/normal_gameplay_v2.py',
    'adapters/normal-gameplay-v2-godot4/Capture.tscn',
    'adapters/normal-gameplay-v2-godot4/capture.gd',
)
INSTRUCTIONS = """These instructions belong to ground-cli-autonomous-v4.
Use only this fresh workspace, native read/write and the seven KiroClops tools.
The three-candidate limit, 30-minute clock and all acceptance gates are unchanged.
Only the r06/r48 calibration recordings may guide edits. No operator will advise
or repair your code during this run. You author the real controller yourself.

Preparation records the unchanged controller's airborne integrity controls.
Each candidate receives the same isolated controls and differential feedback.
Consume that feedback and preserve the inherited airborne response; a motion
score cannot override a failed integrity check. These deliberately seeded
controls are source-preservation diagnostics, not evidence that every seeded
state is reachable through normal play. They compare a finite set of velocity,
state and predicate responses; they do not prove full displacement equivalence.
Independent static source, jump and gameplay checks remain required.
Investigate your own code; do not add
test-case branches or reconstruct a replay from control outputs.
If valid airborne controls already prove a preservation failure, the candidate
evaluation returns early as ineligible. Ground/jump/smoke checks are explicitly
not_run in that result and no motion score exists. It still consumes one of the
three candidates. Correct the reported integrity discrepancy using your own
source reasoning; skipped checks are never passes. Invalid captures and cancelled
jobs remain tool failures, not completed semantic rejections.

Use the preparation's motion_diagnostics and measured profile to reason about
the entire discrete update, including the relationship of action changes,
velocity updates, state transitions and movement. Preserve grounded-only scope
and existing airborne and vertical paths. Ground contact is a physical condition,
not a guarantee implied by a state name. Do not branch on experiment scenarios.

Calibration eligibility permits validation; it does not require stopping at the
first eligible result. Read the candidate's motion_diagnostics. When it recommends
refinement and enough time remains for evaluation plus final validation, use the
measured residuals and your own reading of the target code to investigate and
correct the discrepancy. Do not tune parameters just because a summary score is
imperfect; identify a causal mismatch. Report remaining ambiguity honestly.
Diagnostics compare fixed profile predictions with pixels; their lag hypotheses
are explanations, never permission to time-shift acceptance or replay positions.
A well-fitting forward/release model alone cannot prove unobserved input branches.

Manage remaining time using inspect_session and job results; reserve time for
finalize_candidate and build_report. Its runtime_budget_hint is a conservative
planning estimate from local qualification, not a guarantee. Do not end the turn while a job is running.
The finalizer chooses the lowest-error eligible candidate. After sealing, do not
edit or return to calibration. Report a failed or incomplete outcome as such.

Steady-speed gates use the registered long-hold-v2 policy, not raw last-eight-frame
speed scalars. Only final held legs >=30 ticks qualify; every native-rounding
feasible speed pair must meet 10%. All other curve/braking/endpoint/jump/gameplay
gates stay in force, including short and reversal scenarios. Tool failure is
separate from motion failure. Numerical and independent visual acceptance remain
separate statuses. No reference source or previous ground solution is available.
"""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def early_rejection_policy():
    return {'id': 'valid-airborne-failure-short-circuit-v1', 'counts_as_candidate': True,
        'valid_differential_fail': 'evaluated_ineligible', 'failure_class': 'gameplay_integrity_violation',
        'skipped': ['ground_calibration', 'jump_calibration', 'normal_gameplay_smoke'],
        'motion_score': None, 'invalid_or_cancelled': 'failed_or_cancelled_job',
        'eligible_candidates_require_all_original_checks': True}


def _source_binding():
    original = read_json(ROOT / 'config/model-trials/sol-v1.json')
    files = set(NEW_SOURCES)
    files.update(original['launcher']['source_hashes'])
    files.update(original['reporter_source_hashes'])
    files.update(read_json(ROOT / 'config/phase2b/tracking-lock.json')['files'])
    from .airborne_regression_v1 import ADAPTER_SOURCE, protocol_record
    files.update((Path(ADAPTER_SOURCE) / name).as_posix() for name in protocol_record()['adapter_hashes'])
    return {name: sha256(ROOT / name) for name in sorted(files)}


def _qualifications(*, replay_airborne=False):
    """Bind completed prospective receipts; no raw footage enters model inputs."""
    records = {}
    cases = {
        'runtime': ('isolated-runtime-v3-01', 'kiroclops/integration/isolated_runtime_v3.py'),
        'feedback': ('calibration-feedback-v3-01', 'kiroclops/integration/calibration_motion_feedback_v3.py'),
        'normal_gameplay': ('normal-gameplay-v2-01', 'kiroclops/integration/normal_gameplay_v2.py'),
    }
    for role, (folder, module) in cases.items():
        path = ROOT / 'artifacts/model-trials/preflight' / folder / 'result.json'
        if not path.is_file():
            raise IntegrationError('qualification_missing', f'Complete {role} qualification first')
        value = read_json(path)
        bound = (value.get('code_hashes', {}).get(module) if role == 'runtime' else
                 value.get('module_sha256') if role == 'feedback' else
                 value.get('protocol', {}).get('source_hashes', {}).get(module))
        if value.get('status') != 'pass' or bound != sha256(ROOT / module):
            raise IntegrationError('qualification_invalid', f'{role} has no passing receipt for current code')
        records[role] = {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha256(path),
                         'module': module, 'module_sha256': bound}
    directory = ROOT / 'artifacts/model-trials/preflight' / AIRBORNE_QUALIFICATION
    path = directory / 'qualification.json'
    if not path.is_file() or read_json(path).get('status') != 'pass':
        raise IntegrationError('qualification_invalid', 'Airborne differential native controls have not passed fresh verification')
    # Full source/raw replay is a pre-provision requirement. Routine MCP polls
    # compare this exact receipt hash with its registered value, not every PNG.
    if replay_airborne:
        from .airborne_regression_v1 import verify_qualification
        if verify_qualification(directory).get('status') != 'pass':
            raise IntegrationError('qualification_invalid', 'Airborne native qualification does not replay')
    records['airborne_integrity'] = {'path': path.relative_to(ROOT).as_posix(),
        'sha256': sha256(path), 'module': 'kiroclops/integration/airborne_regression_v1.py',
        'module_sha256': sha256(ROOT / 'kiroclops/integration/airborne_regression_v1.py')}
    return records


def _runtime_budget_hint():
    """Prospective planning from actual complete jobs plus qualified additions."""
    previous = ROOT / 'sessions/cli-autonomous-v3-01'
    launch_path = previous / 'kiro/launch.json'
    launch = read_json(launch_path)
    if launch.get('status') != 'completed' or launch.get('prompt_result', {}).get('stopReason') != 'end_turn':
        raise IntegrationError('qualification_invalid', 'Timing observations require a completed preceding trial')
    observations = {name: [] for name in ('prepare', 'evaluate', 'finalize', 'report')}
    evidence = {launch_path.relative_to(ROOT).as_posix(): sha256(launch_path)}
    for path in sorted((previous / 'jobs').glob('job-*.json')):
        job = read_json(path)
        if job.get('action') not in observations:
            continue
        if (job.get('status') != 'succeeded' or job.get('job_id') != path.stem
                or job.get('session_id') != previous.name):
            raise IntegrationError('qualification_invalid', 'Only bound successful job durations may inform planning')
        start = datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(job['finished_at'].replace('Z', '+00:00'))
        duration = (end - start).total_seconds()
        if start.tzinfo is None or end.tzinfo is None or not math.isfinite(duration) or duration <= 0:
            raise IntegrationError('qualification_invalid', 'Completed job timing is invalid')
        observations[job['action']].append(duration)
        evidence[path.relative_to(ROOT).as_posix()] = sha256(path)
    if not all(observations.values()):
        raise IntegrationError('qualification_invalid', 'All preceding job types require observed durations')
    path = ROOT / 'artifacts/model-trials/preflight' / AIRBORNE_QUALIFICATION / 'qualification.json'
    qualified = read_json(path)
    full_calls = [row['full_call_seconds'] for row in qualified.get('suites', {}).values()]
    comparisons = list(qualified.get('comparison_timing_seconds', {}).values())
    if (qualified.get('status') != 'pass' or len(full_calls) != 4 or len(comparisons) != 3
            or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in full_calls + comparisons)):
        raise IntegrationError('qualification_invalid', 'Qualified suite and differential replay durations are required')
    suite, replay = max(full_calls), max(comparisons)
    evidence[path.relative_to(ROOT).as_posix()] = sha256(path)
    margins = {'prepare': 10, 'evaluate': 20, 'finalize': 30, 'report': 5}
    return {'source': 'complete preceding v3 jobs plus qualified new suite and differential replay wall times',
        'observed_job_seconds': observations, 'observed_max_airborne_suite_seconds': suite,
        'observed_max_airborne_comparison_seconds': replay, 'timing_evidence_sha256': evidence,
        'prepare_planning_seconds': math.ceil(max(observations['prepare']) + suite + margins['prepare']),
        'evaluate_planning_seconds': math.ceil(max(observations['evaluate']) + suite + 2 * replay + margins['evaluate']),
        'early_integrity_rejection_planning_seconds': math.ceil(suite + 2 * replay + margins['evaluate']),
        'finalize_planning_seconds': math.ceil(max(observations['finalize']) + 3 * replay + margins['finalize']),
        'report_planning_seconds': math.ceil(max(observations['report']) + margins['report']),
        'explicit_margin_seconds': margins,
        'differential_replays_per_evaluation': 2,
        'differential_replays_per_finalization': 3,
        'fresh_captures_per_evaluation': 17, 'fresh_captures_per_finalization': 48,
        'fresh_captures_per_early_integrity_rejection': 1,
        'additional_airborne_suite_captures': {'prepare': 1, 'evaluate': 1, 'finalize': 0},
        'airborne_suite_timing_qualified': True, 'not_a_guarantee': True,
        'budget_or_acceptance_modified': False,
        'limits': 'Small local timing sample; model thinking and workload variation can still exhaust the unchanged 1800-second clock.'}


def verify_protocol(directory):
    session = Session(directory)
    state = session.read()
    marker = state.get('execution_protocol', {})
    path = session.directory / 'execution-protocol.json'
    if marker.get('id') != PROTOCOL_ID or not path.is_file() or sha256(path) != marker.get('sha256'):
        raise IntegrationError('execution_protocol_changed', 'A registered repair session is required')
    record = read_json(path)
    if (record.get('protocol_id') != PROTOCOL_ID or record.get('session_id') != session.directory.name
            or record.get('model') != 'gpt-5.6-sol' or record.get('effort') != 'max'
            or record.get('calibration_diagnostics') != 'calibration-motion-feedback-v3'
            or record.get('measurement_policy') != 'ground-speed-long-hold-v2'
            or record.get('source_hashes') != _source_binding()
            or record.get('qualification_receipts') != _qualifications()
            or record.get('runtime_budget_hint') != _runtime_budget_hint()
            or record.get('baseline_hashes') != state.get('baseline_hashes')):
        raise IntegrationError('execution_protocol_changed', 'Repair code, baseline, or policy binding changed')
    from .experiments import get_experiment, GROUND_ID
    from .ground_speed_v2 import LONG_HOLD_POLICY
    from .isolated_runtime_v3 import PRESENTATION_POLICY
    from .calibration_motion_feedback_v3 import POLICY as DIAGNOSTIC_POLICY
    from .normal_gameplay_v2 import protocol_record as normal_protocol
    from .airborne_regression_v1 import protocol_record as airborne_protocol
    if (state.get('schema_version') != 3 or state.get('experiment_id') != GROUND_ID
            or state.get('mode') != 'tools' or state.get('session_id') != session.directory.name
            or Path(state.get('baseline', '')).resolve() != session.directory / 'baseline'
            or Path(state.get('target', '')).resolve() != session.directory / 'target'
            or state.get('experiment_spec') != get_experiment(GROUND_ID).record()
            or state.get('prepared_baseline_hashes') != record['baseline_hashes']
            or project_hashes(state['baseline']) != record['baseline_hashes']
            or record.get('speed_policy') != LONG_HOLD_POLICY.record()
            or record.get('presentation_policy') != PRESENTATION_POLICY
            or record.get('diagnostic_policy') != DIAGNOSTIC_POLICY
            or record.get('supplemental_normal_policy') != normal_protocol()
            or record.get('airborne_integrity_policy') != airborne_protocol()
            or record.get('airborne_evaluation_policy') != early_rejection_policy()
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
    from .model_cli_max_v1 import configure_model_cli
    qualifications = _qualifications(replay_airborne=True)
    budget_hint = _runtime_budget_hint()
    directory = provision_session(name, configure=False)
    session = Session(directory)
    state = session.read()
    engine = ROOT / '.tools/godot4/Godot_v4.5.1-stable_win64.exe'
    cache = publish_import_cache(Path(state['baseline']), directory / 'runtime/asset-cache',
        engine=engine, expected_source_hashes=state['baseline_hashes'])
    from .ground_speed_v2 import LONG_HOLD_POLICY
    from .isolated_runtime_v3 import PRESENTATION_POLICY
    from .calibration_motion_feedback_v3 import POLICY as DIAGNOSTIC_POLICY
    from .normal_gameplay_v2 import protocol_record as normal_protocol
    from .airborne_regression_v1 import protocol_record as airborne_protocol
    record = {'schema_version': 1, 'protocol_id': PROTOCOL_ID, 'session_id': directory.name,
              'registered_at': utc_now(), 'measurement_policy': 'ground-speed-long-hold-v2',
              'speed_policy': LONG_HOLD_POLICY.record(),
              'presentation_policy': PRESENTATION_POLICY,
              'diagnostic_policy': DIAGNOSTIC_POLICY,
              'supplemental_normal_policy': normal_protocol(),
              'airborne_integrity_policy': airborne_protocol(),
              'airborne_evaluation_policy': early_rejection_policy(),
              'source_hashes': _source_binding(), 'engine_sha256': sha256(engine),
              'qualification_receipts': qualifications, 'runtime_budget_hint': budget_hint,
              'baseline_hashes': state['baseline_hashes'], 'asset_cache': cache,
              'limits': {'max_candidates': 3, 'max_seconds': 1800},
              'scope': 'Fresh autonomous max-effort repair trial; no operator messages or target edits during model run; previous failures preserved',
              'model': 'gpt-5.6-sol', 'effort': 'max',
              'calibration_diagnostics': 'calibration-motion-feedback-v3'}
    path = write_json(directory / 'execution-protocol.json', record)
    marker = {'id': PROTOCOL_ID, 'sha256': sha256(path)}
    session.update(lambda s: s.update(execution_protocol=marker))
    config = configure_model_cli(directory, model='gpt-5.6-sol', effort='max',
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
                'runtime_budget_hint': record['runtime_budget_hint'],
                'next_step': 'Use calibration motion_diagnostics to investigate discrepancies; reserve time for final validation.',
                'operator_intervention': 'No target edits or follow-up coaching during the model run'}

    def submit(self, action, candidate_id=None):
        verify_protocol(self.directory)
        if action == 'finalize':
            # Reject changed/missing differential proof before the base class
            # seals a selection. Its completed-job and lowest-error rules remain.
            verify_selection_integrity(self, candidate_id)
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


def _local(directory, path):
    from .ground_benchmark import _owned
    return _owned(Path(directory), path)


def _public_integrity(comparison):
    """Small native feedback; no private seeds, source maps or replay values."""
    return {'status': comparison.get('status', 'unverified'),
        'summary': comparison.get('summary', {}),
        'mismatches': comparison.get('mismatches', []),
        'scope': 'Finite identically seeded velocity/state/predicate checks; not full displacement equivalence or normal-map reachability proof',
        'guidance': 'Preserve inherited airborne responses. Investigate discrepancies in your own source; do not add test-case branches.'}


def _record_suite(session, source, output, expected, *, cancel, check, asset_cache=None):
    from .airborne_regression_v1 import run_suite, verify_suite, protocol_record
    check()
    source, output = Path(source).resolve(), Path(output).resolve()
    if project_hashes(source) != expected:
        raise IntegrationError('airborne_source_changed', 'Airborne control source differs from its immutable snapshot')
    extra = {'asset_cache': asset_cache} if asset_cache is not None else {}
    completed = Path(run_suite(source, output, expected_source_hashes=expected, cancel=cancel, **extra)).resolve()
    check()
    if completed != output or project_hashes(source) != expected:
        raise IntegrationError('airborne_source_changed', 'Airborne controls changed their source or output identity')
    replay = verify_suite(output, expected_source_hashes=expected)
    if replay.get('status') != 'pass':
        raise IntegrationError('airborne_probe_invalid', 'The seeded airborne control capture did not verify')
    return {'schema_version': 1, 'session_id': session.directory.name,
        'source_project': str(source), 'source_hashes': expected,
        'output': str(output), 'suite_path': str(output / 'suite.json'),
        'suite_sha256': sha256(output / 'suite.json'), 'policy': protocol_record(),
        'status': 'pass', 'scope': 'controlled_seeded_airborne_integrity',
        'normal_map_reachability_proven': False, 'full_displacement_equivalence_proven': False}


def _load_baseline_integrity(session):
    from .airborne_regression_v1 import protocol_record
    state = session.read()
    pointer = state.get('airborne_integrity_baseline', {})
    expected_path = session.directory / 'integrity/airborne/baseline-record.json'
    if not pointer or _local(session.directory, pointer['path']) != expected_path:
        raise IntegrationError('airborne_evidence_missing', 'Prepared airborne baseline evidence is required')
    record = read_json(expected_path)
    output = session.directory / 'integrity/airborne/baseline'
    if (pointer.get('sha256') != sha256(expected_path) or pointer.get('status') != 'pass'
            or record.get('session_id') != session.directory.name or record.get('status') != 'pass'
            or record.get('source_hashes') != state['baseline_hashes']
            or _local(session.directory, record['source_project']) != session.directory / 'baseline'
            or _local(session.directory, record['output']) != output
            or _local(session.directory, record['suite_path']) != output / 'suite.json'
            or record.get('suite_sha256') != sha256(output / 'suite.json')
            or record.get('policy') != protocol_record()
            or project_hashes(state['baseline']) != state['baseline_hashes']):
        raise IntegrationError('airborne_evidence_changed', 'Prepared airborne baseline/source binding changed')
    return record, pointer


def _check_differential_binding(session, comparison, baseline, candidate):
    for role, record in (('baseline', baseline), ('candidate', candidate)):
        inner = comparison.get(role, {})
        if (inner.get('source_hashes') != record['source_hashes']
                or _local(session.directory, inner.get('suite_path', '')) != Path(record['suite_path'])
                or inner.get('suite_sha256') != record['suite_sha256']):
            raise IntegrationError('airborne_source_binding', 'Inner suite evidence does not bind this baseline and candidate source')


def verify_candidate_integrity(session, candidate):
    """Fresh source/suite differential replay; no model or engine invocation."""
    from .airborne_regression_v1 import compare_suites, protocol_record
    baseline, baseline_pointer = _load_baseline_integrity(session)
    cid = candidate.get('candidate_id')
    if not isinstance(cid, str) or not re.fullmatch(r'candidate-\d{2}', cid):
        raise IntegrationError('airborne_candidate_identity', 'A canonical immutable candidate is required')
    root = session.directory / 'candidates' / cid
    if read_json(_local(session.directory, root / 'candidate.json')) != candidate:
        raise IntegrationError('airborne_candidate_identity', 'Candidate metadata differs from its immutable published record')
    pointer = candidate.get('airborne_integrity', {})
    path = root / 'airborne-integrity.json'
    if not pointer or _local(session.directory, pointer['path']) != path:
        raise IntegrationError('airborne_evidence_missing', 'Candidate airborne evidence is required')
    record = read_json(path)
    output = root / 'airborne-suite'
    if (pointer.get('sha256') != sha256(path) or record.get('candidate_id') != cid
            or record.get('session_id') != session.directory.name
            or record.get('baseline') != baseline_pointer
            or record.get('candidate', {}).get('source_hashes') != candidate.get('source_hashes')
            or record['candidate'].get('policy') != protocol_record()
            or _local(session.directory, record['candidate']['source_project']) != root / 'project'
            or _local(session.directory, record['candidate']['output']) != output
            or _local(session.directory, record['candidate']['suite_path']) != output / 'suite.json'
            or record['candidate'].get('suite_sha256') != sha256(output / 'suite.json')
            or project_hashes(root / 'project') != candidate.get('source_hashes')):
        raise IntegrationError('airborne_evidence_changed', 'Candidate source or seeded control binding changed')
    comparison = compare_suites(Path(baseline['output']), output)
    _check_differential_binding(session, comparison, baseline, record['candidate'])
    if comparison != record.get('comparison'):
        raise IntegrationError('airborne_result_changed', 'Fresh airborne differential result differs from its receipt')
    if candidate.get('feedback', {}).get('airborne_integrity') != _public_integrity(comparison):
        raise IntegrationError('airborne_feedback_changed', 'Candidate feedback does not bind its actual differential result')
    return {**_public_integrity(comparison), 'candidate_id': cid, 'record_path': str(path),
        'record_sha256': sha256(path), 'source_hashes': candidate['source_hashes']}


def verify_selection_integrity(session, requested=None):
    """Match the frozen finalizer's candidate set before any heldout sealing."""
    state = session.read()
    succeeded = set()
    for path in (session.directory / 'jobs').glob('job-*.json'):
        job = read_json(path)
        if (job.get('job_id') == path.stem and job.get('session_id') == session.directory.name
                and job.get('action') == 'evaluate' and job.get('status') == 'succeeded'):
            succeeded.add(job.get('candidate_id'))
    candidates = [c for c in state['candidates'] if c.get('status') == 'evaluated'
        and c.get('candidate_id') in succeeded and c.get('feedback', {}).get('eligible') is True
        and c.get('smoke', {}).get('status') in {'pass', 'passed'}]
    if not candidates:
        raise IntegrationError('no_eligible_candidate', 'No completed candidate passed all calibration and integrity gates')
    chosen = min(candidates, key=lambda c: c['feedback']['mean_rmse_body'])
    if requested is not None and requested != chosen['candidate_id']:
        raise IntegrationError('candidate_not_best', 'The lowest-error eligible candidate must be selected')
    if verify_candidate_integrity(session, chosen).get('status') != 'pass':
        raise IntegrationError('airborne_integrity_failed', 'The selected eligible candidate lacks passing airborne preservation evidence')
    return chosen


class _AirborneRejected(Exception):
    """Internal control flow only after valid, persisted differential failure."""
    def __init__(self, root, comparison):
        super().__init__('Valid airborne preservation failure')
        self.root, self.comparison = root, comparison


def _publish_early_rejection(session, rejected, *, check):
    from .ground_worker import verify_session
    check()
    candidate = read_json(rejected.root / 'candidate.json')
    if rejected.comparison.get('status') != 'fail':
        raise IntegrationError('airborne_probe_invalid', 'Only valid preservation failures may return early')
    public = _public_integrity(rejected.comparison)
    reason = 'Valid airborne differential failed before motion/gameplay captures began'
    not_run = {'status': 'not_run', 'reason': reason}
    feedback = {'status': 'not_run', 'eligible': False, 'mean_rmse_body': None,
        'failure_class': 'gameplay_integrity_violation', 'airborne_integrity': public,
        'ground_calibration': dict(not_run), 'jump_regression': dict(not_run)}
    candidate.update(status='evaluated', feedback=feedback, smoke=dict(not_run),
        calibration_runs=[], jump_calibration_runs=[], calibration_evidence=dict(not_run),
        failure_class='gameplay_integrity_violation', early_integrity_rejection=True,
        finished_at=utc_now())
    check()
    write_json(rejected.root / 'candidate.json', candidate)
    verified = verify_candidate_integrity(session, candidate)
    if verified.get('status') != 'fail':
        raise IntegrationError('airborne_result_changed', 'Early rejection does not match fresh source-bound control evidence')
    verify_session(session)
    check()
    def publish(state):
        index = int(candidate['candidate_id'][-2:]) - 1
        if state['candidates'][index].get('candidate_id') != candidate['candidate_id']:
            raise IntegrationError('airborne_candidate_identity', 'Candidate reservation changed before semantic rejection')
        state['candidates'][index] = candidate
        state['status'] = 'ready'
    session.update(publish)
    return {'candidate_id': candidate['candidate_id'], 'status': 'evaluated',
        'failure_class': 'gameplay_integrity_violation', 'early_integrity_rejection': True,
        'calibration': feedback, 'gameplay': dict(not_run), 'calibration_evidence': dict(not_run),
        'next_action': 'Investigate the integrity feedback in your own source within the remaining candidate/time budget. Skipped checks have no score.'}


@contextmanager
def airborne_integrity_feedback(directory, *, asset_cache=None):
    """Add one baseline/one snapshot suite inside the existing worker envelope."""
    from . import ground_worker, session as session_module
    from kiroclops.ground import comparison
    from .airborne_regression_v1 import compare_suites
    if not _AIRBORNE_LOCK.acquire(blocking=False):
        raise IntegrationError('airborne_context_active', 'An airborne feedback context is already active')
    original_action = ground_worker.run_action
    directory = Path(directory).resolve()

    def protected(session, job, *, cancel, check, progress):
        if session.directory.resolve() != directory:
            raise IntegrationError('airborne_session_mismatch', 'Airborne controls belong to a different session')
        action = job['action']
        if action == 'prepare':
            state = session.read()
            progress('Recording unchanged airborne integrity controls')
            record = _record_suite(session, state['baseline'], directory / 'integrity/airborne/baseline',
                state['baseline_hashes'], cancel=cancel, check=check, asset_cache=asset_cache)
            path = write_json(directory / 'integrity/airborne/baseline-record.json', record)
            pointer = {'path': str(path), 'sha256': sha256(path), 'status': 'pass'}
            session.update(lambda s: s.update(airborne_integrity_baseline=pointer))
            result = original_action(session, job, cancel=cancel, check=check, progress=progress)
            result['airborne_integrity_baseline'] = {'status': 'pass',
                'scope': record['scope'], 'normal_map_reachability_proven': False,
                'full_displacement_equivalence_proven': False}
            return result
        if action == 'evaluate':
            baseline, baseline_pointer = _load_baseline_integrity(session)
            create_snapshot, original_feedback = session_module.create_candidate_snapshot, comparison.calibration_feedback
            current = {}

            def snapshot(actual_session, cid):
                if actual_session is not session or cid != job['candidate_id'] or current:
                    raise IntegrationError('airborne_snapshot_identity', 'Each immutable candidate receives exactly one suite')
                root = create_snapshot(actual_session, cid)
                candidate = read_json(root / 'candidate.json')
                progress('Checking candidate airborne preservation against unchanged controls')
                recorded = _record_suite(session, root / 'project', root / 'airborne-suite',
                    candidate['source_hashes'], cancel=cancel, check=check, asset_cache=asset_cache)
                differential = compare_suites(Path(baseline['output']), root / 'airborne-suite')
                if differential.get('status') not in {'pass', 'fail'}:
                    raise IntegrationError('airborne_probe_invalid', 'Airborne differential measurement is unavailable or invalid')
                _check_differential_binding(session, differential, baseline, recorded)
                receipt = {'schema_version': 1, 'session_id': session.directory.name, 'candidate_id': cid,
                    'baseline': baseline_pointer, 'candidate': recorded, 'comparison': differential}
                path = write_json(root / 'airborne-integrity.json', receipt)
                candidate['airborne_integrity'] = {'path': str(path), 'sha256': sha256(path)}
                write_json(root / 'candidate.json', candidate)
                current.update(comparison=differential)
                check()
                session.update(lambda s: s['candidates'].__setitem__(int(cid[-2:]) - 1, candidate))
                if differential['status'] == 'fail':
                    raise _AirborneRejected(root, differential)
                return root

            def feedback(*args, **kwargs):
                if not current:
                    raise IntegrationError('airborne_evidence_missing', 'Candidate suite must precede eligibility')
                result = original_feedback(*args, **kwargs)
                return {**result, 'airborne_integrity': _public_integrity(current['comparison']),
                    'eligible': result.get('eligible') is True and current['comparison']['status'] == 'pass'}

            session_module.create_candidate_snapshot, comparison.calibration_feedback = snapshot, feedback
            try:
                try:
                    result = original_action(session, job, cancel=cancel, check=check, progress=progress)
                except _AirborneRejected as rejected:
                    return _publish_early_rejection(session, rejected, check=check)
                candidate = read_json(directory / 'candidates' / job['candidate_id'] / 'candidate.json')
                verify_candidate_integrity(session, candidate)
                check()
                return result
            finally:
                session_module.create_candidate_snapshot, comparison.calibration_feedback = create_snapshot, original_feedback
        if action == 'finalize':
            state = session.read()
            candidate = next((c for c in state['candidates'] if c.get('candidate_id') == job['candidate_id']), None)
            if (not candidate or state.get('selected_candidate_id') != job['candidate_id']
                    or candidate.get('feedback', {}).get('eligible') is not True
                    or verify_candidate_integrity(session, candidate).get('status') != 'pass'):
                raise IntegrationError('airborne_integrity_failed', 'Sealed validation requires passing source-bound airborne preservation')
            result = original_action(session, job, cancel=cancel, check=check, progress=progress)
            candidate = read_json(directory / 'candidates' / job['candidate_id'] / 'candidate.json')
            verified = verify_candidate_integrity(session, candidate)
            if verified.get('status') != 'pass':
                raise IntegrationError('airborne_integrity_failed', 'Airborne evidence changed during validation')
            public = {key: verified[key] for key in ('status', 'summary', 'scope', 'candidate_id', 'record_path', 'record_sha256')}
            session.update(lambda s: s['final'].update(airborne_integrity=public))
            result['airborne_integrity'] = public
            check()
            return result
        return original_action(session, job, cancel=cancel, check=check, progress=progress)

    ground_worker.run_action = protected
    try:
        yield
    finally:
        ground_worker.run_action = original_action
        _AIRBORNE_LOCK.release()


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
    from .isolated_runtime_v3 import isolated_runtime
    from .ground_comparison_v2 import repaired_measurements
    from .worker import run_job
    from .calibration_motion_feedback_v3 import enhanced_calibration_feedback
    session = Session(directory)
    record = verify_protocol(directory)
    job = read_json(session.directory / 'jobs' / (job_id + '.json'))
    if job.get('job_id') != job_id or job.get('session_id') != session.directory.name:
        raise IntegrationError('unexpected_job', 'Job is not bound to this session')
    with isolated_runtime(project_roots=[session.directory]), repaired_imports(directory, record), repaired_measurements(), enhanced_calibration_feedback(directory), airborne_integrity_feedback(directory, asset_cache=record['asset_cache']):
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
        '<strong>' + escape(PROTOCOL_ID) + '</strong> — Sol/max, independent Kiro coding run. '
        'Steady speed is evaluated only on holds of at least 30 steps, with a fixed long window and '
        'pixel uncertainty; the speed threshold is 10%. '
        'Curve, braking and final-position checks for short and reversal movements are retained. '
        'Airborne responses of the candidate are also compared with the baseline source under the same '
        'controlled states; this check is not proof of reachability on the normal map. '
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
            from .autonomous_review_v4 import build_report as reviewed_report
            print(reviewed_report(directory))
        elif args.action in {'review', 'verify-review'}:
            from .autonomous_review_v4 import review_session, verify_review_receipt
            result = (verify_review_receipt(directory) if args.action == 'verify-review' else
                review_session(directory, args.controls, source=args.source,
                    reuse_ground_controls_from=args.reuse_ground_controls,
                    reuse_jump_controls_from=args.reuse_jump_controls))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from .model_cli_max_v1 import preflight_model_cli, run_model_cli
            from .ground_worker import verify_session
            verify_session(Session(directory))
            result = preflight_model_cli(directory) if args.action == 'preflight' else run_model_cli(directory)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if args.action == 'preflight' or result.get('status') == 'completed' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
