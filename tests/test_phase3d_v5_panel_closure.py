"""V5 panel closure: V5 recipes with the V1 window rule only, and the fixed native policy stack."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import media_suite, realistic_policy, review
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.visual.schema import VisualError, digest, write_json
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v4_prepare as v4p
from scripts import phase3d_v5_panel_closure as closure
from scripts import phase3d_v5_prepare as v5p

from tests.evidence import requires_evidence
from tests.frozen_native_chain import EVIDENCE, REASON, V6_DIVERGENCE
from tests.test_suite_web_v3 import calibration_review


def suite_folder(root, rows=None, **changes):
    cases = [{'case_id': c['case_id'], 'role': c['role'], 'scenario': c['scenario'], 'stage': c['stage'],
              'purpose': c['purpose']} for c in (rows if rows is not None else v5p.recipes())]
    sources = media_suite.analysis_source_hashes(2)
    policy = realistic_policy.build_policy('panel', calibration_review())
    suite = {'kind': 'ReferenceMediaSuite', 'schema_version': 2, 'stage': 'panel', 'calibration_frozen': True,
             'execution_policy': resolve_policy(REVISED_ID, 'panel'), 'cases': cases,
             'required_cases': [{'case_id': c['case_id'], 'role': c['role']} for c in cases],
             'analysis_source_hashes': sources, 'source_epoch': digest(sources), 'goal': v5p.GOAL,
             'acceptance_profile': policy['id'], 'acceptance_policy': policy}
    suite.update(changes)
    folder = Path(root) / 'suite'
    write_json(folder / 'suite.json', suite)
    write_json(folder / 'suite-binding.json', {'suite_sha256': digest(suite)})
    return folder


class SuiteCheckTests(unittest.TestCase):
    def test_v5_recipes_with_the_v1_window_rule_pass(self):
        with tempfile.TemporaryDirectory() as root:
            suite = closure._suite_v5(base.Inputs(), suite_folder(root), 'panel')
            self.assertEqual(78, len(suite['cases']))
            self.assertEqual(realistic_policy.POLICY_ID, suite['acceptance_profile'])

    def test_other_recipes_goal_policy_or_stage_fail(self):
        for kwargs in ({'rows': v4p.recipes()}, {'goal': 'Match panel motion.'},
                       {'acceptance_profile': 'realistic-behavior-v3'}, {'acceptance_policy': None}):
            with self.subTest(kwargs=list(kwargs)), tempfile.TemporaryDirectory() as root:
                rows = kwargs.pop('rows', None)
                with self.assertRaises(VisualError):
                    closure._suite_v5(base.Inputs(), suite_folder(root, rows, **kwargs), 'panel')
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                closure._suite_v5(base.Inputs(), suite_folder(root), 'svg')


class ReaderAndConfigTests(unittest.TestCase):
    @requires_evidence(*EVIDENCE)
    @unittest.skipIf(V6_DIVERGENCE, REASON)
    def test_reader_installs_and_restores_the_policy_stack(self):
        original = (base._suite, review.audit_native_tool_outcomes)
        with self.assertRaises(RuntimeError):
            with closure._reader({'environment_receipt': None, 'environment_receipt_sha256': None}, Path('C:/s')) as (_, ledger):
                self.assertIs(closure._suite_v5, base._suite)
                self.assertIsNot(original[1], review.audit_native_tool_outcomes)
                self.assertEqual([], ledger)
                raise RuntimeError('boom')
        self.assertEqual(original, (base._suite, review.audit_native_tool_outcomes))

    def test_config_schema_is_explicit(self):
        good = {'kind': 'Phase3DV5PanelClosureInputs', 'schema_version': 1,
                'stage': {'stage': 'panel', 'session': 'C:/s', 'source_review': 'C:/r.json', 'final_raw_review': 'C:/f'},
                'calibration': {'preparation': 'C:/p', 'review': 'C:/a'},
                'native': {'environment_receipt': 'C:/w/receipt.json', 'environment_receipt_sha256': 'a' * 64}}
        self.assertEqual(good, closure._config(good))
        none = deepcopy(good)
        none['native'] = {'environment_receipt': None, 'environment_receipt_sha256': None}
        self.assertEqual(none, closure._config(none))
        for broken in ({**good, 'extra': 1}, {**good, 'native': {'environment_receipt': 'C:/w', 'environment_receipt_sha256': None}},
                       {**good, 'stage': {'stage': 'svg', 'session': 'x', 'source_review': 'y', 'final_raw_review': 'z'}},
                       {**good, 'calibration': {}}):
            with self.assertRaises(VisualError):
                closure._config(broken)


if __name__ == '__main__':
    unittest.main()
