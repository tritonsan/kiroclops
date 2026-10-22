"""Immediate access revocation and repeatable content cleanup."""
from __future__ import annotations

from .store import key,Conflict


def retire_package(service,tenant,package_key,package):
    if package['status']=='deleted' and not package.get('cleanup_pending'):return
    if not package.get('cleanup_pending'):
        revoked={**package,'status':'deleted','cleanup_pending':True,'deleted_at':service.clock()}
        service.store.transact([(package_key,package,revoked)])
        package=revoked
    for object_key in (package.get('object_key'),package.get('staging_key')):
        if object_key:service.objects.delete(object_key)
    account_key=key(tenant,'account');account=service.store.get(account_key)
    changes=[(package_key,package,{**package,'cleanup_pending':False,'storage_released':True})]
    if not package.get('storage_released'):
        changes.append((account_key,account,{**account,'storage_bytes':max(0,account.get('storage_bytes',0)-package['bytes'])}))
    service.store.transact(changes)


def erase(service,tenant):
    account_key=key(tenant,'account');account=service.store.get(account_key)
    if not account:raise ValueError('Unknown account')
    if account.get('data_state') not in ('deleting','deleted'):
        updated={**account,'data_state':'deleting','deletion_requested_at':service.clock()}
        service.store.transact([(account_key,account,updated)])
        account=updated
    active=service.store.get(key(tenant,'active'))
    if active:
        job_key=key(tenant,'job',active['job_id']);job=service.store.get(job_key)
        if job and job['status'] in ('queued','running'):
            if job['status']=='running' and job.get('lease_until',0)>service.clock():
                if not job.get('cancel_requested'):
                    service.store.transact([(job_key,job,{**job,'cancel_requested':True})])
                return {'status':'deleting','waiting_for_worker':True,'deadline_seconds':86400}
            service.finish(tenant,job,'cancelled',None)
    # A dead lease does not prove its disk was erased. Wait for either that
    # job's finally block or a later verified-empty worker boot.
    boot=service.store.get('system#worker#disk') or {}
    pending_disk=[row['job_id'] for _,row in service.store.list_prefix(key(tenant,'job'))
        if row.get('started_at') and not row.get('disk_cleaned_at')
        and row['started_at']>boot.get('verified_empty_at',0)]
    if pending_disk:
        return {'status':'deleting','waiting_for_worker_disk':True,'deadline_seconds':86400}
    # Account access remains revoked throughout retries and partial failures.
    # Only object keys under this verified subject's namespace are touched.
    service.objects.delete_prefix('users/'+tenant+'/')
    for record,value in list(service.store.list_prefix(key(tenant,'')[:-1])):
        if record==account_key:continue
        try:service.store.transact([(record,value,None)])
        except Conflict:return {'status':'deleting','waiting_for_worker':False,'deadline_seconds':86400}
    current=service.store.get(account_key)
    updated={**current,'data_state':'deleted','storage_bytes':0,'deletion_completed_at':service.clock()}
    service.store.transact([(account_key,current,updated)])
    return {'status':'deleted','access_revoked':True,'cloud_objects_deleted':True,
            'worker_disk':'job_cleanup_or_later_empty_boot_verified'}


def acknowledge(service,tenant,job_id,body):
    from .api import ApiError
    from .cache import validate_job_delivery
    job_key=key(tenant,'job',job_id);job=service.store.get(job_key)
    if not job or job['status']!='succeeded':raise ApiError(404,'result_not_found')
    delivery=service.objects.read_json(job['delivery_key']);validate_job_delivery(job,delivery)
    if body.get('measurement_sha256')!=delivery['measurement_sha256'] or body.get('receipt_sha256')!=delivery['receipt_sha256']:
        raise ApiError(409,'acknowledgement_binding_mismatch')
    if not job.get('acknowledged'):
        service.store.transact([(job_key,job,{**job,'acknowledged':True,'acknowledged_at':service.clock()})])
    package_key=key(tenant,'package',job['request']['package_sha256']);package=service.store.get(package_key)
    if package and package['retention_class']=='candidate' and (package['status']=='verified' or package.get('cleanup_pending')):
        active=service.store.get(key(tenant,'active'))
        if active:
            if not package.get('retire_when_idle'):
                service.store.transact([(package_key,package,{**package,'retire_when_idle':True})])
            return {'acknowledged':True,'candidate_cleanup':'deferred_until_active_job_finishes'}
        # Revocation precedes physical object deletion. A deleted package is
        # never eligible for an exact-cache lookup, even if a cache row remains.
        retire_package(service,tenant,package_key,package)
    return {'acknowledged':True,'candidate_cleanup':'complete_or_not_required'}
