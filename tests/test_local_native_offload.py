"""Actual recorded offload regression and hostile envelope mutations."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine import review
from kiroclops.visual.schema import read_json, file_sha256, digest
from tests.evidence import requires_evidence

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT/'artifacts/phase3a/panel-opus55-diagnostics-retry-01/native-unmatched-evidence.json'


class NativeOffloadTests(unittest.TestCase):
    def setUp(self):
        data = read_json(EVIDENCE)
        self.events = deepcopy(data['events'])
        self.sid = self.events[0]['params']['sessionId']
        self.row = {**deepcopy(data['decisions'][0]['decision']), 'at': 15}
        self.context = {**{k:review._NATIVE_OUTPUT_POLICY[k] for k in ('cli_version','kas_version','launcher_sha256')},
                        'target_dir':self.row['arguments']['cwd'], 'hook_id':'offload#hook-0'}
        self.hook = {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':self.sid,'update':{
            'sessionUpdate':'session_info_update','_meta':{'kiro':{'kind':'hook_update','hook':{
                'hookId':self.context['hook_id'],'operationId':'one-hook','name':'Local trial boundary',
                'status':'completed','actionType':'runCommand'}}}}}}

    def audit(self, events=None, rows=None, context=None):
        return review.audit_native_tool_outcomes((self.events if events is None else events)+[self.hook],
            [self.row] if rows is None else rows, session_id=self.sid, started_at=10, finished_at=20,
            scope_sha256=digest({'fixture':'offload'}),runtime_context=context or self.context)

    @requires_evidence('artifacts/phase3a')
    def test_observed_case_binds_all_arguments_without_reading_offload_file(self):
        before = deepcopy(self.events)
        with patch('builtins.open', side_effect=AssertionError('Decoder must not read external output')):
            result = self.audit()
        self.assertEqual('pass', result['status'], result)
        self.assertTrue(result['hook_cardinality_verified'])
        self.assertEqual(before, self.events)
        self.assertEqual(digest(review._native_arguments('execute_pwsh',self.row['arguments'])),
                         result['calls'][0]['arguments_sha256'])

    @requires_evidence('artifacts/phase3a')
    def test_missing_or_different_guard_decision_still_fails(self):
        for rows in ([], [{**self.row,'allow':False}],
                     [{**self.row,'arguments':{**self.row['arguments'],'command':'different'}}]):
            self.assertEqual('fail',self.audit(rows=rows)['status'])

    @requires_evidence('artifacts/phase3a')
    def test_unknown_runtime_and_old_policy_do_not_enable_offload(self):
        for patch_context in ({'kas_version':'changed'}, {'cli_version':'changed'}, {'launcher_sha256':'0'*64},
                              {'launcher_sha256':review._NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3['launcher_sha256']}):
            self.assertEqual('fail',self.audit(context={**self.context,**patch_context})['status'])

    @requires_evidence('artifacts/phase3a')
    def test_invalid_metadata_preview_and_payload_are_unproven(self):
        cases = []
        def alter(fn):
            events=deepcopy(self.events);fn(events[-1]['params']['update']);cases.append(events)
        alter(lambda u:u['_meta']['kiro'].pop('outputTransformation'))
        alter(lambda u:u['_meta']['kiro'].update(extra=True))
        alter(lambda u:u['_meta']['kiro']['outputTransformation'].update(kind='other'))
        alter(lambda u:u['_meta']['kiro']['outputTransformation'].update(totalChars=True))
        alter(lambda u:u['_meta']['kiro']['outputTransformation'].update(totalChars=30605))
        alter(lambda u:u['_meta']['kiro']['outputTransformation'].update(extra=True))
        for path in ('C:\\outside\\execute_pwsh-12345678.txt',
                     self.events[-1]['params']['update']['_meta']['kiro']['outputTransformation']['absFilePath'].replace(self.sid,'wrong-session'),
                     self.events[-1]['params']['update']['_meta']['kiro']['outputTransformation']['absFilePath'].replace('tool-outputs','..\\tool-outputs')):
            alter(lambda u,p=path:u['_meta']['kiro']['outputTransformation'].update(absFilePath=p))
        alter(lambda u:u['rawOutput'].update(exitCode=True))
        alter(lambda u:u['rawOutput'].update(exitCode=2))
        alter(lambda u:u['rawOutput'].update(message=u['rawOutput']['message']+'hidden'))
        alter(lambda u:u['rawOutput'].update(output=u['rawOutput']['output'].replace('29582','29583')))
        alter(lambda u:u['rawOutput'].update(extra='hidden'))
        alter(lambda u:u['content'].append({'type':'text','text':'hidden'}))
        alter(lambda u:u['content'][0]['content'].update(text='different preview'))
        alter(lambda u:u.update(status='failed'))
        alter(lambda u:u['rawInput'].update(command='changed command'))
        for i,events in enumerate(cases):
            with self.subTest(case=i):
                result=self.audit(events)
                self.assertEqual('fail',result['status'])
                self.assertIn('tool_identity_or_arguments_unproven',{e['code'] for e in result['errors']})

    @requires_evidence('artifacts/phase3a')
    def test_early_update_cannot_receive_terminal_metadata(self):
        events=deepcopy(self.events)
        events[1]['params']['update']['_meta']=deepcopy(events[-1]['params']['update']['_meta'])
        self.assertEqual('fail',self.audit(events)['status'])

    @requires_evidence('artifacts/phase3a')
    def test_source_proof_matches_installed_bytes_and_expected_semantics(self):
        path=ROOT/'artifacts/phase3a/native-offload-audit-01/source-proof.json'
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['shell_offload_source_proof_sha256'],file_sha256(path))
        proof=read_json(path)
        self.assertEqual(review._NATIVE_OUTPUT_POLICY['installed_source_sha256'],proof['source_sha256'])
        self.assertIn('CHAR_THRESHOLD:3e4',proof['fragments']['limits_preview']['text'])
        self.assertIn('rawInput:n.rawInput??r?.args',proof['fragments']['event_fields']['text'])
        self.assertIn('outputTransformation',proof['fragments']['terminal_meta']['text'])
        source=Path(proof['source'])
        if source.exists():
            self.assertEqual(proof['source_sha256'],file_sha256(source))
            data=source.read_bytes()
            for fragment in proof['fragments'].values():
                value=data[fragment['byte_offset']:fragment['byte_offset']+fragment['byte_length']]
                self.assertEqual(fragment['text'],value.decode('utf8'))
