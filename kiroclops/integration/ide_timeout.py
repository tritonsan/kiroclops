"""Record an operator-observed IDE stop after its original 30-minute deadline.

This helper never controls the UI, terminates a process, recovers a job or grants
acceptance. The caller must actually stop the pending turn and resolve jobs first.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import uuid

from . import ide_evidence, ide_run
from .ground_review import _local_file
from .session import TERMINAL_JOBS
from .storage import IntegrationError, file_lock, identifier, project_hashes, read_json, sha256, write_json

RECEIPT = 'ide/operator-stop.json'


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    if not isinstance(value, str):
        raise IntegrationError('invalid_stop_time', 'An explicit stopped-UI timestamp is required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise IntegrationError('invalid_stop_time', 'Stopped-UI timestamps must include a timezone')
    return parsed


def _validate(directory, screenshot, stopped_at):
    names = ('kiro/launch.json', 'kiro/launch-start.json', 'session.json', RECEIPT,
             'ide/finish-receipt.json', '.ide-run.lock', '.session.lock')
    paths = {name: _local_file(directory / name, directory) for name in names}
    if paths[RECEIPT].exists() or paths['ide/finish-receipt.json'].exists():
        raise IntegrationError('ide_already_terminal', 'A previous stop/completion receipt is preserved')
    if any(not paths[name].is_file() for name in ('.ide-run.lock', '.session.lock')):
        raise IntegrationError('ide_not_started', 'The original IDE/session locks must already exist')
    raw = {name: paths[name].read_bytes() for name in names[:3]}
    launch, state = read_json(paths[names[0]]), read_json(paths['session.json'])
    if (raw[names[0]] != raw[names[1]] or launch.get('schema_version') != 2
            or launch.get('surface') != 'ide' or launch.get('transport') != 'ide_gui'
            or launch.get('mode') != 'tools' or launch.get('session_id') != directory.name
            or launch.get('status') != 'running' or 'prompt_result' in launch or 'finished_at' in launch):
        raise IntegrationError('ide_not_running', 'Expected unchanged running IDE launch/start receipts')
    if (state.get('session_id') != directory.name or state.get('mode') != 'tools'
            or state.get('started_at') != launch.get('started_at') or state.get('deadline_at') != launch.get('deadline_at')):
        raise IntegrationError('ide_clock_changed', 'Original session and launch clock must remain identical')
    if state.get('active_job_id'):
        raise IntegrationError('active_job', 'Cancel/recover the existing active job first')
    jobs = {}
    for path in sorted((directory / 'jobs').glob('*.json')):
        path = _local_file(path, directory)
        job = read_json(path)
        if (job.get('session_id') != directory.name or job.get('job_id') != path.stem
                or job.get('status') not in TERMINAL_JOBS):
            raise IntegrationError('active_job', 'All recorded jobs must already be terminal for this session')
        jobs[path.relative_to(directory).as_posix()] = sha256(path)
    started, stopped, now = _time(launch['started_at']), _time(stopped_at), _now()
    deadline = launch.get('deadline_at')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or abs(deadline - started.timestamp() - 1800) > 1):
        raise IntegrationError('ide_clock_changed', 'Expected the original bounded 30-minute deadline')
    if now.timestamp() < deadline or not deadline <= stopped.timestamp() <= now.timestamp():
        raise IntegrationError('deadline_not_expired', 'Actual time and observed stop must both follow the recorded deadline')
    _, prompt, prompt_hash = ide_run._config(directory)
    marker = _local_file(launch['configuration_path'], directory)
    if (str(prompt) != launch.get('prompt_path') or prompt_hash != launch.get('prompt_sha256')
            or sha256(marker) != launch.get('configuration_sha256')):
        raise IntegrationError('ide_configuration_changed', 'Prepared prompt/profile differs from startup evidence')
    startup = launch.get('ui_observed', {}).get('evidence', [])
    if not startup:
        raise IntegrationError('missing_ui_evidence', 'Startup screenshots must remain hash-bound')
    for item in startup:
        if sha256(_local_file(item['path'], directory)) != item['sha256']:
            raise IntegrationError('ui_evidence_changed', 'A startup screenshot changed')
    evidence = ide_run._screenshots(directory, [screenshot])[0]
    _local_file(evidence['path'], directory)
    if any(evidence['path'] == old['path'] or evidence['sha256'] == old['sha256'] for old in startup):
        raise IntegrationError('invalid_ui_evidence', 'Stopped UI evidence must be separate from startup evidence')
    sources = {}
    for name in ('baseline', 'target'):
        project = directory / name
        if Path(state[name]).resolve() != project or not _local_file(project / 'project.godot', directory).is_file():
            raise IntegrationError('source_changed', 'Expected ordinary session-owned project snapshots')
        sources[name] = project_hashes(project)
    if sources['baseline'] != state.get('baseline_hashes'):
        raise IntegrationError('source_changed', 'Baseline source bytes changed')
    candidate_metadata = {}
    for candidate in state.get('candidates', []):
        cid = identifier(candidate['candidate_id'])
        metadata = _local_file(directory / f'candidates/{cid}/candidate.json', directory)
        if metadata.exists():
            candidate_metadata[metadata.relative_to(directory).as_posix()] = sha256(metadata)
        project = directory / f'candidates/{cid}/project'
        if project.exists():
            _local_file(project / 'project.godot', directory)
            current = project_hashes(project)
            if candidate.get('source_hashes') and current != candidate['source_hashes']:
                raise IntegrationError('source_changed', 'Candidate source bytes changed')
            sources[f'candidates/{cid}'] = current
        elif candidate.get('source_hashes'):
            raise IntegrationError('source_changed', 'A recorded candidate snapshot is missing')
    return {'launch': launch, 'state': state, 'raw': raw, 'sources': sources, 'jobs': jobs,
            'candidate_metadata': candidate_metadata, 'screenshot': evidence, 'started': started, 'stopped': stopped}


def _restore(path, raw):
    temporary = path.with_name(path.name + '.restore-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_ide_timeout(directory, *, screenshot, observed_stopped_at, operator, observation, export=None):
    """Record explicit cancellation of the pending UI turn; never stop a live run.

    Optional export is an existing opaque ZIP, not a completed-turn proof.
    Failed validation leaves all evidence untouched; success is one-way.
    """
    directory = ide_evidence._session(directory)
    if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in (operator, observation)):
        raise IntegrationError('missing_operator_observation', 'Operator identity and stopped-UI observation are required')
    context = _validate(directory, screenshot, observed_stopped_at)
    export_record = None
    if export is not None:
        path = _local_file(export, directory)
        if not path.is_file() or path.suffix.lower() != '.zip' or path.stat().st_size == 0:
            raise IntegrationError('invalid_export', 'Optional export must be an existing nonempty session-owned ZIP')
        export_record = {'path': path.relative_to(directory).as_posix(), 'sha256': sha256(path),
                         'interpretation': 'opaque bytes; not parsed for model or turn completion'}
    with file_lock(directory / '.ide-run.lock'), file_lock(directory / '.session.lock'):
        if _validate(directory, screenshot, observed_stopped_at) != context:
            raise IntegrationError('evidence_changed', 'Session/source/UI evidence changed before recording the stop')
        if export_record and sha256(directory / export_record['path']) != export_record['sha256']:
            raise IntegrationError('evidence_changed', 'Optional export changed')
        elapsed = round((context['stopped'] - context['started']).total_seconds(), 6)
        recorded_at = _now().isoformat()
        receipt = {'schema_version': 1, 'session_id': directory.name, 'surface': 'ide', 'transport': 'ide_gui',
                   'status': 'timeout', 'basis': 'operator_observed_ui_stop_after_recorded_deadline',
                   'operator': operator, 'observation': observation, 'observed_stopped_at': context['stopped'].isoformat(),
                   'recorded_at': recorded_at, 'started_at': context['launch']['started_at'],
                   'deadline_at': context['launch']['deadline_at'], 'elapsed_seconds': elapsed,
                   'stopped_ui': context['screenshot'], 'source_hashes': context['sources'],
                   'job_hashes': context['jobs'], 'candidate_metadata_hashes': context['candidate_metadata'],
                   'evidence_sha256': {name: sha256(directory / name) for name in context['raw']},
                   'opaque_export': export_record, 'automatic_process_timeout_verified': False,
                   'historical_model_status': 'unverified', 'historical_effort_status': 'unverified',
                   'end_turn_attested': False, 'acceptance_credit': False,
                   'limitations': ['Stopped UI is an explicit operator observation, not automated process termination proof.',
                                   'No native end_turn or historical per-generation model/effort is inferred.',
                                   'Clock, candidates, final measurements and job outcomes are preserved.']}
        path, created = directory / RECEIPT, False
        try:
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                created = True
                json.dump(receipt, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
            launch = deepcopy(context['launch'])
            launch.update(status='timeout', finished_at=context['stopped'].isoformat(), elapsed_seconds=elapsed,
                          stop_recorded_at=recorded_at, elapsed_basis=receipt['basis'],
                          automatic_process_timeout_verified=False, historical_model_status='unverified',
                          historical_effort_status='unverified', strict_configuration_verified=False,
                          operator_stop_receipt={'path': RECEIPT, 'sha256': sha256(path)},
                          error='Operator observed the pending IDE turn stopped after its original deadline.')
            state = deepcopy(context['state'])
            state['status'] = 'closed_failed'
            write_json(directory / 'kiro/launch.json', launch)
            write_json(directory / 'session.json', state)
        except BaseException:
            _restore(directory / 'kiro/launch.json', context['raw']['kiro/launch.json'])
            _restore(directory / 'session.json', context['raw']['session.json'])
            if created:
                path.unlink()
            raise
    return receipt
