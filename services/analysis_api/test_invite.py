import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from services.analysis_api.invite import invite
from kiroclops.visual.schema import write_json,read_json


class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        write_json(self.root/'state.json',{'purpose':'invited-beta-standard-opencv','Table':'table','Pool':'pool',
            'ApiUrl':'https://example.invalid','Client':'human','AgentClient':'agent'})
        self.calls=[];self.count=0
    def aws(self,service,operation,**kwargs):
        self.calls.append((service,operation,kwargs))
        if operation=='query':return {'Items':[{}]*self.count}
        if operation=='get-item':return {}
        if operation=='admin-create-user':return {'User':{'Attributes':[{'Name':'sub','Value':'new-subject'}]}}
        return {}
    def test_invitation_reserves_slot_and_account_atomically_without_email(self):
        with patch('services.analysis_api.invite.aws',side_effect=self.aws),patch('getpass.getpass',return_value='TestOnly!9aPassword'):
            result=invite(self.root,'tester',self.root/'service.json')
        create=next(k for s,o,k in self.calls if o=='admin-create-user')
        self.assertEqual(create['message_action'],'SUPPRESS')
        tx=next(k for s,o,k in self.calls if o=='transact-write-items')['transact_items']
        self.assertEqual(len(tx),3)
        self.assertTrue(all('ConditionExpression' in w['Put'] for w in tx))
        self.assertNotIn('TestOnly!9aPassword',(self.root/'service.json').read_text())
        self.assertFalse(result['email_sent'])
        self.assertFalse(read_json(self.root/'service.invitation.json')['external_test_completed'])
    def test_sixth_user_is_rejected_before_account_creation(self):
        self.count=5
        with patch('services.analysis_api.invite.aws',side_effect=self.aws),self.assertRaises(ValueError):
            invite(self.root,'sixth',self.root/'service.json')
        self.assertFalse(any(o=='admin-create-user' for _,o,_ in self.calls))
    def test_internal_probe_cannot_invite_external_users(self):
        value=read_json(self.root/'state.json');value['purpose']='internal-jwt-e2e';write_json(self.root/'state.json',value)
        with patch('services.analysis_api.invite.aws') as aws,self.assertRaises(ValueError):
            invite(self.root,'tester',self.root/'service.json')
        aws.assert_not_called()
