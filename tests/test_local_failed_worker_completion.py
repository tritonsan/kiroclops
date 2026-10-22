"""Regression guards for the verified post-cohort repair; no acceptance relaxation."""
import unittest
from copy import deepcopy
from kiroclops.local_engine import acceptance
from kiroclops.visual.schema import VisualError
import tests.test_local_postturn_native_audit as handoff_fixture
import tests.test_acceptance_axes as axes_fixture


class FailedHandoffTests(handoff_fixture.PostturnNativeAuditTests):
    def failed(self):
        error = {'code': 'capture_timing_failure', 'message': 'gap'}
        self.job.update(status='failed', error=error)
        self.state['final'].update(status='failed', pass_=False)
        self.state['final'].pop('pass_')
        self.state['final'].update({'pass': False, 'error': error})
        self.launch.update(status='finalization_failed', native_turn_status='completed')
        self.launch['terminal_cleanup']['terminal_job_status'] = 'failed'
        self.launch['postturn_finalization']['job_status'] = 'failed'

    def test_failed_worker_does_not_erase_bound_end_turn(self):
        self.failed()
        result = self.audit()
        self.assertEqual('pass', result['status'])
        self.assertEqual('failed', result['job_status'])
        self.assertFalse(self.state['final']['pass'])

    def test_failed_worker_requires_every_handoff_binding(self):
        self.failed()
        original = deepcopy(self.launch)
        variants = [lambda x: x.pop('postturn_finalization'),
                    lambda x: x.update(native_turn_status='timeout'),
                    lambda x: x['terminal_cleanup'].update(status='unconfirmed'),
                    lambda x: x['terminal_cleanup']['request'].update(preserved_finalization=False),
                    lambda x: x['postturn_finalization'].update(source_sha256='other'),
                    lambda x: x['postturn_finalization'].update(job_status='succeeded'),
                    lambda x: x['response'].update(stopReason='cancelled')]
        for change in variants:
            self.launch = deepcopy(original)
            change(self.launch)
            with self.assertRaises((VisualError, KeyError)):
                self.audit()

    def test_failed_worker_error_must_match_applied_state(self):
        self.failed()
        self.state['final']['error'] = {'code': 'different'}
        with self.assertRaises(VisualError):
            self.audit()

class CompletionAxesTests(axes_fixture.AcceptanceAxesTests):
    def result(self):
        return acceptance.summarize_acceptance(self.state, self.report, recordings=self.records)

    def failed(self):
        self.state['final'].update(status='failed', error={'code': 'capture_timing_failure'})
        self.report.update(overall_acceptance='fail', acceptance_details={'error': {'code': 'capture_timing_failure'}})
        self.report['kiro_run'].update(status='finalization_failed', native_turn_status='completed',
            postturn_finalization={'status': 'terminal', 'job_status': 'failed'},
            terminal_cleanup={'status': 'pass', 'request': {'preserved_finalization': True}})

    def test_completed_native_and_failed_capture_remain_distinct(self):
        self.failed()
        result = self.result()
        self.assertEqual('pass', result['axes']['model_completion']['status'])
        self.assertEqual('invalid', result['axes']['measurement_validity']['status'])
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])
        self.assertFalse(result['summary']['certified_success'])

    def test_no_claim_for_missing_generation_handoff_or_cleanup(self):
        self.failed()
        original = deepcopy(self.report['kiro_run'])
        for change in (lambda x: x.pop('postturn_finalization'),
                       lambda x: x.update(native_turn_status='timeout'),
                       lambda x: x['terminal_cleanup'].update(status='unconfirmed'),
                       lambda x: x['stream_configuration'].update(generation_configuration_verified=False)):
            self.report['kiro_run'] = deepcopy(original)
            change(self.report['kiro_run'])
            self.assertEqual('incomplete', self.result()['axes']['model_completion']['status'])
