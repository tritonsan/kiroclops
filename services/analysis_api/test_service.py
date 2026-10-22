"""Control-plane invariants independent of the AWS SDK."""
from copy import deepcopy
import unittest

from services.analysis_api.api import Service, ApiError, digest, handler
from services.analysis_api.store import MemoryStore, Conflict, key


class Objects:
    def __init__(self):self.data={};self.sizes={};self.signs=[]
    def descriptor(self):return {'engine_sha256':digest({'opencv':'test'}),'analysis_sources':{'engine.py':'b'*64}}
    def upload_url(self,path,size):self.signs.append(path);self.sizes[path]=size;return 'https://bucket/'+path
    def size(self,path):
        import json
        return self.sizes.get(path,len(json.dumps(self.data[path],allow_nan=False).encode()) if path in self.data else None)
    def read_json(self,path):
        if path=='service/engine.json':return {'identity':{'opencv':'test'},'engine_sha256':digest({'opencv':'test'})}
        return deepcopy(self.data[path])
    def put_json(self,path,value):self.data[path]=deepcopy(value)
    def delete(self,path):self.data.pop(path,None);self.sizes.pop(path,None)
    def delete_prefix(self,prefix):
        for path in list(self.data)+list(self.sizes):
            if path.startswith(prefix):self.delete(path)


class Queue:
    def __init__(self):self.messages=[];self.fail=False
    def send(self,tenant,job):
        if self.fail:raise OSError('lost enqueue acknowledgement')
        self.messages.append((tenant,job))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryStore();self.objects=Objects();self.queue=Queue()
        self.api=Service(self.store,self.objects,self.queue,clock=lambda:1000)
        for user in ('alice','bob'):
            self.store.transact([(key(user,'account'),None,{'enabled':True,'storage_bytes':0})])
        self.package={'package_sha256':'c'*64,'archive_sha256':'d'*64,'bytes':100,'index_for_similarity':False}
        self.body={'schema_version':2,'logical_job_id':'one','engine_sha256':self.objects.descriptor()['engine_sha256'],
                   'analysis_sources_sha256':digest(self.objects.descriptor()['analysis_sources']),
                   'request':{'operation':'analyze','package_sha256':'c'*64,'selection':{},'contract':{},'candidate_sha256':'x'}}

    def call(self,method,path,body=None,user='alice'):
        return self.api.dispatch(user,method,path,body or {})

    def upload(self):
        self.call('POST','/packages/prepare',self.package)
        self.call('POST','/packages/complete',self.package)

    def test_uninvited_identity_and_missing_access_claim_rejected(self):
        with self.assertRaises(ApiError):self.call('GET','/service',user='nobody')
        self.assertEqual(handler({},None)['statusCode'],401)

    def test_duplicate_upload_reserves_bytes_once_and_complete_is_not_verified(self):
        self.upload();self.call('POST','/packages/prepare',self.package)
        self.assertEqual(self.store.get(key('alice','account'))['storage_bytes'],100)
        self.assertFalse(self.call('POST','/packages/complete',self.package)['verified'])

    def test_lost_enqueue_resumes_same_job_and_one_active_reservation(self):
        self.upload();self.queue.fail=True
        with self.assertRaises(OSError):self.call('POST','/jobs',self.body)
        self.queue.fail=False
        first=self.call('POST','/jobs',self.body);second=self.call('POST','/jobs',self.body)
        self.assertEqual(first,second)
        self.assertEqual(len([k for k in self.store.rows if '#job#' in k]),1)
        other={**self.body,'logical_job_id':'two'}
        with self.assertRaises(ApiError) as error:self.call('POST','/jobs',other)
        self.assertEqual(error.exception.status,429)

    def test_idempotency_does_not_rebind_changed_candidate(self):
        self.upload();self.call('POST','/jobs',self.body)
        other=deepcopy(self.body);other['request']['candidate_sha256']='changed'
        with self.assertRaises(ApiError) as error:self.call('POST','/jobs',other)
        self.assertEqual(error.exception.code,'idempotency_conflict')

    def test_cross_user_job_and_package_existence_are_private(self):
        self.upload();job=self.call('POST','/jobs',self.body)['job_id']
        with self.assertRaises(ApiError) as error:self.call('GET','/jobs/'+job,user='bob')
        self.assertEqual(error.exception.status,404)
        with self.assertRaises(ApiError):self.call('POST','/packages/complete',self.package,user='bob')

    def test_cancel_queued_releases_only_its_lock(self):
        self.upload();job=self.call('POST','/jobs',self.body)['job_id']
        self.call('POST','/jobs/'+job+'/cancel')
        self.assertIsNone(self.store.get(key('alice','active')))
        self.assertEqual(self.call('GET','/jobs/'+job)['status'],'cancelled')
        next_job=self.call('POST','/jobs',{**self.body,'logical_job_id':'two'})['job_id']
        self.call('POST','/jobs/'+job+'/cancel')
        self.assertEqual(self.store.get(key('alice','active'))['job_id'],next_job)

    def test_worker_completion_loses_to_cancel_compare_and_swap(self):
        self.upload();job_id=self.call('POST','/jobs',self.body)['job_id']
        before=self.store.get(key('alice','job',job_id))
        self.call('POST','/jobs/'+job_id+'/cancel')
        with self.assertRaises(Conflict):self.api.finish('alice',before,'succeeded','result')
        self.assertEqual(self.call('GET','/jobs/'+job_id)['status'],'cancelled')

    def test_changed_engine_and_expired_package_cannot_run(self):
        self.upload();body={**self.body,'engine_sha256':'f'*64}
        with self.assertRaises(ApiError):self.call('POST','/jobs',body)
        self.api.clock=lambda:1000+31*86400
        with self.assertRaises(ApiError):self.call('POST','/jobs',self.body)

    def test_only_worker_verified_content_skips_upload(self):
        self.upload();pk=key('alice','package','c'*64);row=self.store.get(pk)
        self.store.transact([(pk,row,{**row,'status':'verified','object_key':'immutable'})])
        count=len(self.objects.signs)
        self.assertTrue(self.call('POST','/packages/prepare',self.package)['verified'])
        self.assertEqual(count,len(self.objects.signs))

    def test_quota_and_untrusted_indexing_are_rejected(self):
        with self.assertRaises(ApiError):self.call('POST','/packages/prepare',{**self.package,'index_for_similarity':True})
        account=self.store.get(key('alice','account'))
        self.store.transact([(key('alice','account'),account,{**account,'storage_bytes':2*1024**3})])
        with self.assertRaises(ApiError) as error:self.call('POST','/packages/prepare',self.package)
        self.assertEqual(error.exception.status,413)


if __name__=='__main__':unittest.main()
