"""Session lifecycle and asynchronous, immutable-candidate experiment jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from kiroclops.common import ROOT
from kiroclops.capture import process_options
from .storage import (IntegrationError, assert_allowed_changes, copy_project, file_lock,
                      identifier, process_identity, project_hashes, read_json,
                      session_path, sha256, utc_now, write_json)

TERMINAL_JOBS = {'succeeded', 'failed', 'cancelled', 'interrupted'}
MAX_CANDIDATES = 3
SESSION_SECONDS = 1800


def prepare_session(name=None, *, mode='tools', configure=True, experiment_id='legacy-jump-v1'):
    """Provision a fresh target without invoking any model or motion patch."""
    if mode not in {'tools', 'baseline'}:
        raise ValueError('mode must be tools or baseline')
    from .experiments import GROUND_ID, get_experiment
    spec = get_experiment(experiment_id)
    if spec.experiment_id == GROUND_ID:
        if mode != 'tools':
            raise IntegrationError('unsupported_mode', 'Ground motion currently supports only the tools arm')
        from .ground_worker import provision_session
        return provision_session(name, configure=configure)
    from scripts.run_suite import Suite
    frozen = Suite().verify_frozen()
    name = identifier(name or f'{mode}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}')
    directory = session_path(ROOT / 'sessions' / name)
    if directory.exists():
        raise IntegrationError('session_exists', f'Session already exists: {directory}')
    directory.mkdir(parents=True)
    state = {'schema_version': 2, 'session_id': name, 'mode': mode, 'status': 'provisioning',
             'created_at': utc_now(), 'started_at': None, 'deadline_at': None,
             'target': str(directory / 'target'), 'baseline': str(directory / 'baseline'),
             'source_commit': read_json(ROOT / 'config/sources.json')['slay']['commit'],
             'phase1_frozen_files': frozen['files'], 'baseline_hashes': {},
             'candidates': [], 'reference': {}, 'baseline_runs': {},
             'selected_candidate_id': None, 'active_job_id': None, 'final': None,
             'budget': {'max_candidates': MAX_CANDIDATES, 'max_seconds': SESSION_SECONDS}}
    write_json(directory / 'session.json', state)
    try:
        from .runner import prepare_project, import_project
        copy_project(ROOT / 'vendor/slay', directory / 'baseline')
        prepare_project(directory / 'baseline')
        import_project(directory / 'baseline', runtime_dir=directory / 'runtime/setup')
        copy_project(directory / 'baseline', directory / 'target')
        state['baseline_hashes'] = project_hashes(directory / 'baseline')
        state['status'] = 'new'
        write_json(directory / 'session.json', state)
        export_raw_reference(directory)
        if configure:
            from .kiro import configure_kiro
            configure_kiro(directory, mode=mode)
        return directory
    except Exception as exc:
        state.update(status='setup_failed', error=str(exc))
        write_json(directory / 'session.json', state)
        raise


def export_raw_reference(directory):
    """Identical raw calibration input available in both experimental arms."""
    from scripts.inspect_frames import contact_sheet
    out = Path(directory) / 'target/.kiroclops-reference'
    out.mkdir(parents=True, exist_ok=True)
    inputs = []
    for hold in (3, 24):
        source = ROOT / f'artifacts/runs/tiny-reference-h{hold:02d}-x0-r1'
        target = out / f'hold-{hold:02d}'
        target.mkdir(exist_ok=True)
        shutil.copy2(source / 'video.mp4', target / 'video.mp4')
        # No measurements, source-game constants, or held-out footage exported.
        indices = [0, 29, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60, 63, 66, 69, 72, 80]
        contact_sheet(source, indices, [100, 60, 136, 119], target / 'frames.png', scale=6, columns=6)
        inputs.append({'hold_ticks': hold, 'fps': 60, 'start_tick': 30,
                       'video': f'hold-{hold:02d}/video.mp4', 'contact_sheet': f'hold-{hold:02d}/frames.png'})
    write_json(out / 'inputs.json', {'reference': 'Tiny Crate Classic', 'body_height_px': 7,
                                   'target_body_height_px': 24, 'inputs': inputs,
                                   'note': 'Raw calibration images; no motion measurements supplied here.'})


class Session:
    def __init__(self, directory):
        self.directory = session_path(directory)
        self.path = self.directory / 'session.json'
        if not self.path.is_file():
            raise IntegrationError('missing_session', 'Prepare the session before starting its MCP server')
        self._children = {}

    def read(self):
        return read_json(self.path)

    def update(self, callback):
        with file_lock(self.directory / '.session.lock'):
            state = self.read()
            callback(state)
            write_json(self.path, state)
            return state

    def start_clock(self):
        def update(state):
            if state.get('started_at'):
                raise IntegrationError('budget_locked', 'An experiment clock cannot be reset')
            if state['candidates'] or state.get('selected_candidate_id'):
                raise IntegrationError('budget_locked', 'A used experiment cannot reset its budget')
            if state.get('active_job_id'):
                raise IntegrationError('session_busy', 'Cannot reset a running experiment')
            state['started_at'] = utc_now()
            state['deadline_at'] = time.time() + SESSION_SECONDS
        return self.update(update)

    def inspect(self):
        self.recover()
        state = self.read()
        ground = state.get('experiment_id') == 'ground-motion-v1'
        return {'session_id': state['session_id'], 'mode': state['mode'], 'status': state['status'],
                'experiment_id': state.get('experiment_id', 'legacy-jump-v1'),
                'target_project': state['target'], 'active_job_id': state.get('active_job_id'),
                'candidate_count': len(state['candidates']), 'max_candidates': MAX_CANDIDATES,
                'seconds_remaining': max(0, round(state['deadline_at'] - time.time())) if state.get('deadline_at') else SESSION_SECONDS,
                'selected_candidate_id': state.get('selected_candidate_id'), 'final': state.get('final'),
                'candidates': [{k: c.get(k) for k in ('candidate_id','status','feedback','error')}
                               for c in state['candidates']],
                'editable_files': 'source/characters/player/**/*.gd (existing state interface must remain compatible)',
                'experiment': ('Tiny Crate ground pixels → jump-preserving SlayHorizon; 60Hz; ground calibration r06/r48 only'
                               if ground else 'Tiny Crate pixels → SlayHorizon; 60Hz; fit/calibration holds 3 and 24 only')}

    def recover(self):
        state = self.read()
        job_id = state.get('active_job_id')
        if not job_id:
            return
        path = self.directory / 'jobs' / f'{identifier(job_id)}.json'
        if not path.exists():
            return
        job = read_json(path)
        if job['status'] in TERMINAL_JOBS:
            self.update(lambda s: s.update(active_job_id=None) if s.get('active_job_id') == job_id else None)
            return
        pid = job.get('worker_pid')
        identity = process_identity(pid) if pid else None
        if pid and (identity is None or identity != job.get('worker_identity')):
            job.update(status='interrupted', error={'code':'worker_interrupted','message':'Worker ended before completing the job'}, finished_at=utc_now())
            write_json(path, job)
            self.update(lambda s: s.update(active_job_id=None, status='closed_failed' if s.get('selected_candidate_id') else ('ready' if s.get('reference') else 'new')) if s.get('active_job_id') == job_id else None)

    def submit(self, action, candidate_id=None):
        if action not in {'prepare', 'evaluate', 'finalize', 'report'}:
            raise IntegrationError('unknown_action', 'Unsupported experiment action')
        self.recover()
        job_id = 'job-' + uuid.uuid4().hex[:12]
        def reserve(state):
            if state.get('active_job_id'):
                raise IntegrationError('session_busy', 'An experiment job is already active; use get_job')
            if action == 'prepare':
                if state['status'] != 'new':
                    raise IntegrationError('already_prepared', 'Reference and baseline are already prepared')
            elif action in {'evaluate', 'finalize'}:
                if state.get('selected_candidate_id'):
                    raise IntegrationError('validation_sealed', 'This session has already revealed validation; no further tuning')
                if state['status'] != 'ready':
                    raise IntegrationError('not_ready', 'First finish prepare_experiment')
                if state.get('deadline_at') and time.time() >= state['deadline_at']:
                    raise IntegrationError('budget_exhausted', 'Thirty-minute experiment budget exhausted')
            if state.get('started_at') is None:
                state['started_at'], state['deadline_at'] = utc_now(), time.time()+SESSION_SECONDS
            job = {'schema_version':2, 'job_id':job_id, 'action':action, 'status':'queued',
                   'created_at':utc_now(), 'progress':{'message':'Queued'},
                   'session_id':state['session_id'], 'owner_pid':os.getpid(),
                   'owner_identity':process_identity(os.getpid()), 'candidate_id':candidate_id}
            if action == 'evaluate':
                if len(state['candidates']) >= MAX_CANDIDATES:
                    raise IntegrationError('budget_exhausted', 'All three candidate evaluations have been used')
                cid = f'candidate-{len(state["candidates"])+1:02d}'
                job['candidate_id'] = cid
                state['candidates'].append({'candidate_id':cid,'status':'queued','created_at':utc_now()})
            if action == 'finalize':
                eligible = [c for c in state['candidates'] if c.get('feedback',{}).get('eligible') is True and c.get('smoke',{}).get('status') in {'pass','passed'}]
                if state.get('experiment_id') == 'ground-motion-v1':
                    # Feedback can be published just before a worker's final
                    # cancellation/integrity check fails. Only a completed
                    # evaluation job may promote its candidate to validation.
                    completed = set()
                    for path in sorted((self.directory / 'jobs').glob('job-*.json')):
                        previous = read_json(path)
                        if (isinstance(previous, dict) and previous.get('session_id') == state['session_id']
                                and previous.get('job_id') == path.stem and previous.get('action') == 'evaluate'
                                and previous.get('status') == 'succeeded'):
                            completed.add(previous.get('candidate_id'))
                    eligible = [c for c in eligible if c.get('status') == 'evaluated' and c.get('candidate_id') in completed]
                if not eligible:
                    raise IntegrationError('no_eligible_candidate', 'No candidate passed calibration and gameplay integrity')
                chosen = min(eligible, key=lambda c:c['feedback']['mean_rmse_body'])
                if candidate_id is not None and candidate_id != chosen['candidate_id']:
                    raise IntegrationError('candidate_not_best', f'Lowest calibration error eligible candidate is {chosen["candidate_id"]}')
                job['candidate_id'] = chosen['candidate_id']
                state['selected_candidate_id'] = chosen['candidate_id']
                state['sealed_at'] = utc_now()
            state['active_job_id'] = job_id
            state['status'] = {'prepare':'preparing','evaluate':'evaluating','finalize':'finalizing','report':state['status']}[action]
            write_json(self.directory / 'jobs' / f'{job_id}.json', job)
        self.update(reserve)
        log_path = self.directory / 'jobs' / f'{job_id}.log'
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8='1')
        command = [sys.executable,'-m','kiroclops.integration.worker','--session',str(self.directory),'--job',job_id]
        try:
            with log_path.open('wb') as log:
                process = subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,**process_options())
            self._children[job_id] = process
            # The worker can die before importing its main module. Persist its
            # identity at spawn time, without racing the worker's first write.
            with file_lock(self.directory / '.session.lock'):
                job = read_json(log_path.with_suffix('.json'))
                if job['status'] == 'queued':
                    job.update(worker_pid=process.pid,worker_identity=process_identity(process.pid))
                    write_json(log_path.with_suffix('.json'),job)
        except Exception as exc:
            job = read_json(log_path.with_suffix('.json'))
            job.update(status='failed',error={'code':'worker_start_failed','message':str(exc)})
            write_json(log_path.with_suffix('.json'),job)
            self.update(lambda s:s.update(active_job_id=None,status='ready' if s.get('reference') else 'new'))
            raise
        return {'job_id':job_id,'status':'queued','candidate_id':read_json(log_path.with_suffix('.json')).get('candidate_id')}

    def get_job(self, job_id, wait_seconds=0):
        identifier(job_id)
        path = self.directory / 'jobs' / f'{job_id}.json'
        if not path.is_file():
            raise IntegrationError('unknown_job','Job does not belong to this session')
        deadline = time.monotonic() + min(20,max(0,wait_seconds))
        while True:
            self.recover()
            job = read_json(path)
            if job['status'] in TERMINAL_JOBS or time.monotonic() >= deadline:
                return {k:v for k,v in job.items() if k not in {'owner_identity','worker_identity','owner_pid','worker_pid'}}
            time.sleep(.2)

    def cancel_job(self, job_id):
        job = self.get_job(job_id)
        if job['status'] not in TERMINAL_JOBS:
            (self.directory / 'jobs' / f'{identifier(job_id)}.cancel').touch()
            return {'job_id':job_id,'status':'cancellation_requested'}
        return {'job_id':job_id,'status':job['status']}

    def close(self):
        # Only jobs started by this MCP process are owned by this connection.
        for job_id, child in self._children.items():
            if child.poll() is None:
                self.cancel_job(job_id)
        for child in self._children.values():
            if child.poll() is None:
                try:
                    child.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    from .runner import _terminate_owned_tree
                    _terminate_owned_tree(child)


def create_candidate_snapshot(session, candidate_id):
    state = session.read()
    if project_hashes(state['baseline']) != state['baseline_hashes']:
        raise IntegrationError('experiment_integrity', 'Session baseline changed before snapshot/diff')
    project = Path(state['target'])
    original = assert_allowed_changes(project, state['baseline_hashes'])
    root = session.directory / 'candidates' / identifier(candidate_id)
    root.mkdir(parents=True,exist_ok=True)
    copy_project(project,root/'project')
    if project_hashes(project) != original or project_hashes(root/'project') != original:
        raise IntegrationError('source_changed_during_snapshot','Target was edited while creating the candidate; retry as a new candidate')
    diff = []
    for rel in sorted(set(state['baseline_hashes'])|set(original)):
        if state['baseline_hashes'].get(rel) == original.get(rel):
            continue
        baseline = Path(state['baseline']) / rel
        after = root/'project'/rel
        diff.extend(difflib.unified_diff(baseline.read_text(encoding='utf-8').splitlines(True) if baseline.exists() else [],
                                       after.read_text(encoding='utf-8').splitlines(True),fromfile='before/'+rel,tofile='after/'+rel))
    (root/'patch.diff').write_text(''.join(diff),encoding='utf-8')
    candidate = {'candidate_id':candidate_id,'status':'capturing','source_hashes':original,
                 'diff_path':str(root/'patch.diff'),'snapshot_at':utc_now(),'calibration_runs':[]}
    write_json(root/'candidate.json',candidate)
    session.update(lambda s: s['candidates'].__setitem__(int(candidate_id[-2:])-1,candidate))
    return root
