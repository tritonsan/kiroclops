"""Focused diagnostic negatives; no fixture claims a real model or inspection."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json
from scripts import phase3d_v2_ground_source_diagnostic as diagnostic


def event(identity, kind, args, *, sid='sess_test', title='Write File', terminal=None):
    update = {'sessionUpdate': kind, 'toolCallId': identity, 'title': title,
              'status': 'completed' if terminal is not None else 'in_progress', 'rawInput': args}
    if terminal is not None:
        update.update(terminal)
    return {'params': {'sessionId': sid, 'update': update}}


class GroundDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc ground review café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline, self.selected, self.target = (self.root / name for name in ('baseline', 'selected', 'target'))
        for path in (self.baseline, self.selected, self.target):
            (path / 'src').mkdir(parents=True)
        self.before = 'extends CharacterBody2D\nvar speed = 1\n'
        self.after = 'extends CharacterBody2D\nvar speed = 2\n'
        # Bind the payload's LF bytes exactly.  Windows text-mode writes would
        # add CRLF and intentionally fail the byte-exact native diff replay.
        (self.baseline / 'src/player.gd').write_bytes(self.before.encode('utf-8'))
        (self.selected / 'src/player.gd').write_bytes(self.after.encode('utf-8'))
        self.old_hashes = {'src/player.gd': file_sha256(self.baseline / 'src/player.gd')}
        self.new_hashes = {'src/player.gd': file_sha256(self.selected / 'src/player.gd')}

    def stream(self):
        args = {'path': str(self.target / 'src/player.gd'), 'text': self.after}
        terminal = {'content': [{'type': 'diff', 'oldText': self.before, 'newText': self.after}],
                    'rawOutput': {'message': 'observed successful fixture write'}}
        return [event('write', 'tool_call', args), event('write', 'tool_call_update', args, terminal=terminal),
                event('evaluate', 'tool_call', {}, title='@kiroclops/evaluate_candidate'),
                event('evaluate', 'tool_call_update', {}, title='@kiroclops/evaluate_candidate',
                      terminal={'rawOutput': {'content': [{'type': 'text', 'text': '{"job_id":"job-selected"}'}]}})]

    def replay(self, calls, **kwargs):
        # Existing native envelope tests own the exact snapshot/checkpoint URI
        # semantics.  These fixtures exercise text coverage and candidate/job
        # separation; no real native PASS is created by this stub.
        with patch.object(diagnostic.review, '_builtin_227_edit_terminal', return_value=True):
            return diagnostic.replay_selected_text(calls, checkpoint_id='evaluate', evaluation_job_id='job-selected',
                target=self.target, baseline=self.baseline, selected=self.selected,
                baseline_hashes=self.old_hashes, selected_hashes=self.new_hashes, session_id='sess_test', **kwargs)

    def test_actual_input_manifest_preserves_repeated_native_input_sets(self):
        calls = diagnostic.observed_calls(self.stream(), 'sess_test')
        self.assertEqual(calls['write']['input_set_sha256'], digest(calls['write']['raw_inputs']))
        self.assertEqual(calls['write']['lines'], [1, 2])
        self.assertEqual(calls['evaluate']['status'], 'completed')

    def test_wrong_native_session_and_duplicate_start_are_rejected(self):
        for stream in (self.stream() + [event('another', 'tool_call', {}, sid='other')],
                       self.stream() + [self.stream()[0]]):
            with self.subTest(stream=stream[-1]), self.assertRaises(VisualError):
                diagnostic.observed_calls(stream, 'sess_test')

    def test_selected_checkpoint_job_identity_cannot_be_substituted(self):
        stream = self.stream()
        stream[-1]['params']['update']['rawOutput'] = {'job_id': 'other-job'}
        calls = diagnostic.observed_calls(stream, 'sess_test')
        with self.assertRaises(VisualError) as caught:
            self.replay(calls)
        self.assertEqual(caught.exception.code, 'wrong_authorship_checkpoint')

    def test_observed_write_replay_matches_selected_without_native_acceptance(self):
        value = self.replay(diagnostic.observed_calls(self.stream(), 'sess_test'))
        self.assertEqual(value['status'], 'pass')
        self.assertEqual(value['matches'], {'src/player.gd': True})
        self.assertFalse(value['native_policy_acceptance_granted'])
        self.assertFalse(value['unsupported_cli_version_aliased'])

    def test_wrong_prior_diff_cannot_bind_to_baseline(self):
        stream = self.stream()
        stream[1]['params']['update']['content'][0]['oldText'] = 'different baseline'
        value = self.replay(diagnostic.observed_calls(stream, 'sess_test'))
        self.assertEqual(value['status'], 'fail')
        self.assertIn('native_edit_prior_text_mismatch', {error['code'] for error in value['errors']})

    def test_edit_completed_after_evaluation_does_not_author_selected_snapshot(self):
        stream = self.stream()
        stream[1], stream[2] = stream[2], stream[1]
        value = self.replay(diagnostic.observed_calls(stream, 'sess_test'))
        self.assertEqual(value['status'], 'fail')
        self.assertFalse(value['matches']['src/player.gd'])

    def test_conflicting_progress_edit_inputs_fail_without_guessing(self):
        stream = self.stream()
        stream[1]['params']['update']['rawInput'] = {'path': str(self.target/'src/player.gd'), 'text': 'another source'}
        value = self.replay(diagnostic.observed_calls(stream, 'sess_test'))
        self.assertEqual(value['status'], 'fail')
        self.assertIn('ambiguous_native_edit_input', {error['code'] for error in value['errors']})

    def test_unrecognized_actual_terminal_is_not_a_successful_native_mutation(self):
        calls = diagnostic.observed_calls(self.stream(), 'sess_test')
        with patch.object(diagnostic.review, '_builtin_227_edit_terminal', return_value=False):
            value = diagnostic.replay_selected_text(calls, checkpoint_id='evaluate', evaluation_job_id='job-selected',
                target=self.target, baseline=self.baseline, selected=self.selected,
                baseline_hashes=self.old_hashes, selected_hashes=self.new_hashes, session_id='sess_test')
        self.assertEqual(value['status'], 'fail')
        self.assertIn('unsupported_observed_edit_terminal', {error['code'] for error in value['errors']})

    def test_native_unsupported_reason_is_preserved_without_version_alias(self):
        failure = VisualError('invalid_native_evidence', 'Unsupported exact native version metadata')
        with patch.object(diagnostic.review, 'audit_native_run', side_effect=failure):
            value = diagnostic._native_result(self.root)
        self.assertEqual(value['status'], 'fail')
        self.assertEqual(value['code'], 'invalid_native_evidence')
        self.assertEqual(value['message'], str(failure))
        self.assertFalse(value['acceptance_override'])

    def test_missing_raw_proof_stays_pending_and_never_calls_validator(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('Missing proof must not run a validator')
        value = diagnostic._raw_proof(None, kind='VerifiedCanonicalGroundRawInspection', validator=forbidden,
            output=None, session=self.root, chain=self.root/'chain.json', chain_sha='a'*64,
            source_sha='b'*64, inventory={})
        self.assertEqual(value['status'], 'pending')
        with self.assertRaises(VisualError):
            diagnostic._raw_proof(None, kind='VerifiedCanonicalGroundRawInspection', validator=forbidden,
                output=self.root/'aid', session=self.root, chain=self.root/'chain.json', chain_sha='a'*64,
                source_sha='b'*64, inventory={})

    def test_raw_proof_must_equal_fresh_validator_and_exact_candidate(self):
        bound = self.root/'one-bound-input.txt'
        bound.write_text('immutable input', encoding='utf-8')
        files = {str(bound): file_sha256(bound)}
        value = {'kind':'VerifiedCanonicalGroundRawInspection','status':'pass','parent_chain_sha256':'a'*64,
                 'full_behavior_acceptance':False,'ordinary_reader_compatibility_claimed':False,
                 'sealed_source_sha256':'b'*64,'consumed_files':files,'consumed_files_sha256':digest(files),
                 'result_sha256':'c'*64}
        proof=self.root/'proof.json'
        write_json(proof, value)
        def validate(*args, **kwargs):
            return deepcopy(value)
        for candidate, changed in (('d'*64, False), ('b'*64, True)):
            if changed:
                wrong=deepcopy(value);wrong['result_sha256']='e'*64;write_json(proof, wrong)
            with self.subTest(candidate=candidate, changed=changed), self.assertRaises(VisualError):
                diagnostic._raw_proof(proof, kind=value['kind'],validator=validate,output=self.root/'aid',
                    session=self.root,chain=self.root/'chain.json',chain_sha='a'*64,
                    source_sha=candidate,inventory={})

    def test_append_only_producer_refuses_existing_output_before_any_runtime(self):
        output=self.root/'existing';output.mkdir()
        with patch.object(diagnostic.Session, '_load', side_effect=AssertionError('No Session read permitted')):
            with self.assertRaises(VisualError) as caught:
                diagnostic.prepare(self.root/'session',output,parent_chain=self.root/'chain',
                    parent_chain_sha256='a'*64,decisions=self.root/'decisions')
        self.assertEqual(caught.exception.code, 'output_exists')


class CompleteDiagnosticReceiptTests(unittest.TestCase):
    """Full producer/receipt behavior around synthetic authoritative-reader fixtures.

    Native stream inputs, edit payloads, source bytes and decision gates are
    real local test data.  The completed-Session/parent/raw authoritative
    readers are stubbed; this does not claim any actual experiment acceptance.
    """
    stream = GroundDiagnosticTests.stream

    def setUp(self):
        GroundDiagnosticTests.setUp(self)
        self.session = self.root/'session'
        self.session.mkdir()
        self.suite = self.root/'suite'; self.suite.mkdir()
        self.original = self.root/'original'; self.original.mkdir()
        self.output = self.root/'fresh-diagnostic'
        self.chain = self.root/'chain.json'
        write_json(self.chain, {'kind':'SyntheticParentChainFixture','scope':'test only'})
        self.chain_sha = file_sha256(self.chain)
        self.state = {'status':'finalized','active_job_id':None,'selected_candidate_id':'candidate-01',
            'target':str(self.target),'baseline':str(self.baseline),'baseline_hashes':self.old_hashes,
            'prepare_job_id':'job-prepare','final_job_id':'job-final','final':{'pass':True},
            'candidates':[{'candidate_id':'candidate-01','job_id':'job-selected',
                           'source_sha256':digest(self.new_hashes),'eligible':True,'status':'succeeded'}]}
        write_json(self.session/'session.json', self.state)
        write_json(self.session/'binding.json', {'synthetic_fixture':True})
        self.context = {'directory':str(self.session),'suite':str(self.suite),'suite_sha256':'c'*64,
            'domain':'game','stage':'ground','sealed_source':str(self.selected),
            'authoring_parent':str(self.baseline),'original':str(self.original),
            'cases':[{'case_id':'known','role':'regression'},{'case_id':'new','role':'validation'}]}
        self.final_result = {'pass':True,'preservation_pass':True,'suite_sha256':'c'*64,
            'visual_wall_status':'pass','normal_level_status':'pass',
            'comparisons':[{'case_id':'known','role':'regression','pass':True},
                           {'case_id':'new','role':'validation','pass':True}]}
        for job in ('job-prepare','job-selected','job-final'):
            folder=self.session/'jobs'/job;folder.mkdir(parents=True)
            for name in ('job.json','binding.json'):
                write_json(folder/name, {'job_id':job,'synthetic_fixture':True})
            write_json(folder/'result.json', self.final_result if job=='job-final' else {'eligible':True})
        prompt=self.target/'prompt.txt';prompt.write_text('Synthetic single task fixture.',encoding='utf-8')
        config={'target_dir':str(self.target),'prompt_path':str(prompt),'launcher_sha256':'d'*64}
        inbound=self.stream()
        launch={'finished_at':10.,'terminal_cleanup':{'status':'pass'},'preflight':{'session_id':'sess_test'},
                'stream_configuration':{'events_seen':len(inbound)},'cli_version':'kiro-cli-chat 2.27.1',
                'kas_version':'0.66.22','model':'claude-opus-5.5','effort':'max'}
        outbound=[]
        messages=[{'method':'session/new','params':{'cwd':str(self.target)}},
                  {'method':'session/prompt','params':{'sessionId':'sess_test',
                    'prompt':[{'type':'text','text':prompt.read_text('utf-8')} ]}}]
        for sequence,message in enumerate(messages,1):
            sha=digest(message)
            outbound.extend([{'phase':'attempt','sequence':sequence,'payload_sha256':sha,'message':message},
                             {'phase':'sent','sequence':sequence,'payload_sha256':sha}])
        run=self.session/'kiro/run';run.mkdir(parents=True)
        write_json(self.session/'kiro/config.json',config)
        write_json(self.session/'kiro/launch.json',launch)
        for name,values in (('stdout.jsonl',inbound),('outbound.jsonl',outbound)):
            (run/name).write_text(''.join(json.dumps(value)+'\n' for value in values),encoding='utf-8')
        (self.session/'kiro/tool-audit.jsonl').write_text('synthetic audit fixture\n',encoding='utf-8')
        calls=diagnostic.observed_calls(inbound,'sess_test')
        self.decision_path=self.root/'actual-decisions-fixture.json'
        write_json(self.decision_path, {'kind':'ActualGroundSourceInspectionDecisions','schema_version':1,
            'actual_source_and_native_inputs_inspected':True,'reviewer':'synthetic fixture reviewer',
            'session_directory':str(self.session),'selected_candidate_id':'candidate-01',
            'candidate_sha256':digest(self.new_hashes),'source_hashes':self.new_hashes,'baseline_hashes':self.old_hashes,
            'native_evidence_hashes':{name:file_sha256(self.session/name) for name in diagnostic.NATIVE_FILES},
            'selected_evaluation_tool_call_id':'evaluate',
            'inspected_tool_calls':{identity:call['input_set_sha256'] for identity,call in calls.items()},
            'inspected_source_files':[{'path':str(self.selected/'src/player.gd'),
                'sha256':self.new_hashes['src/player.gd'],'actually_inspected':True,
                'observations':'Synthetic fixture reads this changed code, not an actual human/model experiment.'}],
            'checks':[{'id':name,'verdict':'pass','evidence':'Explicit synthetic decision supplied by this test fixture.'}
                      for name in diagnostic.CHECKS]})
        self.code_file=self.root/'producer-code.txt';self.code_file.write_text('fixture source closure',encoding='utf-8')
        self.code={str(self.code_file):file_sha256(self.code_file)}
        for owner,name,change in (
            (diagnostic.Session,'_load',{'return_value':self.state}),
            (diagnostic.final,'_context_session',{'return_value':(self.context,[])}),
            (diagnostic.ground,'_parent_chain',{'return_value':{'path':str(self.chain),'file_sha256':self.chain_sha}}),
            (diagnostic.evidence,'_completed_job',{'side_effect':lambda session,state,job,op,candidate:
                deepcopy(self.final_result) if op=='finalize' else {'eligible':True}}),
            (diagnostic,'_code_sources',{'return_value':self.code}),
            (diagnostic.review,'_builtin_227_edit_terminal',{'return_value':True}),
            (diagnostic.review,'_builtin_policy_227',{'return_value':None}),
            (diagnostic.review,'audit_native_run',{'side_effect':VisualError('invalid_native_evidence','Unsupported CLI2.27.1 tail fixture')})):
            helper=patch.object(owner,name,**change);helper.start();self.addCleanup(helper.stop)

    def proofs(self, *, raw_source=None):
        bound=self.root/'raw-bound-input.txt';bound.write_text('verified fixture raster identity',encoding='utf-8')
        files={str(bound):file_sha256(bound)}
        values=[]
        for kind,source_key in (('VerifiedCanonicalGroundRawInspection','sealed_source_sha256'),
                                ('VerifiedCanonicalGroundNormalRawInspection','source_sha256')):
            values.append({'kind':kind,'status':'pass','parent_chain_sha256':self.chain_sha,
                'full_behavior_acceptance':False,'ordinary_reader_compatibility_claimed':False,
                source_key:(raw_source or digest(self.new_hashes)),
                'consumed_files':files,'consumed_files_sha256':digest(files),'result_sha256':'e'*64})
        paths=[]
        for index,value in enumerate(values):
            path=self.root/f'proof-{index}.json';write_json(path,value);paths.append(path)
        for owner,value in ((diagnostic.ground,values[0]),(diagnostic.normal,values[1])):
            helper=patch.object(owner,'validate',return_value=value);helper.start();self.addCleanup(helper.stop)
        return {'raw_review':self.root/'raw-aid','raw_proof':paths[0],
                'normal_review':self.root/'normal-aid','normal_proof':paths[1]}

    def produce(self, **kwargs):
        return diagnostic.prepare(self.session,self.output,parent_chain=self.chain,
            parent_chain_sha256=self.chain_sha,decisions=self.decision_path,**kwargs)

    def test_complete_manual_facts_and_raw_pass_do_not_override_native_failure(self):
        receipt=self.produce(**self.proofs())
        self.assertEqual(receipt['source_facts_status'],'pass')
        self.assertEqual(receipt['protected_behavior_status'],'pass')
        self.assertEqual(receipt['native_validation_status'],'fail')
        self.assertEqual(receipt['status'],'fail')
        self.assertFalse(receipt['full_behavior_acceptance'])
        self.assertFalse(receipt['canonical_source_review_published'])
        self.assertFalse((self.session/'source_review.json').exists())
        native=json.loads((self.output/'native-audit.json').read_text('utf-8'))
        self.assertEqual(native['code'],'invalid_native_evidence')
        self.assertIn('CLI2.27.1',native['message'])
        source=json.loads((self.output/'source-diagnostic.json').read_text('utf-8'))
        self.assertEqual(source['raw_review']['status'],source['normal_raw_review']['status'])
        self.assertFalse(source['unsupported_version_aliased'])

    def test_numeric_all_pass_without_both_raw_proofs_keeps_protection_pending(self):
        proof_args=self.proofs()
        for available in ('neither','final-only','normal-only'):
            with self.subTest(available=available):
                self.output=self.root/('fresh-diagnostic-'+available)
                kwargs={}
                if available=='final-only':
                    kwargs={key:proof_args[key] for key in ('raw_review','raw_proof')}
                elif available=='normal-only':
                    kwargs={key:proof_args[key] for key in ('normal_review','normal_proof')}
                receipt=self.produce(**kwargs)
                self.assertEqual(receipt['protected_behavior_status'],'pending')
                self.assertEqual(receipt['source_facts_status'],'pending')
                self.assertEqual(receipt['status'],'fail')
                source=json.loads((self.output/'source-diagnostic.json').read_text('utf-8'))
                self.assertEqual(source['raw_review']['status'],'pass' if available=='final-only' else 'pending')
                self.assertEqual(source['normal_raw_review']['status'],'pass' if available=='normal-only' else 'pending')
                verdicts={row['id']:row['verdict'] for row in source['checks']}
                self.assertEqual(verdicts['actual_code_authorship'],'pass')
                self.assertEqual(verdicts['protected_behavior_preserved'],'pending')

    def test_full_producer_rejects_other_source_raw_even_if_numeric_and_manual_pass(self):
        with self.assertRaises(VisualError) as caught:
            self.produce(**self.proofs(raw_source='f'*64))
        self.assertEqual(caught.exception.code,'wrong_candidate')
        self.assertFalse(self.output.exists())
        self.assertFalse((self.session/'source_review.json').exists())


if __name__ == '__main__':
    unittest.main()
