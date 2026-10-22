"""Frozen deadlines and passive progress; no native model or application capture."""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kiroclops.local_engine import execution_policy as policy, launcher, session, suite_initial_acceptance as initial, suite_web_v2
from kiroclops.visual.schema import VisualError, read_json


class RevisedExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kc policy café ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reference = self.root / 'reference'
        self.reference.mkdir()
        (self.reference / 'clip.json').write_text('{}', encoding='utf8')

    def create(self, stage='panel', *, revised=True):
        directory = self.root / ('new-' + stage + str(revised))
        baseline = self.root / ('source-' + stage + str(revised))
        baseline.mkdir()
        game = stage in ('jump', 'ground')
        if game:
            (baseline / 'player.gd').write_text('extends Node', encoding='utf8')
        else:
            (baseline / 'src').mkdir()
            (baseline / 'src' / ('layout-motion.js' if stage == 'svg' else 'panel-motion.js')).write_text('export const x=1', encoding='utf8')
        domain = 'game' if game else 'svg' if stage == 'svg' else 'web_panel'
        declared = policy.resolve_policy(policy.REVISED_ID, stage) if revised else None
        state = session.create_session(directory, domain, baseline, self.reference, 'Measure actual motion',
            experiment={'stage': stage, 'capture_lock': str(self.root / 'capture.lock')}, execution_policy=declared)
        return session.Session(directory), state

    def test_stage_budgets_are_complete_and_frozen(self):
        for stage in policy.STAGES:
            with self.subTest(stage=stage):
                value = policy.resolve_policy(policy.REVISED_ID, stage)
                self.assertEqual(3600, value['max_seconds'])
                self.assertEqual(5400 if stage == 'ground' else 3600, value['max_validation_seconds'])
                self.assertEqual(3600, value['max_initial_measurement_seconds'])
                self.assertEqual(3, value['max_candidates'])
                self.assertEqual(value, policy.validate_policy(value, stage=stage))

    def test_unknown_partial_cross_stage_and_boolean_policy_rejected(self):
        value = policy.resolve_policy(policy.REVISED_ID, 'panel')
        corrupt = [dict(value, max_seconds=3601), dict(value, max_candidates=True), dict(value, extra=1),
                   {k: v for k, v in value.items() if k != 'max_initial_measurement_seconds'}]
        for item in corrupt:
            with self.subTest(item=item), self.assertRaises(VisualError):
                policy.validate_policy(item)
        with self.assertRaises(VisualError):
            policy.validate_policy(value, stage='ground')
        with self.assertRaises(VisualError):
            policy.resolve_policy(policy.REVISED_ID)

    def test_legacy_session_does_not_gain_new_budget_or_schema(self):
        current, state = self.create(revised=False)
        self.assertEqual(1, state['schema_version'])
        self.assertNotIn('execution_policy', state['experiment'])
        self.assertEqual(session.LIMITS, state['budget'])
        self.assertEqual(1800, current.start_clock()['deadline_at'] - current._load()['started_at'])
        self.assertEqual(1800, policy.execution_job_limit(state, 'finalize'))

    def test_new_policy_is_bound_and_clock_never_restarts(self):
        current, state = self.create()
        first = current.start_clock()
        self.assertEqual(first, current.start_clock())
        self.assertEqual(2, state['schema_version'])
        self.assertEqual(3600, first['deadline_at'] - first['started_at'])
        state = read_json(current.directory / 'session.json')
        state['experiment']['execution_policy']['max_seconds'] = 7200
        session._atomic(current.directory / 'session.json', state)
        with self.assertRaises(VisualError) as caught:
            current.inspect()
        self.assertEqual('session_binding_changed', caught.exception.code)

    def test_launcher_prompt_and_config_use_session_policy(self):
        current, state = self.create('ground')
        config = launcher.configure(current.directory)
        self.assertEqual(state['experiment']['execution_policy'], config['execution_policy'])
        text = (current.directory / 'target/.kiro/instructions.md').read_text(encoding='utf8')
        self.assertIn('3600 seconds of model authoring', text)
        self.assertIn('5400-second validation budget', text)
        self.assertNotIn('1800 seconds', text)
        self.assertEqual(config, launcher.load_config(current.directory))

    def test_generic_job_watchdog_does_not_cut_new_authoring_at_thirty_minutes(self):
        for revised in (False, True):
            with self.subTest(revised=revised):
                current, _ = self.create(revised=revised)
                with patch.object(session.Session, '_spawn'):
                    request = current.submit('prepare')
                elapsed = [0.]
                wall = time.time()
                fake = SimpleNamespace(time=lambda: wall, monotonic=lambda: elapsed[0], sleep=lambda value: None)
                def evaluate(directory, job, check, progress, cancel):
                    elapsed[0] = 1900.
                    check()
                    return {'ready': True}
                with patch.object(session, 'time', fake):
                    session.run_session_job(current.directory, request['job_id'], runner=evaluate)
                result = current.get_job(request['job_id'])
                self.assertEqual('succeeded' if revised else 'failed', result['status'])
                if not revised:
                    self.assertEqual('job_deadline_exceeded', result['error']['code'])

    def test_final_deadline_has_its_own_submission_origin(self):
        current, state = self.create('ground')
        current.start_clock()
        final = {'operation': 'finalize', 'created_at': state['created_at'] + 3500}
        self.assertEqual(final['created_at'] + 5400, session.finalize_validation_deadline(current._load(), final))
        self.assertEqual(5400, policy.execution_job_limit(current._load(), 'finalize'))
        self.assertEqual(3600, policy.execution_job_limit(current._load(), 'evaluate'))

    def test_final_worker_can_finish_after_authoring_clock_without_old_job_cap(self):
        current, _ = self.create('ground')
        def submit(operation, result):
            with patch.object(session.Session, '_spawn'):
                job = current.submit(operation)
            session.run_session_job(current.directory, job['job_id'], runner=lambda *args: result)
            current.get_job(job['job_id'])
            return job
        submit('prepare', {'ready': True})
        submit('evaluate', {'eligible': True, 'score': 0.})
        with patch.object(session.Session, '_spawn'):
            final = current.submit('finalize')
        elapsed = [0.]
        wall = time.time()
        fake = SimpleNamespace(time=lambda: wall + elapsed[0], monotonic=lambda: elapsed[0], sleep=lambda value: None)
        def evaluate(directory, job, check, progress, cancel):
            elapsed[0] = 4000.
            check()  # After 60-minute authoring, before 90-minute ground validation.
            return {'pass': True}
        with patch.object(session, 'time', fake):
            session.run_session_job(current.directory, final['job_id'], runner=evaluate)
        self.assertEqual('succeeded', current.get_job(final['job_id'])['status'])

    def test_revised_schema_without_policy_or_with_wrong_version_rejected(self):
        for state in ({'schema_version': 2, 'experiment': {}},
                      {'schema_version': 1, 'experiment': {'execution_policy': policy.resolve_policy(policy.REVISED_ID, 'panel')},
                       'budget': policy.budget_from_policy(policy.resolve_policy(policy.REVISED_ID, 'panel'))}):
            with self.subTest(state=state), self.assertRaises(VisualError):
                policy.policy_from_state(state)

    def test_initial_measurement_defaults_follow_suite_version(self):
        self.assertEqual(1800, initial._suite_policy({'schema_version': 1})['max_initial_measurement_seconds'])
        new = {'schema_version': 2, 'stage': 'svg', 'execution_policy': policy.resolve_policy(policy.REVISED_ID, 'svg')}
        self.assertEqual(3600, initial._suite_policy(new)['max_initial_measurement_seconds'])
        with self.assertRaises(VisualError):
            initial._suite_policy(dict(new, stage='panel'))

    def test_new_initial_matrix_uses_new_scenarios_and_keeps_all_twenty_eight_cases(self):
        cases = [{'case_id': scenario['case_id'], 'role': role, 'stage': 'svg', 'purpose': 'primary',
                  'scenario': scenario, 'input_provenance': 'recorder_verified'}
                 for split, role in [('calibration', 'calibration'), ('known_regression', 'regression'), ('validation', 'validation')]
                 for scenario in suite_web_v2.scenarios('svg', split)]
        suite = {'schema_version': 2, 'domain': 'svg', 'stage': 'svg', 'heldout_status': 'provided',
                 'execution_policy': policy.resolve_policy(policy.REVISED_ID, 'svg'), 'cases': cases}
        self.assertEqual(28, len(initial._matrix(suite)))
        self.assertTrue(all(case['case_id'].startswith('realistic-') for case in initial._matrix(suite)))
        with self.assertRaises(VisualError):
            initial._matrix(dict(suite, cases=cases[:-1]))

    def test_passive_pause_never_sends_resume_or_extends_deadline(self):
        client = launcher.LocalACPClient.__new__(launcher.LocalACPClient)
        client.prompt_sent = True
        client.deadline = 123.
        client.passive_activity = policy.PassiveNativeActivity()
        client._send = Mock(side_effect=AssertionError('Passive observations cannot send a message'))
        event = {'method': '_kiro/system/notify', 'params': {'level': 'warning',
            'message': 'The model response paused unexpectedly. Waiting for it to resume…'}}
        with patch.object(launcher.NativeClient, '_receive', return_value=event):
            self.assertEqual(event, client._receive())
        self.assertEqual(123., client.deadline)
        client._send.assert_not_called()
        observed = client.passive_activity.summary()
        self.assertTrue(observed['upstream_pause_observed'])
        self.assertEqual('unknown', observed['provider_root_cause'])
        self.assertFalse(observed['automatic_resume_sent'])
        self.assertFalse(observed['prompt_retried'])

    def test_activity_tracks_thought_and_tool_times_separately(self):
        observed = policy.PassiveNativeActivity()
        for kind, status, timestamp in [('agent_thought_chunk', None, 1.), ('tool_call', 'pending', 2.),
                                         ('tool_call_update', 'in_progress', 3.), ('tool_call_update', 'completed', 4.)]:
            observed.observe({'method': 'session/update', 'params': {'update': {'sessionUpdate': kind, 'status': status}}}, timestamp)
        result = observed.summary()
        self.assertEqual(1., result['last_thought_at'])
        self.assertEqual(2., result['last_tool_started_at'])
        self.assertEqual(4., result['last_tool_finished_at'])
        self.assertEqual(4, result['events'])


if __name__ == '__main__':
    unittest.main()
