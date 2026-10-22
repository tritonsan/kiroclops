"""A new recorder must not alter the sealed candidate or original test recipes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.validate_panel_recorder import original_recipes, verify_recorder, validate_sources, run, DRIVER
from kiroclops.visual.schema import write_json, file_sha256, digest
from kiroclops.local_engine.media_session import LIMITS
from kiroclops.web_local.specs import scenarios


class PanelRecorderValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'original.json'
        self.state = {'started_at': 200}
        self.protocol = {'created_at': 100, 'limits': dict(LIMITS), 'heldout': scenarios('panel', 'validation')}
        self.save()

    def save(self):
        write_json(self.path, self.protocol)
        self.prep = {'protocol_sha256': file_sha256(self.path)}

    def recipes(self):
        return original_recipes(self.state, self.prep, self.path)

    def test_only_recorder_changes_original_recipes(self):
        result = self.recipes()
        for new, old in zip(result, self.protocol['heldout']):
            self.assertEqual(new, {**old, 'frame_driver': DRIVER})
        self.assertNotIn('frame_driver', self.protocol['heldout'][0])

    def test_changed_original_inputs_rejected(self):
        self.protocol['heldout'][0]['events'][0]['time_s'] += .1
        write_json(self.path, self.protocol)
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.recipes()

    def test_relaxed_limits_rejected_even_if_bound(self):
        self.protocol['limits']['position_rmse_fraction'] = .5
        self.save()
        with self.assertRaisesRegex(ValueError, 'limits changed'):
            self.recipes()

    def test_late_original_protocol_rejected(self):
        self.protocol['created_at'] = 201
        self.save()
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.recipes()

    def test_missing_case_rejected(self):
        self.protocol['heldout'].pop()
        self.save()
        with self.assertRaisesRegex(ValueError, 'Twelve distinct'):
            self.recipes()

    def setup_qualification(self):
        q = self.root / 'qualification'
        adapter = self.root / 'kiroclops/web_local'
        adapter.mkdir(parents=True)
        (adapter / 'capture.py').write_text('qualified capture')
        (adapter / 'specs.py').write_text('qualified admission')
        gate = {'status': 'pass', 'passed': 7, 'required': 7, 'rows': [{'pass': True, 'errors': []}] * 7}
        write_json(q / 'result.json', gate)
        recipes = self.recipes()
        write_json(q / 'protocol.json', {'limits': LIMITS, 'scenarios': recipes,
                   'adapter_hashes': {'capture.py': file_sha256(adapter / 'capture.py'), 'specs.py': 'before-admission'}})
        bridge = self.root / 'bridge.json'
        write_json(bridge, {'status': 'pass', 'qualified_recorder_behavior_modules_unchanged': True,
                   'changed_adapter_files': ['specs.py'], 'qualification_sha256': file_sha256(q / 'result.json'),
                   'implementation_hashes': {'kiroclops\\web_local\\specs.py': file_sha256(adapter / 'specs.py')}})
        return q, bridge, recipes

    def test_documented_admission_only_is_accepted(self):
        q, bridge, recipes = self.setup_qualification()
        verify_recorder(self.root, q, bridge, recipes)

    def test_capture_implementation_change_rejected(self):
        q, bridge, recipes = self.setup_qualification()
        (self.root / 'kiroclops/web_local/capture.py').write_text('unqualified capture change')
        with self.assertRaisesRegex(ValueError, 'documented specs'):
            verify_recorder(self.root, q, bridge, recipes)

    def test_unqualified_behavior_rejected(self):
        q, bridge, recipes = self.setup_qualification()
        recipes[0]['events'][0]['time_s'] += .123
        with self.assertRaisesRegex(ValueError, 'not covered'):
            verify_recorder(self.root, q, bridge, recipes)

    def test_wrong_candidate_source_rejected(self):
        sources = {role: self.root / role for role in ('reference', 'before', 'candidate')}
        for path in sources.values():
            path.mkdir()
            (path / 'app.js').write_text('current')
        with self.assertRaisesRegex(ValueError, 'candidate source changed'):
            validate_sources(sources, {}, {'source_sha256': 'old-candidate'})

    def test_existing_output_and_nested_output_preserved(self):
        output = self.root / 'old'
        output.mkdir()
        sentinel = output / 'failure.txt'
        sentinel.write_text('historical failure')
        args = [self.root / name for name in ('session', 'protocol', 'reference', 'qualification', 'bridge')]
        with self.assertRaisesRegex(ValueError, 'Fresh output'):
            run(*args, output)
        self.assertEqual(sentinel.read_text(), 'historical failure')
        with self.assertRaisesRegex(ValueError, 'outside original'):
            run(*args, args[0] / 'nested')


if __name__ == '__main__':
    unittest.main()
