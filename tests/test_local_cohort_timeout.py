"""A verified 30-minute native generation timeout remains a failed trial."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.kiro import StreamConfigurationGuard
from kiroclops.local_engine import cohort
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class AuthoringTimeoutEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.session=self.root/'trial'
        self.stream=self.session/'kiro/run';self.stream.mkdir(parents=True)
        self.sid='sess-authoring';self.mode='kiroclops-local-tools';self.prompt_id=7
        self.inbound=[
            {'jsonrpc':'2.0','id':2,'result':{'sessionId':self.sid,'currentModeId':self.mode,
                                            'currentModelId':cohort.MODEL,'effortLevel':cohort.EFFORT}},
            {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':self.sid,
                'update':{'sessionUpdate':'agent_thought_chunk',
                          'content':{'type':'text','text':'Working on the code.'}}}},
        ]
        guard=StreamConfigurationGuard(self.mode,model=cohort.MODEL,effort=cohort.EFFORT)
        for event in self.inbound:guard.observe(event)
        selected={'mode':self.mode,'model':cohort.MODEL,'effort':cohort.EFFORT}
        self.launch={'status':'timeout','native_turn_status':'timeout','prompt_sent':True,
                     'model':cohort.MODEL,'effort':cohort.EFFORT,'approval_mode':cohort.APPROVAL,
                     'prompt_started_at':1000.0,'finished_at':2800.2,
                     'terminal_cleanup':{'status':'pass','cleanup_started_at':2800.05},
                     'preflight':{'session_id':self.sid,'selected':selected,
                                  'approval_mode':cohort.APPROVAL,'autopilot':'on','prompt_sent':False},
                     'stream_configuration':guard.summary(),
                     'error':'Kiro reached its bounded request or session deadline.'}
        self.message={'jsonrpc':'2.0','id':self.prompt_id,'method':'session/prompt',
                      'params':{'sessionId':self.sid,'prompt':[{'type':'text','text':'Transfer jump.'}]}}
        self.outbound=[{'sequence':1,'payload_sha256':digest(self.message),'phase':'attempt',
                        'message':self.message},
                       {'sequence':1,'payload_sha256':digest(self.message),'phase':'sent'}]
        self.write_stream()

    def write_stream(self):
        for name,rows in (('outbound.jsonl',self.outbound),('stdout.jsonl',self.inbound)):
            (self.stream/name).write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')

    def test_exact_prompt_receipt_generation_and_deadline_prove_timeout(self):
        self.assertTrue(cohort._evidenced_authoring_timeout(self.session,self.launch))

    def test_missing_or_mutated_evidence_fails_closed(self):
        cases=[
            ('early_finish',lambda: self.launch.update(finished_at=2799.89)),
            ('early_native_end',lambda: self.launch['terminal_cleanup'].update(cleanup_started_at=2799.89)),
            ('bad_time',lambda: self.launch.update(prompt_started_at='1000')),
            ('wrong_status',lambda: self.launch.update(native_turn_status='cancelled')),
            ('wrong_effort',lambda: self.launch.update(effort='high')),
            ('wrong_approval',lambda: self.launch.update(approval_mode='allow_once')),
            ('no_cleanup',lambda: self.launch['terminal_cleanup'].update(status='unconfirmed')),
            ('malformed_cleanup',lambda: self.launch.update(terminal_cleanup=None)),
            ('malformed_stream',lambda: self.launch.update(stream_configuration=None)),
            ('no_prompt_receipt',lambda: self.launch.update(prompt_sent=False)),
            ('has_response',lambda: self.launch.update(response={'stopReason':'end_turn'})),
            ('wrong_preflight',lambda: self.launch['preflight']['selected'].update(model='auto')),
            ('wrong_preflight_approval',lambda: self.launch['preflight'].update(approval_mode='allow_once')),
            ('wrong_preflight_autopilot',lambda: self.launch['preflight'].update(autopilot='off')),
            ('no_verified_generation',lambda: self.launch['stream_configuration'].update(generation_configuration_verified=False)),
            ('wrong_stream',lambda: self.launch['stream_configuration'].update(events_seen=1)),
            ('wrong_journal_hash',lambda: self.outbound[1].update(payload_sha256='0'*64)),
            ('both_wrong_journal_hashes',lambda: [row.update(payload_sha256='0'*64) for row in self.outbound]),
            ('wrong_prompt_session',lambda: self.message['params'].update(sessionId='other')),
            ('prompt_response_exists',lambda: self.inbound.append({'jsonrpc':'2.0','id':self.prompt_id,'result':{'stopReason':'end_turn'}})),
            ('selected_model_changed_during_generation',lambda: self.inbound.append({'jsonrpc':'2.0','method':'session/update',
                'params':{'sessionId':self.sid,'update':{'sessionUpdate':'agent_message_chunk',
                    'modelId':'auto','content':{'type':'text','text':'oops'}}}})),
        ]
        for name,mutate in cases:
            with self.subTest(name=name):
                old=copy.deepcopy((self.launch,self.message,self.outbound,self.inbound))
                mutate();self.write_stream()
                self.assertFalse(cohort._evidenced_authoring_timeout(self.session,self.launch))
                self.launch,self.message,self.outbound,self.inbound=old
                self.write_stream()

    def _run_one(self):
        registration=self.root/'registration.json';write_json(registration,{'registered':True})
        stage={'stage':'jump','session':str(self.session),'status':'registered'}
        row={'id':'game-tools-01','domain':'game','mode':'tools','repeat':1,
             'status':'registered','stages':[stage]}
        registry={'rows':[row],'sessions':[str(self.session)]}
        write_json(self.root/'progress.json',{'registration_sha256':file_sha256(registration),'rows':[row]})
        write_json(self.session/'session.json',{'status':'new'})
        with patch.object(cohort,'verify_registration',return_value=registry), \
             patch.object(cohort,'_resume_cleanup'),patch.object(cohort,'_prepare_stage'), \
             patch.object(cohort,'run',return_value=self.launch) as run, \
             patch.object(cohort,'Session') as session,patch.object(cohort,'summarize',return_value={}), \
             patch('kiroclops.local_engine.review.audit_native_run',
                   side_effect=VisualError('invalid_native_evidence','Incomplete native turn')) as audit:
            session.return_value.inspect.return_value={'budget':{'used_candidates':0},'candidates':[]}
            cohort.run_cohort(registration,selected_trial='game-tools-01')
            run.assert_called_once()
            return read_json(self.root/'progress.json')['rows'][0]['stages'][0],audit.call_count

    def test_verified_timeout_is_failure_without_retry_or_success_score(self):
        stage,audits=self._run_one()
        self.assertEqual(0,audits)
        self.assertEqual('native_authoring_timeout',stage['error']['code'])
        self.assertEqual('failed',stage['status'])
        self.assertEqual('fail',stage['native_validation_status'])
        self.assertFalse(stage['numerical_pass'])
        self.assertEqual(0,stage['candidate_count'])

    def test_unverified_timeout_falls_through_to_native_audit_failure(self):
        self.outbound[1]['payload_sha256']='0'*64;self.write_stream()
        stage,audits=self._run_one()
        self.assertEqual(1,audits)
        self.assertEqual('invalid_native_evidence',stage['error']['code'])
        self.assertFalse(stage['numerical_pass'])


if __name__=='__main__':unittest.main()
