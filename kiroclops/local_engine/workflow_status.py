"""Small read-only UI progress views; never reconcile or score a live job."""
from __future__ import annotations

import time
from pathlib import Path

from kiroclops.visual.schema import read_json, safe_path

AXES = ('measurement_validity', 'observed_behavior', 'application_integrity', 'model_completion',
        'native_evidence', 'unseen_validation', 'independent_visual_review', 'source_review')
# On Windows, opening a file while the session worker atomically replaces it
# raises PermissionError for a moment; a read-only view retries briefly.
READ_ATTEMPTS = 5
READ_RETRY_SECONDS = .05


def _read_settled(path):
    for _ in range(READ_ATTEMPTS-1):
        try:
            return read_json(path)
        except PermissionError:
            time.sleep(READ_RETRY_SECONDS)
    return read_json(path)


def progress_view(directory, *, now=None):
    directory = Path(directory)
    path = directory / 'session.json'
    if not path.is_file():
        return None
    state = _read_settled(path)
    budget = state.get('budget', {})
    candidates = state.get('candidates', [])
    stamp = time.time() if now is None else now
    deadline = state.get('deadline_at')
    result = {'session_status': state.get('status', 'unknown'), 'prepared': state.get('prepared', False),
              'candidates_used': len(candidates), 'candidate_limit': budget.get('max_candidates'),
              'remaining_seconds': max(0., deadline-stamp) if type(deadline) in (int, float) else None,
              'selected_candidate_id': state.get('selected_candidate_id'),
              'candidates': [{k: c.get(k) for k in ('candidate_id', 'status', 'eligible', 'score')}
                             for c in candidates]}
    identifier = state.get('active_job_id')
    if isinstance(identifier, str) and len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier):
        job_path = safe_path(directory, f'jobs/{identifier}/job.json')
        if job_path.is_file():
            job = _read_settled(job_path)
            if job.get('job_id') == identifier:
                result['active_job'] = {k: job.get(k) for k in ('operation', 'status', 'candidate_id')}
    return result


def acceptance_view(report):
    """Present declared axes without upgrading unknown or incomplete evidence."""
    axes = report.get('acceptance_axes', {})
    return {'overall': report.get('overall_acceptance', 'unknown'),
            'certified_success': report.get('acceptance_summary', {}).get('certified_success') is True,
            'axes': {key: {'status': axes.get(key, {}).get('status', 'unknown'),
                           'reason_codes': axes.get(key, {}).get('reason_codes', [])}
                     for key in AXES}}
