"""V3 panel preparation control flow with captures and suite creation faked."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import realistic_policy, realistic_policy_v3
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from scripts import phase3d_v2_prepare as v2p
from scripts import phase3d_v3_prepare as v3p

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
        FakeCapture.calls.append((case['case_id'], variant, (self.output / 'base-policy.json').is_file()))
        return panel_record(case['scenario'], 1. if variant == 'reference' else .75)

    def close(self):
        pass


def preparation(root, kind=v3p.KIND):
    previous = Path(root) / 'p' / 'v3p-cal'
    parent, original = Path(root) / 'parent', Path(root) / 'original'
    for directory, text in ((parent, 'parent'), (original, 'original')):
        directory.mkdir(parents=True)
        (directory / 'index.html').write_text(text, encoding='utf-8')
    cases = v3p.recipes()
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
        with patch.object(v2p, 'Capture', FakeCapture), patch.object(v3p, '_lock', no_lock), \
                patch.object(v2p, 'verify_calibration_review', lambda *a: None), \
                patch('kiroclops.local_engine.realistic_review.calibration_review', lambda path: review), \
                patch.object(v3p, 'create_suite', self.fake_suite), \
                patch.object(v3p, 'create_suite_session', self.fake_session), \
                patch.object(v3p, '_reference_project', lambda: Path(root) / 'reference'):
            return v3p.freeze(previous, 'v3p01', Path(root) / 'review', root=Path(root)), review

    def test_recipes_have_the_three_roles_and_unique_ids(self):
        cases = v3p.recipes()
        roles = [c['role'] for c in cases]
        self.assertEqual((12, 24, 12), (roles.count('calibration'), roles.count('regression'), roles.count('validation')))
        self.assertEqual(len(cases), len({c['case_id'] for c in cases}))
        self.assertTrue(all(c['stage'] == 'panel' and c['purpose'] == 'primary' for c in cases))

    def test_freeze_fixes_b_first_then_binds_retest_floors(self):
        with tempfile.TemporaryDirectory() as root:
            result, review = self.freeze(root, preparation(root))
            output = Path(root) / 'p' / 'v3p01'
            self.assertEqual('pending_admission', result['status'])
            # Every private capture happened after the base policy existed on disk.
            self.assertEqual(36 * 3, len(FakeCapture.calls))
            self.assertTrue(all(written for _, _, written in FakeCapture.calls))
            base = read_json(output / 'base-policy.json')
            self.assertFalse(base['private_references_captured'])
            self.assertEqual(realistic_policy.build_policy('panel', review), base['policy'])
            policy = self.created['acceptance_policy']
            self.assertIs(policy, realistic_policy_v3.validate_policy(policy, domain='panel'))
            self.assertEqual(base['policy'], policy['base_policy'])
            self.assertEqual(policy['policy_sha256'], result['acceptance_policy_sha256'])
            self.assertEqual(24, len(policy['retest_pairs']))
            self.assertEqual((48, 24, 72), (len(self.created['records']), len(self.created['initial_records']),
                                            len(self.created['baseline_records'])))
            # suite_readiness rebuilds the policy from only these three fields.
            minimal = {item['case_id']: {key: item['record'][key] for key in ('scenario', 'observations', 'selection')}
                       for item in self.created['records']}
            self.assertEqual(policy, realistic_policy_v3.build_policy('panel', review, minimal))
            summary = read_json(output / 'retest-floors.json')
            self.assertEqual(len(policy['retest_floors_px']), summary['windows'])
            self.assertEqual(0, summary['windows_where_2R_exceeds_2px'])
            readiness = read_json(Path(root) / 's' / 'v3p01' / 'environment-readiness.json')
            self.assertEqual('pending_raw_review', readiness['status'])

    def test_v2_preparation_cannot_be_frozen_as_v3(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.freeze(root, preparation(root, kind='V2Something'))
            self.assertFalse((Path(root) / 'p' / 'v3p01').exists())
            self.assertEqual([], FakeCapture.calls)

    def test_changed_recipes_are_rejected_before_any_capture(self):
        with tempfile.TemporaryDirectory() as root:
            previous = preparation(root)
            cases = read_json(previous / 'recipes.json')
            cases[0]['scenario']['events'][0]['time_s'] = .41
            write_json(previous / 'recipes.json', cases)
            binding = read_json(previous / 'start-binding.json')
            binding['recipes_sha256'] = digest(cases)
            write_json(previous / 'start-binding.json', binding)
            with self.assertRaises(VisualError):
                self.freeze(root, previous)
            self.assertEqual([], FakeCapture.calls)

    def test_review_records_never_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'rows.json'
            target.write_text('[]', encoding='utf-8')
            with self.assertRaises(VisualError):
                v3p.review_records(preparation(root), target)


if __name__ == '__main__':
    unittest.main()
