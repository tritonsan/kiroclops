"""A separate Cognito app client identifies agents; scoped tokens add limits."""
import hashlib
import re
import secrets
from .store import key


def issue(service,tenant):
    token=secrets.token_urlsafe(32)
    identifier=hashlib.sha256(token.encode()).hexdigest()
    # Covers one 30-minute authoring stage plus login, connection and sealed
    # result delivery. This does not extend the model's 1800-second budget.
    row={'id':identifier,'issued_at':service.clock(),'expires_at':service.clock()+3600,
         'max_jobs':128,'jobs':0,'scope':['analysis:submit','analysis:read','analysis:cancel'],
         'index_for_similarity':False}
    service.store.transact([(key(tenant,'capability',identifier),None,row)])
    return {'capability':token,'expires_at':row['expires_at'],'max_jobs':128,'scope':row['scope']}


def authorize(service,tenant,token,method,path):
    from .api import ApiError
    if not isinstance(token,str) or not 20<=len(token)<=128:raise ApiError(403,'session_capability_required')
    identifier=hashlib.sha256(token.encode()).hexdigest()
    row=service.store.get(key(tenant,'capability',identifier))
    if not row or row['expires_at']<=service.clock():raise ApiError(403,'session_capability_expired')
    if not ((method=='GET' and path in ('/service','/usage'))
            or (method=='POST' and path in ('/packages/prepare','/packages/complete','/jobs'))
            or re.fullmatch(r'/jobs/[a-f0-9]{32}(/cancel|/ack)?',path)):
        raise ApiError(403,'operation_outside_agent_scope')
    job_path=re.fullmatch(r'/jobs/([a-f0-9]{32})(/cancel|/ack)?',path)
    if job_path:
        job=service.store.get(key(tenant,'job',job_path[1]))
        if not job or job.get('capability_id')!=identifier:raise ApiError(404,'job_not_found')
    return identifier


def reservation(service,tenant,identifier):
    from .api import ApiError
    if identifier is None:return None
    path=key(tenant,'capability',identifier);row=service.store.get(path)
    if not row or row['expires_at']<=service.clock() or row['jobs']>=row['max_jobs']:
        raise ApiError(429,'session_job_limit')
    return (path,row,{**row,'jobs':row['jobs']+1})
