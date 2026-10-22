"""Native transport accounting, using only synthetic temporary receipts."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import kiro as native
from kiroclops.local_engine import review,launcher
from kiroclops.local_engine.execution_policy import budget_from_policy, resolve_policy
from kiroclops.visual.schema import VisualError,digest,write_json


def housekeeping(sid):
    zero={'tokens':0,'percent':0}
    return {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{
        'sessionUpdate':'session_info_update','_meta':{'kiro':{'kind':'context_usage',
        'usagePercentage':1.5,'contextUsage':{'usagePercentage':1.5},'breakdown':{
        'contextFiles':{**zero,'items':[]},'sessionFiles':{**zero,'items':[]},
        'kiroResponses':dict(zero),'yourPrompts':dict(zero),
        'tools':{**zero,'mcp':dict(zero),'builtin':dict(zero)}}}}}}}


class NativePrefixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-prefix-unit-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'kiro/run').mkdir(parents=True)
        self.sid='synthetic-session';self.agent='synthetic-workspace-agent'
        self.config={'agent_name':self.agent,'target_dir':str(self.root/'target'),'prompt_path':str(self.root/'prompt.txt')}
        (self.root/'target').mkdir();(self.root/'prompt.txt').write_text('Synthetic registered task',encoding='utf-8')
        self.response={'jsonrpc':'2.0','id':3,'result':{'stopReason':'end_turn'}}
        self.prefix=[{'jsonrpc':'2.0','id':2,'result':{'sessionId':self.sid,
            'currentModeId':self.agent,'currentModelId':launcher.MODEL,'effortLevel':launcher.EFFORT,
            'configOptions':[{'id':'autopilot','currentValue':'on'}]}},
            {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':self.sid,'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Synthetic reply'}}}},
            self.response]
        guard=native.StreamConfigurationGuard(self.agent,model=launcher.MODEL,effort=launcher.EFFORT)
        for event in self.prefix:guard.observe(event)
        self.launch={'status':'completed','prompt_sent':True,'started_at':10,'prompt_started_at':20,
            'response_received_at':30,'finished_at':40,'model':launcher.MODEL,'effort':launcher.EFFORT,
            'approval_mode':launcher.APPROVAL,'preflight':{'session_id':self.sid},
            'response':self.response['result'],'stream_configuration':guard.summary()}
        # This synthetic legacy Session declares the actual frozen budget
        # consumed by audit_native_run; missing state is not a default policy.
        policy=resolve_policy('phase3d-execution-v1')
        self.state={'kind':'LocalEngineSession','schema_version':1,
            'budget':budget_from_policy(policy),'experiment':{'execution_policy':policy,'stage':None},
            'clock_origin':'prompt_started','started_at':20.,'deadline_at':1820.}
        self.lines=self.prefix+[housekeeping(self.sid)]
        requests=[{'jsonrpc':'2.0','id':2,'method':'session/new','params':{'cwd':str(self.root/'target')}},
            {'jsonrpc':'2.0','id':3,'method':'session/prompt','params':{'sessionId':self.sid,'prompt':[{'type':'text','text':'Synthetic registered task'}]}}]
        self.outbound=[]
        for i,request in enumerate(requests,1):
            stamp=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
            self.outbound.extend([{'phase':'attempt','sequence':i,'payload_sha256':digest(request),'at':stamp(i*10+1),'message':request},
                {'phase':'sent','sequence':i,'payload_sha256':digest(request),'at':stamp(i*10+2)}])
        self.save()

    def save(self):
        write_json(self.root/'session.json',self.state)
        write_json(self.root/'kiro/launch.json',self.launch);write_json(self.root/'kiro/config.json',self.config)
        for name,rows in (('stdout.jsonl',self.lines),('outbound.jsonl',self.outbound)):
            (self.root/'kiro/run'/name).write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        (self.root/'kiro/tool-audit.jsonl').write_text('',encoding='utf-8')

    def replay(self):return review._replay_consumed_native_stream(self.lines,self.launch,self.config,self.response)

    def audit(self):
        self.save()
        with patch.object(launcher,'load_config',return_value=self.config):return review.audit_native_run(self.root)

    def test_complete_audit_binds_prefix_to_registered_prompt_and_checks_tail(self):
        result=self.audit();self.assertEqual(result['status'],'pass')
        self.assertEqual(result['stream_replay']['consumed_events'],3)
        self.assertEqual(result['stream_replay']['recorded_events'],4)
        self.assertEqual(result['stream_replay']['housekeeping_tail_events'],1)
        self.assertIn('does not establish',result['stream_replay']['scope'])

    def test_invalid_tool_audit_row_reports_exact_line_and_fails_closed(self):
        self.save()
        (self.root/'kiro/tool-audit.jsonl').write_text('{}\n}\n',encoding='utf-8')
        with patch.object(launcher,'load_config',return_value=self.config),self.assertRaises(VisualError) as caught:
            review.audit_native_run(self.root)
        self.assertEqual(caught.exception.code,'invalid_native_evidence')
        self.assertIn('line 2',str(caught.exception))

    def test_no_buffered_tail_remains_compatible(self):
        self.lines=self.prefix
        self.assertEqual(self.audit()['stream_replay']['housekeeping_tail_events'],0)

    def test_multiple_strict_housekeeping_messages_are_each_accounted_for(self):
        self.lines.extend([housekeeping(self.sid),housekeeping(self.sid)])
        result=self.replay();self.assertEqual(result['recorded_events'],6)
        self.assertEqual(result['housekeeping_tail_events'],3)

    def test_live_prefix_count_is_required_not_replaced_with_whole_log_count(self):
        for count in (None,True,0,2,4,5):
            self.launch['stream_configuration']['events_seen']=count
            with self.subTest(count=count),self.assertRaises(VisualError):self.replay()

    def test_every_saved_summary_field_must_match(self):
        original=deepcopy(self.launch)
        for field,value in (('selected',{}),('expected',{}),('generation_started',False),('generation_configuration_verified',False),('observed_models',[])):
            self.launch=deepcopy(original);self.launch['stream_configuration'][field]=value
            with self.subTest(field=field),self.assertRaises(VisualError):self.replay()

    def test_prefix_selected_model_change_rejected(self):
        self.lines[1]['params']['update']['modelId']='different-model'
        with self.assertRaises(VisualError):self.replay()

    def test_historical_stream_keeps_registered_model_and_rejects_switch(self):
        self.config.update(model='gpt-5.6-sol', effort='max', approval_mode=launcher.APPROVAL)
        self.launch.update(model='gpt-5.6-sol', effort='max')
        self.prefix[0]['result']['currentModelId']='gpt-5.6-sol'
        guard=native.StreamConfigurationGuard(self.agent, model='gpt-5.6-sol', effort='max')
        for event in self.prefix:guard.observe(event)
        self.launch['stream_configuration']=guard.summary()
        self.assertEqual('pass', self.audit()['status'])
        self.assertEqual('gpt-5.6-sol', self.audit()['model'])
        self.lines[1]['params']['update']['modelId']=launcher.MODEL
        with self.assertRaises(VisualError):self.replay()

    def test_launch_model_must_match_prepared_historical_configuration(self):
        self.config.update(model='gpt-5.6-sol', effort='max', approval_mode=launcher.APPROVAL)
        with self.assertRaisesRegex(VisualError, 'exact model'):
            self.audit()

    def test_prefix_autopilot_off_rejected(self):
        self.lines[0]['result']['configOptions'][0]['currentValue']='off'
        with self.assertRaises(VisualError):self.replay()

    def test_post_response_generation_and_tools_rejected(self):
        for name in ('agent_message_chunk','agent_thought_chunk','tool_call','tool_call_update'):
            self.lines=self.prefix+[{'jsonrpc':'2.0','method':'session/update','params':{'sessionId':self.sid,'update':{'sessionUpdate':name}}}]
            with self.subTest(name=name),self.assertRaises(VisualError):self.replay()

    def test_post_response_config_update_rejected_even_if_same_value(self):
        self.lines=self.prefix+[{'jsonrpc':'2.0','method':'session/update','params':{'sessionId':self.sid,'update':{'sessionUpdate':'config_option_update','currentModelId':launcher.MODEL}}}]
        with self.assertRaises(VisualError):self.replay()

    def test_nested_config_or_unknown_field_not_hidden_in_housekeeping(self):
        for key in ('modelId','arbitrary'):
            tail=housekeeping(self.sid);tail['params']['update']['_meta']['kiro'][key]=launcher.MODEL
            self.lines=self.prefix+[tail]
            with self.subTest(key=key),self.assertRaises(VisualError):self.replay()

    def test_housekeeping_must_belong_to_same_session(self):
        self.lines=self.prefix+[housekeeping('another-session')]
        with self.assertRaises(VisualError):self.replay()

    def test_housekeeping_values_are_finite_numeric_metadata_only(self):
        for value in (float('nan'),float('inf'),-1,101,True,'tool request'):
            tail=housekeeping(self.sid);tail['params']['update']['_meta']['kiro']['usagePercentage']=value
            self.lines=self.prefix+[tail]
            with self.subTest(value=value),self.assertRaises(VisualError):self.replay()
        tail=housekeeping(self.sid);tail['params']['update']['_meta']['kiro']['breakdown']['contextFiles']['items']=[{'tool':'read'}]
        self.lines=self.prefix+[tail]
        with self.assertRaises(VisualError):self.replay()

    def test_duplicate_prompt_response_rejected(self):
        self.lines.append(deepcopy(self.response))
        with self.assertRaises(VisualError):self.audit()

    def test_prefix_response_result_must_equal_saved_completion(self):
        self.launch['response']={'stopReason':'cancelled'}
        with self.assertRaises(VisualError):self.audit()

    def test_outbound_prompt_identity_still_bound(self):
        message=self.outbound[-2]['message'];message['id']=8
        self.outbound[-2]['payload_sha256']=self.outbound[-1]['payload_sha256']=digest(message)
        with self.assertRaises(VisualError):self.audit()

    def test_outbound_actual_prompt_cannot_be_rewritten_even_with_new_hashes(self):
        message=self.outbound[-2]['message'];message['params']['prompt'][0]['text']='Different task'
        self.outbound[-2]['payload_sha256']=self.outbound[-1]['payload_sha256']=digest(message)
        with self.assertRaises(VisualError):self.audit()

    def test_tail_observed_semantic_change_rejected_independently_of_shape(self):
        base=native.StreamConfigurationGuard
        class ChangedGuard(base):
            def observe(self,event):
                super().observe(event)
                if self.events_seen>3:self.observed_models.add('unregistered-tail-model')
        with patch.object(native,'StreamConfigurationGuard',ChangedGuard):
            with self.assertRaisesRegex(VisualError,'semantic stream'):self.replay()


if __name__=='__main__':unittest.main()
