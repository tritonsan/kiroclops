"""Frozen reference pixels stay identical across arms and scoped to sealed splits."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.evaluation import _reference_web_matrix, _run_web
from kiroclops.visual.package import create_package, copy_package, load_package
from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json


class FrozenReferenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='frozen reference naïve ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        frame = self.root/'raw.png'
        frame.write_bytes(cv2.imencode('.png', np.zeros((20, 30, 3), dtype=np.uint8))[1].tobytes())
        self.package = self.root/'frozen-package'
        create_package(self.package, [frame, frame], [0., .02])
        self.bundle = self.root/'reference-bundle.json'
        write_json(self.bundle, {'kind': 'OpaqueLoaderFixture'})
        self.state = {'reference': 'private-project', 'reference_hashes': {'src/panel-motion.js': 'fixed-source'},
                      'mode': 'baseline', 'domain': 'web_panel', 'baseline': 'initial-project',
                      'experiment': {'web_reference_bundle': str(self.bundle),
                                     'web_reference_bundle_sha256': file_sha256(self.bundle)}}
        self.raw = {'scenario': {'id': 'one', 'domain': 'panel'}, 'package': str(self.package),
                    'timing': {'status': 'pass'}, 'functional': {'status': 'pass'},
                    'source_sha256': 'source', 'reference_pack_sha256': 'frozen-pack'}
        self.callbacks = (lambda: None, lambda *a: None, lambda: False)

    def test_two_arms_copy_exact_same_pixels_and_only_tools_analyzes(self):
        with patch('kiroclops.local_engine.web_reference.load_reference_split', return_value=[self.raw]) as loader, \
             patch('kiroclops.local_engine.evaluation._measure_record', side_effect=lambda row, *args: {**row, 'measurement_status': 'ok'}) as measure, \
             patch('kiroclops.local_engine.evaluation._web_matrix', side_effect=AssertionError('Frozen reference must not be recaptured')):
            baseline = _reference_web_matrix(self.state, self.root/'baseline-copy', 'panel', 'calibration', *self.callbacks, analyze=False)
            measure.assert_not_called()
            tools = _reference_web_matrix(self.state, self.root/'tools-copy', 'panel', 'calibration', *self.callbacks, analyze=True)
        self.assertEqual(1, measure.call_count)
        expected = load_package(self.package)['package_sha256']
        for row in (baseline[0], tools[0]):
            self.assertEqual(expected, load_package(row['package'])['package_sha256'])
            self.assertNotEqual(str(self.package), row['package'])
            self.assertEqual('frozen-pack', row['reference_pack_sha256'])
        self.assertNotIn('measurement_status', baseline[0])
        self.assertNotIn('measurement_status', self.raw)
        self.assertEqual(self.state['reference_hashes'], loader.call_args.kwargs['expected_source_hashes'])
        self.assertTrue(all(s['split'] == 'calibration' for s in loader.call_args.kwargs['expected_scenarios']))

    def test_missing_or_changed_bound_manifest_rejected_before_loading_or_copy(self):
        for change in ('missing-binding', 'changed-manifest'):
            with self.subTest(change=change):
                state = deepcopy(self.state)
                if change == 'missing-binding':
                    state['experiment'].pop('web_reference_bundle_sha256')
                else:
                    state['experiment']['web_reference_bundle_sha256'] = 'changed'
                with patch('kiroclops.local_engine.web_reference.load_reference_split') as loader, \
                     self.assertRaises(VisualError) as caught:
                    _reference_web_matrix(state, self.root/change, 'panel', 'calibration', *self.callbacks, analyze=False)
                self.assertEqual('reference_bundle_changed', caught.exception.code)
                loader.assert_not_called()
                self.assertFalse((self.root/change).exists())

    def test_bundle_change_during_local_copy_is_rejected(self):
        def changed_loader(*args, **kwargs):
            write_json(self.bundle, {'kind': 'DifferentPack'})
            return [self.raw]
        with patch('kiroclops.local_engine.web_reference.load_reference_split', side_effect=changed_loader), \
             self.assertRaises(VisualError) as caught:
            _reference_web_matrix(self.state, self.root/'changed-copy', 'panel', 'calibration', *self.callbacks, analyze=False)
        self.assertEqual('reference_bundle_changed', caught.exception.code)

    def test_validation_is_not_loaded_until_sealed(self):
        with patch('kiroclops.local_engine.web_reference.load_reference_split', return_value=[self.raw]) as loader:
            with self.assertRaises(VisualError) as caught:
                _reference_web_matrix(self.state, self.root/'unsealed', 'panel', 'validation', *self.callbacks, analyze=False)
            self.assertEqual('not_sealed', caught.exception.code)
            loader.assert_not_called()
            sealed = {**self.state, 'sealed_at': 1.}
            _reference_web_matrix(sealed, self.root/'sealed', 'panel', 'validation', *self.callbacks, analyze=False)
            self.assertEqual('validation', loader.call_args.args[2])
            self.assertTrue(all(s['split'] == 'validation' for s in loader.call_args.kwargs['expected_scenarios']))

    def test_baseline_prepare_binds_actual_local_raw_packages_without_profile(self):
        session = self.root/'session'
        job = {'job_id': 'prepare-id', 'operation': 'prepare'}
        output = session/'measurements'/job['job_id']
        def capture_before(project, destination, domain, split, *args, **kwargs):
            self.assertEqual('initial-project', project)
            self.assertFalse(kwargs['analyze'])
            package = Path(destination)/'one/package'
            copy_package(self.package, package)
            return [{**self.raw, 'package': str(package)}]
        with patch('kiroclops.local_engine.web_reference.load_reference_split', return_value=[self.raw]) as loader, \
             patch('kiroclops.local_engine.evaluation._web_matrix', side_effect=capture_before) as capture, \
             patch('kiroclops.local_engine.evaluation._measure_record', side_effect=AssertionError('Baseline CV forbidden')), \
             patch('kiroclops.local_engine.evaluation._profile', side_effect=AssertionError('Baseline profile forbidden')):
            result = _run_web(session, job, self.state, *self.callbacks)
        self.assertEqual(1, capture.call_count)
        self.assertEqual('calibration', loader.call_args.args[2])
        self.assertNotIn('profile', result)
        self.assertEqual(file_sha256(session/'prepared-evidence.json'), result['prepared_evidence_sha256'])
        receipt = read_json(session/'prepared-evidence.json')
        self.assertEqual({'reference', 'before'}, set(receipt['indexes']))
        for index in receipt['indexes'].values():
            recorded = index['signature']['records'][0]
            self.assertEqual(load_package(self.package)['package_sha256'], recorded['package_sha256'])
            self.assertIsNone(recorded['observations_sha256'])
            self.assertTrue((output/recorded['package']).is_dir())
        self.assertTrue(Path(result['reference_media'][0]['package']).is_relative_to(session/'target/.kiroclops-evidence'))

    def test_direct_engineering_without_bundle_preserves_original_capture(self):
        state = {key: value for key, value in self.state.items() if key != 'experiment'}
        with patch('kiroclops.local_engine.evaluation._web_matrix', return_value=[self.raw]) as capture:
            result = _reference_web_matrix(state, self.root/'old-path', 'panel', 'calibration', *self.callbacks, analyze=False)
        self.assertEqual([self.raw], result)
        # No bundle and no capture policy: the original scenario recipes are used.
        capture.assert_called_once_with('private-project', self.root/'old-path', 'panel', 'calibration', *self.callbacks, 'reference',
                                        analyze=False, capture_policy=None)


if __name__ == '__main__':
    unittest.main()
