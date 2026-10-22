"""Operator qualification contracts; all native/engine calls are mocked."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.integration import storage
from kiroclops.portable_v2 import qualification as q, runtime, integrity, specs
from kiroclops.portable_v2 import qualification_controls


class NativeCollectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vendor = self.root / 'vendor/heart'
        self.vendor.mkdir(parents=True)
        (self.vendor / 'player.gd').write_text('original controller\n', encoding='utf-8')
        (self.vendor / 'DefaultMovementData.tres').write_text(
            '[resource]\nspeed = 100.0\njump_velocity = -250.0\n', encoding='utf-8')
        self.original = storage.project_hashes(self.vendor)
        self.output = self.root / 'qualification'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(q, 'ROOT', self.root))
        self.source = self.stack.enter_context(patch.object(specs, 'verify_source', return_value={}))
        self.legacy = self.stack.enter_context(patch.object(specs, 'verify_legacy', return_value={}))
        self.copy = self.stack.enter_context(patch.object(storage, 'copy_project', side_effect=self.copy_project))
        self.prepare = self.stack.enter_context(patch.object(runtime, 'prepare_project', side_effect=self.prepare_project))
        self.imported = self.stack.enter_context(patch.object(runtime, 'import_project', return_value={'status': 'completed'}))
        self.capture = self.stack.enter_context(patch.object(runtime, 'capture_project', side_effect=self.capture_project))
        self.checked = self.stack.enter_context(patch.object(integrity, 'run_checks', side_effect=self.run_checks))
        self.overlap = self.stack.enter_context(patch.object(qualification_controls, 'collect_controls',
            side_effect=self.collect_controls))
        self.stack.enter_context(patch.object(runtime, 'protocol_record', return_value={'test_protocol': 1}))
        self.native = self.stack.enter_context(patch.object(runtime.private, 'run_private_desktop',
            side_effect=AssertionError('No actual process is allowed in qualification tests')))

    @staticmethod
    def copy_project(source, destination):
        shutil.copytree(source, destination)
        return Path(destination)

    @staticmethod
    def prepare_project(project):
        (project / 'capture-adapter.txt').write_text('fixture only', encoding='utf-8')

    @staticmethod
    def capture_project(project, run, scenario, *, cancel=None, normal_level=False):
        # Use the actual transport validator to catch unsupported extra fields.
        actual = runtime.canonical_scenario(scenario)
        run.mkdir(parents=True)
        storage.write_json(run / 'input.json', {'scenario': actual, 'normal_level': normal_level})
        return {'raw_frames_sha256': hashlib.sha256(actual['id'].encode()).hexdigest()}

    @staticmethod
    def run_checks(project, baseline, output, stage, *, cancel=None):
        storage.write_json(output / 'comparison.json', {'stage': stage,
            'status': 'pass' if project == baseline else 'fail'})
        return storage.read_json(output / 'comparison.json')

    @staticmethod
    def collect_controls(original_baseline, output, *, progress=print, cancel=None):
        # Qualification helper has separate native tests. This routing fixture
        # must never launch the genuine historical-controller controls.
        path = output / 'controls.json'
        storage.write_json(path, {'fixture': True,
            'original_sources': storage.project_hashes(original_baseline)})
        return str(path)

    def collect(self, **kwargs):
        return q.collect_native(self.output, progress=lambda _: None, **kwargs)

    def test_collects_all_calibration_repeats_and_two_original_map_routes(self):
        result = self.collect()
        calls = self.capture.call_args_list
        self.assertEqual(len(calls), 14)
        calibration = [call for call in calls if not call.kwargs.get('normal_level')]
        actual = [(c.args[2]['id'], c.args[2]['repeat'], c.args[2]['spawn_offset']) for c in calibration]
        self.assertEqual(actual, [(s['id'], s['repeat'], 0) for stage in ('jump', 'ground')
                                 for s in specs.scenarios(stage, 'calibration')])
        self.assertTrue(all(c.args[2]['split'] == 'calibration' for c in calibration))
        self.assertTrue(all('mechanic' not in c.args[2] and 'fps' not in c.args[2] for c in calls))
        for call in calls:
            self.assertEqual(call.args[2]['frame_count'], 240)
            self.assertEqual(call.args[2]['warmup_ticks'], 60)
        normals = [c for c in calls if c.kwargs.get('normal_level')]
        self.assertEqual({c.args[2]['id'] for c in normals}, set(runtime.NORMAL_RECIPES))
        for call in normals:
            scenario = call.args[2]
            self.assertEqual(scenario['split'], 'smoke')
            self.assertEqual([(e['tick'], e['action'], e['pressed']) for e in scenario['events']],
                             list(runtime.NORMAL_RECIPES[scenario['id']]))
        self.assertEqual(result['calibration_root'], str(self.output / 'calibration'))
        self.overlap.assert_called_once()
        self.assertTrue(Path(storage.read_json(result['integrity_receipt'])['wall_coyote_controls']).is_file())
        self.native.assert_not_called()

    def test_original_controller_and_resources_unchanged_negative_controls_are_copies(self):
        result = self.collect()
        baseline = Path(result['baseline'])
        self.assertEqual(storage.project_hashes(self.vendor), self.original)
        baseline_hashes = storage.project_hashes(baseline)
        self.assertTrue(all(baseline_hashes[name] == digest for name, digest in self.original.items()))
        self.assertEqual(len(self.copy.call_args_list), 3)
        self.assertEqual(len(self.checked.call_args_list), 6)
        original_resource = (baseline / 'DefaultMovementData.tres').read_text()
        for label, expected in [('shared-speed', 'speed = 117.0'), ('shared-jump', 'jump_velocity = -275.0')]:
            bad = self.output / 'negative-projects' / label
            changed = storage.project_hashes(bad)
            self.assertEqual([p for p, digest in changed.items() if digest != baseline_hashes[p]],
                             ['DefaultMovementData.tres'])
            self.assertIn(expected, (bad / 'DefaultMovementData.tres').read_text())
            self.assertNotIn(expected, original_resource)
        summary = storage.read_json(result['integrity_receipt'])
        self.assertEqual({item['stage'] for item in summary['baseline']}, {'jump', 'ground'})
        self.assertEqual({(item['stage'], item['kind']) for item in summary['negative_controls']},
                         {(stage, label) for stage in ('jump', 'ground') for label in ('shared-speed', 'shared-jump')})
        for call in self.checked.call_args_list:
            self.assertEqual(call.args[1], baseline)
            self.assertTrue(call.args[2].is_relative_to(self.output / 'integrity'))

    def test_cancel_token_is_forwarded_to_every_native_boundary(self):
        token = threading.Event()
        self.collect(cancel=token)
        for mocked in (self.imported, self.capture, self.checked, self.overlap):
            self.assertTrue(all(call.kwargs['cancel'] is token for call in mocked.call_args_list))

    def test_existing_output_is_rejected_before_source_or_engine_access(self):
        self.output.mkdir()
        with self.assertRaises(storage.IntegrationError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, 'output_exists')
        for mocked in (self.source, self.legacy, self.copy, self.prepare, self.imported, self.capture, self.checked, self.overlap):
            mocked.assert_not_called()

    def test_source_verification_failure_prevents_copy_and_native_calls(self):
        self.source.side_effect = storage.IntegrationError('source_changed', 'fixture source changed')
        with self.assertRaises(storage.IntegrationError):
            self.collect()
        for mocked in (self.copy, self.prepare, self.imported, self.capture, self.checked):
            mocked.assert_not_called()

    def test_cancelled_import_does_not_continue_or_publish_success_receipts(self):
        self.imported.side_effect = runtime.PortableRuntimeError('cancelled', 'test cancellation')
        with self.assertRaises(runtime.PortableRuntimeError):
            self.collect(cancel=threading.Event())
        self.capture.assert_not_called()
        self.checked.assert_not_called()
        self.assertFalse((self.output / 'runtime-receipt.json').exists())
        self.assertFalse((self.output / 'integrity-receipt.json').exists())
        self.assertEqual(storage.project_hashes(self.vendor), self.original)

    def test_capture_error_stops_collection_without_attempting_other_engine_routes(self):
        self.capture.side_effect = runtime.PortableRuntimeError('isolation_error', 'No safe private desktop')
        with self.assertRaises(runtime.PortableRuntimeError):
            self.collect()
        self.assertEqual(self.capture.call_count, 1)
        self.checked.assert_not_called()
        self.native.assert_not_called()
        self.assertFalse((self.output / 'runtime-receipt.json').exists())

    def test_negative_control_requires_exact_pinned_resource_literal(self):
        resource = self.vendor / 'DefaultMovementData.tres'
        resource.write_text('[resource]\nspeed = 100\njump_velocity = -250.0\n')
        with self.assertRaises(storage.IntegrationError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, 'fixture_mismatch')
        self.assertEqual(self.checked.call_count, 2)  # Only the unchanged baselines ran.
        self.assertFalse((self.output / 'integrity-receipt.json').exists())


class QualificationBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(q, 'ROOT', self.root))
        self.stack.enter_context(patch.object(specs, 'CONFIG', self.root / 'config'))

    def test_evidence_map_hashes_actual_bytes_and_changes_after_tampering(self):
        path = self.root / 'proof.json'
        path.write_bytes(b'original')
        before = q._map([path])
        path.write_bytes(b'changed')
        self.assertEqual(before, {'proof.json': hashlib.sha256(b'original').hexdigest()})
        self.assertNotEqual(q._map([path]), before)

    def test_evidence_map_rejects_missing_and_outside_workspace_files(self):
        outside = self.root.parent / (self.root.name + '-outside-proof.json')
        outside.write_bytes(b'outside')
        self.addCleanup(outside.unlink)
        for path in (outside, self.root / 'missing.json'):
            with self.subTest(path=path), self.assertRaises(storage.IntegrationError) as caught:
                q._map([path])
            self.assertEqual(caught.exception.code, 'qualification_binding')

    def test_registered_cohort_blocks_requalification_before_any_evidence_read(self):
        storage.write_json(specs.CONFIG / 'cohorts/registered.json', {'registered': True})
        with patch.object(q, 'read_json', side_effect=AssertionError('No mutable qualification work allowed')):
            with self.assertRaises(storage.IntegrationError) as caught:
                q.qualify('absent-runtime', 'absent-integrity', 'absent-calibration',
                          'absent-controls', 'absent-tests', output=self.root / 'result.json')
        self.assertEqual(caught.exception.code, 'protocol_frozen')
        self.assertFalse((self.root / 'result.json').exists())


class QualificationReceiptReplayTests(unittest.TestCase):
    """Real receipt routing/decisions around mocked measurement/native oracles."""
    copy_project = staticmethod(NativeCollectionTests.copy_project)
    prepare_project = staticmethod(NativeCollectionTests.prepare_project)
    capture_project = staticmethod(NativeCollectionTests.capture_project)
    run_checks = staticmethod(NativeCollectionTests.run_checks)
    collect_controls = staticmethod(NativeCollectionTests.collect_controls)
    collect = NativeCollectionTests.collect

    def setUp(self):
        NativeCollectionTests.setUp(self)
        self.paths = self.collect()
        self.baseline = Path(self.paths['baseline'])
        self.prepared = storage.project_hashes(self.baseline)
        self.stack.enter_context(patch.object(specs, 'CONFIG', self.root / 'config/portable'))
        self.stack.enter_context(patch.object(specs, 'ARTIFACTS', self.root / 'artifacts'))
        self.stack.enter_context(patch.object(specs, 'protocol_sources', return_value={'fixture.py': 'fixed-test-hash'}))
        self.native_record = storage.read_json(self.paths['runtime_receipt'])
        # Explicit source binding is part of the actual collection record.
        self.native_record.update(project=str(self.baseline), prepared_source=self.prepared)
        self.frame_hashes = {'000000.png': '0' * 64}
        for row in self.native_record['captures']:
            run = Path(row['path'])
            value = storage.read_json(run / 'input.json')
            scenario = value['scenario']
            recording = {'fixture_recording': True}
            storage.write_json(run / 'recording.json', recording)
            receipt = run / ('engine.' + runtime.VERSION) / 'bridge.json'
            storage.write_json(receipt, {'fixture_private_source': self.prepared})
            manifest = {'schema_version': 1, 'game': 'heart', 'variant': 'before', 'fps': 60,
                'frame_count': 240, 'viewport': [320, 180], 'warmup_ticks': 60,
                'run_id': run.name, 'scenario': scenario, 'normal_level': value['normal_level'],
                'gameplay': {'status': 'pass'}, 'raw_frames_sha256': runtime._digest(self.frame_hashes),
                'provenance': {'project': str(self.baseline), 'runtime_receipt': str(receipt),
                    'runtime_receipt_sha256': storage.sha256(receipt),
                    'full_source_hashes_before': self.prepared, 'full_source_hashes_after': self.prepared,
                    'recording_sha256': storage.sha256(run / 'recording.json')}}
            storage.write_json(run / 'manifest.json', manifest)
            row['raw_frames_sha256'] = manifest['raw_frames_sha256']
        storage.write_json(self.paths['runtime_receipt'], self.native_record)
        self.integrity_record = storage.read_json(self.paths['integrity_receipt'])
        for kind in ('baseline', 'negative_controls'):
            rows = self.integrity_record[kind]
            for row in rows:
                folder = Path(row['path'])
                project = self.baseline if kind == 'baseline' else self.output / 'negative-projects' / row['kind']
                result = storage.read_json(folder / 'comparison.json')
                result['sources'] = {'baseline': self.prepared, 'candidate': storage.project_hashes(project)}
                storage.write_json(folder / 'comparison.json', result)
                storage.write_json(folder / 'candidate/suite.json', {'source': str(project)})
        self.runtime_verify = self.stack.enter_context(patch.object(runtime, 'verify_runtime_receipt', side_effect=self.verify_native))
        self.integrity_verify = self.stack.enter_context(patch.object(integrity, 'verify_checks', side_effect=self.verify_integrity))
        self.control_verify = self.stack.enter_context(patch.object(qualification_controls, 'verify_controls',
            return_value={'status': 'pass', 'count': 6, 'required': 6, 'cases': []}))
        self.stack.enter_context(patch.object(runtime, '_frames', return_value=self.frame_hashes))
        self.stack.enter_context(patch.object(runtime, 'validate_recording', return_value=None))
        self.stack.enter_context(patch.object(runtime, 'gameplay_checks', return_value={'status': 'pass'}))
        self.stack.enter_context(patch.object(q.measurement, 'reference_runs', side_effect=self.references))
        self.stack.enter_context(patch.object(q.measurement, 'analyze', side_effect=self.analyze))
        self.stack.enter_context(patch.object(q.measurement, 'fit_reference', return_value={'status': 'ok'}))
        self.manual = self.stack.enter_context(patch.object(q, 'validate_observations', return_value={'status': 'pass'}))
        self.stack.enter_context(patch.object(q, 'tracking_negative_controls', return_value={'status': 'pass'}))
        # Hash/path primitive is covered separately above. Do not create fake 7200 reference PNGs.
        self.stack.enter_context(patch.object(q, '_map', return_value={'mocked_file_binding': 'fixture'}))
        self.controls = self.root / 'controls.json'
        storage.write_json(self.controls, {'groups': {stage: {'reference': [], 'before': []} for stage in ('jump', 'ground')}})
        self.tests = self.root / 'tests.json'
        storage.write_json(self.tests, {'status': 'pass', 'failures': 0, 'errors': 0, 'tests_run': 1,
                                       'protocol_sources': specs.protocol_sources()})
        for path in [specs.CONFIG / f'heart-{stage}-annotation.json' for stage in ('jump', 'ground')] + [
                self.root / 'config/tiny-annotation.json', self.root / 'config/phase2b/tiny-annotation.json']:
            storage.write_json(path, {'template_run': str(self.root / 'template'), 'template_frame': 0})

    @staticmethod
    def verify_native(path, *, expected_source_hashes=None, **kwargs):
        return {'status': 'pass' if storage.read_json(path)['fixture_private_source'] == expected_source_hashes else 'fail'}

    @staticmethod
    def verify_integrity(path, *, expected_candidate_sources=None, expected_baseline_sources=None):
        value = storage.read_json(Path(path) / 'comparison.json')
        return {'status': 'pass' if value['sources'] == {'baseline': expected_baseline_sources,
                    'candidate': expected_candidate_sources} else 'fail'}

    @staticmethod
    def references(stage, split, output):
        storage.write_json(output / 'reference.json', {'run_id': str(output)})
        return [str(output / 'reference.json')]

    @staticmethod
    def analyze(run, stage):
        storage.write_json(Path(run) / 'analysis.json', {'run_id': str(run)})

    def qualify(self):
        return q.qualify(self.paths['runtime_receipt'], self.paths['integrity_receipt'],
            self.paths['calibration_root'], self.controls, self.tests, output=self.root / 'qualification-result.json')

    def test_complete_distinct_stage_and_control_receipts_pass_mocked_oracles(self):
        result = self.qualify()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['gates']['integrity_baseline']['count'], 2)
        self.assertEqual(result['gates']['integrity_negative_controls']['count'], 4)
        self.assertEqual(result['gates']['wall_coyote_controls']['status'], 'pass')
        self.control_verify.assert_called_once_with(Path(self.integrity_record['wall_coyote_controls']),
                                                    expected_original_sources=self.prepared)
        self.assertTrue(all(c.kwargs['expected_baseline_sources'] == self.prepared for c in self.integrity_verify.call_args_list))
        self.assertTrue(all(c.kwargs['expected_source_hashes'] == self.prepared for c in self.runtime_verify.call_args_list))
        self.assertTrue(all(c.kwargs['minimum'] == 20 for c in self.manual.call_args_list))

    def test_failed_overlap_controls_cannot_be_hidden_by_other_seven_gates(self):
        self.control_verify.return_value = {'status': 'fail', 'count': 6, 'required': 6,
                                           'reason': 'Known broken wall response was accepted', 'cases': []}
        result = self.qualify()
        self.assertEqual(result['gates']['wall_coyote_controls']['status'], 'fail')
        self.assertEqual(result['status'], 'fail')

    def test_distinct_paths_cannot_replace_missing_ground_baseline_identity(self):
        row = self.integrity_record['baseline'][1]
        row['stage'] = 'jump'
        storage.write_json(self.paths['integrity_receipt'], self.integrity_record)
        comparison = Path(row['path']) / 'comparison.json'
        value = storage.read_json(comparison); value['stage'] = 'jump'; storage.write_json(comparison, value)
        result = self.qualify()
        self.assertEqual(result['gates']['integrity_baseline']['status'], 'fail')
        self.assertEqual(result['status'], 'fail')

    def test_duplicate_control_identity_cannot_count_as_four_qualified_controls(self):
        self.integrity_record['negative_controls'][1]['stage'] = 'jump'
        storage.write_json(self.paths['integrity_receipt'], self.integrity_record)
        result = self.qualify()
        self.assertEqual(result['gates']['integrity_negative_controls']['status'], 'fail')

    def test_same_status_other_source_cannot_pass_baseline_preservation(self):
        row = self.integrity_record['baseline'][0]
        path = Path(row['path']) / 'comparison.json'
        value = storage.read_json(path); value['sources']['candidate']['player.gd'] = 'x' * 64
        storage.write_json(path, value)
        result = self.qualify()
        self.assertEqual(result['gates']['integrity_baseline']['status'], 'fail')

    def test_different_negative_edit_cannot_satisfy_named_control(self):
        row = self.integrity_record['negative_controls'][0]
        path = Path(row['path']) / 'comparison.json'
        value = storage.read_json(path); value['sources']['candidate']['player.gd'] = 'x' * 64
        storage.write_json(path, value)
        result = self.qualify()
        self.assertEqual(result['gates']['integrity_negative_controls']['status'], 'fail')

    def test_manual_pending_stays_failure_even_with_every_automated_gate_pass(self):
        self.manual.return_value = {'status': 'pending'}
        result = self.qualify()
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['gates']['manual_calibration']['status'], 'fail')

    def test_software_test_receipt_from_different_protocol_does_not_pass(self):
        value = storage.read_json(self.tests); value['protocol_sources'] = {'fixture.py': 'stale'}
        storage.write_json(self.tests, value)
        result = self.qualify()
        self.assertEqual(result['gates']['software_tests']['status'], 'fail')

    def test_outer_label_cannot_rename_real_capture_scenario(self):
        self.native_record['captures'][0]['id'] = 'r06'
        storage.write_json(self.paths['runtime_receipt'], self.native_record)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')

    def test_outer_repeat_cannot_rename_real_capture_repeat(self):
        self.native_record['captures'][0]['repeat'] = 3
        storage.write_json(self.paths['runtime_receipt'], self.native_record)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')

    def test_original_map_label_requires_original_map_capture(self):
        row = next(row for row in self.native_record['captures'] if row['id'] == 'normal-slope')
        path = Path(row['path']) / 'manifest.json'
        value = storage.read_json(path); value['normal_level'] = False
        storage.write_json(path, value)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')

    def test_valid_private_receipt_from_other_capture_directory_cannot_be_borrowed(self):
        first, other = [row for row in self.native_record['captures'] if row['id'].startswith('normal-')]
        path = Path(first['path']) / 'manifest.json'
        value = storage.read_json(path)
        borrowed = storage.read_json(Path(other['path']) / 'manifest.json')['provenance']
        value['provenance']['runtime_receipt'] = borrowed['runtime_receipt']
        value['provenance']['runtime_receipt_sha256'] = borrowed['runtime_receipt_sha256']
        storage.write_json(path, value)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')

    def test_manifest_sources_must_match_verified_prepared_baseline(self):
        row = next(row for row in self.native_record['captures'] if row['id'] == 'normal-slope')
        path = Path(row['path']) / 'manifest.json'
        value = storage.read_json(path)
        value['provenance']['full_source_hashes_before']['player.gd'] = 'z' * 64
        storage.write_json(path, value)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')

    def test_calibration_receipt_from_other_run_is_rejected_before_measurement(self):
        first, other = self.native_record['captures'][:2]
        path = Path(first['path']) / 'manifest.json'
        value = storage.read_json(path)
        value['provenance']['runtime_receipt'] = storage.read_json(Path(other['path']) / 'manifest.json')['provenance']['runtime_receipt']
        storage.write_json(path, value)
        with self.assertRaises(storage.IntegrationError) as caught:
            self.qualify()
        self.assertEqual(caught.exception.code, 'wrong_capture')

    def test_repeating_same_native_receipt_cannot_prove_determinism(self):
        first = deepcopy(next(row for row in self.native_record['captures'] if row['id'] == 'h03'))
        self.native_record['captures'] = [deepcopy(first) if row['id'] == 'h03' else row
                                         for row in self.native_record['captures']]
        storage.write_json(self.paths['runtime_receipt'], self.native_record)
        result = self.qualify()
        self.assertEqual(result['gates']['runtime_determinism']['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
