"""V5 panel preparation control flow with captures and suite creation faked."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_policy
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from scripts import phase3d_v2_prepare as v2p
from scripts import phase3d_v5_prepare as v5p

from tests.test_suite_web_v3 import calibration_review, panel_record


@contextmanager
def no_lock(*args, **kwargs):
    yield


class FakeCapture:
    calls = []

    def __init__(self, stage, output, root):
        self.output = Path(output)
        self.runtime = {'capture_lock': str(self.output / 'lock')}

    def progress(self, *args, **kwargs):
        pass

    def run(self, project, case, destination, variant='before', reference=None):
        FakeCapture.calls.append((case['case_id'], variant))
        return panel_record(case['scenario'], 1. if variant == 'reference' else .75)

    def close(self):
        pass


def preparation(root, kind=v5p.KIND, goal=v5p.GOAL):
    previous = Path(root) / 'p' / 'v5p-cal'
    parent, original = Path(root) / 'parent', Path(root) / 'original'
    for directory, text in ((parent, 'parent'), (original, 'original')):
        directory.mkdir(parents=True)
        (directory / 'index.html').write_text(text, encoding='utf-8')
    cases = v5p.recipes()
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


class PrepareTests(unittest.TestCase):
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
        with patch.object(v2p, 'Capture', FakeCapture), patch.object(v5p, '_lock', no_lock), \
                patch.object(v2p, 'verify_calibration_review', lambda *a: None), \
                patch('kiroclops.local_engine.realistic_review.calibration_review', lambda path: review), \
                patch.object(v5p, 'create_suite', self.fake_suite), \
                patch.object(v5p, 'create_suite_session', self.fake_session), \
                patch.object(v5p, '_reference_project', lambda: Path(root) / 'reference'):
            return v5p.freeze(previous, 'v5p01', Path(root) / 'review', root=Path(root)), review

    def test_goal_explains_the_window_feedback_and_the_workspace_rules(self):
        for text in ('get_job', 'position_error_before_change', 'full_feedback.path', 'never delete files', '.kiro/'):
            self.assertIn(text, v5p.GOAL)
        self.assertTrue(v5p.GOAL.startswith(v5p.GOALS['panel']))
        self.assertLess(len(v5p.GOAL), 2000)

    def test_recipes_have_the_three_roles(self):
        roles = [c['role'] for c in v5p.recipes()]
        self.assertEqual((22, 44, 12), (roles.count('calibration'), roles.count('regression'), roles.count('validation')))

    def test_freeze_uses_the_v1_window_rule_from_the_calibration_review(self):
        with tempfile.TemporaryDirectory() as root:
            result, review = self.freeze(root, preparation(root))
            self.assertEqual('pending_admission', result['status'])
            policy = self.created['acceptance_policy']
            self.assertEqual(realistic_policy.build_policy('panel', review), policy)
            self.assertEqual(realistic_policy.POLICY_ID, policy['id'])
            self.assertEqual(56 * 3, len(FakeCapture.calls))
            self.assertEqual(v5p.GOAL, self.created['goal'])
            self.assertEqual((78, 44, 112), (len(self.created['records']), len(self.created['initial_records']),
                                            len(self.created['baseline_records'])))
            readiness = read_json(Path(root) / 's' / 'v5p01' / 'environment-readiness.json')
            self.assertEqual('pending_raw_review', readiness['status'])

    def test_other_preparations_or_changed_recipes_are_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, kind='V4PanelPreparation'))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, goal='Match panel motion.'))
        with tempfile.TemporaryDirectory() as root:
            previous = preparation(root)
            cases = read_json(previous / 'recipes.json')
            cases[-1]['scenario']['events'][0]['time_s'] = .41
            write_json(previous / 'recipes.json', cases)
            binding = read_json(previous / 'start-binding.json')
            binding['recipes_sha256'] = digest(cases)
            write_json(previous / 'start-binding.json', binding)
            with self.assertRaises(VisualError):
                self.freeze(root, previous)
        self.assertEqual([], FakeCapture.calls)


if __name__ == '__main__':
    unittest.main()
