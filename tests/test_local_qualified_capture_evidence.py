"""Regression guards for the verified post-cohort repair; no acceptance relaxation."""
import unittest
import tempfile
from copy import deepcopy
from pathlib import Path
from kiroclops.visual.schema import read_json, write_json, VisualError
from kiroclops.web_local.specs import capture_scenarios, QUALIFIED_CAPTURE_POLICY
from tests.test_local_evidence import MatrixFixture


def qualified(root, domain='web_panel', mode='tools'):
    fixture = MatrixFixture(root, domain=domain, mode=mode)
    canonical = 'svg' if domain == 'svg' else 'panel'
    fixture.state['experiment']['capture_policy'] = QUALIFIED_CAPTURE_POLICY
    fixture.state['experiment'].update(
        calibration=capture_scenarios(canonical, 'calibration', QUALIFIED_CAPTURE_POLICY),
        validation=capture_scenarios(canonical, 'validation', QUALIFIED_CAPTURE_POLICY))
    for path in (root / 'measurements').rglob('*.json'):
        rows = read_json(path)
        for row in rows:
            split = row['scenario']['split']
            matches = capture_scenarios(canonical, split, QUALIFIED_CAPTURE_POLICY)
            row['scenario'] = next(r for r in matches if r['id'] == row['scenario']['id'])
        write_json(path, rows)
    return fixture

class QualifiedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_qualified_panel_svg_both_arms_keep_complete_matrix(self):
        for domain in ('web_panel', 'svg'):
            for mode in ('tools', 'baseline'):
                with self.subTest(domain=domain, mode=mode):
                    f = qualified(self.root / (domain + mode), domain, mode)
                    result, calls = f.check()
                    self.assertEqual(48 if mode == 'tools' else 36, len(result))
                    self.assertEqual(48, len(calls))
                    self.assertTrue(all(c[1]['frame_driver'] == 'natural_browser_raf_v1' for c in calls))

    def test_unknown_policy_or_missing_policy_cannot_accept_qualified_records(self):
        for change in ('unknown', 'missing'):
            f = qualified(self.root / change)
            if change == 'missing':
                del f.state['experiment']['capture_policy']
            else:
                f.state['experiment']['capture_policy'] = 'unqualified-policy'
            with self.assertRaises(VisualError):
                f.check()

    def test_qualified_config_mutation_rejected(self):
        changes = [('frame_driver', 'controlled_begin_frame_v1'),
                   ('analysis_policy', 'unexpected'), ('visual_frame_clock', 'unknown'),
                   ('duration_s', 9), ('viewport', [960, 720])]
        for key, value in changes:
            f = qualified(self.root / key, 'svg')
            f.state['experiment']['validation'][0][key] = value
            with self.assertRaisesRegex(VisualError, 'scenario matrix'):
                f.check()

    def test_actual_record_cannot_drop_qualified_driver(self):
        f = qualified(self.root)
        path = self.root / 'measurements/sealed/final.json'
        rows = read_json(path)
        rows[0]['scenario'].pop('frame_driver')
        write_json(path, rows)
        with self.assertRaisesRegex(VisualError, 'scenarios'):
            f.check()

    def test_changed_input_and_duplicate_still_rejected(self):
        for change in ('input', 'duplicate'):
            f = qualified(self.root / change)
            path = f.root / 'measurements/sealed/final.json'
            rows = read_json(path)
            if change == 'input':
                rows[0]['scenario']['events'][0]['time_s'] += .01
            else:
                rows[1] = deepcopy(rows[0])
            write_json(path, rows)
            with self.assertRaisesRegex(VisualError, 'scenarios'):
                f.check()
