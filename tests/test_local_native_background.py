"""Actual background-launch envelope, exact arguments and hostile mutations."""
from copy import deepcopy
from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch
from kiroclops.local_engine import review
from kiroclops.visual.schema import read_json,file_sha256,digest
from tests.evidence import requires_evidence

ROOT=Path(__file__).resolve().parents[1]


class NativeBackgroundTests(unittest.TestCase):
    def setUp(self):
        data=read_json(ROOT/'artifacts/phase3a/panel-interruption-qualification-01/native-unmatched-evidence.json')
        self.events=deepcopy(data['events']);self.sid=self.events[0]['params']['sessionId']
        self.row={**deepcopy(data['decisions'][0]['decision']),'at':15}
        self.context={**{k:review._NATIVE_OUTPUT_POLICY[k] for k in ('cli_version','kas_version','launcher_sha256')},
            'target_dir':self.row['arguments']['cwd'],'hook_id':'background#hook-0'}
        self.hook={'method':'session/update','params':{'sessionId':self.sid,'update':{
            'sessionUpdate':'session_info_update','_meta':{'kiro':{'kind':'hook_update','hook':{
                'hookId':self.context['hook_id'],'operationId':'one-hook','name':'Local trial boundary',
                'status':'completed','actionType':'runCommand'}}}}}}

    def audit(self,events=None,rows=None,context=None):
        return review.audit_native_tool_outcomes((self.events if events is None else events)+[self.hook],
            [self.row] if rows is None else rows,session_id=self.sid,started_at=10,finished_at=20,
            scope_sha256=digest({'fixture':'background'}),runtime_context=context or self.context)

    @requires_evidence('artifacts/phase3a')
    def test_launch_is_bound_but_never_child_completion(self):
        with patch('builtins.open',side_effect=AssertionError('No process/output inspection in decoder')):
            r=self.audit()
        self.assertEqual(r['status'],'pass',r)
        receipt=r['calls'][0]['background_launch']
        self.assertEqual(receipt['pid'],21152)
        self.assertEqual(receipt['child_completion'],'not_attested')
        self.assertIsNone(receipt['child_exit_code'])

    @requires_evidence('artifacts/phase3a')
    def test_all_guard_fields_remain_mandatory(self):
        for rows in ([],[{**self.row,'allow':False}],
                     [{**self.row,'arguments':{**self.row['arguments'],'run_in_background':False}}]):
            self.assertEqual(self.audit(rows=rows)['status'],'fail')

    @requires_evidence('artifacts/phase3a')
    def test_unknown_or_old_runtime_not_upgraded(self):
        for value in ('0'*64,review._NATIVE_OUTPUT_POLICY_OPUS55_MAX_V3['launcher_sha256']):
            self.assertEqual(self.audit(context={**self.context,'launcher_sha256':value})['status'],'fail')

    @requires_evidence('artifacts/phase3a')
    def test_malformed_or_extra_output_is_rejected(self):
        mutations=[lambda u:u['rawOutput'].update(exitCode=True),lambda u:u['rawOutput'].update(exitCode=1),
            lambda u:u['rawOutput'].update(output='Background process started (pid: unknown)'),
            lambda u:u['rawOutput'].update(message=u['rawOutput']['message']+'extra'),
            lambda u:u['rawOutput'].update(extra='hidden'),lambda u:u['content'].append({'type':'text','text':'hidden'}),
            lambda u:u.update(status='failed'),lambda u:u['rawInput'].update(command='different'),
            lambda u:u['_meta']['kiro'].update(outputTransformation={'kind':'offloaded'})]
        for mutate in mutations:
            events=deepcopy(self.events);mutate(events[-1]['params']['update'])
            self.assertEqual(self.audit(events=events)['status'],'fail')

    @requires_evidence('artifacts/phase3a')
    def test_source_proof_is_exact_and_spawn_semantics_are_explicit(self):
        path=ROOT/'artifacts/phase3a/native-background-audit-01/source-proof.json';p=read_json(path)
        self.assertEqual(file_sha256(path),review._NATIVE_OUTPUT_POLICY['shell_background_source_proof_sha256'])
        self.assertIn('detached:!0',p['fragments']['background_terminal']['text'])
        self.assertIn('c.on("spawn"',p['fragments']['background_terminal']['text'])
        source=Path(p['source'])
        if source.exists():
            self.assertEqual(file_sha256(source),p['source_sha256']);data=source.read_bytes()
            for f in p['fragments'].values():
                raw=data[f['byte_offset']:f['byte_offset']+f['byte_length']]
                self.assertEqual(raw.decode(),f['text']);self.assertEqual(hashlib.sha256(raw).hexdigest(),f['sha256'])


if __name__=='__main__':unittest.main()
