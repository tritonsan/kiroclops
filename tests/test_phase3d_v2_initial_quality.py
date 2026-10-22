"""Tiny PNG initial-quality fixtures; no application, model or capture."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from scripts import phase3d_v2_initial_quality as initial
from kiroclops.local_engine import media_suite, realistic_policy, suite_web_v2, suite_revision
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class ExistingParentInitialQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='initial quality café naïve ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.project('parent', 'existing successful model source')
        self.original = self.project('original', 'original comparison code')
        self.reference = self.project('reference', 'private reference code')
        self.code = self.root / 'fixture-source.py'; self.code.write_text('fixed fixture code', encoding='utf8')
        self.sources = {'fixture-source.py': file_sha256(self.code)}
        self.patches = [patch.object(media_suite, 'ROOT', self.root),
            patch.object(media_suite, 'analysis_source_hashes', return_value=self.sources),
            patch.object(suite_revision, 'measurement_source_hashes', return_value=self.sources),
            patch.object(suite_revision, 'verify_measurement_record', return_value={'fixture': True}),
            patch.object(initial, '_binding', return_value={'fixture': True})]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        self.image = self.root / 'frame.png'
        cv2.imencode('.png', np.zeros((3, 3, 3), np.uint8))[1].tofile(self.image)
        points = [{'object_id': 'a', 'frame_sha256': digest(['frame', i]), 'error_px': .2,
                   'role': 'reference', 'split': 'calibration', 'independent': True} for i in range(20)]
        self.policy = realistic_policy.build_policy('svg', {'status': 'pass', 'basis': 'independent_raw_pixels', 'points': points})
        self.cases = [{'case_id': scenario['case_id'], 'role': role, 'stage': 'svg', 'purpose': 'primary', 'scenario': scenario}
            for split, role in [('calibration', 'calibration'), ('known_regression', 'regression'), ('validation', 'validation')]
            for scenario in suite_web_v2.scenarios('svg', split)]
        references, cal, known = [], [], []
        for case in self.cases:
            references.append({**case, 'record': self.record(case, 'reference', self.reference)})
            if case['role'] != 'validation':
                pool = cal if case['role'] == 'calibration' else known
                for role, source in [('original', self.original), ('before', self.parent)]:
                    pool.append({'case_id': case['case_id'], 'role': role, 'record': self.record(case, role, source)})
        self.suite_path = self.root / 'suite'
        self.suite = media_suite.create_suite(self.suite_path, domain='svg', stage='svg', records=references,
            required_cases=[{key: case[key] for key in ('case_id', 'role')} for case in self.cases],
            original=self.original, authoring_parent=self.parent, goal='Preserve existing SVG behavior',
            event_contract=suite_web_v2.event_contract('svg'), initial_records=cal, baseline_records=known,
            acceptance_policy=self.policy, execution_policy=resolve_policy(REVISED_ID, 'svg'))
        self.output = self.root / 'quality'

    def project(self, name, value):
        root = self.root / name; (root / 'src').mkdir(parents=True)
        (root / 'index.html').write_text('<main>fixture</main>', encoding='utf8')
        (root / 'src/layout-motion.js').write_text(value, encoding='utf8')
        return root

    def record(self, case, role, source):
        output = self.root / 'record-inputs' / role / case['case_id']
        manifest = create_package(output / 'package', [self.image] * 3, [0., .05, .1],
            {'provenance': 'recorder_verified', 'events': []}, {'fixture': True})
        selected = {'objects': [{'id': 'a', 'roi': [0, 0, 3, 3]}], 'connections': []}
        observed = {'package_sha256': manifest['package_sha256'], 'status': 'ok', 'objects': []}
        events = {'kind': 'EventSet', 'motion': []}
        write_json(output / 'analysis.json', {'observations': observed, 'events': events, 'measurement_status': 'ok'})
        return {'package': str(output / 'package'), 'analysis': str(output / 'analysis.json'),
            'scenario': deepcopy(case['scenario']), 'measurement_status': 'ok', 'observations': observed,
            'events': events, 'selection': selected, 'source_sha256': digest(source_hashes(source)),
            'functional': {'status': 'pass', 'checks_source': 'private_dom_oracle'}}

    @staticmethod
    def compared(passed=True, measurement=True):
        return {'pass': passed, 'score': 0., 'measurement_pass': measurement, 'baseline_measurement_pass': measurement,
                'failure_class': None if passed else 'per_window_pixel_failure' if measurement else 'input_history_mismatch'}

    def run_fixture(self, passed=True, measurement=True):
        value = self.compared(passed, measurement)
        with patch.object(media_suite, '_compare', return_value=deepcopy(value)) as compare, \
             patch.object(media_suite, '_parent_acceptance', return_value=deepcopy(value)), \
             patch.object(media_suite, '_capture', side_effect=AssertionError('No application capture')), \
             patch.object(media_suite, '_checks', side_effect=AssertionError('No new browser functional check')), \
             patch.object(media_suite, 'create_suite_session', side_effect=AssertionError('No session/model candidate')):
            result = initial.run(self.suite_path, self.parent, self.output)
        return result, compare

    def rebind(self, suite):
        write_json(self.suite_path / 'suite.json', suite)
        write_json(self.suite_path / 'suite-binding.json', {'suite_sha256': digest(suite)})

    def test_existing_quality_pass_is_not_new_model_or_full_acceptance(self):
        result, compare = self.run_fixture()
        self.assertEqual('sufficient_initial_quality', result['status'])
        self.assertEqual(16, compare.call_count)
        self.assertEqual({'required': 4, 'compared': 4, 'passed': 4}, result['groups']['calibration'])
        self.assertEqual({'required': 12, 'compared': 12, 'passed': 12}, result['groups']['regression'])
        for key in ('validation_opened', 'new_model_generation', 'new_candidate_created', 'candidate_budget_consumed',
                    'app_or_capture_started', 'new_functional_checks_run', 'full_behavior_acceptance'):
            self.assertFalse(result[key])
        for call in compare.call_args_list:
            self.assertFalse(call.kwargs['require_improvement'])
            self.assertEqual(self.policy, call.kwargs['acceptance_policy'])

    def test_quality_fail_requires_repair_but_measurement_fail_does_not(self):
        result, _ = self.run_fixture(passed=False)
        self.assertEqual('requires_repair', result['status'])
        self.assertTrue(result['repair_required'])
        self.output = self.root / 'quality-measurement-fail'
        result, _ = self.run_fixture(passed=False, measurement=False)
        self.assertEqual('initial_evidence_incomplete', result['status'])
        self.assertFalse(result['repair_required'])

    def test_reader_never_opens_validation_records_or_packages(self):
        forbidden = [case['case_id'] for case in self.cases if case['role'] == 'validation']
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            self.assertFalse(any(name in str(path) for name in forbidden), 'Private validation file opened: ' + str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', new=guarded_open), \
             patch.object(media_suite, 'load_suite', side_effect=AssertionError('Full suite loader reads validation')):
            result, _ = self.run_fixture()
        self.assertEqual('sufficient_initial_quality', result['status'])
        self.assertEqual([], read_json(self.output / 'source-bindings.json')['validation_files_opened'])

    def test_v1_wrong_parent_and_changed_policy_are_refused_before_output(self):
        old = deepcopy(self.suite); old['schema_version'] = 1
        self.rebind(old)
        with self.assertRaises(VisualError) as caught:
            initial.run(self.suite_path, self.parent, self.output)
        self.assertEqual('unsupported_schema', caught.exception.code)
        self.rebind(self.suite)
        with self.assertRaises(VisualError) as caught:
            initial.run(self.suite_path, self.original, self.output)
        self.assertEqual('wrong_authoring_parent', caught.exception.code)
        wrong = deepcopy(self.suite); wrong['acceptance_policy']['rules']['curve_fraction'] = .20
        self.rebind(wrong)
        with self.assertRaises(VisualError):
            initial.run(self.suite_path, self.parent, self.output)
        self.assertFalse(self.output.exists())

    def test_selected_missing_changed_or_cross_role_record_cannot_pass(self):
        case = next(case for case in self.cases if case['role'] == 'calibration')
        selected = next(item for item in self.suite['initial_records'] if item['role'] == 'before' and item['case_id'] == case['case_id'])
        corrupt = deepcopy(self.suite)
        record = next(item for item in corrupt['initial_records'] if item['role'] == 'before' and item['case_id'] == case['case_id'])
        validation = next(item for item in corrupt['cases'] if item['role'] == 'validation')
        record['record']['observations'] = validation['record']['observations']
        # If observations are inline in a future fixture, use the package path
        # as the unequivocal forbidden role pointer.
        record['record']['package'] = validation['record']['package']
        self.rebind(corrupt)
        result, _ = self.run_fixture()
        self.assertEqual('initial_evidence_incomplete', result['status'])
        self.assertEqual('wrong_record_role', result['error']['code'])
        self.assertFalse(result['repair_required'])

    def test_changed_selected_pixel_and_duplicate_source_role_do_not_become_behavior_failures(self):
        case = next(case for case in self.cases if case['role'] == 'calibration')
        pixel = self.suite_path / 'initial/before' / case['case_id'] / 'package/frames/000001.png'
        previous = pixel.read_bytes(); pixel.write_bytes(b'corrupt pixels')
        result, _ = self.run_fixture()
        self.assertEqual('suite_changed', result['error']['code'])
        self.assertFalse(result['repair_required'])
        pixel.write_bytes(previous)
        duplicated = deepcopy(self.suite)
        duplicated['initial_records'].append(deepcopy(next(item for item in duplicated['initial_records']
            if item['case_id'] == case['case_id'] and item['role'] == 'before')))
        self.rebind(duplicated)
        self.output = self.root / 'quality-duplicated'
        result, _ = self.run_fixture()
        self.assertEqual('needs_reference', result['error']['code'])
        self.assertFalse(result['repair_required'])

    def test_outputs_and_every_comparison_are_hashed_and_append_only(self):
        result, _ = self.run_fixture()
        receipt = read_json(self.output / 'result-binding.json')
        self.assertEqual(digest(result), receipt['result_sha256'])
        self.assertEqual(16, sum(name.startswith('comparisons/') for name in receipt['output_file_hashes']))
        before = source_hashes(self.output)
        with self.assertRaises(VisualError) as caught:
            self.run_fixture()
        self.assertEqual('output_exists', caught.exception.code)
        self.assertEqual(before, source_hashes(self.output))


if __name__ == '__main__':
    unittest.main()
