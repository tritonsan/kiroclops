"""Regression guards for the verified post-cohort repair; no acceptance relaxation."""
import unittest
from tests.test_local_native_tool_outcomes import event, hook_event, decision, audit, CONTEXT


class RejectedDeleteTests(unittest.TestCase):
    def fixture(self):
        args = {'explanation': 'Remove temporary helper', 'targetFile': r'D:\workspace\.kiroclops-scratch\track.py'}
        rejected = event({'sessionUpdate': 'tool_call_update', 'toolCallId': 'delete-1',
            'status': 'failed', 'title': 'Delete File', 'rawInput': dict(args),
            'locations': [{'path': args['targetFile']}], '_meta': {'kiro': {'toolOrigin': 'acp'}}})
        return [rejected, hook_event(1)], [decision('delete_file', args, allow=False)]

    def test_bound_prehandle_delete_rejection(self):
        events, rows = self.fixture()
        result = audit(events, rows, CONTEXT)
        self.assertEqual(result['status'], 'pass', result)
        self.assertTrue(result['calls'][0]['denial_proven'])
        self.assertFalse(result['calls'][0]['decision_allow'])

    def test_adversarial_rejections(self):
        for case in ('output', 'empty_output', 'content', 'wrong_location', 'missing_hook',
                     'changed_input', 'completed', 'unknown_tool', 'extra_schema_field', 'wrong_type'):
            with self.subTest(case=case):
                events, rows = self.fixture()
                update = events[0]['params']['update']
                if case == 'output': update['rawOutput'] = {'message': 'deleted'}
                elif case == 'empty_output': update['rawOutput'] = {}
                elif case == 'content': update['content'] = []
                elif case == 'wrong_location': update['locations'][0]['path'] = 'unrelated.txt'
                elif case == 'missing_hook': events.pop()
                elif case == 'changed_input': rows[0]['arguments']['targetFile'] += '.other'
                elif case == 'completed': update['status'] = 'completed'
                elif case == 'unknown_tool': update['title'] = 'Unverified File Tool'
                elif case == 'extra_schema_field':
                    update['rawInput']['unverified'] = True
                    rows[0]['arguments']['unverified'] = True
                elif case == 'wrong_type':
                    update['rawInput']['targetFile'] = []
                    rows[0]['arguments']['targetFile'] = []
                self.assertEqual(audit(events, rows, CONTEXT)['status'], 'fail')
