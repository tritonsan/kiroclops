"""Remeasuring old pixels must not silently adopt a changed recorder."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.qualification import requalify_web_corpus
from kiroclops.visual.schema import VisualError, read_json, write_json


class RecordingBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'old'
        self.output = self.root / 'new'
        self.adapter = {name: 'original-' + name for name in (
            '__init__.py', 'capture.py', 'oracle.py', 'specs.py',
            'clock_runtime.py', 'transport.py', 'selection.py')}
        self.project = {'src/app.js': 'unchanged-source'}
        self.index = {'references': [{'record': {'package': str(self.root / 'pixels')},
            'capture': {'source_hashes': self.project, 'adapter_hashes': self.adapter}}], 'cases': []}
        write_json(self.source / 'capture-index.json', self.index)
        write_json(self.source / 'registration.json', {'controls': ['bound-control']})
        write_json(self.source / 'manifest.json', {'status': 'fail'})
        self.metadata = {'metadata': {'source_hashes': self.project, 'adapter_hashes': self.adapter}}

    def call(self, current):
        with patch('kiroclops.web_local.capture.adapter_hashes', return_value=current), \
             patch('kiroclops.visual.package.load_package', return_value=self.metadata), \
             patch('kiroclops.local_engine.qualification.analyzer_sources', return_value={'analysis.py': 'new'}), \
             patch('kiroclops.local_engine.qualification.measure_web_corpus', return_value={'status': 'pending_review'}) as measure:
            result = requalify_web_corpus(self.source, self.output)
            measure.assert_called_once()
            return result

    def reject(self, current):
        with self.assertRaises(VisualError) as raised:
            self.call(current)
        self.assertEqual(raised.exception.code, 'source_changed')
        self.assertFalse(self.output.exists())
        self.assertEqual(read_json(self.source / 'manifest.json'), {'status': 'fail'})

    def test_clock_runtime_change_requires_new_native_capture(self):
        current = {**self.adapter, 'clock_runtime.py': 'changed-clock'}
        self.reject(current)

    def test_transport_change_requires_new_native_capture(self):
        current = {**self.adapter, 'transport.py': 'changed-delivery-order'}
        self.reject(current)

    def test_new_recording_dependency_is_not_silently_ignored(self):
        self.reject({**self.adapter, 'new_capture_helper.py': 'new-source'})

    def test_removed_recording_dependency_is_not_silently_ignored(self):
        current = deepcopy(self.adapter)
        current.pop('transport.py')
        self.reject(current)

    def test_only_pixel_selection_can_change_without_rewriting_history(self):
        current = {**self.adapter, 'selection.py': 'new-image-selection'}
        self.assertEqual(self.call(current), {'status': 'pending_review'})
        self.assertEqual(read_json(self.output / 'capture-index.json'), self.index)
        registration = read_json(self.output / 'registration.json')
        self.assertEqual(registration['reuses_immutable_captures_from'], str(self.source))
        self.assertEqual(read_json(self.source / 'manifest.json'), {'status': 'fail'})


if __name__ == '__main__':
    unittest.main()
