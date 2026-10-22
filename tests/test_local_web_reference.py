"""Reference fairness/integrity tests; mocked recorder, no browser or model."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import web_reference as subject
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, read_json, write_json, digest, file_sha256


class FrozenWebReferences(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='web-reference-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = {}
        for domain in subject.DOMAINS:
            project = self.root / ('private-' + domain); project.mkdir()
            (project / 'app.js').write_text('private reference implementation ' + domain)
            self.projects[domain] = project
        self.recorder = {'capture.py': 'a' * 64}
        self.calls = []
        self.timing_status = 'pass'; self.functional_status = 'pass'
        self.lock_entries = []
        for name, replacement in (
            ('fixture_project', lambda domain, variant='reference': self.projects[domain]),
            ('scenarios', self.recipes), ('adapter_hashes', lambda: deepcopy(self.recorder)),
            ('_capture', self.capture), ('ROOT', self.root)):
            context = patch.object(subject, name, replacement); context.start(); self.addCleanup(context.stop)

    def recipes(self, domain, split):
        return [{'id': domain + '-' + split, 'domain': domain, 'split': split,
            'viewport': [320, 320], 'device_scale_factor': 1, 'duration_s': 1,
            'events': [], 'timing_limits': {'clock_uncertainty_s': .02}, 'version': 'test-only'}]

    def capture(self, project, output, scenario, check, progress, cancel):
        check(); self.calls.append((scenario['domain'], scenario['split']))
        output.mkdir(parents=True)
        sources = subject.source_hashes(project)
        timing = {'status': self.timing_status, 'errors': []}
        functional = {'status': self.functional_status, 'checks': {'fixture': True}}
        inputs = {'provenance': 'recorder_verified', 'events': []}
        write_json(output / 'capture-start.json', {'variant': 'reference', 'scenario': scenario,
            'source_hashes': sources, 'adapter_hashes': self.recorder})
        write_json(output / 'private/oracle.json', {'functional': functional, 'private_positions': [1, 2]})
        write_json(output / 'private/clock.json', {'private_clock': True})
        raw = output / 'raw.png'
        raw.write_bytes(cv2.imencode('.png', np.zeros((320, 320, 3), np.uint8))[1].tobytes())
        create_package(output / 'package', [raw, raw], [0, .02], inputs=inputs,
            metadata={'domain': scenario['domain'], 'variant': 'reference', 'scenario': scenario,
                'source_hashes': sources, 'adapter_hashes': self.recorder, 'timing': timing})
        write_json(output / 'capture-record.json', {'scenario': scenario, 'timing': timing,
            'inputs': inputs, 'timestamps': [0., .02]})
        write_json(output / 'capture-result.json', {'status': 'ok' if self.timing_status == self.functional_status == 'pass' else 'failed',
            'timing': timing, 'functional': functional, 'source_hashes': sources, 'adapter_hashes': self.recorder})
        return {'scenario': scenario, 'package': str(output / 'package')}

    def prepare(self, name='pack'):
        result = subject.prepare_reference_pack(self.root / name, progress=lambda text: None)
        return Path(result['manifest'])

    def reseal(self, path, value):
        value.pop('pack_sha256', None); value['pack_sha256'] = digest(value); write_json(path, value)

    def test_exact_four_matrices_verify_and_two_arms_receive_same_pixels(self):
        manifest = self.prepare()
        self.assertEqual(self.calls, [('panel', 'calibration'), ('panel', 'validation'), ('svg', 'calibration'), ('svg', 'validation')])
        self.assertEqual(subject.verify_reference_pack(manifest)['verified_records'], 4)
        tools = subject.load_reference_split(manifest, 'panel', 'calibration')
        baseline = subject.load_reference_split(manifest, 'panel', 'calibration')
        self.assertEqual(tools, baseline)
        self.assertEqual(tools[0]['reference_pack_sha256'], file_sha256(manifest))
        self.assertNotIn('analysis', tools[0]); self.assertNotIn('oracle', tools[0])
        self.assertTrue((self.root / 'artifacts/local-engine/capture.lock').is_file())

    def test_never_overwrite_finished_or_failed_output(self):
        self.prepare()
        with self.assertRaises(VisualError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, 'output_exists')

    def test_failed_timing_is_not_sealed_or_retried(self):
        self.timing_status = 'fail'
        with self.assertRaises(VisualError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, 'capture_timing_failure')
        self.assertFalse((self.root / 'pack/manifest.json').exists())
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(read_json(self.root / 'pack/failure.json')['usable_reference_manifest'])

    def test_private_functional_failure_prevents_seal(self):
        self.functional_status = 'fail'
        with self.assertRaises(VisualError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, 'application_integrity_failure')

    def test_moved_pack_retains_relative_source_receipt_and_package_bindings(self):
        old = self.prepare(); new = self.root / 'café package'
        shutil.move(str(old.parent), new)
        self.assertEqual(subject.verify_reference_pack(new / 'manifest.json')['verified_records'], 4)
        self.assertTrue(subject.load_reference_split(new, 'svg', 'validation')[0]['package'].startswith(str(new)))

    def test_calibration_export_contains_no_heldout_or_private_receipts(self):
        path = self.prepare(); output = self.root / 'model-visible'
        result = subject.export_calibration(path, 'panel', output)
        self.assertEqual(result['split'], 'calibration')
        self.assertEqual(len(result['records']), 1)
        self.assertFalse(any(p.name in ('oracle.json', 'app.js', 'capture-result.json') or 'heldout' in p.parts for p in output.rglob('*')))
        with self.assertRaises(VisualError):
            subject.export_calibration(path, 'panel', output)

    def test_changed_pixels_or_private_receipt_are_rejected(self):
        for relative in ('package/frames/000000.png', 'private/oracle.json'):
            with self.subTest(relative=relative):
                path = self.prepare('case-' + relative.split('/')[0]); value = read_json(path)
                capture = path.parent / value['records'][0]['capture_dir']
                (capture / relative).write_bytes(b'changed')
                with self.assertRaises(VisualError) as raised:
                    subject.load_reference_split(path, 'panel', 'calibration')
                self.assertEqual(raised.exception.code, 'reference_pack_changed')

    def test_each_load_hashes_capture_tree_once_and_decodes_package(self):
        path = self.prepare()
        original = subject._tree
        with patch.object(subject, '_tree', wraps=original) as tree, \
             patch.object(subject, 'load_package', wraps=subject.load_package) as package:
            records = subject.load_reference_split(path, 'panel', 'calibration')
        self.assertEqual(tree.call_count, len(records))
        self.assertEqual(package.call_count, len(records))

    def test_foreign_split_package_cannot_be_substituted(self):
        path = self.prepare(); value = read_json(path)
        value['records'][0]['package_dir'] = value['records'][1]['package_dir']
        self.reseal(path, value)
        with self.assertRaises(VisualError) as raised:
            subject.load_reference_split(path, 'panel', 'calibration')
        self.assertEqual(raised.exception.code, 'foreign_reference_package')

    def test_missing_duplicate_and_extra_records_rejected(self):
        for kind in ('missing', 'duplicate', 'extra'):
            with self.subTest(kind=kind):
                path = self.prepare(kind); value = read_json(path)
                if kind == 'missing': value['records'].pop(1)
                elif kind == 'duplicate': value['records'][1] = deepcopy(value['records'][0])
                else: value['records'].append({**deepcopy(value['records'][0]), 'domain': 'foreign'})
                self.reseal(path, value)
                with self.assertRaises(VisualError): subject.verify_reference_pack(path)

    def test_current_source_scenario_and_recorder_expected_hashes_are_required(self):
        path = self.prepare()
        with self.assertRaises(VisualError):
            subject.load_reference_split(path, 'panel', 'calibration', expected_source_hashes={})
        with self.assertRaises(VisualError):
            subject.load_reference_split(path, 'panel', 'calibration', expected_scenarios=[])
        (self.projects['panel'] / 'app.js').write_text('changed')
        with self.assertRaises(VisualError): subject.load_reference_split(path, 'panel', 'calibration')
        self.recorder['capture.py'] = 'b' * 64
        with self.assertRaises(VisualError): subject.load_reference_split(path, 'svg', 'calibration')

    def test_cancellation_has_no_completed_reference_manifest(self):
        with self.assertRaises(VisualError) as raised:
            subject.prepare_reference_pack(self.root / 'cancelled', progress=lambda text: None, cancel=lambda: True)
        self.assertEqual(raised.exception.code, 'cancelled')
        self.assertFalse((self.root / 'cancelled/manifest.json').exists())
        self.assertEqual(self.calls, [])

    def test_manifest_tampering_and_implicit_split_rejected(self):
        path = self.prepare(); value = read_json(path); value['records'][0]['timing']['status'] = 'fail'; write_json(path, value)
        with self.assertRaises(VisualError): subject.load_reference_split(path, 'panel', 'calibration')
        with self.assertRaises(VisualError): subject.load_reference_split(path, 'panel', None)


if __name__ == '__main__':
    unittest.main()
