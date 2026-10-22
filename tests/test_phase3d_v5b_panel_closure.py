"""V5b panel closure: V5b recipes and goal with the V1 window rule, and the V5b native policy stack."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kiroclops.local_engine import realistic_policy, review
from kiroclops.visual.schema import VisualError
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v5_prepare as v5p
from scripts import phase3d_v5b_native_offload as offload
from scripts import phase3d_v5b_panel_closure as closure
from scripts import phase3d_v5b_prepare as v5b

from tests.evidence import requires_evidence
from tests.frozen_native_chain import EVIDENCE, REASON, V6_DIVERGENCE
from tests.test_phase3d_v5_panel_closure import suite_folder


class SuiteCheckTests(unittest.TestCase):
    def test_v5b_recipes_and_goal_with_the_v1_window_rule_pass(self):
        with tempfile.TemporaryDirectory() as root:
            suite = closure._suite_v5b(base.Inputs(), suite_folder(root, v5b.recipes(), goal=v5b.GOAL), 'panel')
            self.assertEqual(72, len(suite['cases']))
            self.assertEqual(realistic_policy.POLICY_ID, suite['acceptance_profile'])

    def test_v5_recipes_or_goal_and_other_rules_fail(self):
        for rows, changes in ((v5p.recipes(), {'goal': v5b.GOAL}), (v5b.recipes(), {'goal': v5p.GOAL}),
                              (v5b.recipes(), {'goal': None}),
                              (v5b.recipes(), {'goal': v5b.GOAL, 'acceptance_profile': 'realistic-behavior-v3'})):
            with self.subTest(goal=changes.get('goal', '')[:20] if changes.get('goal') else None, rows=len(rows)), \
                    tempfile.TemporaryDirectory() as root:
                with self.assertRaises(VisualError):
                    closure._suite_v5b(base.Inputs(), suite_folder(root, rows, **changes), 'panel')


class ReaderAndConfigTests(unittest.TestCase):
    def native(self, **changes):
        value = {'environment_receipt': None, 'environment_receipt_sha256': None, 'offloads': None, 'offloads_sha256': None}
        value.update(changes)
        return value

    @requires_evidence(*EVIDENCE)
    @unittest.skipIf(V6_DIVERGENCE, REASON)
    def test_reader_installs_and_restores_the_policy_stack(self):
        original = (base._suite, review.audit_native_tool_outcomes, review._builtin_227_v3_offload_output)
        with self.assertRaises(RuntimeError):
            with closure._reader(self.native(), Path('C:/s')) as (_, ledger, verified):
                self.assertIs(closure._suite_v5b, base._suite)
                self.assertIsNot(original[1], review.audit_native_tool_outcomes)
                self.assertEqual(([], []), (ledger, verified))
                raise RuntimeError('boom')
        self.assertEqual(original, (base._suite, review.audit_native_tool_outcomes, review._builtin_227_v3_offload_output))

    @requires_evidence(*EVIDENCE)
    @unittest.skipIf(V6_DIVERGENCE, REASON)
    def test_reader_applies_the_named_offload_manifest(self):
        with mock.patch.object(offload, 'offload_policy', return_value=nullcontext(['row'])) as policy:
            with closure._reader(self.native(offloads='C:/o/manifest.json', offloads_sha256='c' * 64),
                                 Path('C:/s')) as (_, _ledger, verified):
                self.assertEqual(['row'], verified)
        policy.assert_called_once_with(Path('C:/o/manifest.json'), 'c' * 64)

    def test_config_schema_is_explicit(self):
        good = {'kind': 'Phase3DV5bPanelClosureInputs', 'schema_version': 1,
                'stage': {'stage': 'panel', 'session': 'C:/s', 'source_review': 'C:/r.json', 'final_raw_review': 'C:/f'},
                'calibration': {'preparation': 'C:/p', 'review': 'C:/a'},
                'native': self.native(environment_receipt='C:/w/receipt.json', environment_receipt_sha256='a' * 64,
                                      offloads='C:/o/manifest.json', offloads_sha256='b' * 64)}
        self.assertEqual(good, closure._config(good))
        none = deepcopy(good)
        none['native'] = self.native()
        self.assertEqual(none, closure._config(none))
        for broken in ({**good, 'kind': 'Phase3DV5PanelClosureInputs'},
                       {**good, 'native': self.native(offloads='C:/o/manifest.json')},
                       {**good, 'native': {'environment_receipt': None, 'environment_receipt_sha256': None}},
                       {**good, 'stage': {'stage': 'svg', 'session': 'x', 'source_review': 'y', 'final_raw_review': 'z'}}):
            with self.assertRaises(VisualError):
                closure._config(broken)


if __name__ == '__main__':
    unittest.main()
