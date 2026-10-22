import os
from pathlib import Path
import tempfile
import unittest

from kiroclops.cloud.credentials import save,load
from kiroclops.visual.schema import VisualError


class CredentialTests(unittest.TestCase):
    def test_logout_after_deletion_or_disconnect_removes_local_tokens(self):
        from kiroclops.cloud.__main__ import logout
        from unittest.mock import Mock
        for code in ('cloud_data_deleted','analysis_connection_lost'):
            with tempfile.TemporaryDirectory() as temp:
                config={k:str(Path(temp)/k) for k in ('human_credential_file','capability_file')}
                for path in config.values():save(path,{'access_token':'test'})
                backend=Mock();backend._call.side_effect=VisualError(code,'not logged')
                value=logout(config,backend)
                self.assertTrue(all(not Path(path).exists() for path in config.values()))
                self.assertEqual(value['status'],'logged_out' if code=='cloud_data_deleted' else 'logged_out_locally')

    def test_round_trip_and_at_rest_protection(self):
        with tempfile.TemporaryDirectory(prefix='credential café ') as temp:
            path=Path(temp)/'credential'
            value={'access_token':'secret-for-test','service_capability':'test-capability','expires_at':123}
            save(path,value);self.assertEqual(load(path),value)
            if os.name=='nt':self.assertNotIn(b'secret-for-test',path.read_bytes())
            else:self.assertEqual(path.stat().st_mode & 0o777,0o600)
            save(path,{**value,'expires_at':456});self.assertEqual(load(path)['expires_at'],456)

    def test_corrupt_file_fails_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'credential';path.write_text('private broken data')
            with self.assertRaises(VisualError) as error:load(path)
            self.assertEqual(error.exception.code,'cloud_login_required')
            self.assertNotIn('private broken',str(error.exception))
