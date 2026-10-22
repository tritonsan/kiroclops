"""Computation quota is separate from EC2 startup/idle-hour accounting."""
from datetime import datetime,timezone
from .store import key

DAILY_SECONDS=1800
MAX_JOB_SECONDS=120


def usage_key(tenant,now):
    return key(tenant,'usage',datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d'))


def reserve(store,tenant,now):
    from .api import ApiError
    path=usage_key(tenant,now);previous=store.get(path)
    current=previous or {'charged_seconds':0,'reserved_seconds':0,'compute_jobs':0}
    if current['charged_seconds']+current['reserved_seconds']+MAX_JOB_SECONDS>DAILY_SECONDS:
        raise ApiError(429,'daily_compute_quota')
    updated={**current,'reserved_seconds':current['reserved_seconds']+MAX_JOB_SECONDS,'compute_jobs':current['compute_jobs']+1}
    return (path,previous,updated)


def settle(store,job,now):
    path=job.get('usage_key')
    if not path:return None
    previous=store.get(path)
    if previous is None:raise ValueError('Missing quota reservation')
    # Queued cancellation charges no compute. Failed running jobs count too.
    charged=min(MAX_JOB_SECONDS,max(0,now-job['started_at'])) if job.get('started_at') else 0
    return (path,previous,{**previous,'reserved_seconds':previous['reserved_seconds']-MAX_JOB_SECONDS,
                          'charged_seconds':previous['charged_seconds']+charged})
