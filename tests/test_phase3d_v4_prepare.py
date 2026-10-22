"""V4 panel preparation control flow with captures and suite creation faked."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_policy
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from scripts import phase3d_v2_prepare as v2p
from scripts import phase3d_v4_prepare as v4p

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


def preparation(root, kind=v4p.KIND):
    previous = Path(root) / 'p' / 'v4p-cal'
    parent, original = Path(root) / 'parent', Path(root) / 'original'
    for directory, text in ((parent, 'parent'), (original, 'original')):
        directory.mkdir(parents=True)
        (directory / 'index.html').write_text(text, encoding='utf-8')
    cases = v4p.recipes()
    records = []
    for case in (c for c in cases if c['role'] == 'calibration'):
        records.append({**case, 'source_role': 'reference', 'record': panel_record(case['scenario'])})
        for role in ('original', 'before'):
            records.append({**case, 'source_role': role, 'record': panel_record(case['scenario'], .75)})
    write_json(previous / 'recipes.json', cases)
    write_json(previous / 'start-binding.json', {'kind': kind, 'stage': 'panel', 'parent': str(parent),
        'original': str(original), 'parent_source_sha256': digest(source_hashes(parent)),
        'original_source_sha256': digest(source_hashes(original)), 'model_started': False,
        'recipes_sha256': digest(cases)})
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
        with patch.object(v2p, 'Capture', FakeCapture), patch.object(v4p, '_lock', no_lock), \
                patch.object(v2p, 'verify_calibration_review', lambda *a: None), \
                patch('kiroclops.local_engine.realistic_review.calibration_review', lambda path: review), \
                patch.object(v4p, 'create_suite', self.fake_suite), \
                patch.object(v4p, 'create_suite_session', self.fake_session), \
                patch.object(v4p, '_reference_project', lambda: Path(root) / 'reference'):
            return v4p.freeze(previous, 'v4p01', Path(root) / 'review', root=Path(root)), review

    def test_recipes_have_the_three_roles(self):
        roles = [c['role'] for c in v4p.recipes()]
        self.assertEqual((16, 36, 12), (roles.count('calibration'), roles.count('regression'), roles.count('validation')))

    def test_freeze_uses_the_v1_window_rule_from_the_calibration_review(self):
        with tempfile.TemporaryDirectory() as root:
            result, review = self.freeze(root, preparation(root))
            self.assertEqual('pending_admission', result['status'])
            policy = self.created['acceptance_policy']
            self.assertEqual(realistic_policy.build_policy('panel', review), policy)
            self.assertEqual(realistic_policy.POLICY_ID, policy['id'])
            self.assertEqual(48 * 3, len(FakeCapture.calls))
            self.assertEqual((64, 32, 96), (len(self.created['records']), len(self.created['initial_records']),
                                            len(self.created['baseline_records'])))
            readiness = read_json(Path(root) / 's' / 'v4p01' / 'environment-readiness.json')
            self.assertEqual('pending_raw_review', readiness['status'])

    def test_other_preparations_or_changed_recipes_are_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, kind='V3PanelPreparation'))
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
