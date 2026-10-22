"""Regression guards for the verified post-cohort repair; no acceptance relaxation."""
import unittest
from copy import deepcopy
from tests.test_local_native_described_pwsh import ARGS, CONTEXT, OPUS_IDENTITY, audit, call, decision, rejected_call


class RejectedExternalCwdTests(unittest.TestCase):
    def fixture(self, cwd=r'D:\workspace\private\preservation'):
        args = {**ARGS, 'cwd': cwd, 'timeout': None}
        return args, rejected_call(args), [{**decision(args), 'allow': False, 'reason': 'Outside boundary'}]

    def test_exact_rejection_is_denied_not_authorized(self):
        for cwd in (r'D:\workspace\private\preservation', r'D:\workspace\target\..\private',
                    r'E:\private', r'\\server\share\private'):
            args, events, rows = self.fixture(cwd)
            original = deepcopy((events, rows))
            result = audit(events, rows)
            self.assertEqual(result['status'], 'pass', result)
            self.assertFalse(result['calls'][0]['decision_allow'])
            self.assertTrue(result['calls'][0]['denial_proven'])
            self.assertEqual(result['calls'][0]['denial_class'], 'denied_no_output')
            self.assertEqual((events, rows), original)

    def test_executed_external_cwd_does_not_gain_recognition(self):
        args, _, rows = self.fixture()
        args['timeout'] = 120000
        for allowed in (False, True):
            rows = [{**decision(args), 'allow': allowed}]
            self.assertEqual(audit(call(args, identity=OPUS_IDENTITY), rows)['status'], 'fail')

    def test_missing_mismatched_or_allow_decision_cannot_pass(self):
        args, events, rows = self.fixture()
        variants = [[], [decision(args)], rows + deepcopy(rows),
                    [{**rows[0], 'arguments': {**args, 'cwd': args['cwd'].upper()}}],
                    [{**rows[0], 'arguments': {**args, 'command': 'different'}}],
                    [{**rows[0], 'tool': 'read_file'}]]
        for supplied in variants:
            self.assertEqual(audit(events, supplied)['status'], 'fail')
        self.assertEqual(audit(events[1:], rows)['status'], 'fail')

    def test_any_output_or_different_native_shape_rejects(self):
        changes = [('rawOutput', {}), ('rawOutput', {'message': 'private data'}),
                   ('content', []), ('status', 'completed'), ('title', 'Read File'),
                   ('toolCallId', OPUS_IDENTITY), ('_meta', {'kiro': {'toolOrigin': 'client'}})]
        for field, value in changes:
            _, events, rows = self.fixture()
            events[-1]['params']['update'][field] = value
            self.assertEqual(audit(events, rows)['status'], 'fail')

    def test_unverified_runtime_cannot_borrow_rejection_shape(self):
        _, events, rows = self.fixture()
        self.assertEqual(audit(events, rows, {**CONTEXT, 'kas_version': 'unknown'})['status'], 'fail')

    def test_invalid_external_arguments_remain_invalid(self):
        for cwd in ('relative', r'\root-relative', r'C:drive-relative', '', None, 42):
            args, events, rows = self.fixture(cwd)
            if cwd is None:  # Existing schema intentionally accepts omitted/null cwd.
                continue
            self.assertEqual(audit(events, rows)['status'], 'fail')
        for key, value in [('run_in_background', True), ('timeout', 0), ('extra', 'unknown')]:
            args, events, rows = self.fixture()
            events[-1]['params']['update']['rawInput'][key] = value
            rows[0]['arguments'][key] = value
            self.assertEqual(audit(events, rows)['status'], 'fail')
