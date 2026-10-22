"""Post-hoc event codec tests; no cohort, engine or model is executed."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops import portable_review_v2 as revision
from kiroclops.portable import audit as original
from kiroclops.integration.storage import read_json, write_json, sha256
from tests import test_portable_audit as original_tests


class DeniedReadCodecTests(unittest.TestCase):
    def setUp(self):
        original_tests.NativeAuthorshipReplay.setUp(self)
        write_json(self.root / 'kiro/launch.json', {'acp_preflight': {'session_id': 'sid'},
            'started_at': '2026-09-21T01:00:00+00:00', 'finished_at': '2026-09-21T01:01:00+00:00'})
        self.original_events = deepcopy(self.inbound)
        self.prefix = []

    def add_denied(self, name='one'):
        path = str(self.root / 'measurements' / name / 'profile.json')
        notification = {'method': 'session/update', 'params': {'sessionId': 'sid', 'update': {
            'sessionUpdate': 'session_info_update', '_meta': {'kiro': {'kind': 'hook_update', 'hook': {
                'hookId': str(self.root / 'target/.kiro/hooks/kiroclops-guard.json') + '#hook-0',
                'operationId': 'operation-' + name, 'status': 'completed', 'actionType': 'runCommand'}}}}}}
        failure = {'method': 'session/update', 'params': {'sessionId': 'sid', 'update': {
            'sessionUpdate': 'tool_call_update', 'toolCallId': 'denied-' + name, 'status': 'failed',
            'title': 'Read File', 'rawInput': {'path': path, 'offset': None, 'limit': None},
            'locations': [{'path': path}], '_meta': {'kiro': {'toolOrigin': 'default'}}}}}
        self.prefix.extend([notification, failure])
        self.inbound = self.prefix + deepcopy(self.original_events)
        self.hooks.insert(len(self.prefix) // 2 - 1, {'allow': False, 'tool': 'read_file', 'paths': [path],
            'at': '2026-09-21T01:00:20+00:00', 'reason': 'Outside this stage readable workspace'})

    def materialize(self, *, blanks=False):
        for filename, rows in [('stdout.jsonl', self.inbound), ('tool-audit.jsonl', self.hooks)]:
            text = '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n'
            (self.root / 'kiro' / filename).write_text(('\n' if blanks else '') + text, encoding='utf-8')

    def verify(self):
        self.materialize()
        return revision.verify_authorship(self.root, self.state, self.candidate, self.inbound, self.hooks)

    def test_five_exact_denied_reads_keep_original_write_and_evaluation_replay(self):
        for n in range(5):
            self.add_denied(str(n))
        before_events, before_hooks = deepcopy(self.inbound), deepcopy(self.hooks)
        with self.assertRaisesRegex(ValueError, 'Orphan or repeated'):
            original.verify_authorship(self.root, self.state, self.candidate, self.inbound, self.hooks)
        result = self.verify()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['denied_read_codec']['denied_read_count'], 5)
        self.assertEqual(len(result['writes']), 1)
        self.assertEqual(set(result['evaluation_bindings']), {'candidate-01'})
        self.assertEqual(result['writes'][0]['stdout_line'], 12)
        self.assertEqual(result['writes'][0]['projected_stdout_line'], 7)
        self.assertEqual(self.inbound, before_events)
        self.assertEqual(self.hooks, before_hooks)
        self.assertTrue(all(b['executed'] is False for b in result['denied_read_codec']['bindings']))

    def test_normal_complete_call_lifecycle_remains_unchanged(self):
        result = self.verify()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['denied_read_codec']['denied_read_count'], 0)

    def test_success_or_cancelled_orphans_rejected(self):
        self.add_denied()
        for status in ('completed', 'cancelled'):
            self.inbound[1]['params']['update']['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.verify()

    def test_mutation_shell_and_mcp_orphans_rejected(self):
        self.add_denied()
        original_update = deepcopy(self.inbound[1]['params']['update'])
        for changes in ({'kind': 'edit'}, {'kind': 'execute'},
                        {'_meta': {'kiro': {'toolOrigin': 'default', 'serverName': 'kiroclops'}}},
                        {'rawInput': {'path': original_update['rawInput']['path'], 'oldStr': 'a', 'newStr': 'b'}}):
            self.inbound[1]['params']['update'] = {**deepcopy(original_update), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.verify()

    def test_unknown_native_origin_rejected(self):
        self.add_denied()
        self.inbound[1]['params']['update']['_meta']['kiro']['toolOrigin'] = 'external'
        with self.assertRaises(ValueError):
            self.verify()

    def test_output_or_content_even_empty_is_not_ignored(self):
        self.add_denied()
        for field in ('rawOutput', 'content'):
            self.inbound[1]['params']['update'][field] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
            del self.inbound[1]['params']['update'][field]

    def test_missing_or_allowed_hook_does_not_prove_denial(self):
        self.add_denied()
        self.hooks[0]['allow'] = True
        with self.assertRaises(ValueError):
            self.verify()
        self.hooks.pop(0)
        with self.assertRaises(ValueError):
            self.verify()

    def test_ambiguous_duplicate_denials_rejected(self):
        self.add_denied()
        self.hooks.insert(1, deepcopy(self.hooks[0]))
        with self.assertRaises(ValueError):
            self.verify()

    def test_duplicate_failure_notification_rejected(self):
        self.add_denied()
        self.inbound.insert(2, deepcopy(self.inbound[1]))
        with self.assertRaises(ValueError):
            self.verify()

    def test_mismatched_hook_path_or_tool_rejected(self):
        self.add_denied()
        self.hooks[0]['paths'] = [str(self.root / 'different.json')]
        with self.assertRaises(ValueError):
            self.verify()
        self.hooks[0]['paths'] = [self.inbound[1]['params']['update']['rawInput']['path']]
        self.hooks[0]['tool'] = 'str_replace'
        with self.assertRaises(ValueError):
            self.verify()

    def test_relative_or_locations_mismatched_path_rejected(self):
        self.add_denied()
        update = self.inbound[1]['params']['update']
        update['locations'][0]['path'] = 'other'
        with self.assertRaises(ValueError):
            self.verify()
        update['rawInput']['path'] = 'relative.json'
        update['locations'] = [{'path': 'relative.json'}]
        self.hooks[0]['paths'] = ['relative.json']
        with self.assertRaises(ValueError):
            self.verify()

    def test_cross_session_event_and_out_of_window_denial_rejected(self):
        self.add_denied()
        self.inbound[1]['params']['sessionId'] = 'another-session'
        with self.assertRaises(ValueError):
            self.verify()
        self.inbound[1]['params']['sessionId'] = 'sid'
        self.hooks[0]['at'] = '2026-09-21T00:59:00+00:00'
        with self.assertRaises(ValueError):
            self.verify()

    def test_guard_notification_is_required_and_cannot_be_foreign(self):
        self.add_denied()
        self.inbound[0]['params']['update']['_meta']['kiro']['hook']['hookId'] = 'different-hook'
        with self.assertRaises(ValueError):
            self.verify()

    def test_denial_order_and_operation_reuse_rejected(self):
        self.add_denied('a'); self.add_denied('b')
        self.hooks[0], self.hooks[1] = self.hooks[1], self.hooks[0]
        with self.assertRaises(ValueError):
            self.verify()
        self.hooks[0], self.hooks[1] = self.hooks[1], self.hooks[0]
        self.inbound[2]['params']['update']['_meta']['kiro']['hook']['operationId'] = 'operation-a'
        with self.assertRaises(ValueError):
            self.verify()

    def test_failure_with_partial_lifecycle_or_permission_request_rejected(self):
        self.add_denied()
        progress = deepcopy(self.inbound[1]); progress['params']['update']['status'] = 'in_progress'
        self.inbound.insert(0, progress)
        with self.assertRaises(ValueError):
            self.verify()
        self.inbound.pop(0)
        self.inbound.append({'method': 'session/request_permission', 'params': {'toolCall': {'toolCallId': 'denied-one'}}})
        with self.assertRaises(ValueError):
            self.verify()

    def test_ordinary_started_call_with_failure_remains_for_original_verifier(self):
        read = {'method': 'session/update', 'params': {'sessionId': 'sid', 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': 'ordinary-read', 'kind': 'read',
            'rawInput': {'path': str(self.root / 'target/player.gd')}, '_meta': {'kiro': {'toolOrigin': 'default'}}}}}
        failed = deepcopy(read); failed['params']['update'].update(sessionUpdate='tool_call_update', status='failed')
        self.inbound = [read, failed] + self.inbound
        result = self.verify()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['denied_read_codec']['denied_read_count'], 0)

    def test_original_mutation_and_evaluation_identity_checks_are_not_bypassed(self):
        self.add_denied()
        self.candidate['source_hashes']['player.gd'] = 'x' * 64
        with self.assertRaisesRegex(ValueError, 'not the source authored'):
            self.verify()

    def test_raw_physical_lines_are_bound_even_if_journal_contains_blank_lines(self):
        self.add_denied(); self.materialize(blanks=True)
        result = revision.verify_authorship(self.root, self.state, self.candidate, self.inbound, self.hooks)
        binding = result['denied_read_codec']['bindings'][0]
        self.assertEqual(binding['stdout']['line'], 3)
        self.assertEqual(binding['denied_hook']['line'], 2)
        self.assertEqual(result['writes'][0]['stdout_line'], 5)

    def test_in_memory_filtered_journal_cannot_be_supplied_as_raw_evidence(self):
        self.add_denied(); self.materialize()
        with self.assertRaisesRegex(ValueError, 'unchanged raw journals'):
            revision.verify_authorship(self.root, self.state, self.candidate, self.original_events, self.hooks)


class RevisionReceiptTests(unittest.TestCase):
    """Reuse setup helpers without inheriting or duplicating codec test cases."""
    setUp = DeniedReadCodecTests.setUp
    add_denied = DeniedReadCodecTests.add_denied
    materialize = DeniedReadCodecTests.materialize
    def fake_compute(self):
        namespace = {'VERSION': original.VERSION, 'verify_authorship': original.verify_authorship,
            'state': self.state, 'candidate': self.candidate, 'inbound': self.inbound, 'hooks': self.hooks,
            'sha256': sha256}
        exec("def compute(directory, observations_path, source_review_path):\n"
             "    authorship = verify_authorship(directory, state, candidate, inbound, hooks)\n"
             "    return {'schema_version':1, 'audit_version':VERSION, 'status':'pass',\n"
             "      'candidate_id':candidate['candidate_id'], 'source_sha256':candidate['source_sha256'],\n"
             "      'comparison_sha256':'fixed-comparison', 'observations_path':str(observations_path),\n"
             "      'source_review_path':str(source_review_path), 'gates':{'native_authorship':authorship},\n"
             "      'evidence_files':{str(directory/'kiro'/n):sha256(directory/'kiro'/n) for n in ('stdout.jsonl','tool-audit.jsonl','launch.json')}}\n", namespace)
        return namespace['compute'], namespace

    def test_copied_globals_change_only_verifier_and_original_objects_files_stay_unchanged(self):
        self.add_denied(); self.materialize()
        compute, namespace = self.fake_compute()
        before = dict(namespace)
        original_verifier = original.verify_authorship
        source_before = revision.source_binding()
        journal_before = sha256(self.root / 'kiro/stdout.jsonl')
        with patch.object(original, '_compute', compute):
            result = revision._compute(self.root, 'observations', 'source-review')
        self.assertEqual(result['audit_version'], revision.VERSION)
        self.assertEqual(result['parent_audit_version'], original.VERSION)
        self.assertEqual(namespace, before)
        self.assertIs(original.verify_authorship, original_verifier)
        self.assertEqual(revision.source_binding(), source_before)
        self.assertEqual(sha256(self.root / 'kiro/stdout.jsonl'), journal_before)

    def test_separate_immutable_receipt_preserves_original_rejection_and_replays(self):
        self.add_denied(); self.materialize()
        prior = self.root / 'final/independent-review.json'
        diagnosis = self.root / 'diagnosis.json'
        write_json(prior, {'status': 'fail', 'reason': 'Orphan or repeated native completion'})
        write_json(diagnosis, {'diagnostic_only': True})
        prior_hash = sha256(prior)
        compute, _ = self.fake_compute()
        with patch.object(original, '_compute', compute):
            result = revision.audit_session(self.root, 'observations', 'source-review', original_rejection_path=diagnosis)
            self.assertEqual(result['status'], 'pass')
            self.assertEqual(revision.verify_review(self.root)['status'], 'pass')
            with self.assertRaises(ValueError):
                revision.audit_session(self.root, 'observations', 'source-review')
        self.assertEqual(sha256(prior), prior_hash)
        self.assertTrue((self.root / 'final/independent-review-v2.json').is_file())

    def test_original_acceptance_failure_remains_failure_and_is_not_overwritable(self):
        with patch.object(original, '_compute', lambda *_: (_ for _ in ()).throw(ValueError('Original numerical gate failed'))):
            result = revision.audit_session(self.root, 'observations', 'source-review')
        self.assertEqual(result['status'], 'fail')
        self.assertIn('numerical', result['reason'])
        self.assertEqual(revision.verify_review(self.root)['status'], 'fail')
        with self.assertRaises(ValueError):
            revision.audit_session(self.root, 'observations', 'source-review')

    def test_changed_bound_journal_fails_before_replay(self):
        self.add_denied(); self.materialize()
        compute, _ = self.fake_compute()
        with patch.object(original, '_compute', compute):
            self.assertEqual(revision.audit_session(self.root, 'observations', 'source-review')['status'], 'pass')
        (self.root / 'kiro/stdout.jsonl').write_text('{}\n', encoding='utf-8')
        with patch.object(revision, '_compute', side_effect=AssertionError('Do not replay changed evidence')):
            self.assertEqual(revision.verify_review(self.root)['status'], 'fail')

    def test_missing_review_pending_and_unbound_pass_rejected(self):
        self.assertEqual(revision.verify_review(self.root)['status'], 'pending')
        write_json(self.root / 'final/independent-review-v2.json', {'status': 'pass', 'audit_version': revision.VERSION})
        self.assertEqual(revision.verify_review(self.root)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
