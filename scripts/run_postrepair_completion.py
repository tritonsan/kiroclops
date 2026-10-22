"""Finish the Phase 3A measurements left incomplete by recorder faults.

The original cohort in artifacts/phase3a/completion-paired-cohort-01 is only
read. Each sealed web candidate is re-validated by the production finalize job
in a fresh session built from the original session's inputs and prepared by
its own prepare job (amendment 01), run from an isolated copy of the
cohort-era protocol code that differs only in the repaired recorder. The
reference pack is recaptured and re-gated by that same code because the
reference cache binds the recorder sources. All outputs stay in
postrepair-completion-01.

Every measured batch (qualification, reference preparation, each web attempt)
starts only on a quiet machine (no other browser, Godot, session worker or
test run), with Playwright's own node.exe served from the NVMe system disk,
after unevaluated warm-up captures pass every timing gate twice in a row. A
batch that ends in a recorder infrastructure failure is kept and repeated as a
new numbered attempt, within fixed caps; any completed measurement is final.

Usage (from the repository root, with the .venv-local interpreter):
  python scripts/run_postrepair_completion.py qualify
  python scripts/run_postrepair_completion.py references
  python scripts/run_postrepair_completion.py gate
  python scripts/run_postrepair_completion.py register
  python scripts/run_postrepair_completion.py amend
  python scripts/run_postrepair_completion.py amend-02
  python scripts/run_postrepair_completion.py web-dryrun --trial svg-tools-01
  python scripts/run_postrepair_completion.py web --trial panel-tools-02
  python scripts/run_postrepair_completion.py web-summary
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from postrepair_quiet import HEAVY_PATTERNS, QUIET_POLICY, quiet_gate

MAIN = Path(__file__).resolve().parents[1]
BASE = MAIN/'artifacts/phase3a'
COHORT = BASE/'completion-paired-cohort-01'
STUDY = BASE/'postrepair-completion-01'
WEB_RUNTIME = STUDY/'runtime/web-code'
RUNTIME_LOCK = WEB_RUNTIME/'artifacts/local-engine/capture.lock'
ORIGINAL_GATE = BASE/'completion-reference-observability-02/result.json'
# Numbered attempt directories (web-qualification-01, ...). The frozen cohort
# pack binds the old recorder (reference-gate/ keeps that rejection), so the
# references are recaptured by the repaired recorder.
QUALIFICATION = 'web-qualification'
REFERENCES = 'shared-references'
GATE_DIR = STUDY/'reference-gate-02'
WEB_TRIALS = ('panel-tools-01', 'panel-baseline-02', 'svg-baseline-03',
              'panel-tools-02', 'panel-tools-03', 'svg-tools-01')
RECORDER_FILES = ('kiroclops/web_local/capture.py', 'kiroclops/web_local/clock_runtime.py')
# Recorder-level failures carry no motion result. Everything else is final.
INFRASTRUCTURE_CODES = frozenset({'capture_timing_failure', 'capture_environment_failure',
                                  'capture_progress_failure'})
MAX_QUALIFICATION_ATTEMPTS = 5
MAX_REFERENCE_ATTEMPTS = 3
MAX_WEB_ATTEMPTS = 3
JOB_WAIT_LIMIT_S = 2400
# Amendment 01 (after a pre-capture clone defect): every attempt is a fresh,
# self-prepared session. These outcomes carry no motion result and allow a new attempt.
AMENDMENT = STUDY/'registration-amendment-01.json'
PROCEDURE_V2 = 'fresh-prepared-session-v2'
RETRYABLE = frozenset({'infrastructure_failure', 'preparation_infrastructure_failure', 'harness_failure'})
DRYRUN_TRIAL = 'panel-tools-01'
# Amendment 02: another process keeps the repository disk saturated with random
# reads, and the same measurement took 30 s there versus 19 s on the NVMe disk.
# Fresh sessions therefore run on the NVMe disk and are archived into the study.
AMENDMENT_02 = STUDY/'registration-amendment-02.json'
PROCEDURE = 'fresh-prepared-session-v3'
WORK_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'kc3a-runtime/postrepair-sessions'
# Byte-identical copy of Playwright 1.63.0's packaged driver node.exe on the
# NVMe system disk. Served from the 5400 rpm repository disk, its page-ins
# blocked the driver event loop for 100-500 ms in the middle of recordings.
DRIVER_NODE = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'kc3a-runtime/playwright-1.63.0-node/node.exe'
DRIVER_NODE_SHA256 = 'ba4e6d110e8c1592a1ecd390f6b05f3da124b13871a5be62b341a07a853c6c32'
# Unevaluated fixture captures before each measured batch.
WARMUP_POLICY = {'minimum_consecutive_clean': 2, 'maximum_captures': 6, 'maximum_rounds': 3, 'round_pause_s': 120}

# The isolated runtime must be imported before anything from kiroclops.
sys.path.insert(0, str(WEB_RUNTIME))
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json  # noqa: E402


def _runtime_sources():
    from kiroclops.local_engine.cohort import protocol_sources
    return protocol_sources()


def _require_runtime():
    import kiroclops
    loaded = Path(kiroclops.__file__).resolve().parent
    if loaded != (WEB_RUNTIME/'kiroclops').resolve():
        raise VisualError('wrong_runtime', f'kiroclops loaded from {loaded}, not the isolated web runtime')


def _driver_environment():
    """Serve the Playwright driver from the SSD copy, after proving identical bytes."""
    import playwright
    packaged = Path(playwright.__file__).resolve().parent/'driver/node.exe'
    packaged_sha, copy_sha = file_sha256(packaged), file_sha256(DRIVER_NODE)
    if packaged_sha != DRIVER_NODE_SHA256 or copy_sha != DRIVER_NODE_SHA256:
        raise VisualError('driver_changed', 'Playwright driver node.exe differs from the registered bytes')
    os.environ['PLAYWRIGHT_NODEJS_PATH'] = str(DRIVER_NODE)
    return {'PLAYWRIGHT_NODEJS_PATH': str(DRIVER_NODE), 'sha256': copy_sha, 'packaged_node': str(packaged),
            'packaged_sha256': packaged_sha, 'playwright': importlib.metadata.version('playwright')}


def _gate_binding():
    result = read_json(GATE_DIR/'result.json')
    if result.get('status') != 'pass' or result.get('passed') != 32:
        raise VisualError('reference_not_qualified', 'Re-measured reference gate did not pass 32/32')
    return {'manifest_sha256': result['manifest_sha256'], 'path': str((GATE_DIR/'result.json').resolve()),
            'records': 32, 'scope': 'Reference adequacy only; same frozen raw pixels re-measured by the repaired-recorder runtime.',
            'sha256': file_sha256(GATE_DIR/'result.json'), 'status': 'pass'}


def _attempts(prefix):
    return sorted(p for p in STUDY.glob(prefix+'-[0-9][0-9]') if p.is_dir())


def _qualification_outcome(directory):
    result_path, precision_path = directory/'run-result.json', STUDY/(directory.name+'-precision.json')
    result = read_json(result_path) if result_path.is_file() else {}
    precision = read_json(precision_path) if precision_path.is_file() else {}
    if not result:
        classification = 'interrupted'
    elif result.get('status') == 'classification_pass_review_pending' and precision.get('status') == 'pass':
        classification = 'qualified'
    elif result.get('status') == 'failed' and (result.get('error') or {}).get('code') in INFRASTRUCTURE_CODES:
        classification = 'infrastructure_failure'
    else:
        classification = 'final_failure'
    return {'attempt': directory.name, 'classification': classification, 'status': result.get('status'),
            'error': result.get('error'), 'run_result': str(result_path),
            'run_result_sha256': file_sha256(result_path) if result else None,
            'precision': str(precision_path) if precision else None,
            'precision_sha256': file_sha256(precision_path) if precision else None}


def _reference_outcome(directory):
    manifest, failure = directory/'manifest.json', directory/'failure.json'
    if manifest.is_file():
        classification = 'sealed' if read_json(manifest).get('status') == 'sealed' else 'final_failure'
        detail = {'manifest': str(manifest), 'manifest_sha256': file_sha256(manifest)}
    elif failure.is_file():
        value = read_json(failure)
        classification = 'infrastructure_failure' if value.get('code') in INFRASTRUCTURE_CODES else 'final_failure'
        detail = {'failure': str(failure), 'failure_sha256': file_sha256(failure), 'code': value.get('code'),
                  'message': value.get('message'), 'completed_records': value.get('completed_records')}
    else:
        classification, detail = 'interrupted', {}
    return {'attempt': directory.name, 'classification': classification, **detail}


def _next_attempt(prefix, outcome, done, limit):
    """The next numbered attempt directory; only infrastructure failures allow one."""
    attempts = _attempts(prefix)
    for item in map(outcome, attempts):
        if item['classification'] == done:
            raise VisualError('already_complete', f'{item["attempt"]} already completed')
        if item['classification'] != 'infrastructure_failure':
            raise VisualError('attempt_final', f'{item["attempt"]} ended {item["classification"]}; no new attempt')
    if len(attempts) >= limit:
        raise VisualError('attempts_exhausted', f'{prefix}: all {limit} attempts were used')
    return STUDY/f'{prefix}-{len(attempts)+1:02}'


def _completed(prefix, outcome, done):
    """All attempts, the last completed and every earlier one an infrastructure failure."""
    outcomes = [outcome(p) for p in _attempts(prefix)]
    if (not outcomes or outcomes[-1]['classification'] != done
            or any(item['classification'] != 'infrastructure_failure' for item in outcomes[:-1])):
        raise VisualError('not_ready', f'{prefix}: no completed attempt')
    return outcomes


def _reference_manifest():
    return STUDY/_completed(REFERENCES, _reference_outcome, 'sealed')[-1]['attempt']/'manifest.json'


def _fixture_domain(domain):
    return 'panel' if domain == 'web_panel' else domain


def _warmup_capture(domain, directory, capture_lock):
    from kiroclops.local_engine.session import _lock
    from kiroclops.web_local.capture import capture_run
    from kiroclops.web_local.specs import capture_scenarios, fixture_project
    scenario = capture_scenarios(domain, 'calibration', 'qualified-natural-20260927')[0]
    started = time.time()
    try:
        with _lock(Path(capture_lock), timeout=600):
            result = capture_run(fixture_project(domain, 'target'), directory, scenario,
                                 variant='warmup_not_evaluated', build_package=False)
        status = {'timing': result['timing']['status'], 'errors': result['timing']['errors']}
    except Exception as exc:
        status = {'error': {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}}
    return {'domain': domain, 'scenario_id': scenario['id'], 'directory': str(directory),
            'started_at': started, 'finished_at': time.time(), **status}


def _warmup(domains, directory, capture_lock):
    """Unevaluated fixture captures, stopping once two in a row pass every timing gate."""
    need, captures = WARMUP_POLICY['minimum_consecutive_clean'], []

    def clean():
        return len(captures) >= need and all(item.get('timing') == 'pass' for item in captures[-need:])

    for index in range(WARMUP_POLICY['maximum_captures']):
        domain = domains[index % len(domains)]
        captures.append(_warmup_capture(domain, directory/f'{index+1:02}-{domain}', capture_lock))
        if clean():
            break
    print(f'  warm-up {directory.name}: ' + ' '.join(item.get('timing') or item['error']['code'] for item in captures)
          + f' -> ready={clean()}', flush=True)
    return {'policy': dict(WARMUP_POLICY), 'evaluated': False, 'ready': clean(), 'captures': captures}


def _ready(label, domains, capture_lock):
    """Quiet machine, SSD-served driver, then clean warm-up captures; else no attempt starts."""
    rounds, stamp = [], time.strftime('%Y%m%dT%H%M%S')
    for number in range(1, WARMUP_POLICY['maximum_rounds']+1):
        quiet = quiet_gate(label)
        driver = _driver_environment()
        warmup = _warmup(domains, STUDY/'warmups'/f'{label}-{stamp}-r{number}', capture_lock)
        rounds.append({'quiet_gate': quiet, 'driver': driver, 'warmup': warmup})
        if warmup['ready']:
            return {'label': label, 'ready': True, 'ready_at': time.time(), 'rounds': rounds}
        time.sleep(WARMUP_POLICY['round_pause_s'])
    write_json(STUDY/'warmups'/f'{label}-{stamp}-not-ready.json', {'label': label, 'ready': False, 'rounds': rounds})
    raise VisualError('capture_environment_not_ready', f'{label}: warm-up captures never passed twice in a row')


def qualify():
    """The 16-capture/12-control engineering qualification, then its precision check."""
    _require_runtime()
    directory = _next_attempt(QUALIFICATION, _qualification_outcome, 'qualified', MAX_QUALIFICATION_ATTEMPTS)
    write_json(STUDY/(directory.name+'-readiness.json'), _ready(directory.name, ('panel', 'svg'), RUNTIME_LOCK))
    tool = WEB_RUNTIME/'artifacts/phase3a/tools/run_qualified_web_corpus.py'
    subprocess.run([sys.executable, '-u', '-B', str(tool), '--output', str(directory)], cwd=str(WEB_RUNTIME))
    result = read_json(directory/'run-result.json')
    unresolved = []
    if result.get('status') == 'classification_pass_review_pending':
        paths = sorted((directory/'corpus/analysis').rglob('analysis.json'))
        for path in paths:
            events = read_json(path)['events']
            for scope, key in (('event', 'events'), ('relationship', 'relations')):
                unresolved += [{'analysis_path': str(path), 'scope': scope, 'index': index}
                               for index, item in enumerate(events.get(key, [])) if item.get('timing_status') == 'unresolved']
        precision = {'records': len(paths), 'unresolved_timing': unresolved,
                     'status': 'pass' if len(paths) == 16 and not unresolved else 'fail'}
    else:
        precision = {'status': 'not_run', 'reason': result.get('status')}
    write_json(STUDY/(directory.name+'-precision.json'), precision)
    outcome = _qualification_outcome(directory)
    print({'attempt': directory.name, 'qualification': result.get('status'), 'error': result.get('error'),
           'precision': precision['status'], 'unresolved': len(unresolved),
           'classification': outcome['classification']}, flush=True)
    return 0 if outcome['classification'] == 'qualified' else 1


def references():
    """Capture each of the 32 reference recipes once with the repaired recorder."""
    _require_runtime()
    from kiroclops.local_engine.web_reference import prepare_reference_pack
    from kiroclops.web_local.specs import QUALIFIED_CAPTURE_POLICY
    _completed(QUALIFICATION, _qualification_outcome, 'qualified')
    directory = _next_attempt(REFERENCES, _reference_outcome, 'sealed', MAX_REFERENCE_ATTEMPTS)
    write_json(STUDY/(directory.name+'-readiness.json'), _ready(directory.name, ('panel', 'svg'), RUNTIME_LOCK))
    try:
        pack = prepare_reference_pack(directory, capture_policy=QUALIFIED_CAPTURE_POLICY,
                                      progress=lambda message: print(message, flush=True))
    except Exception as exc:
        # The pack wrote failure.json and keeps every completed capture.
        print({'attempt': directory.name, 'failed': getattr(exc, 'code', type(exc).__name__),
               'message': str(exc), 'classification': _reference_outcome(directory)['classification']}, flush=True)
        return 1
    print(pack, flush=True)
    return 0


def gate():
    """Measure the 32 new reference recordings with the unchanged 32-record gate."""
    _require_runtime()
    if GATE_DIR.exists():
        raise VisualError('output_exists', 'Reference gate output already exists; never replace it')
    manifest = _reference_manifest()
    tool = WEB_RUNTIME/'artifacts/phase3a/tools/check_qualified_reference_observability.py'
    command = [sys.executable, '-u', '-B', str(tool), '--manifest', str(manifest),
               '--expected-manifest-sha256', file_sha256(manifest), '--output', str(GATE_DIR)]
    code = subprocess.run(command, cwd=str(WEB_RUNTIME)).returncode
    new, old = read_json(GATE_DIR/'result.json'), read_json(ORIGINAL_GATE)
    comparison = {'kind': 'ReferenceGateComparison', 'original_gate': str(ORIGINAL_GATE),
                  'original_gate_sha256': file_sha256(ORIGINAL_GATE), 'new_gate_sha256': file_sha256(GATE_DIR/'result.json'),
                  'new_manifest': str(manifest), 'new_status': new['status'], 'new_passed': new['passed'],
                  'original_passed': old['passed'],
                  'source_differences_from_original_gate': sorted(
                      k for k, v in new['source_hashes'].items() if old['source_hashes'].get(k) != v)}
    write_json(STUDY/'reference-gate-comparison-02.json', comparison)
    print(comparison, flush=True)
    return code


def _web_trial_spec(trial):
    from kiroclops.local_engine.session import source_hashes
    original = next(p for p in (COHORT/'sessions'/trial).iterdir() if (p/'session.json').is_file())
    state = read_json(original/'session.json')
    selected = state['selected_candidate_id']
    candidate = next(c for c in state['candidates'] if c['candidate_id'] == selected)
    source = original/'candidates'/selected/'source'
    if not state.get('sealed_at') or digest(source_hashes(source)) != candidate['source_sha256']:
        raise VisualError('source_changed', f'{trial}: sealed candidate is missing or changed')
    if source_hashes(original/'baseline') != state['baseline_hashes']:
        raise VisualError('baseline_changed', f'{trial}: original baseline changed')
    final_job = read_json(original/'jobs'/state['final_job_id']/'job.json')
    return {'trial': trial, 'stage': original.name, 'domain': state['domain'], 'mode': state['mode'],
            'original_session': str(original), 'original_session_json_sha256': file_sha256(original/'session.json'),
            'selected_candidate_id': selected, 'candidate_source_sha256': candidate['source_sha256'],
            'baseline_sha256': digest(state['baseline_hashes']),
            'original_final_job_id': state['final_job_id'], 'original_final_error': final_job.get('error'),
            'original_final_job_sha256': file_sha256(original/'jobs'/state['final_job_id']/'job.json')}


def register():
    _require_runtime()
    path = STUDY/'registration.json'
    if path.exists():
        raise VisualError('output_exists', 'The completion is already registered; never replace it')
    binding = _gate_binding()
    runtime = _runtime_sources()
    cohort = read_json(COHORT/'registration.json')
    manifest = _reference_manifest()
    registry = {
        'schema_version': 1, 'kind': 'PostRepairCompletionRegistration', 'registered_at': time.time(),
        'status': 'registered',
        'purpose': 'Complete the six sealed web final validations that ended without a measurement, using the repaired recorder.',
        'original_cohort': {'registration': str(COHORT/'registration.json'),
                            'registration_sha256': file_sha256(COHORT/'registration.json'),
                            'progress_sha256': file_sha256(COHORT/'progress.json'),
                            'original_results_modified': False},
        'runtime': {'root': str(WEB_RUNTIME), 'protocol_source_hashes': runtime,
                    'differs_from_cohort_protocol': sorted(k for k in set(runtime) | set(cohort['source_hashes'])
                                                           if runtime.get(k) != cohort['source_hashes'].get(k)),
                    'recorder_repair': {name: file_sha256(WEB_RUNTIME/name) for name in RECORDER_FILES},
                    'python': sys.version},
        'recorder_repair': [
            'The live capture loop reads cancellation from an in-memory event; the caller probe (a job-directory stat) runs on a separate thread.',
            'Private receipts written while the browser is live are deferred until the recording ends.',
            'The owned capture opts out of Windows power throttling and fails closed unless a measured 1 ms wait is honored.'],
        'unchanged': ['capture sampling policy qualified-natural-20260927', 'timing limits', 'comparison and acceptance code',
                      'sealed candidate sources', 'reference fixture source and scenario matrix', 'analysis code'],
        'capture_environment': {
            'driver': _driver_environment(),
            'quiet_policy': QUIET_POLICY, 'foreign_workload_patterns': [list(p) for p in HEAVY_PATTERNS],
            'warmup_policy': WARMUP_POLICY,
            'rule': 'Before every measured batch: wait until no browser, Godot, session worker or test run from another tool is running, serve the byte-identical Playwright driver from the NVMe system disk, then run unevaluated fixture captures until two in a row pass every timing gate. Otherwise no attempt starts.'},
        'recorder_qualification': {'attempts': _completed(QUALIFICATION, _qualification_outcome, 'qualified'),
                                   'max_attempts': MAX_QUALIFICATION_ATTEMPTS,
                                   'note': 'Attempts 01 and 02 ran before the readiness rule, with a single warm-up capture (02) or none (01). The shared web-qualification-precision.json is attempt 02 (not run).',
                                   'independent_review_status': 'not_claimed'},
        'reference_pack': {'attempts': _completed(REFERENCES, _reference_outcome, 'sealed'),
                           'max_attempts': MAX_REFERENCE_ATTEMPTS,
                           'manifest': str(manifest), 'manifest_sha256': file_sha256(manifest),
                           'rejected_original_pack': str(STUDY/'reference-gate/result.json'),
                           'reason': 'The cohort pack binds the pre-repair recorder hashes; verify_reference_pack rejects it.'},
        'reference_gate': binding,
        'reference_gate_comparison': str(STUDY/'reference-gate-comparison-02.json'),
        'web_trials': [_web_trial_spec(trial) for trial in WEB_TRIALS],
        'attempt_policy': {
            'max_attempts_per_trial': MAX_WEB_ATTEMPTS,
            'infrastructure_codes': sorted(INFRASTRUCTURE_CODES),
            'rule': 'A new attempt starts only when the previous finalize job failed with one of the recorder infrastructure codes, which carry no motion result. Any completed comparison, pass or fail, is final. Every attempt is kept.'},
        'not_in_scope': ['New model runs', 'Game ground stages (separate registration)', 'Independent visual review'],
    }
    STUDY.mkdir(parents=True, exist_ok=True)
    write_json(path, registry)
    print({'registered': str(path), 'sha256': file_sha256(path)}, flush=True)


def amend():
    """Amendment 01: record the clone defect and switch to prepared fresh sessions."""
    _require_runtime()
    if AMENDMENT.exists():
        raise VisualError('output_exists', 'Amendment 01 already exists; never replace it')
    failed = STUDY/'web/panel-tools-01/attempt-01/attempt.json'
    record = read_json(failed)
    job_path = Path(record['session'])/'jobs'/record['job_id']/'job.json'
    job = read_json(job_path)
    error = job.get('error') or {}
    if (record['classification'] != 'other_failure' or job['progress'].get('phase') != 'queued'
            or error.get('code') != 'execution_error' or 'prepared-evidence.json' not in error.get('message', '')
            or (Path(record['session'])/'measurements').exists()):
        raise VisualError('unexpected_failure', 'Attempt 01 is not the pre-capture clone defect this amendment covers')
    amendment = {
        'schema_version': 1, 'kind': 'PostRepairCompletionAmendment', 'number': 1, 'written_at': time.time(),
        'registration': str(STUDY/'registration.json'), 'registration_sha256': file_sha256(STUDY/'registration.json'),
        'known_results': 'No sealed candidate had been measured with the repaired recorder when this was written.',
        'defect': ('The first procedure copied the sealed session record into a clone. The production finalize path '
                   'first validates that session\'s own completed prepare job (prepared-evidence.json, bound to the '
                   'session identity, paths and experiment), which a clone cannot carry. panel-tools-01 attempt-01 '
                   'stopped there after 0.2 s: no browser started, no measurements directory exists.'),
        'harness_failures': {'panel-tools-01/attempt-01': {
            'attempt_json_sha256': file_sha256(failed), 'job_json_sha256': file_sha256(job_path),
            'error': error, 'phase': job['progress']['phase'], 'captures': 0,
            'counts_toward_attempt_cap': True}},
        'procedure': PROCEDURE_V2,
        'procedure_steps': [
            'create_session with the original inputs: a copy of the original baseline, the isolated fixture reference, '
            'the same goal, mode and experiment, whose reference bundle and gate fields name the repaired-recorder pack '
            'and gate; the capture lock is the machine-wide lock the original session used',
            'Session.submit("prepare"): the session\'s own prepare job (frozen calibration references and baseline '
            'recordings; the candidate is not involved)',
            'seal a byte-identical copy of the original selected candidate and run the production finalize job'],
        'attempt_rules': {
            'retry_after': sorted(RETRYABLE),
            'final': ['completed_pass', 'completed_fail', 'other_failure', 'preparation_failure'],
            'max_attempts_per_trial': MAX_WEB_ATTEMPTS,
            'note': 'A preparation failure outside the recorder infrastructure codes stops the trial for inspection.'},
        'dryrun': ('Before any trial, one unregistered run of the corrected procedure with the unchanged baseline as '
                   'stand-in candidate (web-dryrun/). It measures no sealed candidate and is not counted.'),
    }
    write_json(AMENDMENT, amendment)
    print({'amendment': str(AMENDMENT), 'sha256': file_sha256(AMENDMENT)}, flush=True)
    return 0


def _registration():
    registry = read_json(STUDY/'registration.json')
    if registry['runtime']['protocol_source_hashes'] != _runtime_sources():
        raise VisualError('protocol_changed', 'Isolated runtime sources changed after registration')
    if registry['reference_gate'] != _gate_binding():
        raise VisualError('reference_changed', 'Registered reference gate changed')
    if file_sha256(registry['reference_pack']['manifest']) != registry['reference_pack']['manifest_sha256']:
        raise VisualError('reference_changed', 'Registered reference pack changed')
    amendment = read_json(AMENDMENT)
    if amendment['registration_sha256'] != file_sha256(STUDY/'registration.json') or amendment['procedure'] != PROCEDURE_V2:
        raise VisualError('protocol_changed', 'Amendment 01 does not bind this registration and procedure')
    second = read_json(AMENDMENT_02)
    if (second['registration_sha256'] != file_sha256(STUDY/'registration.json')
            or second['amendment_01_sha256'] != file_sha256(AMENDMENT) or second['procedure'] != PROCEDURE
            or Path(second['work_root']) != WORK_ROOT):
        raise VisualError('protocol_changed', 'Amendment 02 does not bind this registration, amendment 01 and procedure')
    registry['amendment'] = amendment
    registry['amendment_02'] = second
    return registry


def amend_02():
    """Amendment 02: working sessions on the NVMe disk, reference evidence pre-read, archived afterwards."""
    _require_runtime()
    if AMENDMENT_02.exists():
        raise VisualError('output_exists', 'Amendment 02 already exists; never replace it')
    dryrun = STUDY/'web-dryrun/run-01/dryrun.json'
    amendment = {
        'schema_version': 1, 'kind': 'PostRepairCompletionAmendment', 'number': 2, 'written_at': time.time(),
        'registration': str(STUDY/'registration.json'), 'registration_sha256': file_sha256(STUDY/'registration.json'),
        'amendment_01_sha256': file_sha256(AMENDMENT),
        'known_results': ('No sealed candidate had been measured with the repaired recorder when this was written. '
                          'Only the baseline stand-in dry run (not counted) had completed.'),
        'observations': {
            'dryrun': str(dryrun), 'dryrun_sha256': file_sha256(dryrun),
            'panel_finalize_seconds': 1369, 'validation_budget_seconds': 1800,
            'cohort_panel_finalize_seconds': [775, 829], 'cohort_svg_finalize_seconds': [1299, 1302, 1311, 1355],
            'same_measurement_seconds': {'repository_disk_D': [30.4, 30.1], 'nvme_disk_C': [19.0, 18.6]},
            'repository_disk': ('5400 rpm HDD with a sustained queue of 4-7 and 100-380 random 4 KB reads/s while no '
                                'completion work ran; the reads matched Memory Pages Input/sec and tracked the read '
                                'operations of the Kiro IDE main process (three IDE windows have this repository open).')},
        'procedure': PROCEDURE,
        'work_root': str(WORK_ROOT),
        'changes': [
            'Each fresh session (and its prepare, before, final and reference-reuse outputs) is created under the work '
            'root on the NVMe system disk instead of inside the study folder. Code, reference pack, gate and '
            'candidates stay where they are registered.',
            'Immediately before prepare and before finalize, the domain\'s registered reference recordings and gate '
            'evidence are read once, so the budgeted job does not wait on the saturated disk. Nothing is written.',
            'After each job ends, the whole working session is stored in the attempt folder as a stored (uncompressed) '
            'zip with a per-file SHA-256 manifest; the working copy is kept.'],
        'unchanged': ['every amendment 01 rule and attempt cap', 'timing limits and validation budget',
                      'capture policy, comparison and acceptance code', 'sealed candidate sources'],
        'dryrun_rule': ('A trial starts only after a completed dry run of this procedure (any domain) and a completed '
                        'dry run of the trial\'s domain (any procedure).'),
    }
    write_json(AMENDMENT_02, amendment)
    print({'amendment': str(AMENDMENT_02), 'sha256': file_sha256(AMENDMENT_02)}, flush=True)
    return 0


def _reference_domain(domain):
    return 'panel' if domain == 'web_panel' else 'svg'


def _prewarm(domain):
    """Read the registered reference recordings and gate evidence of one domain; writes nothing."""
    name = _reference_domain(domain)
    manifest = Path(read_json(STUDY/'registration.json')['reference_pack']['manifest']).parent
    roots = [manifest/'calibration'/name, manifest/'heldout'/name, GATE_DIR/name]
    started, count, size = time.time(), 0, 0
    for root in roots:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                with open(path, 'rb') as handle:
                    while chunk := handle.read(1 << 20):
                        size += len(chunk)
                count += 1
    return {'roots': [str(r) for r in roots], 'files': count, 'bytes': size, 'seconds': round(time.time()-started, 1)}


def _archive(session_dir, destination):
    """Stored zip of the working session plus a per-file SHA-256 manifest."""
    import zipfile
    archive_path = destination/'session-archive.zip'
    files = {}
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(p for p in session_dir.rglob('*') if p.is_file()):
            relative = path.relative_to(session_dir.parent).as_posix()
            files[relative] = file_sha256(path)
            archive.write(path, relative)
    manifest = {'kind': 'PostRepairSessionArchive', 'working_directory': str(session_dir), 'files': files,
                'count': len(files), 'zip': str(archive_path), 'zip_sha256': file_sha256(archive_path)}
    write_json(destination/'session-archive-manifest.json', manifest)
    return {'zip': str(archive_path), 'zip_sha256': manifest['zip_sha256'], 'files': len(files),
            'manifest_sha256': file_sha256(destination/'session-archive-manifest.json')}


def _wait(session_dir, job_id, label):
    from kiroclops.local_engine.backend import TERMINAL
    from kiroclops.local_engine.session import Session
    deadline, last = time.monotonic()+JOB_WAIT_LIMIT_S, None
    while True:
        job = Session(session_dir).get_job(job_id, wait_seconds=30)
        phase = (job.get('progress') or {}).get('phase')
        if phase != last or job['status'] in TERMINAL:
            print(f"  {label}: {job['status']} {job.get('progress')}", flush=True)
            last = phase
        if job['status'] in TERMINAL:
            return job
        if time.monotonic() > deadline:
            raise VisualError('completion_wait_timeout', f'{label} did not finish in the bounded wait')


def _prepared_session(spec, directory, registry, label):
    """A fresh session with the original inputs, prepared by its own prepare job."""
    from kiroclops.local_engine.session import Session, create_session, source_hashes
    original = Path(spec['original_session'])
    if file_sha256(original/'session.json') != spec['original_session_json_sha256']:
        raise VisualError('source_changed', 'Original session record changed')
    state = read_json(original/'session.json')
    reference = WEB_RUNTIME/Path(state['reference']).relative_to(MAIN)
    if source_hashes(reference) != state['reference_hashes']:
        raise VisualError('reference_changed', 'Isolated fixture reference differs from the original session')
    manifest = Path(registry['reference_pack']['manifest'])
    experiment = deepcopy(state['experiment'])
    # Same scenarios and capture policy; the pack and gate come from the repaired
    # recorder. The lock is the machine-wide one the original session used.
    experiment.update(web_reference_bundle=str(manifest.resolve()), web_reference_bundle_sha256=file_sha256(manifest),
                      web_reference_cache=deepcopy(registry['reference_gate']), capture_lock=state['capture_lock'])
    directory.parent.mkdir(parents=True, exist_ok=True)
    created = create_session(directory, state['domain'], original/'baseline', reference, state['goal'],
                             mode=state['mode'], experiment=experiment)
    if created['baseline_hashes'] != state['baseline_hashes'] or created['budget'] != state['budget']:
        raise VisualError('baseline_changed', 'The fresh session differs from the original baseline or budget')
    submitted = Session(directory).submit('prepare')
    return state, _wait(directory, submitted['job_id'], f'{label} prepare')


def _seal_and_finalize(directory, candidate_id, source, source_sha256, candidate, provenance):
    """Seal a byte-identical snapshot and queue the production finalize job for it."""
    from kiroclops.local_engine.backend import _lock
    from kiroclops.local_engine.session import JOB_BINDING_FIELDS, Session, _atomic, copy_project
    snapshot = directory/'candidates'/candidate_id/'source'
    copied = copy_project(source, snapshot)
    if digest(copied) != source_sha256:
        raise VisualError('source_changed', 'Copied candidate differs from the sealed source')
    job_id = uuid.uuid4().hex
    with _lock(directory/'session.lock', timeout=60):
        session = Session(directory)
        state = session._load()
        if not state['prepared'] or state['active_job_id'] or state['sealed_at'] is not None:
            raise VisualError('not_prepared', 'The fresh session must be prepared and idle before sealing')
        job_dir = directory/'jobs'/job_id
        job_dir.mkdir(parents=True)
        job = {'schema_version': 1, 'job_id': job_id, 'operation': 'finalize', 'status': 'queued',
               'created_at': time.time(), 'started_at': None, 'finished_at': None, 'worker': None,
               'progress': {'phase': 'queued', 'completed': 0, 'total': 1}, 'candidate_id': candidate_id,
               'source_path': str(snapshot), 'source_hashes': copied, 'source_sha256': source_sha256,
               'error': None, 'result': None, 'result_applied': False}
        _atomic(job_dir/'job.json', job)
        _atomic(job_dir/'binding.json', {'sha256': digest({key: job[key] for key in JOB_BINDING_FIELDS})})
        state['candidates'].append({**candidate, 'candidate_id': candidate_id, 'job_id': job_id,
                                    'created_at': time.time(), 'source_sha256': source_sha256})
        state.update(selected_candidate_id=candidate_id, sealed_at=time.time(), status='sealed',
                     final_job_id=job_id, active_job_id=job_id, postrepair_completion=provenance)
        session._save(state)
    Session(directory)._spawn(job_id)
    return job_id


def _finalize_outcome(job):
    error, result = job.get('error') or {}, job.get('result') or {}
    if job['status'] == 'succeeded':
        classification = 'completed_pass' if result.get('pass') is True else 'completed_fail'
    elif error.get('code') in INFRASTRUCTURE_CODES:
        classification = 'infrastructure_failure'
    else:
        classification = 'other_failure'
    return classification, result


def _provenance(**values):
    return {**values, 'registration_sha256': file_sha256(STUDY/'registration.json'),
            'amendment_sha256': file_sha256(AMENDMENT), 'amendment_02_sha256': file_sha256(AMENDMENT_02),
            'procedure': PROCEDURE}


def _attempt(spec, directory, registry, attempt):
    state = read_json(Path(spec['original_session'])/'session.json')
    label = f"{spec['trial']} attempt {attempt}"
    readiness = _ready(f"{spec['trial']}-attempt-{attempt:02}", (_fixture_domain(spec['domain']),), state['capture_lock'])
    directory.mkdir(parents=True)
    session_dir = WORK_ROOT/'web'/spec['trial']/f'attempt-{attempt:02}'/spec['stage']
    record = {'trial': spec['trial'], 'attempt': attempt, 'procedure': PROCEDURE, 'started_at': time.time(),
              'readiness': readiness, 'session': str(session_dir), 'prewarm': [_prewarm(spec['domain'])]}
    write_json(directory/'attempt.json', record)
    provenance = _provenance(original_session=spec['original_session'], original_session_id=state['session_id'],
                             original_final_job_id=state['final_job_id'], original_final=state['final'], attempt=attempt)
    original, prepare = _prepared_session(spec, session_dir, registry, label)
    record.update(prepare_job_id=prepare['job_id'], prepare_status=prepare['status'], prepare_error=prepare.get('error'))
    write_json(directory/'attempt.json', record)
    if prepare['status'] != 'succeeded':
        code = (prepare.get('error') or {}).get('code')
        record.update(finished_at=time.time(), classification='preparation_infrastructure_failure'
                      if code in INFRASTRUCTURE_CODES else 'preparation_failure', archive=_archive(session_dir, directory))
        write_json(directory/'attempt.json', record)
        return record
    selected = spec['selected_candidate_id']
    candidate = next(c for c in original['candidates'] if c['candidate_id'] == selected)
    copied = {'status': 'succeeded', 'eligible': candidate.get('eligible'), 'score': candidate.get('score'),
              'error': None, 'original_candidate': deepcopy(candidate)}
    record['prewarm'].append(_prewarm(spec['domain']))
    job_id = _seal_and_finalize(session_dir, selected, Path(spec['original_session'])/'candidates'/selected/'source',
                                spec['candidate_source_sha256'], copied, provenance)
    record.update(job_id=job_id)
    write_json(directory/'attempt.json', record)
    job = _wait(session_dir, job_id, f'{label} finalize')
    classification, result = _finalize_outcome(job)
    record.update(finished_at=time.time(), job_status=job['status'], error=job.get('error'),
                  classification=classification, passed=result.get('passed'), required=result.get('required'),
                  job_sha256=file_sha256(session_dir/'jobs'/job_id/'job.json'),
                  result_sha256=file_sha256(session_dir/'jobs'/job_id/'result.json') if job['status'] == 'succeeded' else None)
    write_json(directory/'attempt.json', record)
    if job['status'] == 'succeeded':
        write_json(directory/'result.json', read_json(session_dir/'jobs'/job_id/'result.json'))
    record['archive'] = _archive(session_dir, directory)
    write_json(directory/'attempt.json', record)
    return record


def _effective(record, amendment):
    """Amendment-listed pre-capture harness failures are kept but carry no outcome."""
    key = f"{record['trial']}/attempt-{record['attempt']:02}"
    return 'harness_failure' if key in amendment['harness_failures'] else record['classification']


def web(trial):
    _require_runtime()
    registry = _registration()
    amendment = registry['amendment']
    spec = next(t for t in registry['web_trials'] if t['trial'] == trial)
    if not (_dryrun_complete(procedure=PROCEDURE) and _dryrun_complete(domain=spec['domain'])):
        raise VisualError('dryrun_missing', 'Complete a dry run of this procedure and one of this domain first')
    root = STUDY/'web'/trial
    previous = sorted(root.glob('attempt-*/attempt.json')) if root.exists() else []
    records = [read_json(p) for p in previous]
    if any('classification' not in r for r in records):
        raise VisualError('interrupted_attempt', 'An earlier attempt has no outcome; inspect it before continuing')
    if (root/'summary.json').is_file() and not (root/'summary-before-amendment-01.json').exists() and \
            any(_effective(r, amendment) == 'harness_failure' for r in records):
        write_json(root/'summary-before-amendment-01.json', read_json(root/'summary.json'))
    while not records or _effective(records[-1], amendment) in RETRYABLE:
        if len(records) >= MAX_WEB_ATTEMPTS:
            break
        number = len(records)+1
        print(f"{trial}: attempt {number}", flush=True)
        records.append(_attempt(spec, root/f'attempt-{number:02}', registry, number))
    effective = [_effective(r, amendment) for r in records]
    summary = {'trial': trial, 'attempts': records, 'effective_classifications': effective,
               'final_classification': effective[-1], 'passed': records[-1].get('passed'),
               'required': records[-1].get('required'), 'amendment_sha256': file_sha256(AMENDMENT)}
    write_json(root/'summary.json', summary)
    print({k: summary[k] for k in ('trial', 'final_classification', 'passed', 'required')}, flush=True)
    return 0


def _dryruns():
    return sorted(p for p in (STUDY/'web-dryrun').glob('run-[0-9][0-9]') if p.is_dir())


def _dryrun_complete(*, procedure=None, domain=None):
    for path in _dryruns():
        log = read_json(path/'dryrun.json') if (path/'dryrun.json').is_file() else {}
        # Run 01 predates the domain field; it used panel-tools-01 inputs.
        if (log.get('procedure_complete') and procedure in (None, log.get('procedure'))
                and domain in (None, log.get('domain', 'web_panel'))):
            return True
    return False


def web_dryrun(trial):
    """The corrected procedure once, with the unchanged baseline standing in for a candidate."""
    _require_runtime()
    registry = _registration()
    spec = next(t for t in registry['web_trials'] if t['trial'] == trial)
    if _dryrun_complete(procedure=PROCEDURE, domain=spec['domain']):
        raise VisualError('already_complete', 'A dry run of this procedure and domain already completed')
    runs = _dryruns()
    if any(not (p/'dryrun.json').is_file() for p in runs):
        raise VisualError('interrupted_attempt', 'An earlier dry run has no record; inspect it first')
    root = STUDY/'web-dryrun'/f'run-{len(runs)+1:02}'
    state = read_json(Path(spec['original_session'])/'session.json')
    readiness = _ready(f'{trial}-dryrun', (_fixture_domain(spec['domain']),), state['capture_lock'])
    root.mkdir(parents=True)
    session_dir = WORK_ROOT/'web-dryrun'/root.name/spec['stage']
    log = {'kind': 'WebProcedureDryRun', 'trial_inputs': trial, 'domain': spec['domain'], 'procedure': PROCEDURE,
           'counted': False, 'candidate': 'unchanged baseline stand-in; no sealed candidate is measured',
           'readiness': readiness, 'session': str(session_dir), 'prewarm': [_prewarm(spec['domain'])]}
    write_json(root/'dryrun.json', log)
    provenance = _provenance(dryrun=True, trial_inputs=trial)
    original, prepare = _prepared_session(spec, session_dir, registry, 'dryrun')
    log['prepare'] = {'job_id': prepare['job_id'], 'status': prepare['status'], 'error': prepare.get('error')}
    write_json(root/'dryrun.json', log)
    if prepare['status'] == 'succeeded':
        stand_in = {'status': 'stand_in', 'eligible': False, 'score': None, 'error': None}
        log['prewarm'].append(_prewarm(spec['domain']))
        job_id = _seal_and_finalize(session_dir, 'candidate-01', session_dir/'baseline',
                                    digest(original['baseline_hashes']), stand_in, provenance)
        job = _wait(session_dir, job_id, 'dryrun finalize')
        classification, result = _finalize_outcome(job)
        finished = read_json(session_dir/'jobs'/job_id/'job.json')
        log['finalize'] = {'job_id': job_id, 'status': job['status'], 'error': job.get('error'),
                           'classification': classification, 'pass': result.get('pass'),
                           'passed': result.get('passed'), 'required': result.get('required'),
                           'seconds': (finished.get('finished_at') or 0)-(finished.get('started_at') or 0)}
    log['procedure_complete'] = log.get('finalize', {}).get('status') == 'succeeded'
    log['archive'] = _archive(session_dir, root)
    write_json(root/'dryrun.json', log)
    print({'procedure_complete': log['procedure_complete'], 'prepare': log['prepare']['status'],
           'finalize': log.get('finalize')}, flush=True)
    return 0 if log['procedure_complete'] else 1


def web_summary():
    rows = []
    for trial in WEB_TRIALS:
        path = STUDY/'web'/trial/'summary.json'
        rows.append(read_json(path) if path.exists() else {'trial': trial, 'final_classification': 'not_run'})
    result = {'kind': 'PostRepairWebCompletionSummary', 'trials': rows,
              'completed': sum(r['final_classification'].startswith('completed') for r in rows),
              'passed_trials': sum(r['final_classification'] == 'completed_pass' for r in rows)}
    write_json(STUDY/'web-summary.json', result)
    print({k: result[k] for k in ('completed', 'passed_trials')}, flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('qualify', 'references', 'gate', 'register', 'amend', 'amend-02',
                                            'web-dryrun', 'web', 'web-summary'))
    parser.add_argument('--trial', choices=WEB_TRIALS)
    args = parser.parse_args()
    commands = {'qualify': qualify, 'references': references, 'gate': gate, 'register': register, 'amend': amend,
                'amend-02': amend_02, 'web-summary': web_summary}
    if args.command in ('web', 'web-dryrun'):
        if not args.trial:
            raise SystemExit('--trial is required')
        return web(args.trial) if args.command == 'web' else web_dryrun(args.trial)
    return commands[args.command]()


if __name__ == '__main__':
    raise SystemExit(main())
