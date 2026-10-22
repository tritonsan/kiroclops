"""Bounded historical inventory: explicit job stores, no recursive artifact walk."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from kiroclops.visual.schema import digest, read_json, write_json


def inventory(stores, output):
    jobs, duplicates, conflicts, invalid = {}, [], [], []
    for root in stores:
        for path in sorted((Path(root) / 'jobs').glob('*/state.json')):
            state = read_json(path)
            if state.get('status') != 'succeeded':
                continue
            job_id = state.get('job_id')
            try:
                result = read_json(path.parent / 'result.json')
                if not job_id or digest(result) != state['result_sha256']:
                    raise ValueError('result checksum')
                duration = float(state['finished_at']) - float(state['started_at'])
                if duration < 0:
                    raise ValueError('negative duration')
            except (OSError, KeyError, TypeError, ValueError) as exc:
                invalid.append({'path': str(path), 'reason': str(exc)})
                continue
            row = {'job_id': job_id, 'request_sha256': state['request_sha256'],
                   'result_sha256': state['result_sha256'], 'duration_s': duration,
                   'started_at': state['started_at'], 'path': str(path)}
            if job_id in jobs:
                (duplicates if all(jobs[job_id][k] == row[k] for k in ('request_sha256', 'result_sha256', 'duration_s')) else conflicts).append(row)
            else:
                jobs[job_id] = row
    seen, equal, different = {}, [], []
    rows = sorted(jobs.values(), key=lambda r: (r['started_at'], r['job_id']))
    for row in rows:
        key = row['request_sha256']
        if key in seen:
            (equal if row['result_sha256'] in seen[key] else different).append(row)
        seen.setdefault(key, set()).add(row['result_sha256'])
    total = sum(row['duration_s'] for row in rows)
    report = {'schema_version': 1, 'kind': 'BoundedHistoryInventory',
              'stores': [str(Path(p)) for p in stores], 'unique_jobs': len(rows),
              'copied_jobs_ignored': len(duplicates), 'conflicting_copies': conflicts, 'invalid_results': invalid,
              'same_request_same_result_repeats': len(equal), 'same_request_different_result_repeats': len(different),
              'observed_equal_repeat_time_fraction': sum(r['duration_s'] for r in equal) / total if total else None,
              'worker_duration_s': total, 'rows': rows,
              'interpretation': 'Descriptive only, not a cache hit guarantee or upper bound. Historical keys omit code/engine. No historical job is enrolled in cache.'}
    write_json(output, report)
    return report
