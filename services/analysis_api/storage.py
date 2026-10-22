"""Input packages and result objects share the same persistent byte quota."""
import json
from .store import key

LIMIT_BYTES=2*1024**3
RESULT_RESERVE_BYTES=6*1024**2


def result_size(objects,path):
    value=objects.size(path)
    if type(value) is not int or not 0<value<=RESULT_RESERVE_BYTES:raise ValueError('Invalid delivery size')
    return value


def reserve(service,tenant,amount):
    from .api import ApiError
    path=key(tenant,'account');previous=service.account(tenant)
    if previous.get('storage_bytes',0)+amount>LIMIT_BYTES:raise ApiError(413,'storage_quota_exceeded')
    return (path,previous,{**previous,'storage_bytes':previous.get('storage_bytes',0)+amount})


def settle(service,tenant,job,actual):
    reserved=job.get('reserved_delivery_bytes',0)
    if not reserved:return None
    if not 0<=actual<=reserved:raise ValueError('Delivery exceeds its storage reservation')
    path=key(tenant,'account');previous=service.account(tenant)
    return (path,previous,{**previous,'storage_bytes':previous.get('storage_bytes',0)-reserved+actual})


def release(service,tenant,job):
    charged=job.get('delivery_bytes',0)
    if not charged:return None
    path=key(tenant,'account');previous=service.account(tenant)
    return (path,previous,{**previous,'storage_bytes':max(0,previous.get('storage_bytes',0)-charged)})
