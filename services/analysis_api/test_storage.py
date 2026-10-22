import unittest
from services.analysis_api import test_service as fixtures
from services.analysis_api import test_cache as cache_fixtures
from services.analysis_api.api import ApiError
from services.analysis_api.store import key
from services.analysis_api.storage import RESULT_RESERVE_BYTES,LIMIT_BYTES


class StorageTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call=fixtures.ServiceTests.call
    upload=fixtures.ServiceTests.upload
    seed=cache_fixtures.ExactCacheTests.seed
    def test_result_reservation_is_refunded_once_on_cancel(self):
        self.upload();before=self.api.account('alice')['storage_bytes']
        job=self.call('POST','/jobs',self.body)['job_id']
        self.assertEqual(self.api.account('alice')['storage_bytes'],before+RESULT_RESERVE_BYTES)
        self.call('POST','/jobs/'+job+'/cancel');self.call('POST','/jobs/'+job+'/cancel')
        self.assertEqual(self.api.account('alice')['storage_bytes'],before)
    def test_cache_results_count_toward_storage_without_compute(self):
        source,_=self.seed();before=self.api.account('alice')['storage_bytes']
        result=self.call('POST','/jobs',{**self.body,'logical_job_id':'cache'})
        self.assertEqual(result['status'],'succeeded')
        job=self.store.get(key('alice','job',result['job_id']))
        self.assertEqual(self.api.account('alice')['storage_bytes'],before+job['delivery_bytes'])
        self.assertGreater(job['delivery_bytes'],0)
    def test_full_storage_blocks_compute_and_cache_admission(self):
        self.seed();path=key('alice','account');old=self.store.get(path)
        self.store.transact([(path,old,{**old,'storage_bytes':LIMIT_BYTES})])
        for body in ({**self.body,'logical_job_id':'reuse'},
                     {**self.body,'logical_job_id':'compute','request':{**self.body['request'],'contract':{'change':True}}}):
            with self.assertRaises(ApiError) as exc:self.call('POST','/jobs',body)
            self.assertEqual(exc.exception.code,'storage_quota_exceeded')
        self.assertIsNone(self.store.get(key('alice','active')))
