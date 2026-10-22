from copy import deepcopy
import unittest

from services.analysis_api.api import ApiError,digest
from services.analysis_api.store import key
from services.analysis_api import test_service as fixtures
from kiroclops.cloud.results import envelope,make_measurement
from kiroclops.visual.schema import VisualError


class ExactCacheTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call=fixtures.ServiceTests.call
    upload=fixtures.ServiceTests.upload
    def seed(self):
        self.upload()
        job_id=self.call('POST','/jobs',self.body)['job_id']
        job=self.store.get(key('alice','job',job_id))
        package=self.store.get(key('alice','package','c'*64))
        self.store.transact([(key('alice','package','c'*64),package,{**package,'status':'verified','object_key':'verified'})])
        measurement=make_measurement({'package_sha256':'c'*64,'status':'ok','objects':[]},{'events':[]})
        delivery=envelope(measurement,job['request'],logical_job_id=job['logical_job_id'],
            engine=self.objects.read_json('service/engine.json'),sources=job['analysis_sources'],timings={'worker_wall_seconds':5})
        self.objects.data['result']=delivery
        self.api.finish('alice',job,'succeeded','result')
        self.call('GET','/jobs/'+job_id)
        return job_id,delivery

    def test_same_measurement_new_candidate_receipt_no_queue_no_acceptance(self):
        source,original=self.seed();count=len(self.queue.messages)
        body=deepcopy(self.body);body['logical_job_id']='two';body['request']['candidate_sha256']='new-candidate'
        reply=self.call('POST','/jobs',body)
        self.assertEqual(reply['status'],'succeeded');self.assertEqual(count,len(self.queue.messages))
        result=self.call('GET','/jobs/'+reply['job_id'])['delivery']
        self.assertEqual(result['measurement_sha256'],original['measurement_sha256'])
        self.assertNotEqual(result['receipt_sha256'],original['receipt_sha256'])
        self.assertEqual(result['receipt']['candidate_sha256'],'new-candidate')
        self.assertEqual(result['receipt']['origin']['kind'],'exact_reuse')
        self.assertIsNone(result['advice']);self.assertFalse(result['indexed_for_similarity'])
        self.assertNotIn('acceptance',result)

    def test_changed_contract_and_selection_miss(self):
        self.seed()
        for field in ('contract','selection'):
            with self.subTest(field=field):
                body=deepcopy(self.body);body['logical_job_id']=field;body['request'][field]={'changed':True}
                reply=self.call('POST','/jobs',body)
                self.assertEqual(reply['status'],'queued')
                self.call('POST','/jobs/'+reply['job_id']+'/cancel')

    def test_deleted_or_expired_package_or_source_never_reuses(self):
        source,_=self.seed()
        package_key=key('alice','package','c'*64);package=self.store.get(package_key)
        self.store.transact([(package_key,package,{**package,'status':'deleted'})])
        with self.assertRaises(ApiError):self.call('POST','/jobs',{**self.body,'logical_job_id':'two'})
        removed=self.store.get(package_key);self.store.transact([(package_key,removed,package)])
        source_key=key('alice','job',source);job=self.store.get(source_key)
        self.store.transact([(source_key,job,{**job,'status':'deleted'})])
        self.assertEqual(self.call('POST','/jobs',{**self.body,'logical_job_id':'three'})['status'],'queued')

    def test_missing_engine_receipt_or_tampered_measurement_is_not_used(self):
        self.seed();self.objects.data['result']['measurement']['measurement_status']='changed'
        with self.assertRaises(VisualError):self.call('POST','/jobs',{**self.body,'logical_job_id':'two'})

    def test_another_tenant_cannot_hit_same_input(self):
        self.seed()
        with self.assertRaises(ApiError):self.call('POST','/jobs',{**self.body,'logical_job_id':'two'},user='bob')

    def test_explicit_reupload_does_not_resurrect_deleted_cache_generation(self):
        source,delivery=self.seed()
        self.call('POST','/jobs/'+source+'/ack',{'measurement_sha256':delivery['measurement_sha256'],'receipt_sha256':delivery['receipt_sha256']})
        self.assertEqual(self.store.get(key('alice','package','c'*64))['status'],'deleted')
        self.upload()
        package_key=key('alice','package','c'*64);package=self.store.get(package_key)
        self.assertEqual(package['generation'],2)
        # Even a verified fresh upload cannot make the old cache entry valid.
        self.store.transact([(package_key,package,{**package,'status':'verified'})])
        result=self.call('POST','/jobs',{**self.body,'logical_job_id':'new-upload'})
        self.assertEqual(result['status'],'queued')

    def test_acknowledgement_during_other_job_is_saved_for_cleanup(self):
        source,delivery=self.seed()
        changed=deepcopy(self.body);changed['logical_job_id']='active';changed['request']['contract']={'changed':True}
        self.call('POST','/jobs',changed)
        result=self.call('POST','/jobs/'+source+'/ack',{'measurement_sha256':delivery['measurement_sha256'],'receipt_sha256':delivery['receipt_sha256']})
        self.assertEqual(result['candidate_cleanup'],'deferred_until_active_job_finishes')
        package=self.store.get(key('alice','package','c'*64))
        self.assertTrue(package['retire_when_idle'])
        self.assertEqual(package['status'],'verified')


if __name__=='__main__':unittest.main()
