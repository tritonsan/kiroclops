"""Tenant-local exact reuse of verified measurements, never acceptance."""
from __future__ import annotations

import time
from kiroclops.cloud.results import envelope, validate_delivery
from kiroclops.cloud.identity import computation_key
from kiroclops.visual.schema import digest
from .store import key, Conflict


def validate_job_delivery(job, delivery):
    return validate_delivery(delivery,job['request'],logical_job_id=job['logical_job_id'],
        expected_engine_sha256=job['engine_sha256'],expected_sources=job['analysis_sources'])


def enroll(service,tenant,job,delivery):
    validate_job_delivery(job,delivery)
    package=service.store.get(key(tenant,'package',job['request']['package_sha256']))
    if (not package or package['status']!='verified' or package['expires_at']<=service.clock()
            or package.get('generation',1)!=job.get('package_generation',1)):
        return
    # No similarity indexing. A source with no complete engine/code receipt
    # never reaches this path; legacy result.json files cannot be enrolled.
    cache_key=delivery['receipt']['computation_key']
    row={'computation_key':cache_key,'source_job_id':job['job_id'],
         'package_generation':package.get('generation',1),
         'measurement_sha256':delivery['measurement_sha256'],'expires_at':package['expires_at']}
    previous=service.store.get(key(tenant,'cache',cache_key))
    if previous is not None and previous.get('package_generation',1)==row['package_generation']:return
    try:service.store.transact([(key(tenant,'cache',cache_key),previous,row)])
    except Conflict:pass  # another reader enrolled the same verified result


def lookup(service,tenant,request,logical,descriptor):
    started=time.monotonic()
    engine=service.objects.read_json('service/engine.json')
    if engine['engine_sha256']!=descriptor['engine_sha256']:
        return None
    code=descriptor['analysis_sources']
    cache_key=computation_key(request['package_sha256'],request['selection'],request.get('contract'),code,engine)
    row=service.store.get(key(tenant,'cache',cache_key))
    if not row or row['expires_at']<=service.clock():return None
    package=service.store.get(key(tenant,'package',request['package_sha256']))
    if (not package or package['status']!='verified' or package['expires_at']<=service.clock()
            or package.get('generation',1)!=row.get('package_generation',1)):return None
    source=service.store.get(key(tenant,'job',row['source_job_id']))
    if not source or source['status']!='succeeded':return None
    delivery=service.objects.read_json(source['delivery_key'])
    validate_job_delivery(source,delivery)
    if (delivery['receipt']['computation_key']!=cache_key
            or delivery['measurement_sha256']!=row['measurement_sha256']):
        return None
    avoided=delivery['receipt'].get('timings',{}).get('worker_wall_seconds',0)
    return envelope(delivery['measurement'],request,logical_job_id=logical,engine=engine,sources=code,
        timings={'cache_wall_seconds':time.monotonic()-started,'avoided_worker_seconds':avoided},
        origin={'kind':'exact_reuse','source_job_id':source['job_id'],'source_measurement_sha256':delivery['measurement_sha256']})
