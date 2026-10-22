"""JWT-authenticated control plane for the first manually started worker.

No request bodies, bearer tokens or signed URLs are logged. The API never
decodes user images. Only a worker can mark a package as verified.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
import os
import re
import time
import uuid

from .store import Conflict, DynamoStore, key

TERMINAL = {'succeeded','failed','cancelled','interrupted'}
MAX_BYTES = 512 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def identifier(value, length=64):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{' + str(length) + '}', value):
        raise ApiError(400, 'invalid_identifier')
    return value


class Service:
    def __init__(self, store, objects, queue, *, clock=time.time):
        self.store, self.objects, self.queue, self.clock = store, objects, queue, clock
        self.capability_id=None
        self.require_budget=False

    def check_budget(self,kind):
        if not self.require_budget:return
        budget=self.store.get('system#budget#worker')
        if not budget or self.clock()-budget.get('last_accounted_at',0)>600:
            raise ApiError(503,'worker_budget_unavailable')
        if not budget.get(kind+'_allowed'):raise ApiError(429,'worker_budget_exhausted')

    def account(self, tenant):
        account = self.store.get(key(tenant,'account'))
        if not account or not account.get('enabled'):
            raise ApiError(403, 'invitation_required')
        return account

    def dispatch(self, tenant, method, path, body):
        account = self.account(tenant)
        if method=='DELETE' and path=='/data':
            from .lifecycle import erase
            return erase(self,tenant)
        if account.get('data_state') in ('deleting','deleted') and path!='/usage':
            raise ApiError(410,'data_revoked')
        if method == 'GET' and path == '/service':
            return self.objects.descriptor()
        if method=='POST' and path=='/sessions':
            from .capabilities import issue
            return issue(self,tenant)
        if method=='POST' and path=='/sessions/revoke':
            count=0
            for cap_key,row in list(self.store.list_prefix(key(tenant,'capability'))):
                self.store.transact([(cap_key,row,{**row,'expires_at':0})]);count+=1
            return {'revoked':count}
        if method == 'GET' and path == '/usage':
            from .quota import usage_key,DAILY_SECONDS
            return {'storage_bytes':account.get('storage_bytes',0),'data_state':account.get('data_state','active'), 'active_job':(self.store.get(key(tenant,'active')) or {}).get('job_id'),
                    'daily_compute':self.store.get(usage_key(tenant,self.clock())) or {'charged_seconds':0,'reserved_seconds':0,'compute_jobs':0},
                    'limits':{'active_jobs':1,'storage_bytes':2*1024**3,'daily_compute_seconds':DAILY_SECONDS}, 'beta_readiness':'internal-single-package'}
        if method == 'POST' and path == '/packages/prepare':
            return self.prepare(tenant, account, body)
        if method == 'POST' and path == '/packages/complete':
            return self.complete(tenant, body)
        if method == 'POST' and path == '/jobs':
            return self.submit(tenant, body)
        ack=re.fullmatch(r'/jobs/([a-f0-9]{32})/ack',path)
        if method=='POST' and ack:
            from .lifecycle import acknowledge
            return acknowledge(self,tenant,ack[1],body)
        match = re.fullmatch(r'/jobs/([a-f0-9]{32})(/cancel)?',path)
        if match:
            job_key = key(tenant,'job',match[1])
            job = self.store.get(job_key)
            if not job:
                raise ApiError(404, 'job_not_found')
            if method == 'POST' and match[2]:
                if job['status'] not in TERMINAL:
                    updated = {**job,'cancel_requested':True}
                    self.store.transact([(job_key,job,updated)])
                    # Queued jobs can be cancelled without waiting for EC2.
                    if job['status']=='queued':
                        self.finish(tenant,updated,'cancelled',None)
                return {'job_id':job['job_id'],'cancel_requested':True}
            if method == 'GET' and not match[2]:
                if job['status']=='queued':
                    self.queue.send(tenant,job['job_id'])  # repair a lost enqueue response
                reply = {k:job.get(k) for k in ('job_id','status','progress','error')}
                if job['status']=='succeeded':
                    reply['delivery'] = self.objects.read_json(job['delivery_key'])
                    from .cache import enroll
                    enroll(self,tenant,job,reply['delivery'])
                return reply
        raise ApiError(404,'route_not_found')

    def prepare(self, tenant, account, body):
        package = identifier(body.get('package_sha256'))
        archive = identifier(body.get('archive_sha256'))
        size = body.get('bytes')
        if type(size) is not int or not 0 < size <= MAX_BYTES or body.get('index_for_similarity') is not False:
            raise ApiError(400,'invalid_package')
        retention = body.get('retention_class','candidate')
        if retention not in ('candidate','reference','user'):
            raise ApiError(400,'invalid_retention')
        package_key = key(tenant,'package',package)
        previous = self.store.get(package_key)
        if previous and previous['status']=='verified' and previous['expires_at']>self.clock():
            if previous['retention_class']=='candidate' and retention in ('reference','user'):
                self.store.transact([(package_key,previous,{**previous,'retention_class':retention})])
            return {'verified':True,'package_sha256':package}
        self.check_budget('uploads')
        replacing=bool(previous and (previous['status']=='deleted' or previous['expires_at']<=self.clock()))
        if replacing:
            if self.store.get(key(tenant,'active')):raise ApiError(409,'package_in_use')
            from .lifecycle import retire_package
            retire_package(self,tenant,package_key,previous)
            previous=self.store.get(package_key)
            account=self.account(tenant)
        if previous and not replacing:
            if previous['archive_sha256']!=archive or previous['bytes']!=size:
                raise ApiError(409,'package_conflict')
            current = previous
        else:
            if account.get('storage_bytes',0)+size > 2*1024**3:
                raise ApiError(413,'storage_quota_exceeded')
            current = {'package_sha256':package,'archive_sha256':archive,'bytes':size,
                       'status':'uploading','staging_key':f'users/{tenant}/uploads/{uuid.uuid4().hex}.zip',
                       'generation':previous.get('generation',1)+1 if previous else 1,
                       'retention_class':retention,'expires_at':int(self.clock())+30*86400,'indexed_for_similarity':False}
            self.store.transact([(package_key,previous,current),
                (key(tenant,'account'),account,{**account,'storage_bytes':account.get('storage_bytes',0)+size})])
        return {'verified':False,'upload_url':self.objects.upload_url(current['staging_key'],size)}

    def complete(self, tenant, body):
        package = identifier(body.get('package_sha256'))
        package_key = key(tenant,'package',package)
        previous = self.store.get(package_key)
        if not previous or previous['archive_sha256']!=identifier(body.get('archive_sha256')):
            raise ApiError(404,'package_not_found')
        if previous['status'] in ('received','verified'):
            return {'received':True,'verified':previous['status']=='verified'}
        if previous['status']!='uploading' or self.objects.size(previous['staging_key'])!=previous['bytes']:
            raise ApiError(409,'upload_incomplete')
        self.store.transact([(package_key,previous,{**previous,'status':'received'})])
        return {'received':True,'verified':False}

    def submit(self, tenant, body):
        logical = body.get('logical_job_id')
        if not isinstance(logical,str) or not 1<=len(logical)<=256 or body.get('schema_version')!=2:
            raise ApiError(400,'invalid_request')
        request = body.get('request',{})
        if (request.get('operation')!='analyze' or not isinstance(request.get('selection'),dict)
                or set(request)-{'operation','package_sha256','selection','contract','candidate_sha256'}):
            raise ApiError(400,'invalid_request')
        if len(json.dumps(request))>160*1024:
            raise ApiError(413,'request_too_large')
        package = identifier(request.get('package_sha256'))
        descriptor = self.objects.descriptor()
        if body.get('engine_sha256')!=descriptor['engine_sha256'] or body.get('analysis_sources_sha256')!=digest(descriptor['analysis_sources']):
            raise ApiError(409,'engine_changed')
        job_id = digest([tenant,logical])[:32]
        job_key = key(tenant,'job',job_id)
        previous = self.store.get(job_key)
        binding = digest(body)
        if previous:
            if previous['binding_sha256']!=binding or previous.get('capability_id')!=self.capability_id:
                raise ApiError(409,'idempotency_conflict')
            if previous['status']=='queued':
                self.queue.send(tenant,job_id)
            return {'job_id':job_id,'status':previous['status']}
        package_row = self.store.get(key(tenant,'package',package))
        if not package_row or package_row['status'] not in ('received','verified') or package_row['expires_at']<=self.clock():
            raise ApiError(404,'package_not_found')
        from .cache import lookup
        cached=lookup(self,tenant,request,logical,descriptor)
        if cached is not None:
            from .storage import reserve,RESULT_RESERVE_BYTES
            size=len(json.dumps(cached,allow_nan=False).encode())
            if size>RESULT_RESERVE_BYTES:raise ApiError(413,'result_too_large')
            storage=reserve(self,tenant,size)
            # One independent delivery and request binding per new candidate.
            # Only the immutable measurement is shared with the source job.
            path=f'users/{tenant}/deliveries/{job_id}-{uuid.uuid4().hex}.json'
            self.objects.put_json(path,cached)
            job={'job_id':job_id,'logical_job_id':logical,'request':deepcopy(request),'binding_sha256':binding,
                 'status':'succeeded','created_at':self.clock(),'finished_at':self.clock(),'cancel_requested':False,
                 'engine_sha256':descriptor['engine_sha256'],'analysis_sources':descriptor['analysis_sources'],
                 'delivery_key':path,'delivery_bytes':size,'origin':'exact_reuse','worker_woken':False,'package_generation':package_row.get('generation',1)}
            from .capabilities import reservation
            cap=reservation(self,tenant,self.capability_id)
            job['capability_id']=self.capability_id
            try:self.store.transact([(job_key,None,job),storage]+([cap] if cap else []))
            except Conflict:
                self.objects.delete(path)
                raise
            return {'job_id':job_id,'status':'succeeded','origin':'exact_reuse'}
        active_key = key(tenant,'active')
        self.check_budget('compute')
        if self.store.get(active_key):
            raise ApiError(429,'active_job_limit')
        job = {'job_id':job_id,'logical_job_id':logical,'request':deepcopy(request),'binding_sha256':binding,
               'package_generation':package_row.get('generation',1),
               'status':'queued','created_at':self.clock(),'cancel_requested':False,'lease_until':0,
               'engine_sha256':descriptor['engine_sha256'],'analysis_sources':descriptor['analysis_sources']}
        from .quota import reserve
        quota=reserve(self.store,tenant,self.clock())
        job['usage_key']=quota[0]
        from .capabilities import reservation
        cap=reservation(self,tenant,self.capability_id)
        job['capability_id']=self.capability_id
        from .storage import reserve,RESULT_RESERVE_BYTES
        storage=reserve(self,tenant,RESULT_RESERVE_BYTES);job['reserved_delivery_bytes']=RESULT_RESERVE_BYTES
        self.store.transact([(job_key,None,job),(active_key,None,{'job_id':job_id}),quota,storage]+([cap] if cap else []))
        self.queue.send(tenant,job_id)
        return {'job_id':job_id,'status':'queued'}

    def finish(self, tenant, current, status, delivery_key, error=None):
        if status not in TERMINAL:
            raise ValueError('Terminal status required')
        job_key = key(tenant,'job',current['job_id'])
        if current['status'] in TERMINAL or self.store.get(job_key)!=current:raise Conflict()
        active_key = key(tenant,'active')
        active = self.store.get(active_key)
        from .storage import result_size,settle as settle_storage
        actual=result_size(self.objects,delivery_key) if status=='succeeded' and current.get('reserved_delivery_bytes') else 0
        changes = [(job_key,current,{**current,'status':status,'delivery_key':delivery_key,'delivery_bytes':actual,
                                    'error':error,'finished_at':self.clock()})]
        storage=settle_storage(self,tenant,current,actual)
        if storage:changes.append(storage)
        from .quota import settle
        quota=settle(self.store,current,self.clock())
        if quota:changes.append(quota)
        if active and active['job_id']==current['job_id']:
            changes.append((active_key,active,None))
        self.store.transact(changes)
        if status!='succeeded' and delivery_key:self.objects.delete(delivery_key)


class S3Objects:
    def __init__(self, bucket):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.client = boto3.client('s3',region_name='us-east-1',config=Config(signature_version='s3v4',
            s3={'addressing_style':'virtual','us_east_1_regional_endpoint':'regional'}))

    def descriptor(self):
        return self.read_json('service/descriptor.json')

    def read_json(self, path):
        return json.loads(self.client.get_object(Bucket=self.bucket,Key=path)['Body'].read(6*1024*1024))

    def upload_url(self, path, size):
        return self.client.generate_presigned_url('put_object',Params={'Bucket':self.bucket,'Key':path,'ContentLength':size},ExpiresIn=300)

    def put_json(self,path,value):
        self.client.put_object(Bucket=self.bucket,Key=path,Body=json.dumps(value,allow_nan=False).encode(),ContentType='application/json')

    def size(self,path):
        try:
            return self.client.head_object(Bucket=self.bucket,Key=path)['ContentLength']
        except self.client.exceptions.ClientError:
            return None

    def delete(self,path):
        self.client.delete_object(Bucket=self.bucket,Key=path)

    def delete_prefix(self,prefix):
        if not re.fullmatch(r'users/[a-f0-9]{32}/',prefix):raise ValueError('Invalid deletion scope')
        # Delete a page before requesting the next first page; no skipped
        # continuation tokens when deletion changes the listing.
        while True:
            page=self.client.list_objects_v2(Bucket=self.bucket,Prefix=prefix,MaxKeys=1000)
            objects=[{'Key':row['Key']} for row in page.get('Contents',[])]
            if not objects:return
            result=self.client.delete_objects(Bucket=self.bucket,Delete={'Objects':objects,'Quiet':True})
            if result.get('Errors'):raise RuntimeError('Object deletion incomplete')


class Queue:
    def __init__(self,url):
        import boto3
        self.client, self.url = boto3.client('sqs'),url

    def send(self, tenant, job, *, wake=True):
        self.client.send_message(QueueUrl=self.url,MessageBody=json.dumps({'tenant':tenant,'job_id':job}))
        if wake and os.environ.get('CONTROLLER'):
            import boto3
            boto3.client('lambda').invoke(FunctionName=os.environ['CONTROLLER'],InvocationType='Event',
                Payload=json.dumps({'source':'kiroclops.wake'}).encode())


def handler(event, context):
    try:
        claims = event.get('requestContext',{}).get('authorizer',{}).get('jwt',{}).get('claims',{})
        # API Gateway verifies issuer, audience, signature and expiration first.
        if claims.get('token_use')!='access' or not claims.get('sub'):
            raise ApiError(401,'access_token_required')
        tenant = digest(claims['sub'])[:32]
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw,validate=True).decode()
        if len(raw)>200*1024:
            raise ApiError(413,'request_too_large')
        body = json.loads(raw,parse_constant=lambda v: (_ for _ in ()).throw(ValueError()))
        service = Service(DynamoStore(os.environ['TABLE']),S3Objects(os.environ['BUCKET']),Queue(os.environ['QUEUE']))
        service.require_budget=os.environ.get('REQUIRE_BUDGET')=='1'
        if claims.get('client_id')==os.environ.get('AGENT_CLIENT','unconfigured'):
            from .capabilities import authorize
            service.account(tenant)
            service.capability_id=authorize(service,tenant,event.get('headers',{}).get('x-kiroclops-capability'),
                                            event['requestContext']['http']['method'],event['rawPath'])
        elif claims.get('client_id')!=os.environ.get('HUMAN_CLIENT'):
            raise ApiError(403,'unknown_client')
        result = service.dispatch(tenant,event['requestContext']['http']['method'],event['rawPath'],body)
        return {'statusCode':200,'headers':{'Content-Type':'application/json','Cache-Control':'no-store'},'body':json.dumps(result,allow_nan=False)}
    except ApiError as exc:
        status, code = exc.status, exc.code
    except Conflict:
        status, code = 409, 'concurrent_update_retry'
    except (ValueError,TypeError,KeyError):
        status, code = 400, 'invalid_request'
    except Exception:
        # Never log SDK exceptions: those can embed signed URLs or user data.
        status, code = 503, 'service_unavailable'
    return {'statusCode':status,'headers':{'Content-Type':'application/json','Cache-Control':'no-store'},'body':json.dumps({'error':code})}
