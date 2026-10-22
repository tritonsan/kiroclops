"""Terminal authoring-turn cleanup, separate from durable client disconnects.

Only the session's recorded active worker can be cancelled/terminated. Ordinary
MCP close/reconnect remains durable. No result, source or authoring budget is
rewritten, and a queued job is never spawned to cancel it.
"""
from copy import deepcopy
from pathlib import Path
import time

from kiroclops.visual.schema import VisualError
from .backend import TERMINAL, _alive, _atomic, _digest, _lock, _read, _terminate_owned
from .session import JOB_BINDING_FIELDS, finalize_validation_deadline


def _job(session, identifier, expected=None):
    directory = session._job_dir(identifier)
    job = _read(directory / 'job.json')
    binding = _read(directory / 'binding.json').get('sha256')
    if (job.get('job_id') != identifier or _digest({k: job.get(k) for k in JOB_BINDING_FIELDS}) != binding
            or expected is not None and binding != expected):
        raise VisualError('cleanup_binding_changed', 'Terminal cleanup job identity changed.')
    return directory, job, binding


def _preserved_job(session, state, request, job):
    """Verify the sole job allowed to outlive a completed native turn."""
    identifier = request.get('job_id')
    selected = next((candidate for candidate in state['candidates']
                     if candidate['candidate_id'] == state.get('selected_candidate_id')), None)
    if (not identifier or state.get('final_job_id') != identifier or job['operation'] != 'finalize'
            or not selected or selected['status'] != 'succeeded' or selected['eligible'] is not True
            or job['candidate_id'] != selected['candidate_id']
            or job['source_sha256'] != selected['source_sha256']
            or request.get('candidate_id', selected['candidate_id']) != selected['candidate_id']
            or request.get('source_sha256', selected['source_sha256']) != selected['source_sha256']
            or _digest(job['source_hashes']) != selected['source_sha256']
            or Path(job['source_path']).resolve() != session.directory / 'candidates' / selected['candidate_id'] / 'source'
            or state.get('sealed_at') is None or state.get('deadline_at') is None
            or not job['created_at'] <= state['sealed_at'] < state['deadline_at']):
        raise VisualError('cleanup_binding_changed', 'Only the timely sealed final job may survive the native turn.')
    deadline = finalize_validation_deadline(state, job)
    if request.get('validation_deadline_at', deadline) != deadline:
        raise VisualError('cleanup_binding_changed', 'Sealed validation deadline changed during cleanup.')
    return deadline


def _adopt_preserved_worker(request, job):
    """A queued job may attach its one owned process after the turn closes."""
    current, recorded = job.get('worker'), request.get('worker')
    if current == recorded:
        return recorded
    provisional = request.get('provisional_worker')
    if (request.get('preserved_finalization') is True and recorded is None
            and isinstance(current, dict) and type(current.get('pid')) is int and current['pid'] > 0
            and isinstance(current.get('identity'), str) and current['identity']
            and (provisional is None or current['pid'] == provisional.get('pid'))):
        request['worker'] = deepcopy(current)
        return current
    raise VisualError('cleanup_binding_changed', 'Recorded worker identity changed during terminal cleanup.')


def begin_terminal_cleanup(session, *, preserve_finalization=False):
    """Cancel ordinary active work; hand off only the exact sealed final job."""
    state = session._load()
    request = {'session_id': state['session_id'], 'capture_lock': state['capture_lock'],
               'started_at': time.time(), 'job_id': state.get('active_job_id'),
               'had_pending_job': False, 'worker': None, 'preserved_finalization': False}
    if not request['job_id']:
        return request
    directory = session._job_dir(request['job_id'])
    with _lock(directory / 'job.lock', timeout=2):
        _, job, binding = _job(session, request['job_id'])
        request.update(job_binding_sha256=binding, operation=job['operation'],
                       status_at_start=job['status'], worker=deepcopy(job.get('worker')),
                       had_pending_job=job['status'] not in TERMINAL)
        if preserve_finalization:
            request['validation_deadline_at'] = _preserved_job(session, state, request, job)
            request['preserved_finalization'] = True
            request['candidate_id'] = job['candidate_id']
            request['source_sha256'] = job['source_sha256']
            worker = request['worker']
            if isinstance(worker, dict) and not worker.get('identity'):
                request['provisional_worker'] = worker
                request['worker'] = None
        elif request['had_pending_job']:
            if not (directory / 'cancel.json').exists():
                _atomic(directory / 'cancel.json', {'requested_at': time.time(), 'reason': 'authoring_turn_terminal'})
            if job['status'] == 'queued' and job.get('worker') is None:
                # Session.get_job would otherwise spawn this job while trying
                # to cancel/reconcile it. Mark it terminal under the spawn lock.
                job.update(status='cancelled', finished_at=time.time(),
                           error={'code': 'job_cancelled', 'message': 'Authoring turn ended before job execution.'})
                _atomic(directory / 'job.json', job)
    return request


def cancel_preserved_finalization(session, request):
    """Abort a handed-off final job only by its original session/job binding."""
    if request.get('preserved_finalization') is not True or not request.get('job_id'):
        raise VisualError('cleanup_binding_changed', 'No preserved final job belongs to this request.')
    state = session._load()
    if (state['session_id'] != request.get('session_id') or state['capture_lock'] != request.get('capture_lock')
            or state.get('active_job_id') not in (None, request['job_id'])):
        raise VisualError('cleanup_binding_changed', 'Preserved final job session changed.')
    directory = session._job_dir(request['job_id'])
    with _lock(directory / 'job.lock', timeout=2):
        _, job, _ = _job(session, request['job_id'], request['job_binding_sha256'])
        _preserved_job(session, state, request, job)
        _adopt_preserved_worker(request, job)
        if job['status'] not in TERMINAL:
            if not (directory / 'cancel.json').exists():
                _atomic(directory / 'cancel.json', {'requested_at': time.time(), 'reason': 'postturn_validation_abort'})
            if job['status'] == 'queued' and job.get('worker') is None:
                job.update(status='cancelled', finished_at=time.time(),
                           error={'code': 'job_cancelled', 'message': 'Sealed validation stopped before execution.'})
                _atomic(directory / 'job.json', job)
    return request


def finish_terminal_cleanup(session, request, *, timeout=10.0, terminate=True):
    """After ACP close, await the exact owned worker and the capture lease.

An explicit resume can replay the same request; it must not replace the old
worker identity with whichever process happens to be present later.
"""
    started = time.monotonic()
    result = {'kind': 'TerminalTrialCleanup', 'status': 'unconfirmed', 'request': deepcopy(request),
              'worker_exit_verified': False, 'client_exit_verified': False, 'capture_lease_released': False,
              'termination_requested': False, 'cleanup_outside_authoring_budget': True}
    try:
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise VisualError('invalid_cleanup_timeout', 'Cleanup requires a bounded positive timeout of at most 30 seconds.')
        state = session._load()
        if (request.get('session_id') != state['session_id'] or request.get('capture_lock') != state['capture_lock']
                or state.get('active_job_id') not in (None, request.get('job_id'))):
            raise VisualError('cleanup_binding_changed', 'Terminal cleanup session or active job changed.')
        worker = request.get('worker')
        client = request.get('client_process')
        for owned in (worker, client):
            if owned is not None and (type(owned.get('pid')) is not int or owned['pid'] <= 0
                                      or not isinstance(owned.get('identity'), str) or not owned['identity']):
                raise VisualError('cleanup_identity_missing', 'Owned process lacks a verifiable creation identity.')
        while True:
            if request.get('job_id'):
                directory, job, _ = _job(session, request['job_id'], request['job_binding_sha256'])
                if request.get('preserved_finalization'):
                    _preserved_job(session, state, request, job)
                    worker = _adopt_preserved_worker(request, job)
                    result['request'] = deepcopy(request)
                elif job.get('worker') != worker:
                    raise VisualError('cleanup_binding_changed', 'Recorded worker identity changed during terminal cleanup.')
                if worker is None and job['status'] not in TERMINAL:
                    raise VisualError('cleanup_identity_missing', 'Nonterminal job has no verifiable owned worker.')
            alive = _alive(worker)
            result['client_exit_verified'] = not _alive(client)
            result['worker_exit_verified'] = not alive
            if not alive and result['client_exit_verified']:
                try:
                    with _lock(Path(state['capture_lock']), timeout=0):
                        result['capture_lease_released'] = True
                    break
                except TimeoutError:
                    pass
            elif (alive and terminate and not result['termination_requested']
                  and (not request.get('preserved_finalization') or job['status'] in TERMINAL
                       or (directory / 'cancel.json').exists())
                  and time.monotonic() - started >= min(.25, timeout / 2)):
                # _terminate_owned checks PID + creation identity again before
                # killing. The worker's containment owns only its descendants.
                result['termination_requested'] = True
                _terminate_owned(worker)
            if time.monotonic() - started >= timeout:
                raise VisualError('terminal_cleanup_unconfirmed', 'Owned worker exit or capture lease release was not confirmed.')
            time.sleep(min(.025, max(0, timeout - (time.monotonic() - started))))
        if request.get('job_id'):
            with _lock(directory / 'job.lock', timeout=max(0, timeout - (time.monotonic() - started))):
                _, job, _ = _job(session, request['job_id'], request['job_binding_sha256'])
                if job['status'] not in TERMINAL:
                    if request.get('preserved_finalization') and not (directory / 'cancel.json').exists():
                        raise VisualError('finalization_pending', 'Sealed validation is still running.')
                    job.update(status='cancelled', finished_at=time.time(),
                               error={'code': 'job_cancelled', 'message': 'Authoring turn ended; owned execution stopped.'})
                    _atomic(directory / 'job.json', job)
            # Normal inspect/get_job can now apply this terminal result without
            # respawning. Do not enter its separate state-lock wait here: this
            # barrier has its own strict wall-time bound. Result bytes survive.
            result['terminal_job_status'] = job['status']
        state = session._load()
        if state.get('active_job_id') not in (None, request.get('job_id')):
            raise VisualError('terminal_cleanup_unconfirmed', 'Another active job appeared during terminal cleanup.')
        result['terminal_state_reconciliation_pending'] = state.get('active_job_id') is not None
        result['status'] = 'pass'
    except (VisualError, OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        result['error'] = {'code': getattr(exc, 'code', 'terminal_cleanup_unconfirmed'), 'message': str(exc)}
    result.update(finished_at=time.time(), elapsed_seconds=time.monotonic() - started)
    return result
