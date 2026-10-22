"""Small PNG/receipt fixtures; no browser, model, capture or real acceptance."""
from copy import deepcopy
from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import media_suite, suite_initial_acceptance as initial, suite_web
from kiroclops.local_engine.session import copy_project, source_hashes
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from scripts import phase3d_svg_initial_acceptance as cli


class InitialAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.project(self.root / 'parent', 'existing historical motion')
        self.original = self.project(self.root / 'original', 'original motion')
        self.reference = self.project(self.root / 'reference', 'reference motion')
        self.image = self.root / 'image.png'
        cv2.imencode('.png', np.zeros((2, 2, 3), np.uint8))[1].tofile(self.image)
        self.sources = {'fixture-measurement': digest('frozen')}
        self.patches = [patch.object(media_suite, 'analysis_source_hashes', return_value=self.sources),
                        patch.object(initial.suite_revision, 'measurement_source_hashes', return_value=self.sources),
                        patch.object(initial.suite_revision, 'verify_measurement_record', return_value={'verified': True}),
                        patch.object(initial, '_binding', return_value=('fixture',)),
                        patch.object(initial.suite_revision, 'stamp_new_measurement', side_effect=lambda record, *args, **kw: deepcopy(record))]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)
        self.all_cases = [{'case_id': s['case_id'], 'role': role, 'stage': 'svg', 'purpose': 'primary', 'scenario': s}
            for split, role in (('calibration', 'calibration'), ('known_regression', 'regression'), ('validation', 'validation'))
            for s in suite_web.scenarios('svg', split)]
        # Static tests below check all 28 exact recipes. Exercise file-based
        # orchestration with one real tiny PNG package per role; repeating the
        # same mocked renderer 28 times adds no native-capture coverage.
        self.cases = [next(c for c in self.all_cases if c['role'] == role) for role in media_suite.ROLES]
        self.real_scenarios = suite_web.scenarios
        self.scenario_patch = patch.object(suite_web, 'scenarios', side_effect=lambda *args, **kw: self.real_scenarios(*args, **kw)[:1])
        self.scenario_patch.start()
        self.addCleanup(self.scenario_patch.stop)
        self.suite_path = self.root / 'suite'
        records = [{**case, 'record': self.record(self.root / 'reference-records' / case['case_id'], self.reference, case['scenario'])}
                   for case in self.cases]
        self.suite = media_suite.create_suite(self.suite_path, domain='svg', stage='svg', records=records,
            required_cases=[{'case_id': c['case_id'], 'role': c['role']} for c in self.cases],
            original=self.original, authoring_parent=self.parent, goal='Preserve SVG behavior from calibration only.',
            event_contract=suite_web.event_contract('svg'))
        self.historical = self.root / 'historical'
        copy_project(self.parent, self.historical / 'candidates/candidate-02/source')
        copy_project(self.original, self.historical / 'baseline')
        write_json(self.historical / 'session.json', {'domain': 'svg', 'baseline_hashes': source_hashes(self.original),
            'candidates': [{'candidate_id': 'candidate-02',
            'status': 'succeeded', 'source_sha256': digest(source_hashes(self.parent)), 'job_id': 'historical-job'}]})
        self.output = self.root / 'initial'

    @staticmethod
    def project(path, value):
        (path / 'src').mkdir(parents=True)
        (path / 'index.html').write_text('<div>fixture</div>', encoding='utf-8')
        (path / 'src/layout-motion.js').write_text(value, encoding='utf-8')
        return path

    def record(self, path, project, scenario):
        manifest = create_package(path / 'package', [self.image] * 3, [0., .1, .2],
            {'provenance': 'recorder_verified', 'events': []}, {'scenario': scenario, 'source_hashes': source_hashes(project)})
        selection = {'objects': [{'id': 'a', 'roi': [0, 0, 2, 2]}], 'lifecycle_bindings': {}}
        observations = {'package_sha256': manifest['package_sha256'], 'status': 'ok', 'objects': [
            {'id': 'a', 'valid_ratio': 1., 'frames': [{'index': i, 'time_s': i / 10, 'valid': True, 'x': 1., 'y': 1.} for i in range(3)]}]}
        events = {'kind': 'EventSet', 'motion': []}
        write_json(path / 'measurement/analysis.json', {'observations': observations, 'events': events,
            'candidate_sha256': digest(source_hashes(project)), 'measurement_status': 'ok'})
        return {'package': str(path / 'package'), 'analysis': str(path / 'measurement/analysis.json'),
            'scenario': deepcopy(scenario), 'measurement_status': 'ok', 'observations': observations, 'events': events,
            'selection': selection, 'source_sha256': digest(source_hashes(project)), 'functional': {'status': 'pass'},
            'suite_event_contract': suite_web.event_contract('svg'), 'independent_review_contract': suite_web.event_contract('svg'),
            'event_contract_sha256': digest(suite_web.event_contract('svg'))}

    def register(self, output=None, **kwargs):
        return initial.register_initial_acceptance(self.suite_path, self.parent, output or self.output,
            historical_session=self.historical, historical_candidate_id='candidate-02', **kwargs)

    @staticmethod
    def compared(passed=True, failure=None):
        return {'pass': passed, 'score': .01, 'measurement_pass': True, 'baseline_measurement_pass': True,
            'baseline_coverage_failures': [], 'required_order_status': 'pass', 'failure_class': failure,
            'svg_geometry_preservation': {'measurement_status': 'ok', 'pass': passed}}

    def run_fixture(self, *, failure=None, functional_pass=True, capture_side_effect=None):
        def capture(project, case, destination, check, progress, cancel, variant, reference):
            check()
            if capture_side_effect:
                return capture_side_effect(project, case, destination, check, progress, cancel, variant, reference)
            return self.record(destination, project, case['scenario'])

        def compare(reference, original, candidate, stage, *, require_improvement):
            self.assertEqual(stage, 'svg')
            self.assertEqual(reference['scenario'], candidate['scenario'])
            if require_improvement and failure:
                return self.compared(False, failure)
            return self.compared()

        functional = {'status': 'pass' if functional_pass else 'fail', 'checks': [{'id': 'fixture', 'pass': functional_pass}],
                      'source_sha256': digest(source_hashes(self.parent))}
        with patch.object(media_suite, '_capture', side_effect=capture) as captured, \
             patch.object(media_suite, '_compare', side_effect=compare), \
             patch.object(media_suite, '_checks', return_value=functional), \
             patch.object(media_suite, '_parent_acceptance', return_value=self.compared()), \
             patch.object(media_suite, 'create_suite_session', side_effect=AssertionError('No Session allowed')):
            result = initial.run_initial_acceptance(self.output)
        return result, captured.call_args_list

    def exposure(self, *, model_exposure=False, validation_exported=False, exposed=None):
        return initial.attest_validation_exposure(self.output, self.root / 'exposure.json', reviewer='Fixture reviewer',
            inspected_logs=['initial-plan.json', 'run-start.json', 'exposure-log.json', 'result.json', 'result-binding.json'],
            private_evidence=True, model_exposure=model_exposure, validation_exported=validation_exported,
            repair_prompt=self.suite['goal'], exposed_case_ids=exposed or [c['case_id'] for c in self.cases if c['role'] == 'calibration'])

    def test_register_binds_exact_28_cases_and_historical_generation_only(self):
        hashes = source_hashes(self.parent)
        plan = self.register()
        self.assertEqual([sum(c['role'] == role for c in plan['cases']) for role in media_suite.ROLES], [1, 1, 1])
        self.assertEqual(source_hashes(self.output / 'unchanged-source'), hashes)
        self.assertFalse(plan['model_started'])
        self.assertFalse(plan['new_model_generation'])
        self.assertEqual(plan['historical_generation']['historical_candidate_id'], 'candidate-02')
        self.assertNotIn('model_identity', plan)
        self.assertNotIn('candidate_id', plan)
        self.assertEqual(source_hashes(self.parent), hashes)

    def test_wrong_historical_or_changed_parent_is_rejected_before_capture(self):
        state = read_json(self.historical / 'session.json')
        state['candidates'][0]['source_sha256'] = digest('different')
        write_json(self.historical / 'session.json', state)
        with self.assertRaises(VisualError) as caught:
            self.register()
        self.assertEqual(caught.exception.code, 'wrong_historical_source')
        self.assertFalse(self.output.exists())

    def test_original_improvement_denominator_is_the_historical_clean_baseline(self):
        (self.historical / 'baseline/src/layout-motion.js').write_text('a different denominator', encoding='utf-8')
        with self.assertRaises(VisualError) as caught:
            self.register()
        self.assertEqual(caught.exception.code, 'wrong_historical_original')
        self.assertFalse(self.output.exists())

    def test_incomplete_or_changed_protocol_is_not_downgraded(self):
        self.scenario_patch.stop()
        suite = deepcopy(self.suite)
        suite['cases'] = [{**case, 'input_provenance': 'recorder_verified'} for case in self.all_cases]
        self.assertEqual([sum(c['role'] == role for c in initial._matrix(suite)) for role in media_suite.ROLES], [4, 12, 12])
        suite['cases'] = suite['cases'][:-1]
        with self.assertRaises(VisualError) as caught:
            initial._matrix(suite)
        self.assertEqual(caught.exception.code, 'needs_reference')
        suite = deepcopy(self.suite)
        suite['cases'] = [{**case, 'input_provenance': 'recorder_verified'} for case in self.all_cases]
        suite['cases'][0]['scenario']['events'][0]['time_s'] = .5
        with self.assertRaises(VisualError):
            initial._matrix(suite)

    def test_pass_is_numerical_only_and_all_roles_are_preserved(self):
        hashes = source_hashes(self.parent)
        self.register()
        result, captures = self.run_fixture()
        self.assertEqual(result['status'], 'numerical_pass_pending_review')
        self.assertTrue(result['numerical_pass'])
        self.assertFalse(result['full_behavior_acceptance'])
        self.assertFalse(result['repair_preregistration_allowed'])
        self.assertFalse(result['new_model_generation'])
        self.assertEqual(result['candidate_count'], 0)
        self.assertEqual(len(captures), 9)
        self.assertEqual(source_hashes(self.parent), hashes)
        self.assertEqual(source_hashes(self.output / 'unchanged-source'), hashes)
        self.assertEqual(len(initial.required_observation_bindings(self.output)), 12)
        self.assertEqual(initial.load_result(self.output), result)
        self.assertTrue((self.output / 'report.html').is_file())
        with self.assertRaises(VisualError) as caught:
            initial.preregister_repair(self.output, self.root / 'repair')
        self.assertEqual(caught.exception.code, 'repair_not_permitted')

    def test_behavior_failure_is_retained_and_separate_repair_needs_exposure_authority(self):
        self.register()
        result, _ = self.run_fixture(failure='per_window_pixel_failure')
        self.assertEqual(result['status'], 'behavior_fail_pending_review')
        self.assertTrue(result['repair_preregistration_allowed'])
        before = source_hashes(self.output)
        declaration = initial.preregister_repair(self.output, self.root / 'repair')
        self.assertEqual(declaration['reference_status'], 'needs_reference')
        self.assertEqual(declaration['heldout_status'], 'not_provided')
        self.assertFalse(declaration['execution_ready'])
        self.assertFalse(declaration['model_started'])
        self.assertEqual(source_hashes(self.output), before)
        self.assertEqual(initial.load_result(self.output), result)

    def test_operator_scored_private_pool_can_remain_blind_to_repair_model(self):
        self.register()
        result, _ = self.run_fixture(failure='per_window_pixel_failure')
        attestation = self.exposure()
        self.assertTrue(attestation['private_pool_reuse_allowed'])
        declaration = initial.preregister_repair(self.output, self.root / 'repair', exposure_attestation=self.root / 'exposure.json')
        self.assertEqual(declaration['repair_suite_sha256'], digest(self.suite))
        self.assertTrue(declaration['validation_previously_scored'])
        self.assertTrue(declaration['validation_blind_to_repair_model'])
        self.assertFalse(declaration['model_exposure'])
        self.assertEqual(declaration['repair_prompt'], self.suite['goal'])
        self.assertFalse(declaration['execution_ready'])
        self.assertEqual(declaration['initial_result_sha256'], digest(result))

    def test_actual_model_exposure_or_export_requires_other_references(self):
        self.register()
        self.run_fixture(failure='per_window_pixel_failure')
        self.exposure(model_exposure=True)
        declaration = initial.preregister_repair(self.output, self.root / 'repair', exposure_attestation=self.root / 'exposure.json')
        self.assertEqual(declaration['reference_status'], 'needs_reference')
        self.assertFalse(declaration['execution_ready'])
        self.assertTrue(declaration['model_exposure'])
        with self.assertRaises(VisualError) as caught:
            initial.preregister_repair(self.output, self.root / 'repair-explicit', repair_suite_directory=self.suite_path,
                                      exposure_attestation=self.root / 'exposure.json')
        self.assertEqual(caught.exception.code, 'validation_already_seen')

    def test_unknown_geometry_is_measurement_failure_and_cannot_open_repair(self):
        self.register()
        result, _ = self.run_fixture(failure='insufficient_observability')
        self.assertEqual(result['status'], 'measurement_failed')
        self.assertFalse(result['behavior_failure_observed'])
        with self.assertRaises(VisualError):
            initial.preregister_repair(self.output, self.root / 'repair')

    def test_functional_behavior_failure_is_distinct_and_source_bound(self):
        self.register()
        result, _ = self.run_fixture(functional_pass=False)
        self.assertTrue(result['behavior_failure_observed'])
        self.assertFalse(result['numerical_pass'])

    def test_capture_failure_retains_partial_evidence_and_cannot_repair(self):
        self.register()
        def fail(*args):
            raise VisualError('recorder_failed', 'Fixture recorder failed.')
        result, _ = self.run_fixture(capture_side_effect=fail)
        self.assertFalse(result['complete'])
        self.assertEqual(result['code'], 'recorder_failed')
        self.assertFalse(result['repair_preregistration_allowed'])
        self.assertTrue((self.output / 'indexes/reference.json').is_file())
        self.assertEqual(initial.load_result(self.output), result)

    def test_source_change_is_rejected_and_second_run_never_overwrites_first(self):
        self.register()
        self.run_fixture()
        before = source_hashes(self.output)
        with self.assertRaises(VisualError) as caught:
            initial.run_initial_acceptance(self.output)
        self.assertEqual(caught.exception.code, 'initial_already_run')
        self.assertEqual(source_hashes(self.output), before)
        (self.output / 'unchanged-source/src/layout-motion.js').write_text('operator patch', encoding='utf-8')
        with self.assertRaises(VisualError) as caught:
            initial.load_plan(self.output)
        self.assertEqual(caught.exception.code, 'source_changed')

    def test_private_log_tampering_and_changed_repair_prompt_are_rejected(self):
        self.register()
        self.run_fixture(failure='per_window_pixel_failure')
        self.exposure()
        attestation = read_json(self.root / 'exposure.json')
        attestation['repair_prompt'] = 'Use validation failure coordinates to repair.'
        write_json(self.root / 'exposure.json', attestation)
        with self.assertRaises(VisualError) as caught:
            initial.preregister_repair(self.output, self.root / 'repair', exposure_attestation=self.root / 'exposure.json')
        self.assertEqual(caught.exception.code, 'validation_exposure_unknown')
        logs = read_json(self.output / 'exposure-log.json')
        logs['media_exported_by_workflow'] = True
        write_json(self.output / 'exposure-log.json', logs)
        with self.assertRaises(VisualError) as caught:
            initial.load_result(self.output)
        self.assertEqual(caught.exception.code, 'initial_result_changed')

    def test_frozen_baselines_keep_roles_and_are_not_captured_as_new_candidates(self):
        initial_records, fixed = [], []
        for case in self.cases:
            for role, project in (('original', self.original), ('before', self.parent)):
                item = {'case_id': case['case_id'], 'role': role,
                        'record': self.record(self.root / 'cached' / role / case['case_id'], project, case['scenario'])}
                (initial_records if case['role'] == 'calibration' else fixed).append(item)
        self.suite_path = self.root / 'cached-suite'
        self.suite = media_suite.create_suite(self.suite_path, domain='svg', stage='svg',
            records=[{**case, 'record': media_suite.record_from_suite(self.root / 'suite', case)} for case in self.suite['cases']],
            required_cases=self.suite['required_cases'], original=self.original, authoring_parent=self.parent,
            goal=self.suite['goal'], event_contract=suite_web.event_contract('svg'), initial_records=initial_records, baseline_records=fixed)
        self.register()
        result, captures = self.run_fixture()
        self.assertTrue(result['numerical_pass'])
        self.assertEqual(len(captures), 3)
        self.assertTrue(all(args.args[0] == self.output / 'unchanged-source' and args.args[-2] == 'candidate' for args in captures))
        for role in ('before', 'original'):
            self.assertTrue(all(record['initial_acceptance_capture_basis'] == 'retained_frozen_baseline'
                                for record in read_json(self.output / 'indexes' / (role + '.json'))))


class InitialAcceptanceCLITests(unittest.TestCase):
    def test_native_cli_owns_common_lease_and_restores_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            lock = Path(temporary) / 'common-capture.lock'
            runtime = {'environment': {'KIROCLOPS_TEST_DRIVER': 'child'}, 'capture_lock': str(lock)}
            observed = {}
            def measured(path, progress):
                observed.update(driver=os.environ['KIROCLOPS_TEST_DRIVER'], backend=os.environ['KIROCLOPS_ANALYSIS_BACKEND'])
                progress('fixture')
                return {'kind': 'NoCodeChangeInitialAcceptance', 'model_started': False}
            with patch.dict(os.environ, {'KIROCLOPS_TEST_DRIVER': 'parent', 'KIROCLOPS_ANALYSIS_BACKEND': 'previous'}), \
                 patch.object(cli.initial, 'load_plan'), patch.object(cli, 'prepare_runtime', return_value=runtime), \
                 patch.object(cli, '_lock', return_value=nullcontext()) as lease, \
                 patch.object(cli.initial, 'run_initial_acceptance', side_effect=measured):
                result = cli.run(output)
                lease.assert_called_once_with(lock, timeout=60)
                self.assertEqual(observed, {'driver': 'child', 'backend': 'local'})
                self.assertEqual(os.environ['KIROCLOPS_TEST_DRIVER'], 'parent')
                self.assertEqual(os.environ['KIROCLOPS_ANALYSIS_BACKEND'], 'previous')
                self.assertFalse(result['model_started'])

    def test_environment_is_restored_when_native_work_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = {'environment': {'KIROCLOPS_TEST_DRIVER': 'child'}, 'capture_lock': str(Path(temporary) / 'lease')}
            with patch.dict(os.environ, {'KIROCLOPS_TEST_DRIVER': 'parent'}), patch.object(cli.initial, 'load_plan'), \
                 patch.object(cli, 'prepare_runtime', return_value=runtime), patch.object(cli, '_lock', return_value=nullcontext()), \
                 patch.object(cli.initial, 'run_initial_acceptance', side_effect=VisualError('fixture', 'stop')):
                with self.assertRaises(VisualError):
                    cli.run(Path(temporary) / 'output')
                self.assertEqual(os.environ['KIROCLOPS_TEST_DRIVER'], 'parent')


if __name__ == '__main__':
    unittest.main()
