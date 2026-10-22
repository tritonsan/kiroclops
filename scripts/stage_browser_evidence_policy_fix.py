"""Stage and test an exact registered capture-policy reader; no production edits."""
import ast
from copy import deepcopy
import difflib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from kiroclops.local_engine import evidence as e
from kiroclops.visual.schema import read_json, write_json, file_sha256, VisualError
from kiroclops.web_local.specs import scenarios, capture_scenarios, QUALIFIED_CAPTURE_POLICY
from test_local_evidence import MatrixFixture


def staged_function(path):
    tree = ast.parse(Path(path).read_text('utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'required_observation_bindings')
    namespace = dict(e.__dict__)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    # Keep calls through the real module so existing reader/source test doubles
    # and production dependencies retain exactly their normal behavior.
    import types
    value = namespace[node.name]
    result = types.FunctionType(value.__code__, e.__dict__, value.__name__, value.__defaults__)
    result.__kwdefaults__ = value.__kwdefaults__
    return result


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


def main():
    output = ROOT / 'artifacts/phase3a/browser-evidence-policy-compatibility-01'
    assert not output.exists()
    original = Path(e.__file__)
    original_sha = file_sha256(original)
    text = original.read_text('utf-8')
    old = """            from kiroclops.web_local.specs import scenarios
            kind='web';canonical='svg' if domain=='svg' else 'panel'
            calibration=scenarios(canonical,'calibration');validation=scenarios(canonical,'validation')
"""
    new = """            from kiroclops.web_local.specs import scenarios,capture_scenarios
            kind='web';canonical='svg' if domain=='svg' else 'panel'
            policy=experiment.get('capture_policy')
            if policy is None:
                calibration=scenarios(canonical,'calibration');validation=scenarios(canonical,'validation')
            else:
                calibration=capture_scenarios(canonical,'calibration',policy)
                validation=capture_scenarios(canonical,'validation',policy)
"""
    assert text.count(old) == 1
    proposed = text.replace(old, new)
    output.mkdir()
    path = output / 'evidence-proposed.py'
    path.write_text(proposed, 'utf-8')
    (output / 'evidence.patch').write_text(''.join(difflib.unified_diff(
        text.splitlines(True), proposed.splitlines(True), fromfile='kiroclops/local_engine/evidence.py',
        tofile='kiroclops/local_engine/evidence.py')), 'utf-8')
    with tempfile.TemporaryDirectory() as td:
        f = qualified(Path(td))
        try:
            f.check()
        except VisualError as error:
            assert 'Registered browser scenario matrix differs' in str(error)
        else:
            raise AssertionError('Original defect did not reproduce')
    with patch.object(e, 'required_observation_bindings', staged_function(path)):
        suite = unittest.TestSuite()
        for name in ('test_local_evidence', 'test_local_evidence_sources', 'test_local_evidence_report_failure'):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(QualifiedPolicyTests))
        with (output / 'tests.txt').open('w', encoding='utf-8') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    assert file_sha256(original) == original_sha
    write_json(output / 'result.json', {
        'status': 'pass' if result.wasSuccessful() else 'fail', 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'original_defect_reproduced': True, 'production_modified': False,
        'production_evidence_sha256': original_sha, 'proposed_evidence_sha256': file_sha256(path),
        'proposal_builder_sha256': file_sha256(__file__),
        'scope': 'Only use the already registered capture_policy to generate the exact expected scenario. Unknown policies and mutated records remain rejected. No thresholds or source checks weakened.'})
    print({'tests': result.testsRun, 'pass': result.wasSuccessful(), 'production_modified': False}, flush=True)
    return 0 if result.wasSuccessful() else 2


if __name__ == '__main__':
    raise SystemExit(main())
