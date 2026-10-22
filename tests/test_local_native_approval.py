"""No native process: strict approval replies and complete journal replay fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import kiro as native
from kiroclops.local_engine import launcher
from kiroclops.local_engine.review import audit_native_run
from kiroclops.visual.schema import VisualError, digest, write_json


ALWAYS={'kind':'allow_always','optionId':'always'}
ONCE={'kind':'allow_once','optionId':'once'}
CONSENT={'kiro':{'consent':{'scope':'session'}}}


class ReplyTests(unittest.TestCase):
    def client(self):
        client=object.__new__(launcher.LocalACPClient)
        client.config={'session_dir':'unused'}
        client.tool_inputs={'call-1':{'path':'index.html'}}
        client.tool_identifiers={'call-1':'fs_read'}
        client.sent=[];client._send=client.sent.append
        return client

    def event(self,options):
        return {'jsonrpc':'2.0','id':17,'method':'session/request_permission','params':{
            'toolCall':{'toolCallId':'call-1'},'_meta':{'kiro':{'toolId':'fs_read'}},'options':options}}

    def test_always_selected_even_when_once_is_first(self):
        client=self.client()
        with patch.object(launcher,'guard',return_value={'allow':True}):
            client._permission_reply(self.event([ONCE,ALWAYS]))
        self.assertEqual([{'jsonrpc':'2.0','id':17,'result':{
            'outcome':{'outcome':'selected','optionId':'always'},'_meta':CONSENT}}],client.sent)

    def test_only_once_cancels_then_reports_unavailable(self):
        client=self.client()
        with patch.object(launcher,'guard',return_value={'allow':True}):
            with self.assertRaises(native.KiroError) as caught:client._permission_reply(self.event([ONCE]))
        self.assertEqual('native_approval_unavailable',caught.exception.code)
        self.assertEqual({'outcome':{'outcome':'cancelled'}},client.sent[0]['result'])

    def test_empty_or_malformed_options_never_fallback(self):
        for options in ([],None,[None],{'kind':'allow_always'},[{'kind':'allow_always'}],[{'kind':'allow_always','optionId':''}]):
            with self.subTest(options=options):
                client=self.client()
                with patch.object(launcher,'guard',return_value={'allow':True}):
                    with self.assertRaises(native.KiroError) as caught:client._permission_reply(self.event(options))
                self.assertEqual('native_approval_unavailable',caught.exception.code)
                self.assertEqual('cancelled',client.sent[0]['result']['outcome']['outcome'])

    def test_scope_denial_remains_denial_with_or_without_always(self):
        for options in ([ALWAYS],[ONCE]):
            client=self.client()
            with patch.object(launcher,'guard',return_value={'allow':False}):
                with self.assertRaises(native.KiroError) as caught:client._permission_reply(self.event(options))
            self.assertEqual('tool_denied',caught.exception.code)
            self.assertEqual({'outcome':{'outcome':'cancelled'}},client.sent[0]['result'])

    def test_mismatched_native_tool_identity_cannot_use_always(self):
        client=self.client();client.tool_identifiers['call-1']='fs_write'
        with patch.object(launcher,'guard') as guard:
            with self.assertRaises(native.KiroError) as caught:client._permission_reply(self.event([ALWAYS]))
        guard.assert_not_called();self.assertEqual('tool_denied',caught.exception.code)


class NativeReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name);self.target=self.directory/'target';self.target.mkdir()
        write_json(self.directory/'session.json',{'schema_version':1,'experiment':{},
            'budget':{'max_candidates':3,'max_seconds':1800,'max_validation_seconds':1800}})
        self.prompt=self.directory/'prompt.md';self.prompt.write_text('Synthetic test task.',encoding='utf-8')
        self.config={'agent_name':'fixture-agent','target_dir':str(self.target),'prompt_path':str(self.prompt)}
        self.options=[ONCE,ALWAYS]
        self.reply={'outcome':{'outcome':'selected','optionId':'always'},'_meta':deepcopy(CONSENT)}

    def replay(self):
        new={'jsonrpc':'2.0','id':1,'method':'session/new','params':{'cwd':str(self.target),'mcpServers':[]}}
        prompt={'jsonrpc':'2.0','id':2,'method':'session/prompt','params':{
            'sessionId':'session-fixture','prompt':[{'type':'text','text':self.prompt.read_text('utf-8')}]}}
        permission={'jsonrpc':'2.0','id':'permission-1','method':'session/request_permission',
            'params':{'options':self.options}}
        inbound=[{'jsonrpc':'2.0','id':1,'result':{'sessionId':'session-fixture'}},
            {'jsonrpc':'2.0','method':'session/update','params':{'update':{
                'sessionUpdate':'config_option_update','configOptions':[
                    {'id':'mode','currentValue':'fixture-agent'}, {'id':'model','currentValue':launcher.MODEL},
                    {'id':'effortLevel','currentValue':launcher.EFFORT},{'id':'autopilot','currentValue':'on'}]}}},
            {'jsonrpc':'2.0','method':'session/update','params':{'update':{
                'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Synthetic fixture.'}}}},
            permission,{'jsonrpc':'2.0','id':2,'result':{'stopReason':'end_turn'}}]
        messages=[new,prompt,{'jsonrpc':'2.0','id':'permission-1','result':self.reply}]
        outbound=[]
        for index,message in enumerate(messages):
            stamp=datetime.fromtimestamp(1001+index,timezone.utc).isoformat()
            common={'sequence':index+1,'payload_sha256':digest(message),'at':stamp}
            outbound.extend([{**common,'phase':'attempt','message':message},{**common,'phase':'sent'}])
        guard=native.StreamConfigurationGuard('fixture-agent',model=launcher.MODEL,effort=launcher.EFFORT)
        for event in inbound:guard.observe(event)
        launch={'status':'completed','prompt_sent':True,'model':launcher.MODEL,'effort':launcher.EFFORT,
            'approval_mode':launcher.APPROVAL,'started_at':1000,'finished_at':1010,
            'prompt_started_at':1002,'response_received_at':1009,'preflight':{'session_id':'session-fixture'},
            'response':{'stopReason':'end_turn'},'stream_configuration':guard.summary()}
        write_json(self.directory/'kiro/config.json',self.config);write_json(self.directory/'kiro/launch.json',launch)
        run=self.directory/'kiro/run';run.mkdir(exist_ok=True)
        for name,values in (('stdout.jsonl',inbound),('outbound.jsonl',outbound)):
            (run/name).write_text(''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in values),encoding='utf-8')
        (self.directory/'kiro/tool-audit.jsonl').write_text('',encoding='utf-8')
        with patch.object(launcher,'load_config',return_value=self.config):
            return audit_native_run(self.directory)

    def test_full_native_fixture_accepts_always_session_consent(self):
        self.assertEqual('pass',self.replay()['status'])

    def test_once_only_selection_rejected_even_with_forged_session_consent(self):
        self.options=[ONCE];self.reply['outcome']['optionId']='once'
        with self.assertRaises(VisualError) as caught:self.replay()
        self.assertEqual('native_approval_unavailable',caught.exception.code)

    def test_once_selection_rejected_when_always_was_available(self):
        self.reply['outcome']['optionId']='once'
        with self.assertRaisesRegex(VisualError,'Always Allow was not selected'):self.replay()

    def test_missing_or_non_session_consent_rejected(self):
        for consent in (None,{'kiro':{'consent':{'scope':'global'}}}):
            with self.subTest(consent=consent):
                self.reply.pop('_meta',None)
                if consent is not None:self.reply['_meta']=consent
                with self.assertRaisesRegex(VisualError,'consent scope'):self.replay()

    def test_cancelled_reply_cannot_hide_unavailable_always(self):
        self.options=[ONCE];self.reply={'outcome':{'outcome':'cancelled'}}
        with self.assertRaises(VisualError) as caught:self.replay()
        self.assertEqual('native_approval_unavailable',caught.exception.code)

    def test_denied_tool_cancel_is_not_relabelled_as_approval(self):
        self.reply={'outcome':{'outcome':'cancelled'}}
        self.assertEqual('pass',self.replay()['status'])


if __name__=='__main__':unittest.main()
