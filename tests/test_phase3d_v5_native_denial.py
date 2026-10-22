"""V5 prevented-denial policy: only calls proven refused before they ran are set aside."""
import json
from pathlib import Path
import unittest

from kiroclops.local_engine import review
from scripts import phase3d_v5_native_denial as denial

SESSION = 'sess_test'
V4P01 = Path('C:/kc3d/s/v4p01')


def update(call_id, title, raw_input, *, status='failed', origin='acp', locations=None, **extra):
    value = {'sessionUpdate': 'tool_call_update', 'toolCallId': call_id, 'status': status, 'title': title,
             'rawInput': raw_input, '_meta': {'kiro': {'toolOrigin': origin}}, **extra}
    if locations is not None:
        value['locations'] = locations
    return {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': SESSION, 'update': value}}


def deny(tool, arguments, allow=False):
    return {'at': 1., 'arguments': arguments, 'audit_origin': 'pre_tool_use', 'allow': allow,
            'reason': 'Outside the registered experiment boundary', 'tool': tool, 'paths': []}


DELETE = {'explanation': 'remove helper', 'targetFile': 'c:/t/src/a.js'}
SHELL = {'command': "python -c \"open(r'..\\x.json')\"", 'cwd': 'c:/t/.kiroclops-scratch'}


class RefusedCallTests(unittest.TestCase):
    def test_a_refused_delete_and_shell_are_set_aside_with_their_decisions(self):
        inbound = [update('toolu_1', 'Delete File', DELETE, locations=[{'path': DELETE['targetFile']}]),
                   update('toolu_2', 'Run Command', SHELL, origin='default')]
        decisions = [deny('fs_write', {'path': 'x'}, allow=True), deny('delete_file', DELETE), deny('execute_pwsh', SHELL)]
        ids, rows, ledger = denial.refused_calls(inbound, decisions)
        self.assertEqual({'toolu_1', 'toolu_2'}, ids)
        self.assertEqual({1, 2}, rows)
        self.assertEqual([2, 3], [row['decision_index'] for row in ledger])

    def test_output_progress_shell_start_mcp_or_missing_decision_are_kept(self):
        cases = [
            ([update('toolu_1', 'Delete File', DELETE, rawOutput={'x': 1})], [deny('delete_file', DELETE)]),
            ([update('toolu_1', 'Delete File', DELETE, status='completed')], [deny('delete_file', DELETE)]),
            ([update('run_command_1', 'Run Command', SHELL)], [deny('execute_pwsh', SHELL)]),
            ([update('toolu_1', '@kiroclops/inspect_session', {})], [deny('@kiroclops/inspect_session', {})]),
            ([update('toolu_1', 'Delete File', DELETE)], []),
            ([update('toolu_1', 'Delete File', DELETE)], [deny('delete_file', DELETE, allow=True)]),
            ([update('toolu_1', 'Delete File', DELETE)], [deny('delete_file', {**DELETE, 'targetFile': 'c:/t/b.js'})]),
            ([update('toolu_1', 'Delete File', DELETE, origin='unknown')], [deny('delete_file', DELETE)]),
            ([update('toolu_1', 'Delete File', DELETE, locations=[{'path': 'c:/elsewhere'}])], [deny('delete_file', DELETE)]),
        ]
        for inbound, decisions in cases:
            with self.subTest(inbound=inbound[0]['params']['update'].get('title'), decisions=len(decisions)):
                self.assertEqual((set(), set(), []), denial.refused_calls(inbound, decisions))

    def test_unequal_call_and_decision_counts_set_aside_nothing(self):
        inbound = [update('toolu_1', 'Delete File', DELETE), update('toolu_2', 'Delete File', DELETE)]
        self.assertEqual(set(), denial.refused_calls(inbound, [deny('delete_file', DELETE)])[0])
        both = denial.refused_calls(inbound, [deny('delete_file', DELETE), deny('delete_file', DELETE)])
        self.assertEqual({'toolu_1', 'toolu_2'}, both[0])


class PolicyTests(unittest.TestCase):
    def test_the_canonical_audit_receives_the_rest_and_is_restored(self):
        seen = {}
        original = review.audit_native_tool_outcomes

        def fake(inbound, decisions, **kwargs):
            seen.update(inbound=inbound, decisions=decisions, kwargs=kwargs)
            return {'status': 'pass'}

        inbound = [update('toolu_1', 'Delete File', DELETE), update('toolu_2', 'Read File', {'path': 'c:/t/a'},
                                                                   status='completed')]
        decisions = [deny('delete_file', DELETE), deny('read_file', {'path': 'c:/t/a'}, allow=True)]
        review.audit_native_tool_outcomes = fake
        try:
            with denial.denial_policy() as ledger:
                self.assertEqual({'status': 'pass'}, review.audit_native_tool_outcomes(inbound, decisions, session_id=SESSION))
            self.assertIs(fake, review.audit_native_tool_outcomes)
        finally:
            review.audit_native_tool_outcomes = original
        self.assertEqual(['toolu_2'], [e['params']['update']['toolCallId'] for e in seen['inbound']])
        self.assertEqual([decisions[1]], seen['decisions'])
        self.assertEqual({'session_id': SESSION}, seen['kwargs'])
        self.assertEqual(['toolu_1'], [row['tool_call_id'] for row in ledger])

    def test_the_original_is_restored_after_an_error(self):
        original = review.audit_native_tool_outcomes
        with self.assertRaises(RuntimeError):
            with denial.denial_policy():
                raise RuntimeError('boom')
        self.assertIs(original, review.audit_native_tool_outcomes)


@unittest.skipUnless((V4P01 / 'kiro/run/stdout.jsonl').is_file(), 'V4 panel native evidence is not on this machine')
class RecordedEvidenceTests(unittest.TestCase):
    def test_v4p01_has_exactly_its_four_refusals_set_aside(self):
        inbound = [json.loads(line) for line in (V4P01 / 'kiro/run/stdout.jsonl').read_text(encoding='utf-8').splitlines()
                   if line.strip().startswith('{')]
        decisions = [json.loads(line) for line in (V4P01 / 'kiro/tool-audit.jsonl').read_text(encoding='utf-8').splitlines()
                     if line.strip()]
        ids, rows, ledger = denial.refused_calls(inbound, decisions)
        self.assertEqual([4, 5, 34, 91], [row['decision_index'] for row in ledger])
        self.assertEqual(4, len(ids))


if __name__ == '__main__':
    unittest.main()
