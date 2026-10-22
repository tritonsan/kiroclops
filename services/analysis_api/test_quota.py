import unittest
from services.analysis_api import test_service as fixtures
from services.analysis_api.api import ApiError
from services.analysis_api.store import key,Conflict
from services.analysis_api.quota import usage_key


class QuotaTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call=fixtures.ServiceTests.call
    upload=fixtures.ServiceTests.upload

    def test_duplicate_submission_reserves_once_and_cancel_refunds_once(self):
        self.upload();job=self.call('POST','/jobs',self.body)['job_id']
        self.call('POST','/jobs',self.body)
        self.assertEqual(self.store.get(usage_key('alice',1000))['reserved_seconds'],120)
        self.call('POST','/jobs/'+job+'/cancel');self.call('POST','/jobs/'+job+'/cancel')
        self.assertEqual(self.store.get(usage_key('alice',1000))['reserved_seconds'],0)

    def test_failed_work_is_charged_and_second_finish_cannot_double_refund(self):
        self.upload();job_id=self.call('POST','/jobs',self.body)['job_id'];path=key('alice','job',job_id)
        job=self.store.get(path);running={**job,'status':'running','started_at':995}
        self.store.transact([(path,job,running)])
        self.api.finish('alice',running,'failed',None,{'code':'failure'})
        usage=self.store.get(usage_key('alice',1000))
        self.assertEqual(usage['charged_seconds'],5);self.assertEqual(usage['reserved_seconds'],0)
        with self.assertRaises(Conflict):self.api.finish('alice',running,'failed',None)
        self.assertEqual(self.store.get(usage_key('alice',1000)),usage)

    def test_limit_does_not_create_partial_job_or_active_lock(self):
        self.upload();path=usage_key('alice',1000)
        self.store.transact([(path,None,{'charged_seconds':1790,'reserved_seconds':0,'compute_jobs':1})])
        with self.assertRaises(ApiError) as exc:self.call('POST','/jobs',self.body)
        self.assertEqual(exc.exception.code,'daily_compute_quota')
        self.assertIsNone(self.store.get(key('alice','active')))
        self.assertFalse(any('#job#' in p for p in self.store.rows))
