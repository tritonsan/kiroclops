"""The source review replays V5 native evidence inside the V5 policy stack fixed before the runs."""
from argparse import Namespace
from pathlib import Path
from unittest import mock
import unittest

from kiroclops.local_engine import review
from kiroclops.visual.schema import VisualError
from scripts import phase3d_v3_source_review as source_review


def args(native_v5=False, receipt=None, sha=None, offloads=None, offloads_sha=None):
    return Namespace(native_v5=native_v5, v5_receipt=receipt, v5_receipt_sha256=sha,
                     v5b_offloads=offloads, v5b_offloads_sha256=offloads_sha)


class NativeStackTests(unittest.TestCase):
    def test_v5_stack_installs_the_denial_policy_and_restores_it(self):
        original = review.audit_native_tool_outcomes
        with source_review._environment(False, Path('C:/s'), {'receipt': None, 'receipt_sha256': None}) as ledger:
            self.assertIsNot(original, review.audit_native_tool_outcomes)
            self.assertEqual([], ledger)
        self.assertIs(original, review.audit_native_tool_outcomes)

    def test_v5_stack_uses_the_named_window_receipt(self):
        from contextlib import nullcontext
        from scripts import phase3d_v4_native_environment as environment
        with mock.patch.object(environment, 'environment_policy', return_value=nullcontext()) as policy:
            with source_review._environment(False, Path('C:/s'), {'receipt': 'C:/w/receipt.json', 'receipt_sha256': 'a' * 64}):
                pass
        policy.assert_called_once_with([Path('C:/s')], receipt=Path('C:/w/receipt.json'), receipt_sha256='a' * 64)

    def test_v4_and_v5_cannot_be_combined(self):
        with self.assertRaises(VisualError):
            source_review._environment(True, Path('C:/s'), {'receipt': None, 'receipt_sha256': None})

    def test_command_line_options(self):
        self.assertIsNone(source_review._v5(args()))
        self.assertEqual({'receipt': None, 'receipt_sha256': None}, source_review._v5(args(True)))
        self.assertEqual({'receipt': str(Path('C:/w/r.json')), 'receipt_sha256': 'b' * 64},
                         source_review._v5(args(True, Path('C:/w/r.json'), 'b' * 64)))
        for broken in (args(False, Path('C:/w/r.json'), 'b' * 64), args(True, Path('C:/w/r.json'), None)):
            with self.assertRaises(VisualError):
                source_review._v5(broken)

    def test_v5b_offload_options(self):
        self.assertEqual({'receipt': None, 'receipt_sha256': None, 'offloads': str(Path('C:/o/m.json')),
                          'offloads_sha256': 'c' * 64},
                         source_review._v5(args(True, offloads=Path('C:/o/m.json'), offloads_sha='c' * 64)))
        for broken in (args(False, offloads=Path('C:/o/m.json'), offloads_sha='c' * 64),
                       args(True, offloads=Path('C:/o/m.json'))):
            with self.assertRaises(VisualError):
                source_review._v5(broken)

    def test_v5b_stack_adds_the_offload_policy(self):
        from contextlib import nullcontext
        from scripts import phase3d_v5b_native_offload as offload
        with mock.patch.object(offload, 'offload_policy', return_value=nullcontext()) as policy:
            with source_review._environment(False, Path('C:/s'), {'receipt': None, 'receipt_sha256': None,
                                                                  'offloads': 'C:/o/m.json', 'offloads_sha256': 'c' * 64}):
                pass
        policy.assert_called_once_with(Path('C:/o/m.json'), 'c' * 64)


if __name__ == '__main__':
    unittest.main()
