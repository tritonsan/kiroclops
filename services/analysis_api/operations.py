"""Worker-state accounting includes startup, idle and internal test time."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta

DAILY_SECONDS=4*3600
MONTHLY_SECONDS=120*3600
STOP_USD=40
# Standard c8g.xlarge plus public IPv4. COOL adds its measured software rate.
HOURLY_USD=.15952+.005
NON_COMPUTE_RESERVE_USD=5


def account_interval(value,start,end,hourly=HOURLY_USD):
    from copy import deepcopy
    value=deepcopy(value)
    value.setdefault('daily_seconds',{});value.setdefault('monthly_seconds',{})
    value.setdefault('monthly_compute_usd',{})
    point=start
    while point<end:
        current=datetime.fromtimestamp(point,timezone.utc)
        midnight=(current+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        stop=min(end,midnight);seconds=stop-point
        day=current.strftime('%Y-%m-%d');month=current.strftime('%Y-%m')
        value['daily_seconds'][day]=value['daily_seconds'].get(day,0)+seconds
        value['monthly_seconds'][month]=value['monthly_seconds'].get(month,0)+seconds
        value['monthly_compute_usd'][month]=value['monthly_compute_usd'].get(month,0)+seconds/3600*hourly
        point=stop
    return value


def observe(previous,state,now,launch_time):
    previous=previous or {'last_accounted_at':launch_time,'observed_state':'pending'}
    start=min(now,previous['last_accounted_at'])
    # Conservatively charge the entire observation gap around a stop. State
    # events normally close the gap immediately; a lost event overestimates
    # usage instead of leaving uncounted startup/idle time.
    active=previous['observed_state'] in ('pending','running','stopping') or state in ('pending','running')
    updated=account_interval(previous,start,now) if active else dict(previous)
    updated.update(last_accounted_at=now,observed_state=state)
    current=datetime.fromtimestamp(now,timezone.utc)
    day=current.strftime('%Y-%m-%d');month=current.strftime('%Y-%m')
    daily=updated.get('daily_seconds',{}).get(day,0);monthly=updated.get('monthly_seconds',{}).get(month,0)
    estimated=updated.get('monthly_compute_usd',{}).get(month,0)+NON_COMPUTE_RESERVE_USD
    # Five minutes are reserved for controller cadence and EC2 stop latency.
    # This is a conservative resource meter, not a final AWS invoice.
    updated['compute_allowed']=daily<DAILY_SECONDS-300 and monthly<MONTHLY_SECONDS-300 and estimated<STOP_USD
    updated['uploads_allowed']=estimated<STOP_USD
    updated['estimated_monthly_usd_before_credits']=estimated
    updated['meter_scope']='EC2 observed state intervals + public IPv4 + fixed 5 USD non-compute reserve; not billing API'
    return updated


def action(state,pending,budget):
    if not budget['compute_allowed']:
        return 'stop' if state in ('pending','running') else 'none'
    if pending and state=='stopped':return 'start'
    if pending and state=='stopping':return 'wait_for_stopped_event'
    return 'none'
