"""Calibration-only advice: no capture, model, source mutation or held-out reads."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.calibration_planning import recommend_calibration, advice_for_job, inspect_advice
from kiroclops.local_engine.session import JOB_BINDING_FIELDS
from kiroclops.visual.schema import VisualError, digest, write_json, file_sha256

SOURCE = 'a' * 64


def case(name, selectors, repeat=1):
    return {'id': name, 'split': 'calibration', 'repeat': repeat,
            'events': [{'time_s': .4 + i, 'action': 'click', 'selector': selector}
                       for i, selector in enumerate(selectors)]}


def receipt(scenario, *, passed=True, status='ok', source=SOURCE):
    return {'case_key': digest(scenario), 'source_sha256': source,
            'measurement_status': status, 'analysis_sha256': 'b' * 64,
            'package_sha256': 'c' * 64, 'behavior_pass': passed}


class CalibrationPlanningTests(unittest.TestCase):
    def setUp(self):
        self.open = case('open', ['#open-panel'])
        self.close = case('open-close', ['#open-panel', '#close-panel'])

    def test_qualification_binds_actual_planner_source(self):
        from kiroclops.local_engine.qualification import analyzer_sources, ROOT
        relative = 'kiroclops/local_engine/calibration_planning.py'
        self.assertEqual(file_sha256(ROOT/relative), analyzer_sources()[relative])

    def test_next_case_covers_unmeasured_behavior_and_is_deterministic(self):
        args = dict(source_sha256=SOURCE, measurements=[receipt(self.open)])
        a = recommend_calibration([self.open, self.close], **args)
        self.assertEqual(a, recommend_calibration([self.open, self.close], **args))
        self.assertEqual('open-close', a['next_case']['scenario_id'])
        self.assertEqual(['panel_close'], a['missing_behavior_tags'])
        self.assertIn('panel_close', a['why'])

    def test_measured_behavior_failure_is_not_untested(self):
        result = recommend_calibration([self.open], source_sha256=SOURCE,
                                       measurements=[receipt(self.open, passed=False)])
        self.assertEqual('known_pool_measured', result['status'])
        self.assertEqual('measured_mismatch', result['cases'][0]['status'])
        self.assertIsNone(result['next_case'])
        self.assertEqual([], result['missing_behavior_tags'])

    def test_rejected_measurement_is_not_behavior_failure_or_untested(self):
        result = recommend_calibration([self.open], source_sha256=SOURCE,
                                       measurements=[receipt(self.open, status='rejected')])
        self.assertEqual('measurement_repair_required', result['status'])
        self.assertEqual('measurement_rejected', result['cases'][0]['status'])
        self.assertIsNone(result['next_case'])

    def test_repeats_do_not_invent_new_behavior(self):
        second = case('open', ['#open-panel'], 2)
        result = recommend_calibration([self.open, second], source_sha256=SOURCE,
                                       measurements=[receipt(self.open)])
        self.assertEqual([], result['missing_behavior_tags'])
        self.assertEqual(2, result['next_case']['scenario']['repeat'])
        self.assertIn('repeat', result['why'])

    def test_foreign_source_and_foreign_recipe_fail(self):
        for bad in (receipt(self.open, source='d' * 64), receipt(self.close)):
            with self.assertRaises(VisualError):
                recommend_calibration([self.open], source_sha256=SOURCE, measurements=[bad])

    def test_heldout_pool_is_rejected_without_discovery(self):
        heldout = {**self.open, 'split': 'validation'}
        with self.assertRaises(VisualError):
            recommend_calibration([heldout], source_sha256=SOURCE)

    def test_single_uploaded_condition_never_generates_another_scenario(self):
        result = recommend_calibration([self.open], source_sha256=SOURCE,
                                       measurements=[receipt(self.open)])
        self.assertEqual(1, len(result['cases']))
        self.assertIsNone(result['next_case'])
        self.assertFalse(result['unknown_behaviors_generated'])
        self.assertFalse(result['heldout_accessed'])

    def test_baseline_and_finalization_do_not_read_any_calibration_file(self):
        for mode, operation in [('baseline', 'prepare'), ('baseline', 'evaluate'), ('tools', 'finalize')]:
            with patch('kiroclops.local_engine.calibration_planning.read_json', side_effect=AssertionError('unexpected read')):
                original = {'eligible': True}
                self.assertIs(original, advice_for_job({'mode': mode}, {'operation': operation}, 'unused', original))

    def test_job_advice_binds_own_files_and_full_matrix_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package, analysis = root/'capture/package/manifest.json', root/'capture/analysis.json'
            write_json(package, {'package_sha256': 'c' * 64})
            write_json(analysis, {'measurement_status': 'ok'})
            record = {'scenario': self.open, 'package': str(package.parent), 'analysis': str(analysis),
                      'measurement_status': 'ok', 'source_sha256': SOURCE}
            write_json(root/'candidate.json', [record])
            state = {'mode': 'tools', 'experiment': {'calibration': [self.open, self.close],
                                                   'validation': ['NEVER READ THIS']}}
            original = {'eligible': False, 'comparisons': [{'scenario_id': 'open', 'pass': False}]}
            result = advice_for_job(state, {'operation': 'evaluate', 'source_sha256': SOURCE}, root, original)
            self.assertEqual(original['comparisons'], result['comparisons'])
            self.assertFalse(result['eligible'])
            plan = result['calibration_planning']
            self.assertEqual('measured_mismatch', plan['cases'][0]['status'])
            self.assertEqual('open-close', plan['next_case']['scenario_id'])
            self.assertIn('complete fixed calibration matrix', plan['execution_policy'])

    def test_foreign_job_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'job'; root.mkdir()
            write_json(root/'before.json', [{'scenario': self.open, 'package': str(root.parent/'outside'),
                                            'measurement_status': 'ok', 'analysis': None}])
            with self.assertRaises(VisualError):
                advice_for_job({'mode': 'tools', 'experiment': {'calibration': [self.open]}},
                               {'operation': 'prepare', 'source_sha256': SOURCE}, root, {})

    def _stored_job(self, root, advice):
        identifier = '1' * 32
        job = {key: None for key in JOB_BINDING_FIELDS}
        job.update(job_id=identifier, operation='evaluate', source_sha256=SOURCE, status='succeeded')
        result = {'calibration_planning': advice}
        job['result_sha256'] = digest(result)
        directory = root/'jobs'/identifier
        write_json(directory/'job.json', job)
        write_json(directory/'binding.json', {'sha256': digest({key: job[key] for key in JOB_BINDING_FIELDS})})
        write_json(directory/'result.json', result)
        return {'experiment': {'calibration': [self.open]}, 'candidates': [{'job_id': identifier}]}, directory

    def test_inspect_reuses_only_hashed_same_source_calibration_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            advice = recommend_calibration([self.open], source_sha256=SOURCE, measurements=[receipt(self.open)])
            state, _ = self._stored_job(root, advice)
            result = inspect_advice(root, state, SOURCE)
            self.assertEqual('known_pool_measured', result['status'])
            self.assertIn('evidence_result_sha256', result)
            changed = inspect_advice(root, state, 'e' * 64)
            self.assertEqual('recommendation', changed['status'])
            self.assertEqual([], changed['covered_behavior_tags'])

    def test_inspect_rejects_changed_result_and_source_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            advice = recommend_calibration([self.open], source_sha256=SOURCE)
            state, directory = self._stored_job(root, advice)
            write_json(directory/'result.json', {'calibration_planning': {**advice, 'status': 'forged'}})
            with self.assertRaises(VisualError):
                inspect_advice(root, state, SOURCE)
            state, directory = self._stored_job(root, advice)
            write_json(directory/'binding.json', {'sha256': 'wrong'})
            with self.assertRaises(VisualError):
                inspect_advice(root, state, SOURCE)

    def test_inspect_skips_active_and_final_jobs_and_sealed_advice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            advice = recommend_calibration([self.open], source_sha256=SOURCE)
            state, directory = self._stored_job(root, advice)
            write_json(directory/'job.json', {'status': 'running', 'operation': 'evaluate'})
            self.assertEqual('recommendation', inspect_advice(root, state, SOURCE)['status'])
            state['sealed_at'] = 1
            with patch('kiroclops.local_engine.calibration_planning.read_json', side_effect=AssertionError('sealed read')):
                result = inspect_advice(root, state, SOURCE)
            self.assertEqual('blocked', result['status'])
            self.assertIsNone(result['next_case'])

    def test_legacy_display_labels_do_not_break_inspection_or_invent_recipes(self):
        result = inspect_advice(Path('unused'), {'experiment': {'calibration': ['open']}}, SOURCE)
        self.assertEqual('not_configured', result['status'])
        self.assertEqual([], result['cases'])

    def test_game_tick_recipes_have_distinct_short_long_behavior_tags(self):
        short = {'id': 'r06', 'split': 'calibration', 'repeat': 1, 'events': [
            {'tick': 30, 'action': 'right', 'pressed': True}, {'tick': 36, 'action': 'right', 'pressed': False}]}
        long = deepcopy(short); long['id'] = 'r48'; long['events'][1]['tick'] = 78
        result = recommend_calibration([short, long], source_sha256=SOURCE, measurements=[receipt(short)])
        self.assertEqual(['ground_input_ticks:48'], result['missing_behavior_tags'])
        self.assertEqual('r48', result['next_case']['scenario_id'])

    def test_shared_dispatch_adds_tools_advice_without_changing_execution(self):
        from kiroclops.local_engine.evaluation import run_job
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {'mode': 'tools', 'domain': 'web_panel', 'experiment': {'calibration': [self.open]}}
            write_json(root/'session.json', state)
            job = {'operation': 'prepare', 'job_id': '2' * 32, 'source_sha256': SOURCE}
            with patch('kiroclops.local_engine.evaluation._run_web', return_value={'status': 'ready'}) as execution:
                result = run_job(root, job, lambda: None, lambda *args: None, lambda: False)
            self.assertEqual(1, execution.call_count)
            self.assertEqual('ready', result['status'])
            self.assertEqual('recommendation', result['calibration_planning']['status'])
            state['mode'] = 'baseline'; write_json(root/'session.json', state)
            with patch('kiroclops.local_engine.evaluation._run_web', return_value={'status': 'ready'}) as execution:
                result = run_job(root, job, lambda: None, lambda *args: None, lambda: False)
            self.assertEqual({'status': 'ready'}, result)
            self.assertEqual(1, execution.call_count)


if __name__ == '__main__':
    unittest.main()
