"""An upstream ACP throttle remains a failed trial with a specific, proven cause."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import cohort
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class PromptThrottleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.session=self.root/'trial';self.stream=self.session/'kiro/run';self.stream.mkdir(parents=True)
        self.sid='sess-throttled';self.prompt_id=7
        self.failure={'code':-32000,'message':'Selected model is busy',
                      'data':{'errorType':'ModelThrottleError','retryErrorType':'THROTTLING',
                              'requestId':'request-123'}}
        self.launch={'status':'acp_request_failed','native_turn_status':'acp_request_failed',
                     'prompt_sent':True,'model':cohort.MODEL,'effort':cohort.EFFORT,
                     'approval_mode':cohort.APPROVAL,'terminal_cleanup':{'status':'pass'},
                     'preflight':{'session_id':self.sid,'selected':{'mode':'kiroclops-local-tools',
                                                                   'model':cohort.MODEL,'effort':cohort.EFFORT}},
                     'stream_configuration':{'selected':{'mode':'kiroclops-local-tools',
                                                         'model':cohort.MODEL,'effort':cohort.EFFORT},
                                             'expected':{'mode':'kiroclops-local-tools',
                                                         'model':cohort.MODEL,'effort':cohort.EFFORT},
                                             'generation_started':False},
                     'error':f'session/prompt: {self.failure}',
                     'postturn_report':{'status':'not_run','reason':'native_turn_incomplete'}}
        self.message={'jsonrpc':'2.0','id':self.prompt_id,'method':'session/prompt',
                      'params':{'sessionId':self.sid,'prompt':[{'type':'text','text':'Transfer jump.'}]}}
        self.outbound=[{'sequence':1,'payload_sha256':digest(self.message),'phase':'attempt',
                        'message':self.message},
                       {'sequence':1,'payload_sha256':digest(self.message),'phase':'sent'}]
        self.inbound=[{'jsonrpc':'2.0','id':self.prompt_id,'error':self.failure}]
        self.write_stream()

    def tearDown(self):self.temp.cleanup()

    def write_stream(self):
        for name,rows in (('outbound.jsonl',self.outbound),('stdout.jsonl',self.inbound)):
            (self.stream/name).write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')

    def test_exact_prompt_receipt_and_acp_error_prove_throttle(self):
        self.assertTrue(cohort._evidenced_prompt_throttle(self.session,self.launch))

    def test_other_or_tampered_transport_evidence_does_not_claim_throttle(self):
        cases=[]
        cases.append(('wrong_error_type',lambda: self.failure['data'].update(errorType='TransportError')))
        cases.append(('wrong_prompt_id',lambda: self.inbound[0].update(id=8)))
        cases.append(('changed_receipt',lambda: self.outbound[1].update(payload_sha256='0'*64)))
        cases.append(('changed_attempt',lambda: self.outbound[0].update(payload_sha256='0'*64)))
        cases.append(('matching_wrong_hashes',lambda: [row.update(payload_sha256='0'*64) for row in self.outbound]))
        cases.append(('generation_started',lambda: self.launch['stream_configuration'].update(generation_started=True)))
        cases.append(('cleanup_unconfirmed',lambda: self.launch['terminal_cleanup'].update(status='unconfirmed')))
        cases.append(('launch_error_mismatch',lambda: self.launch.update(error='session/prompt: unrelated')))
        for name,mutate in cases:
            with self.subTest(name=name):
                saved=(copy.deepcopy(self.failure),copy.deepcopy(self.launch),
                       copy.deepcopy(self.outbound),copy.deepcopy(self.inbound))
                mutate();self.write_stream()
                self.assertFalse(cohort._evidenced_prompt_throttle(self.session,self.launch))
                self.failure,self.launch,self.outbound,self.inbound=saved
                self.write_stream()

    def test_cohort_records_failure_without_native_success_or_retry(self):
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
             patch('kiroclops.local_engine.review.audit_native_run') as audit:
            session.return_value.inspect.return_value={'budget':{'used_candidates':0},'candidates':[]}
            cohort.run_cohort(registration,selected_trial='game-tools-01')
            run.assert_called_once();audit.assert_not_called()
        actual=read_json(self.root/'progress.json')['rows'][0]
        self.assertEqual('failed',actual['status'])
        self.assertEqual('upstream_model_throttle',actual['stages'][0]['error']['code'])
        self.assertEqual('fail',actual['stages'][0]['native_validation_status'])
        self.assertFalse(actual['stages'][0]['numerical_pass'])
        self.assertEqual(0,actual['stages'][0]['candidate_count'])

    def test_unproven_throttle_keeps_generic_native_failure(self):
        registration=self.root/'registration.json';write_json(registration,{'registered':True})
        stage={'stage':'jump','session':str(self.session),'status':'registered'}
        row={'id':'game-tools-01','domain':'game','mode':'tools','repeat':1,
             'status':'registered','stages':[stage]}
        registry={'rows':[row],'sessions':[str(self.session)]}
        write_json(self.root/'progress.json',{'registration_sha256':file_sha256(registration),'rows':[row]})
        write_json(self.session/'session.json',{'status':'new'})
        self.inbound[0]['id']=8;self.write_stream()
        with patch.object(cohort,'verify_registration',return_value=registry), \
             patch.object(cohort,'_resume_cleanup'),patch.object(cohort,'_prepare_stage'), \
             patch.object(cohort,'run',return_value=self.launch) as run, \
             patch.object(cohort,'Session') as session,patch.object(cohort,'summarize',return_value={}), \
             patch('kiroclops.local_engine.review.audit_native_run',
                   side_effect=VisualError('invalid_native_evidence','Unbound native error')) as audit:
            session.return_value.inspect.return_value={'budget':{'used_candidates':0},'candidates':[]}
            cohort.run_cohort(registration,selected_trial='game-tools-01')
            run.assert_called_once();audit.assert_called_once()
        actual=read_json(self.root/'progress.json')['rows'][0]
        self.assertEqual('failed',actual['status'])
        self.assertEqual('invalid_native_evidence',actual['stages'][0]['error']['code'])
        self.assertFalse(actual['stages'][0]['numerical_pass'])


if __name__=='__main__':unittest.main()
