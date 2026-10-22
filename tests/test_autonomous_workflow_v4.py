"""Additive airborne integration boundaries; no engines or models."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import autonomous_workflow_v4 as workflow
from kiroclops.integration import ground_worker, session as session_module
from kiroclops.ground import comparison
from kiroclops.integration.storage import IntegrationError, project_hashes, read_json, write_json, sha256

REAL_GROUND_ACTION = ground_worker.run_action


class AirborneIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve() / 'session'
        self.directory.mkdir()
        self.baseline = self.directory / 'baseline'
        self.baseline.mkdir()
        (self.baseline / 'player.gd').write_text('unchanged airborne code')
        self.state = {'session_id': self.directory.name, 'baseline': str(self.baseline),
            'baseline_hashes': project_hashes(self.baseline), 'candidates': [], 'status': 'new'}
        self.session = Mock(directory=self.directory)
        self.session.read.side_effect = lambda: deepcopy(self.state)
        self.session.update.side_effect = lambda fn: fn(self.state)
        self.check = Mock()
        self.progress = Mock()
        self.compare_status = 'pass'
        self.suite_count = 0
        self.policy = {'id': 'seeded-airborne-v1', 'case_count': 18}
        self.fake = ModuleType('kiroclops.integration.airborne_regression_v1')
        self.fake.protocol_record = lambda: deepcopy(self.policy)
        self.fake.run_suite = self.run_suite
        self.fake.verify_suite = self.verify_suite
        self.fake.compare_suites = self.compare
        self.fake.verify_qualification = Mock(return_value={'status': 'pass'})
        self.addCleanup(patch.stopall)
        # Restore only this fake import, not the entire process import table.
        # Whole-table restoration can leave package attributes pointing at a
        # different module object than a subsequent from-import resolves.
        previous = sys.modules.get(self.fake.__name__)
        sys.modules[self.fake.__name__] = self.fake
        def restore_airborne_import():
            if previous is None: sys.modules.pop(self.fake.__name__, None)
            else: sys.modules[self.fake.__name__] = previous
        self.addCleanup(restore_airborne_import)
        patch.object(ground_worker, 'run_action', self.original_action).start()
        patch.object(ground_worker, 'verify_session', side_effect=lambda *a: deepcopy(self.state)).start()
        patch.object(session_module, 'create_candidate_snapshot', self.create_snapshot).start()
        patch.object(comparison, 'calibration_feedback', return_value={
            'eligible': True, 'status': 'ok', 'mean_rmse_body': .1, 'diagnostic_gates_pass': True}).start()

    def run_suite(self, project, output, *, expected_source_hashes, cancel=None):
        self.suite_count += 1
        output = Path(output)
        if output.exists(): raise ValueError('suite must be fresh')
        output.mkdir(parents=True)
        write_json(output / 'suite.json', {'source_hashes': expected_source_hashes})
        return output

    def verify_suite(self, output, *, expected_source_hashes):
        return {'status': 'pass' if read_json(Path(output) / 'suite.json')['source_hashes'] == expected_source_hashes else 'fail'}

    def compare(self, baseline, candidate):
        return {'status': self.compare_status, 'summary': {'case_count': 18, 'compared_steps': 72,
            'failed_cases': int(self.compare_status == 'fail'), 'mismatch_count': int(self.compare_status == 'fail')},
            'mismatches': [{'case_id': 'seeded-control', 'step_index': 0, 'fields': ['horizontal_response']}]
                if self.compare_status == 'fail' else [],
            'baseline': {'suite_path': str(Path(baseline) / 'suite.json'),
                'suite_sha256': sha256(Path(baseline) / 'suite.json'),
                'source_hashes': read_json(Path(baseline) / 'suite.json')['source_hashes']},
            'candidate': {'suite_path': str(Path(candidate) / 'suite.json'),
                'suite_sha256': sha256(Path(candidate) / 'suite.json'),
                'source_hashes': read_json(Path(candidate) / 'suite.json')['source_hashes']}}

    def create_snapshot(self, session, cid):
        root = self.directory / 'candidates' / cid
        root.mkdir(parents=True)
        shutil.copytree(self.baseline, root / 'project')
        candidate = {'candidate_id': cid, 'source_hashes': project_hashes(root / 'project'), 'status': 'capturing'}
        write_json(root / 'candidate.json', candidate)
        self.state['candidates'] = [candidate]
        return root

    def original_action(self, session, job, *, cancel, check, progress):
        if job['action'] == 'prepare':
            self.state['status'] = 'ready'
            return {'status': 'ready', 'reference_profile': {'unchanged': True}}
        if job['action'] == 'evaluate':
            root = session_module.create_candidate_snapshot(session, job['candidate_id'])
            feedback = comparison.calibration_feedback([], [], [])
            item = read_json(root / 'candidate.json')
            item.update(status='evaluated', feedback=feedback, smoke={'status': 'passed'})
            write_json(root / 'candidate.json', item)
            self.state['candidates'] = [item]
            return {'status': 'evaluated', 'calibration': feedback}
        if job['action'] == 'finalize':
            self.state['final'] = {'numerical_status': 'pass'}
            return {'status': 'finished', 'numerical_status': 'pass'}
        return {'status': 'ready'}

    def action(self, action, cid=None):
        return ground_worker.run_action(self.session, {'action': action, 'candidate_id': cid},
            cancel=threading.Event(), check=self.check, progress=self.progress)

    def prepare_evaluate(self, *, failed=False):
        self.compare_status = 'fail' if failed else 'pass'
        with workflow.airborne_integrity_feedback(self.directory):
            self.action('prepare')
            return self.action('evaluate', 'candidate-01')

    def mark_completed(self, status='succeeded'):
        path = self.directory / 'jobs/job-evaluation.json'
        write_json(path, {'job_id': path.stem, 'session_id': self.directory.name,
                         'candidate_id': 'candidate-01', 'action': 'evaluate', 'status': status})

    def test_one_baseline_and_one_snapshot_suite_then_passing_eligibility(self):
        result = self.prepare_evaluate()
        self.assertEqual(self.suite_count, 2)
        self.assertTrue(result['calibration']['eligible'])
        self.assertEqual(result['calibration']['airborne_integrity']['status'], 'pass')
        self.assertNotIn('source_hashes', result['calibration']['airborne_integrity'])
        self.assertEqual(project_hashes(self.baseline), self.state['baseline_hashes'])

    def test_valid_differential_fail_returns_semantic_rejection_without_running_motion(self):
        result = self.prepare_evaluate(failed=True)
        feedback = result['calibration']
        self.assertFalse(feedback['eligible'])
        self.assertIsNone(feedback['mean_rmse_body'])
        self.assertNotIn('diagnostic_gates_pass', feedback)
        self.assertEqual(feedback['airborne_integrity']['status'], 'fail')
        self.assertEqual(feedback['ground_calibration']['status'], 'not_run')
        self.assertEqual(feedback['jump_regression']['status'], 'not_run')
        self.assertEqual(result['gameplay']['status'], 'not_run')
        self.assertEqual(result['status'], 'evaluated')
        self.assertEqual(result['failure_class'], 'gameplay_integrity_violation')
        self.assertEqual(self.state['candidates'][0]['calibration_runs'], [])
        self.assertEqual(self.state['status'], 'ready')
        comparison.calibration_feedback.assert_not_called()
        self.mark_completed()
        with self.assertRaises(IntegrationError) as error:
            workflow.verify_selection_integrity(self.session)
        self.assertEqual(error.exception.code, 'no_eligible_candidate')

    def test_invalid_probe_never_yields_eligible_feedback(self):
        self.compare_status = 'probe_invalid'
        with workflow.airborne_integrity_feedback(self.directory):
            self.action('prepare')
            with self.assertRaises(IntegrationError): self.action('evaluate', 'candidate-01')
        self.assertEqual(len(self.state['candidates']), 1)
        self.assertNotEqual(self.state['candidates'][0].get('status'), 'evaluated')
        comparison.calibration_feedback.assert_not_called()

    def test_cancelled_evaluation_cannot_be_selected(self):
        self.prepare_evaluate()
        self.mark_completed('cancelled')
        with self.assertRaises(IntegrationError): workflow.verify_selection_integrity(self.session)
        self.mark_completed()
        self.assertEqual(workflow.verify_selection_integrity(self.session)['candidate_id'], 'candidate-01')

    def test_missing_or_changed_candidate_receipt_fails_selection(self):
        self.prepare_evaluate(); self.mark_completed()
        path = self.directory / 'candidates/candidate-01/airborne-integrity.json'
        receipt = read_json(path); receipt['comparison']['status'] = 'fail'; write_json(path, receipt)
        with self.assertRaises(IntegrationError): workflow.verify_selection_integrity(self.session)

    def test_changed_raw_suite_or_actual_source_is_rejected(self):
        self.prepare_evaluate(); self.mark_completed()
        root = self.directory / 'candidates/candidate-01'
        source = root / 'project/player.gd'; before = source.read_bytes()
        source.write_text('changed after snapshot')
        with self.assertRaises(IntegrationError): workflow.verify_selection_integrity(self.session)
        source.write_bytes(before)
        (root / 'airborne-suite/suite.json').write_text('{}')
        with self.assertRaises(IntegrationError): workflow.verify_selection_integrity(self.session)

    def test_saved_pass_cannot_override_fresh_differential_failure(self):
        self.prepare_evaluate(); self.mark_completed()
        self.compare_status = 'fail'
        with self.assertRaises(IntegrationError): workflow.verify_selection_integrity(self.session)

    def test_outer_correct_but_inner_different_source_cannot_pass(self):
        self.prepare_evaluate(); self.mark_completed()
        root = self.directory / 'candidates/candidate-01'
        path = root / 'airborne-integrity.json'
        record = read_json(path)
        record['comparison']['candidate']['source_hashes'] = {'other-player.gd': 'different-source'}
        write_json(path, record)
        candidate = read_json(root / 'candidate.json')
        candidate['airborne_integrity']['sha256'] = sha256(path)
        write_json(root / 'candidate.json', candidate)
        self.state['candidates'] = [candidate]
        self.fake.compare_suites = lambda *args: deepcopy(record['comparison'])
        with self.assertRaises(IntegrationError) as error: workflow.verify_selection_integrity(self.session)
        self.assertEqual(error.exception.code, 'airborne_source_binding')

    def test_finalization_requires_bound_passing_selected_candidate(self):
        self.prepare_evaluate(); self.mark_completed()
        self.state['selected_candidate_id'] = 'candidate-01'
        with workflow.airborne_integrity_feedback(self.directory):
            result = self.action('finalize', 'candidate-01')
        self.assertEqual(result['airborne_integrity']['status'], 'pass')
        self.assertEqual(result['numerical_status'], 'pass')
        self.assertEqual(self.suite_count, 2)  # Finalization replays; it never captures again.

    def test_patch_lifetime_restores_on_failure_and_refuses_other_session(self):
        original = ground_worker.run_action
        with self.assertRaises(IntegrationError):
            with workflow.airborne_integrity_feedback(self.directory):
                ground_worker.run_action(Mock(directory=self.directory / 'other'), {'action': 'prepare'},
                    cancel=None, check=self.check, progress=self.progress)
        self.assertEqual(ground_worker.run_action, original)
        with workflow.airborne_integrity_feedback(self.directory):
            with self.assertRaises(IntegrationError):
                with workflow.airborne_integrity_feedback(self.directory): pass

    def test_prepare_qualification_failure_precedes_session_creation(self):
        with patch.object(workflow, '_qualifications', side_effect=IntegrationError('qualification_invalid', 'bad')), \
                patch.object(ground_worker, 'provision_session') as provision:
            with self.assertRaises(IntegrationError): workflow.prepare('new-v4-only')
            provision.assert_not_called()

    def test_old_v3_session_is_rejected_before_any_new_work(self):
        self.state['execution_protocol'] = {'id': 'ground-cli-autonomous-v3', 'sha256': 'old'}
        with patch.object(workflow, 'Session', return_value=self.session):
            with self.assertRaises(IntegrationError) as error: workflow.verify_protocol(self.directory)
        self.assertEqual(error.exception.code, 'execution_protocol_changed')

    def test_worker_receives_registered_cache_without_changing_source(self):
        cache = {'cache_dir': 'registered-cache', 'manifest_sha256': 'registered-digest'}
        original = self.fake.run_suite
        calls = []
        def cached(project, output, **kwargs):
            calls.append(kwargs.pop('asset_cache'))
            return original(project, output, **kwargs)
        self.fake.run_suite = cached
        with workflow.airborne_integrity_feedback(self.directory, asset_cache=cache):
            self.action('prepare'); self.action('evaluate', 'candidate-01')
        self.assertEqual(calls, [cache, cache])

    def test_cancelled_valid_failure_cannot_publish_semantic_success(self):
        self.compare_status = 'fail'
        with workflow.airborne_integrity_feedback(self.directory):
            self.action('prepare')
            publish = workflow._publish_early_rejection
            def cancel_before_publish(*args, **kwargs):
                self.check.side_effect = IntegrationError('cancelled', 'cancelled before publishing')
                return publish(*args, **kwargs)
            with patch.object(workflow, '_publish_early_rejection', side_effect=cancel_before_publish):
                with self.assertRaises(IntegrationError): self.action('evaluate', 'candidate-01')
        self.assertEqual(self.state['candidates'][0]['status'], 'capturing')
        self.assertEqual(read_json(self.directory / 'candidates/candidate-01/candidate.json')['status'], 'capturing')
        comparison.calibration_feedback.assert_not_called()

    def test_real_frozen_action_short_circuits_before_import_ground_jump_or_smoke(self):
        with workflow.airborne_integrity_feedback(self.directory): self.action('prepare')
        self.compare_status = 'fail'
        with patch.object(ground_worker, 'run_action', REAL_GROUND_ACTION), \
                patch('kiroclops.integration.ground_runner.import_project') as importer, \
                patch.object(ground_worker, '_ground_group') as ground, \
                patch.object(ground_worker, '_jump_group') as jump, \
                patch('kiroclops.integration.ground_runner.smoke_project') as smoke:
            with workflow.airborne_integrity_feedback(self.directory):
                result = self.action('evaluate', 'candidate-01')
            self.assertEqual(result['status'], 'evaluated')
            self.assertFalse(result['calibration']['eligible'])
            for skipped in (importer, ground, jump, smoke): skipped.assert_not_called()

    def test_invalid_capture_cannot_be_converted_to_early_rejection(self):
        with workflow.airborne_integrity_feedback(self.directory):
            self.action('prepare')
            self.fake.verify_suite = lambda *a, **kw: {'status': 'probe_invalid'}
            with self.assertRaises(IntegrationError) as error: self.action('evaluate', 'candidate-01')
        self.assertEqual(error.exception.code, 'airborne_probe_invalid')
        self.assertNotEqual(self.state['candidates'][0]['status'], 'evaluated')

    def test_report_retains_not_run_history_without_reusing_a_score(self):
        self.prepare_evaluate(failed=True)
        before = deepcopy(self.state['candidates'])
        with workflow.airborne_integrity_feedback(self.directory):
            self.assertEqual(self.action('report')['status'], 'ready')
        self.assertEqual(self.state['candidates'], before)
        self.assertIsNone(self.state['candidates'][0]['feedback']['mean_rmse_body'])

    def test_real_report_renders_semantic_rejection_as_unmeasured(self):
        from kiroclops.integration.ground_reporting import generate_ground_report
        self.prepare_evaluate(failed=True)
        state = {**self.state, 'experiment_id': 'ground-motion-v1',
                 'reference': {}, 'baseline_runs': {}, 'final': None}
        path = write_json(self.directory / 'session.json', state)
        before = path.read_bytes()
        html = generate_ground_report(self.directory).read_text(encoding='utf-8')
        self.assertIn('gameplay_integrity_violation', html)
        self.assertIn('not_run', html)
        self.assertIn('Horizontal numerical: pending', html)
        self.assertEqual(path.read_bytes(), before)


class RuntimePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.previous = self.root / 'sessions/cli-autonomous-v3-01'
        write_json(self.previous / 'kiro/launch.json', {'status': 'completed', 'prompt_result': {'stopReason': 'end_turn'}})
        self.jobs = []
        begin = datetime(2026, 9, 19, tzinfo=timezone.utc)
        for number, (action, seconds) in enumerate([('prepare', 99), ('evaluate', 238), ('evaluate', 239), ('finalize', 612), ('report', 17)]):
            path = self.previous / f'jobs/job-{number}.json'
            write_json(path, {'job_id': path.stem, 'session_id': self.previous.name, 'status': 'succeeded',
                'action': action, 'started_at': begin.isoformat(), 'finished_at': (begin + timedelta(seconds=seconds)).isoformat()})
            self.jobs.append(path)
        self.qualification = self.root / 'artifacts/model-trials/preflight' / workflow.AIRBORNE_QUALIFICATION / 'qualification.json'
        self.qualified = {'status': 'pass', 'suites': {str(i): {'full_call_seconds': seconds}
            for i, seconds in enumerate([55, 60, 56, 59])}, 'comparison_timing_seconds': {'self': 8, 'positive': 9, 'negative': 10}}
        write_json(self.qualification, self.qualified)
        self.patch = patch.object(workflow, 'ROOT', self.root); self.patch.start(); self.addCleanup(self.patch.stop)

    def test_qualified_suite_and_all_replay_work_are_budgeted_separately(self):
        value = workflow._runtime_budget_hint()
        self.assertEqual(value['prepare_planning_seconds'], 169)
        self.assertEqual(value['evaluate_planning_seconds'], 339)
        self.assertEqual(value['early_integrity_rejection_planning_seconds'], 100)
        self.assertEqual(value['finalize_planning_seconds'], 672)
        self.assertEqual(value['report_planning_seconds'], 22)
        self.assertTrue(value['not_a_guarantee'])
        self.assertFalse(value['budget_or_acceptance_modified'])
        self.assertEqual(value['timing_evidence_sha256'][self.qualification.relative_to(self.root).as_posix()], sha256(self.qualification))

    def test_failed_job_or_unqualified_suite_cannot_supply_planning_numbers(self):
        record = read_json(self.jobs[1]); record['status'] = 'failed'; write_json(self.jobs[1], record)
        with self.assertRaises(IntegrationError): workflow._runtime_budget_hint()
        record['status'] = 'succeeded'; write_json(self.jobs[1], record)
        self.qualified['status'] = 'running'; write_json(self.qualification, self.qualified)
        with self.assertRaises(IntegrationError): workflow._runtime_budget_hint()

    def test_boolean_or_missing_comparison_duration_is_not_accepted(self):
        self.qualified['comparison_timing_seconds']['self'] = True
        write_json(self.qualification, self.qualified)
        with self.assertRaises(IntegrationError): workflow._runtime_budget_hint()
        self.qualified['comparison_timing_seconds'].pop('self')
        write_json(self.qualification, self.qualified)
        with self.assertRaises(IntegrationError): workflow._runtime_budget_hint()


if __name__ == '__main__':
    unittest.main()
