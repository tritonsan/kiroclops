"""Fake-Session boundaries only: no model, capture, browser or application."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase3d_v2_unchanged as unchanged
from kiroclops.local_engine.execution_policy import budget_from_policy, REVISED_ID, resolve_policy
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, source_hashes
from kiroclops.local_engine import realistic_policy, suite_web_v2
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class FakeSession:
    """Durable job/candidate shape without spawning any subprocess."""
    def __init__(self, fixture, directory, state):
        self.fixture, self.directory, self.state = fixture, Path(directory), state
        self.calls, self.jobs = [], {}

    def _load(self):
        return deepcopy(self.state)

    def _save(self):
        write_json(self.directory / 'session.json', self.state)

    def _controls(self):
        return {'kiro/config.json': file_sha256(self.directory / 'kiro/config.json')}

    def _verify_sources(self, state):
        if state['controls_hashes'] != self._controls():
            raise VisualError('control_changed', 'fixture controls changed')
        return source_hashes(state['target'])

    def start_clock(self, origin):
        self.calls.append(('start_clock', origin))
        self.state.update(started_at=self.fixture.now, deadline_at=self.fixture.now + 3600, clock_origin=origin)
        self._save()
        return {key: self.state[key] for key in ('started_at', 'deadline_at')}

    def submit(self, operation, candidate_id=None):
        self.calls.append(('submit', operation, candidate_id))
        index = len(self.jobs) + 1
        job_id = f'{index:032x}'
        if operation == 'prepare':
            self.state.update(prepared=True, prepare_job_id=job_id)
            result = {'status': 'ready'}
        elif operation == 'evaluate':
            candidate_id = 'candidate-01'
            shutil.copytree(self.state['target'], self.directory / 'candidates' / candidate_id / 'source',
                            ignore=shutil.ignore_patterns('.kiro'))
            result = {'eligible': self.fixture.eligible, 'score': .001}
            self.state['candidates'].append({'candidate_id': candidate_id, 'job_id': job_id, 'status': 'succeeded',
                'eligible': self.fixture.eligible, 'score': .001, 'source_sha256': digest(self.state['baseline_hashes'])})
        elif operation == 'finalize':
            self.state.update(selected_candidate_id=candidate_id, sealed_at=self.fixture.now,
                              final_job_id=job_id, final={'pass': self.fixture.final_pass})
            result = {'pass': self.fixture.final_pass, 'independent_review_status': 'pending'}
        else:
            raise AssertionError('Unexpected operation ' + operation)
        status = 'failed' if operation == self.fixture.fail_operation else 'succeeded'
        job = {'job_id': job_id, 'operation': operation, 'status': status, 'candidate_id': candidate_id,
            'created_at': self.fixture.now, 'started_at': self.fixture.now, 'finished_at': self.fixture.now,
            'source_sha256': digest(self.state['baseline_hashes']), 'result': result if status == 'succeeded' else None,
            'error': {'code': 'fixture_capture_failure', 'message': 'unchanged failure'} if status == 'failed' else None}
        self.jobs[job_id] = job
        write_json(self.directory / 'jobs' / job_id / 'job.json', job)
        self._save()
        return {key: job[key] for key in ('job_id', 'status', 'candidate_id')}

    def get_job(self, job_id, wait_seconds=0, *, allow_final_result=False):
        self.calls.append(('get_job', job_id, wait_seconds, allow_final_result))
        job = deepcopy(self.jobs[job_id])
        operation = job['operation']
        if operation in self.fixture.phase_finish_times:
            self.fixture.now = self.fixture.phase_finish_times[operation]
            job['finished_at'] = self.fixture.now
        if operation == self.fixture.mutate_operation:
            (Path(self.state['target']) / self.fixture.entry).write_text('unexpected mutation', encoding='utf8')
        if operation == self.fixture.timeout_operation:
            self.fixture.now = self.state['deadline_at'] + 1
            job['status'], job['finished_at'] = 'running', None
        if operation == self.fixture.late_operation:
            job['finished_at'] = self.state['deadline_at'] + 1
        if operation == self.fixture.foreign_operation:
            job['job_id'] = 'f' * 32
        if operation == self.fixture.wrong_source_operation:
            job['source_sha256'] = '0' * 64
        return job

    def cancel_job(self, job_id):
        self.calls.append(('cancel_job', job_id))
        self.jobs[job_id]['status'] = 'cancelled'
        return {'job_id': job_id, 'status': 'cancelled'}


class UnchangedV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unchanged café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.
        self.eligible = self.final_pass = True
        self.fail_operation = self.mutate_operation = self.timeout_operation = self.late_operation = None
        self.foreign_operation = self.wrong_source_operation = None
        self.phase_finish_times = {}
        self.make_fixture('svg')

    def make_fixture(self, stage):
        self.stage = stage
        base = self.root / stage
        base.mkdir()
        self.directory = base / 'session'
        self.suite_directory = base / 'suite'
        self.quality = base / 'quality'
        self.origin = base / 'origin.json'
        self.output = base / 'result'
        self.historical = base / 'historical'
        self.entry = 'src/layout-motion.js' if stage == 'svg' else 'src/panel-motion.js'
        for source in (self.directory / 'target', self.directory / 'baseline',
                       self.historical / 'candidates/candidate-01/source'):
            (source / 'src').mkdir(parents=True)
            (source / self.entry).write_text('existing Kiro behavior code', encoding='utf8')
            (source / 'index.html').write_text('<main>unchanged fixture</main>', encoding='utf8')
        write_json(self.directory / 'kiro/config.json', {'prepared': True, 'launch_forbidden': True})
        expected = source_hashes(self.directory / 'baseline')
        execution = resolve_policy(REVISED_ID, stage)
        points = [{'object_id': 'a', 'frame_sha256': digest(['frame', i]), 'error_px': .2,
                   'role': 'reference', 'split': 'calibration', 'independent': True} for i in range(20)]
        policy = realistic_policy.build_policy(stage, {'status': 'pass', 'basis': 'independent_raw_pixels', 'points': points})
        self.cases = [{'case_id': scenario['case_id'], 'role': role, 'stage': stage, 'purpose': 'primary', 'scenario': scenario}
            for split, role in [('calibration', 'calibration'), ('known_regression', 'regression'), ('validation', 'validation')]
            for scenario in suite_web_v2.scenarios(stage, split)]
        code = self.root / 'fixture-analysis.py'
        code.write_text('frozen fixture analysis', encoding='utf8')
        code_sources = {str(code): file_sha256(code)}
        self.suite = {'schema_version': 2, 'kind': 'ReferenceMediaSuite', 'stage': stage,
            'domain': 'svg' if stage == 'svg' else 'web_panel', 'authoring_parent_source_hashes': expected,
            'execution_policy': execution, 'acceptance_policy': policy, 'acceptance_profile': policy['id'],
            'source_epoch': digest(code_sources), 'analysis_source_hashes': code_sources, 'cases': self.cases}
        write_json(self.suite_directory / 'suite.json', self.suite)
        self.state = {'schema_version': 2, 'kind': 'LocalSession', 'session_id': 'fresh-fixture',
            'domain': self.suite['domain'], 'mode': 'tools', 'status': 'new', 'started_at': None, 'deadline_at': None,
            'clock_origin': None, 'prepared': False, 'prepare_job_id': None, 'candidates': [], 'active_job_id': None,
            'selected_candidate_id': None, 'sealed_at': None, 'final_job_id': None, 'final': None,
            'target': str(self.directory / 'target'), 'baseline': str(self.directory / 'baseline'),
            'baseline_hashes': expected, 'reference': str(self.suite_directory),
            'controls_hashes': {'kiro/config.json': file_sha256(self.directory / 'kiro/config.json')},
            'budget': budget_from_policy(execution), 'experiment': {'reference_mode': 'media_suite', 'stage': stage,
                'suite_sha256': digest(self.suite), 'execution_policy': execution, 'acceptance_profile': policy['id'],
                'acceptance_policy_sha256': policy['policy_sha256'], 'source_epoch': self.suite['source_epoch']}}
        self.session = FakeSession(self, self.directory, self.state)
        self.session._save()
        write_json(self.directory / 'binding.json', {'fixture': 'new session binding'})
        write_json(self.directory / 'admission.json', {'fixture': 'passed image admission'})
        self.make_quality()
        self.make_origin()

    def make_quality(self):
        selected = [{key: deepcopy(case[key]) for key in ('case_id', 'role', 'stage', 'purpose', 'scenario')}
            for case in self.cases if case['role'] in ('calibration', 'regression')]
        plan = {'kind': 'ExistingParentInitialQualityPlan', 'schema_version': 1, 'suite_sha256': digest(self.suite),
            'execution_policy': deepcopy(self.suite['execution_policy']), 'acceptance_policy_sha256': digest(self.suite['acceptance_policy']),
            'selected_cases': selected}
        rows = [{'case_id': case['case_id'], 'role': case['role'],
                 'comparison': {'pass': True, 'measurement_pass': True, 'baseline_measurement_pass': True}}
                for case in selected]
        result = {'kind': 'ExistingParentInitialQuality', 'schema_version': 1, 'status': 'sufficient_initial_quality',
            'complete': True, 'measurement_and_source_qualified': True, 'initial_quality_pass': True, 'repair_required': False,
            'error': None, 'plan_sha256': digest(plan), 'suite_sha256': digest(self.suite),
            'source_epoch': self.suite['source_epoch'], 'acceptance_profile': self.suite['acceptance_profile'],
            'authoring_parent_source_sha256': digest(self.state['baseline_hashes']), 'comparisons': rows}
        for key in ('validation_opened', 'model_started', 'new_model_generation', 'new_candidate_created',
                    'candidate_budget_consumed', 'app_or_capture_started', 'new_functional_checks_run', 'full_behavior_acceptance'):
            result[key] = False
        producer = Path(unchanged.__file__).with_name('phase3d_v2_initial_quality.py').resolve()
        self.consumed = {str(producer): file_sha256(producer), str(self.suite_directory / 'suite.json'):
                         file_sha256(self.suite_directory / 'suite.json')}
        write_json(self.quality / 'plan.json', plan)
        write_json(self.quality / 'source-bindings.json', {'consumed_files': self.consumed, 'validation_files_opened': []})
        for row in rows:
            write_json(self.quality / 'comparisons' / (row['case_id'] + '.json'), row)
        write_json(self.quality / 'result.json', result)
        self.rebind_quality()

    def rebind_quality(self):
        files = source_hashes(self.quality)
        files.pop('result-binding.json', None)
        write_json(self.quality / 'result-binding.json', {'kind': 'ExistingParentInitialQualityReceipt', 'schema_version': 1,
            'result_sha256': digest(read_json(self.quality / 'result.json')), 'output_file_hashes': files,
            'input_bindings_sha256': digest(self.consumed)})

    def make_origin(self):
        expected = self.state['baseline_hashes']
        job_id = 'a' * 32
        self.history_state = {'candidates': [{'candidate_id': 'candidate-01', 'job_id': job_id,
            'source_sha256': digest(expected), 'status': 'succeeded'}]}
        self.history = FakeSession(self, self.historical, self.history_state)
        self.history._save()
        write_json(self.historical / 'binding.json', {'fixture': 'historical scope'})
        job = {'schema_version': 1, 'job_id': job_id, 'operation': 'evaluate', 'created_at': 1.,
            'candidate_id': 'candidate-01', 'source_path': str(self.historical / 'candidates/candidate-01/source'),
            'source_hashes': expected, 'source_sha256': digest(expected), 'status': 'succeeded',
            'result_sha256': digest({'eligible': True})}
        job_directory = self.historical / 'jobs' / job_id
        write_json(job_directory / 'job.json', job)
        write_json(job_directory / 'binding.json', {'sha256': digest({key: job[key] for key in JOB_BINDING_FIELDS})})
        write_json(job_directory / 'result.json', {'eligible': True})
        files = {str(self.historical / name): file_sha256(self.historical / name) for name in ('session.json', 'binding.json')}
        files.update({str(job_directory / name): file_sha256(job_directory / name) for name in ('job.json', 'binding.json', 'result.json')})
        files.update({str(self.historical / 'candidates/candidate-01/source' / name): sha for name, sha in expected.items()})
        self.origin_value = {'kind': 'ExistingKiroCandidateOrigin', 'schema_version': 1,
            'historical_session': str(self.historical), 'historical_candidate_id': 'candidate-01',
            'source_sha256': digest(expected), 'source_hashes': expected, 'historical_binding_files': files,
            'new_model_generation': False}
        write_json(self.origin, self.origin_value)
        self.origin_sha = file_sha256(self.origin)

    def invoke(self):
        def session_factory(directory):
            path = Path(directory)
            if path == self.directory:
                return self.session
            if path == self.historical:
                return self.history
            raise AssertionError('No extra sessions, retries or model orchestration')
        with patch.object(unchanged, 'Session', side_effect=session_factory), \
             patch.object(unchanged.media_suite, 'load_suite', return_value=deepcopy(self.suite)), \
             patch.object(unchanged.media_suite, 'analysis_source_hashes', return_value=self.suite['analysis_source_hashes']), \
             patch.object(unchanged.suite_readiness, 'validate', return_value=read_json(self.directory / 'admission.json')) as admission, \
             patch.object(unchanged.time, 'time', side_effect=lambda: self.now):
            result = unchanged.run(self.directory, self.quality, self.origin, self.origin_sha, self.output)
        return result, admission

    def test_svg_and_panel_use_canonical_jobs_and_never_claim_new_generation(self):
        for stage in ('svg', 'panel'):
            if stage == 'panel':
                self.make_fixture(stage)
            result, admission = self.invoke()
            self.assertEqual('numerical_pass_pending_reviews', result['status'])
            self.assertEqual(['prepare', 'evaluate', 'finalize'], [call[1] for call in self.session.calls if call[0] == 'submit'])
            self.assertEqual(('start_clock', 'unchanged_initial_measurement'), self.session.calls[0])
            self.assertTrue(all(call[3] is True for call in self.session.calls if call[0] == 'get_job'))
            self.assertEqual(1, len(self.session.state['candidates']))
            self.assertFalse(any(call[0] == 'cancel_job' for call in self.session.calls))
            self.assertTrue(result['source_unchanged'])
            self.assertFalse(result['new_model_generation'])
            self.assertFalse(result['model_started'])
            self.assertFalse(result['code_written'])
            self.assertFalse(result['full_behavior_acceptance'])
            self.assertEqual('measurement_snapshot_of_existing_kiro_code', result['candidate_origin'])
            admission.assert_called_once()
            intent = read_json(self.output / 'intent.json')
            self.assertEqual(intent, read_json(self.directory / 'unchanged-stage-intent.json'))
            self.assertEqual(3600, intent['execution_policy']['max_seconds'])
            self.assertEqual(3600, intent['execution_policy']['max_validation_seconds'])
            self.assertEqual(180, intent['safety_seconds'])
            self.assertEqual(self.origin_sha, intent['historical_origin']['manifest_sha256'])

    def test_failed_calibration_never_seals_or_repairs(self):
        self.eligible = False
        result, _ = self.invoke()
        self.assertEqual('calibration_failed', result['error']['code'])
        self.assertEqual(['prepare', 'evaluate'], [call[1] for call in self.session.calls if call[0] == 'submit'])
        self.assertFalse(result['final_submitted'])
        self.assertFalse(result['automatic_repair'])
        self.assertFalse(result['automatic_restart'])

    def test_failed_final_preserves_canonical_result(self):
        self.final_pass = False
        result, _ = self.invoke()
        self.assertEqual('numerical_failure', result['status'])
        self.assertFalse(result['numerical_pass'])
        self.assertTrue(result['final_submitted'])
        self.assertFalse(result['completed_jobs'][-1]['result']['pass'])
        self.assertIsNone(result['error'])

    def test_execution_failure_never_starts_another_job(self):
        self.fail_operation = 'prepare'
        result, _ = self.invoke()
        self.assertEqual('fixture_capture_failure', result['error']['code'])
        self.assertEqual(['prepare'], [call[1] for call in self.session.calls if call[0] == 'submit'])
        self.assertEqual('failed', result['completed_jobs'][0]['status'])

    def test_source_mutation_stops_without_repair(self):
        self.mutate_operation = 'evaluate'
        result, _ = self.invoke()
        self.assertEqual('source_changed', result['error']['code'])
        self.assertFalse(result['source_unchanged'])
        self.assertFalse(result['final_submitted'])

    def test_timeout_cancels_only_the_submitted_owned_job(self):
        self.timeout_operation = 'evaluate'
        result, _ = self.invoke()
        self.assertEqual('phase_timeout', result['error']['code'])
        cancelled = [call[1] for call in self.session.calls if call[0] == 'cancel_job']
        self.assertEqual([result['owned_job_ids'][-1]], cancelled)
        self.assertEqual(['prepare', 'evaluate'], [call[1] for call in self.session.calls if call[0] == 'submit'])
        self.assertFalse(result['automatic_restart'])

    def test_late_terminal_result_is_not_budget_success(self):
        self.late_operation = 'evaluate'
        result, _ = self.invoke()
        self.assertEqual('phase_timeout', result['error']['code'])
        self.assertFalse(result['final_submitted'])

    def test_separate_final_budget_can_finish_after_authoring_deadline(self):
        self.phase_finish_times = {'evaluate': 4500., 'finalize': 8000.}
        result, _ = self.invoke()
        self.assertTrue(result['numerical_pass'])
        self.assertEqual(4600., self.session.state['deadline_at'])
        self.assertEqual(4500., self.session.state['sealed_at'])
        self.assertEqual(8000., result['finished_at'])
        final_job = result['completed_jobs'][-1]
        self.assertEqual(4500., final_job['created_at'])
        self.assertLess(final_job['finished_at'], final_job['created_at'] + 3600.)
        self.assertFalse(result['full_behavior_acceptance'])

    def test_changed_current_parent_or_historical_source_is_rejected(self):
        (Path(self.state['target']) / self.entry).write_text('different current target', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('source_changed', error.exception.code)
        (Path(self.state['target']) / self.entry).write_text('existing Kiro behavior code', encoding='utf8')
        (self.historical / 'candidates/candidate-01/source' / self.entry).write_text('different historical candidate', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('source_changed', error.exception.code)
        self.assertEqual([], self.session.calls)

    def test_foreign_job_or_source_never_seals(self):
        self.foreign_operation = 'evaluate'
        result, _ = self.invoke()
        self.assertEqual('wrong_job', result['error']['code'])
        self.assertFalse(result['final_submitted'])
        self.output = self.root / 'source-other-output'
        self.make_fixture('panel')
        self.foreign_operation, self.wrong_source_operation = None, 'evaluate'
        result, _ = self.invoke()
        self.assertEqual('wrong_candidate', result['error']['code'])
        self.assertFalse(result['final_submitted'])

    def test_started_model_or_prior_candidates_rejected_before_clock(self):
        write_json(self.directory / 'kiro/launch.json', {'status': 'succeeded'})
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('model_already_started', error.exception.code)
        self.assertEqual([], self.session.calls)
        (self.directory / 'kiro/launch.json').unlink()
        self.session.state['started_at'] = 1.
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('session_not_fresh', error.exception.code)
        self.assertEqual([], self.session.calls)

    def test_wrong_suite_policy_parent_and_origin_are_preflight_failures(self):
        self.session.state['schema_version'] = 1
        with self.assertRaises(VisualError):
            self.invoke()
        self.session.state['schema_version'] = 2
        self.session.state['experiment']['execution_policy']['max_seconds'] = 1800
        with self.assertRaises(VisualError):
            self.invoke()
        self.session.state['experiment']['execution_policy']['max_seconds'] = 3600
        self.origin_sha = '0' * 64
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('origin_changed', error.exception.code)
        self.assertEqual([], self.session.calls)

    def test_initial_quality_failure_or_receipt_mutation_rejected_before_jobs(self):
        result = read_json(self.quality / 'result.json')
        result['status'] = 'requires_repair'
        write_json(self.quality / 'result.json', result)
        self.rebind_quality()
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('initial_quality_not_sufficient', error.exception.code)
        result['status'] = 'sufficient_initial_quality'
        write_json(self.quality / 'result.json', result)
        self.rebind_quality()
        row = self.quality / 'comparisons' / (result['comparisons'][0]['case_id'] + '.json')
        row.write_text('changed comparison', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('initial_quality_changed', error.exception.code)
        self.assertEqual([], self.session.calls)

    def test_readmission_failure_never_creates_intent_or_clock(self):
        with patch.object(unchanged.suite_readiness, 'validate', side_effect=VisualError('environment_not_ready', 'fixture')):
            # invoke patches admission; exercise preflight directly instead.
            def factory(path):
                return self.history if Path(path) == self.historical else self.session
            with patch.object(unchanged, 'Session', side_effect=factory), \
                 patch.object(unchanged.media_suite, 'load_suite', return_value=self.suite), \
                 patch.object(unchanged.media_suite, 'analysis_source_hashes', return_value=self.suite['analysis_source_hashes']):
                with self.assertRaises(VisualError) as error:
                    unchanged.run(self.directory, self.quality, self.origin, self.origin_sha, self.output)
        self.assertEqual('environment_not_ready', error.exception.code)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.directory / 'unchanged-stage-intent.json').exists())
        self.assertEqual([], self.session.calls)


if __name__ == '__main__':
    unittest.main()
