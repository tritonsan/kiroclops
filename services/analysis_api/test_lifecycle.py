import unittest
from services.analysis_api import test_service as fixtures
from services.analysis_api.api import ApiError
from services.analysis_api.store import key


class LifecycleTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call=fixtures.ServiceTests.call
    upload=fixtures.ServiceTests.upload

    def test_delete_revokes_access_purges_only_owner_and_repeats(self):
        self.upload();self.objects.data['users/alice/private']={'private':True}
        self.objects.data['users/bob/private']={'private':True}
        result=self.call('DELETE','/data')
        self.assertEqual(result['status'],'deleted')
        self.assertNotIn('users/alice/private',self.objects.data)
        self.assertIn('users/bob/private',self.objects.data)
        with self.assertRaises(ApiError) as error:self.call('POST','/packages/prepare',self.package)
        self.assertEqual(error.exception.status,410)
        self.assertEqual(self.call('GET','/usage')['storage_bytes'],0)
        self.assertEqual(self.call('DELETE','/data')['status'],'deleted')

    def test_active_worker_is_cancelled_before_physical_cleanup(self):
        self.upload();job_id=self.call('POST','/jobs',self.body)['job_id'];path=key('alice','job',job_id)
        job=self.store.get(path);running={**job,'status':'running','started_at':999,'lease_until':1010}
        self.store.transact([(path,job,running)])
        result=self.call('DELETE','/data')
        self.assertEqual(result['status'],'deleting')
        self.assertTrue(self.store.get(path)['cancel_requested'])
        with self.assertRaises(ApiError):self.call('GET','/jobs/'+job_id)
        self.api.clock=lambda:1011
        self.assertTrue(self.call('DELETE','/data')['waiting_for_worker_disk'])
        self.store.transact([('system#worker#disk',None,{'verified_empty_at':1011})])
        self.assertEqual(self.call('DELETE','/data')['status'],'deleted')

    def test_expired_lease_is_not_proof_of_physical_disk_deletion(self):
        self.upload();job_id=self.call('POST','/jobs',self.body)['job_id'];path=key('alice','job',job_id)
        job=self.store.get(path)
        self.store.transact([(path,job,{**job,'started_at':999,'lease_until':999,'status':'running'})])
        result=self.call('DELETE','/data')
        self.assertEqual(result['status'],'deleting')
        self.assertTrue(result['waiting_for_worker_disk'])
        current=self.store.get(path)
        self.store.transact([(path,current,{**current,'disk_cleaned_at':1000})])
        self.assertEqual(self.call('DELETE','/data')['status'],'deleted')

    def test_failed_physical_cleanup_stays_revoked_and_retryable(self):
        self.upload();original=self.objects.delete_prefix
        self.objects.delete_prefix=lambda p: (_ for _ in ()).throw(OSError('storage unavailable'))
        with self.assertRaises(OSError):self.call('DELETE','/data')
        with self.assertRaises(ApiError):self.call('POST','/jobs',self.body)
        self.objects.delete_prefix=original
        self.assertEqual(self.call('DELETE','/data')['status'],'deleted')
