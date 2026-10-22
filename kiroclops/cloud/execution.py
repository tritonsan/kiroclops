"""Shared production analysis execution with durable reconnect semantics."""
from __future__ import annotations

from pathlib import Path
import os
import time
import uuid

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from .results import request_binding

TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}
EXPLICIT_CANCELLATION = {'job_cancelled', 'budget_exhausted', 'job_deadline_exceeded'}


def backend_for(output):
    return backend_for_store(Path(output) / 'jobs')


def backend_for_store(store):
    mode = os.environ.get('KIROCLOPS_ANALYSIS_BACKEND', 'local')
    if mode == 'local':
        from kiroclops.local_engine.backend import LocalAnalysisBackend
        return LocalAnalysisBackend(store)
    if mode == 'remote':
        from .remote import RemoteAnalysisBackend
        config = os.environ.get('KIROCLOPS_CLOUD_CONFIG')
        if not config:
            raise VisualError('remote_not_configured', 'Explicit remote execution requires a service configuration')
        return RemoteAnalysisBackend(store, read_json(config))
    raise VisualError('invalid_backend', 'Select local or remote explicitly; no fallback is performed')


def execute(package, selection, output, *, contract=None, candidate_sha256=None,
            check=lambda: None, progress=lambda *args: None, backend=None):
    output = Path(output)
    manifest = load_package(package, verify='hashes')
    request = request_binding(manifest['package_sha256'], selection, contract, candidate_sha256)
    request_sha = digest(request)
    backend = backend if backend is not None else backend_for(output)
    receipt_path = output / 'analysis-operation.json'
    # A local operation ID exists before the first network request. Retrying
    # submit after a lost response uses exactly this key and immutable request.
    from kiroclops.local_engine.backend import _lock
    output.mkdir(parents=True, exist_ok=True)
    with _lock(output / 'analysis-operation.lock'):
        if receipt_path.exists():
            operation = read_json(receipt_path)
            if operation['request_sha256'] != request_sha:
                raise VisualError('operation_conflict', 'This output directory belongs to another immutable request')
        else:
            operation = {'schema_version': 1, 'logical_job_id': uuid.uuid4().hex,
                         'request_sha256': request_sha, 'job_id': None, 'status': 'prepared'}
            write_json(receipt_path, operation)
        try:
            check()
        except VisualError as exc:
            if exc.code in EXPLICIT_CANCELLATION and operation.get('job_id'):
                backend.cancel_job(operation['job_id'])
            raise
        if operation['job_id'] is None:
            submitted = backend.submit({**request, 'package_path': str(package)},
                                       idempotency_key=operation['logical_job_id'])
            operation.update(job_id=submitted['job_id'], status=submitted['status'])
            write_json(receipt_path, operation)
    try:
        while True:
            check()
            job = backend.get_job(operation['job_id'], wait_seconds=1)
            if job['status'] in TERMINAL and job.get('worker_exited', True):
                break
            state = job.get('progress') or {}
            progress('analysis_' + str(state.get('phase', job['status'])), state.get('completed', 0), state.get('total', 1))
        operation['status'] = job['status']
        write_json(receipt_path, operation)
        if job['status'] != 'succeeded':
            error = job.get('error') or {}
            raise VisualError(error.get('code', 'analysis_failure'), error.get('message', 'Analysis did not complete'))
        result = job['result']
        if (result.get('package_sha256') != request['package_sha256']
                or result.get('candidate_sha256') != candidate_sha256
                or result.get('request_sha256') != request_sha
                or job.get('result_sha256') != digest(result)):
            raise VisualError('result_binding_mismatch', 'Result does not match the current request/candidate')
        write_json(output / 'analysis.json', result)
        return result
    except VisualError as exc:
        if exc.code in EXPLICIT_CANCELLATION:
            backend.cancel_job(operation['job_id'])
        # Network failures and caller disconnections preserve the remote job.
        raise


def measure_direct(package, selection, contract=None, check=lambda: None, progress=lambda *args: None):
    """Common in-process path for qualification, recorder-adjacent diagnostics."""
    from kiroclops.visual.tracking import analyze_package
    from kiroclops.visual.events import extract_events
    observations = analyze_package(package, selection, contract, check=check, progress=progress)
    check()
    return {'observations': observations, 'events': extract_events(observations, selection, contract)}


def analyze_package(package, selection, contract=None, check=lambda: None, progress=lambda *args: None,
                    *, output=None, candidate_sha256=None):
    """Observation-only compatibility entry for game/intake production callers."""
    destination = output or Path(package).parent / ('analysis-execution-' + digest({'selection': selection, 'contract': contract})[:16])
    return execute(package, selection, destination, contract=contract, candidate_sha256=candidate_sha256,
                   check=check, progress=progress)['observations']
