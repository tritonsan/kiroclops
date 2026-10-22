"""V5b panel preparation (PHASE3D_V5B_PLAN.md 2.2-2.3) with captures and suite creation faked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_policy
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest, write_json
from scripts import phase3d_v2_prepare as v2p
from scripts import phase3d_v5_prepare as v5p
from scripts import phase3d_v5b_prepare as v5b

from tests.test_phase3d_v5_prepare import FakeCapture, no_lock
from tests.test_suite_web_v3 import calibration_review, panel_record


def preparation(root, kind=v5b.KIND, goal=v5b.GOAL, cases=None):
    previous = Path(root) / 'p' / 'v5p02-cal'
    parent, original = Path(root) / 'parent', Path(root) / 'original'
    for directory, text in ((parent, 'parent'), (original, 'original')):
        directory.mkdir(parents=True)
        (directory / 'index.html').write_text(text, encoding='utf-8')
    cases = v5b.recipes() if cases is None else cases
    records = []
    for case in (c for c in cases if c['role'] == 'calibration'):
        records.append({**case, 'source_role': 'reference', 'record': panel_record(case['scenario'])})
        for role in ('original', 'before'):
            records.append({**case, 'source_role': role, 'record': panel_record(case['scenario'], .75)})
    write_json(previous / 'recipes.json', cases)
    write_json(previous / 'start-binding.json', {'kind': kind, 'stage': 'panel', 'parent': str(parent),
        'original': str(original), 'parent_source_sha256': digest(source_hashes(parent)),
        'original_source_sha256': digest(source_hashes(original)), 'model_started': False,
        'recipes_sha256': digest(cases), 'goal': goal})
    write_json(previous / 'calibration.json', {'status': 'pending_independent_calibration_review', 'stage': 'panel',
        'kind': kind, 'records': records, 'warmups': {'receipt_path': str(previous / 'warmups/receipt.json')},
        'model_started': False})
    return previous


class PrepareV5bTests(unittest.TestCase):
    def setUp(self):
        FakeCapture.calls = []
        self.created = {}

    def fake_suite(self, directory, **kwargs):
        self.created.update(kwargs, directory=directory)
        return {'acceptance_profile': kwargs['acceptance_policy']['id']}

    def fake_session(self, session, suite, parent, runtime):
        Path(session).mkdir(parents=True)

    def freeze(self, root, previous):
        review = calibration_review()
        with patch.object(v2p, 'Capture', FakeCapture), patch.object(v5b, '_lock', no_lock), \
                patch.object(v2p, 'verify_calibration_review', lambda *a: None), \
                patch('kiroclops.local_engine.realistic_review.calibration_review', lambda path: review), \
                patch.object(v5b, 'create_suite', self.fake_suite), \
                patch.object(v5b, 'create_suite_session', self.fake_session), \
                patch.object(v5p, '_reference_project', lambda: Path(root) / 'reference'):
            return v5b.freeze(previous, 'v5p02', Path(root) / 'review', root=Path(root)), review

    def test_goal_is_the_v5_goal_plus_the_v5b_rules(self):
        self.assertTrue(v5b.GOAL.startswith(v5p.GOAL))
        for text in ('30,000 characters', 'Never list .kiroclops-evidence recursively', 'filter or page',
                     'about 10 minutes', '-r1', '-r2', 'a fix must pass both'):
            self.assertIn(text, v5b.GOAL)
        self.assertLess(len(v5b.GOAL), 2700)

    def test_recipes_drop_exactly_six_easy_calibration_repeats(self):
        old, new = v5p.recipes(), v5b.recipes()
        roles = [c['role'] for c in new]
        self.assertEqual((16, 44, 12), (roles.count('calibration'), roles.count('regression'), roles.count('validation')))
        dropped = [c['case_id'] for c in old if c not in new]
        self.assertEqual(sorted(v5b.DROPPED_CALIBRATION), sorted(dropped))
        self.assertTrue(all(case_id.endswith('-r2') for case_id in dropped))
        self.assertEqual([c for c in old if c['role'] != 'calibration'], [c for c in new if c['role'] != 'calibration'])
        kept = {c['case_id'] for c in new if c['role'] == 'calibration'}
        for case_id in ('realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1',
                        'realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2',
                        'realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2',
                        'realistic-v3-suite-panel-calibration-interrupt-w768-r2'):
            self.assertIn(case_id, kept)

    def test_freeze_uses_the_v1_window_rule_and_the_v5b_goal(self):
        with tempfile.TemporaryDirectory() as root:
            result, review = self.freeze(root, preparation(root))
            self.assertEqual('pending_admission', result['status'])
            policy = self.created['acceptance_policy']
            self.assertEqual(realistic_policy.build_policy('panel', review), policy)
            self.assertEqual(realistic_policy.POLICY_ID, policy['id'])
            self.assertEqual(56 * 3, len(FakeCapture.calls))
            self.assertEqual(v5b.GOAL, self.created['goal'])
            self.assertEqual('phase3d-execution-v2', self.created['execution_policy']['policy_id'])
            self.assertEqual(3600, self.created['execution_policy']['max_seconds'])
            self.assertEqual((72, 32, 112), (len(self.created['records']), len(self.created['initial_records']),
                                            len(self.created['baseline_records'])))

    def test_v5_preparations_and_other_goals_or_recipes_are_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, kind=v5p.KIND))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, goal=v5p.GOAL))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, cases=v5p.recipes()))
        self.assertEqual([], FakeCapture.calls)


if __name__ == '__main__':
    unittest.main()
