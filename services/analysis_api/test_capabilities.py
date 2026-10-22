import unittest
from unittest.mock import patch
from services.analysis_api import test_service as fixtures
from services.analysis_api.api import ApiError
from services.analysis_api.capabilities import authorize
from services.analysis_api.store import key


class CapabilityTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call=fixtures.ServiceTests.call
    upload=fixtures.ServiceTests.upload

    def test_capability_cannot_delete_data_mint_sessions_or_use_other_scope(self):
        issued=self.call('POST','/sessions')
        for method,path in [('DELETE','/data'),('POST','/sessions'),('POST','/sessions/revoke')]:
            with self.subTest(path=path),self.assertRaises(ApiError):authorize(self.api,'alice',issued['capability'],method,path)
        with self.assertRaises(ApiError):authorize(self.api,'bob',issued['capability'],'GET','/usage')
        with self.assertRaises(ApiError):authorize(self.api,'alice',None,'GET','/usage')

    def test_own_job_only_and_repeat_does_not_consume_session_quota_twice(self):
        issued=self.call('POST','/sessions');self.upload()
        capability=authorize(self.api,'alice',issued['capability'],'POST','/jobs');self.api.capability_id=capability
        job=self.call('POST','/jobs',self.body)['job_id'];self.call('POST','/jobs',self.body)
        self.assertEqual(self.store.get(key('alice','capability',capability))['jobs'],1)
        self.assertEqual(authorize(self.api,'alice',issued['capability'],'GET','/jobs/'+job),capability)
        self.api.capability_id=None;other=self.call('POST','/sessions')
        with self.assertRaises(ApiError):authorize(self.api,'alice',other['capability'],'GET','/jobs/'+job)

    def test_logout_revokes_capability_even_before_jwt_expires(self):
        issued=self.call('POST','/sessions')
        self.call('POST','/sessions/revoke')
        with self.assertRaises(ApiError):authorize(self.api,'alice',issued['capability'],'GET','/usage')
