from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.acceptance import recording_fact, summarize_acceptance
from kiroclops.local_engine import reporting
from kiroclops.local_engine.session import Session, create_session, run_session_job
from kiroclops.visual.schema import VisualError, read_json


class AcceptanceAxesTests(unittest.TestCase):
    def setUp(self):
        self.state = {'domain': 'web_panel', 'selected_candidate_id': 'candidate-01',
            'experiment': {}, 'final': {'job_id': 'final-01', 'status': 'succeeded', 'pass': True},
            'candidates': [{'candidate_id': 'candidate-01', 'score': .01, 'status': 'succeeded'}]}
        self.details = {'pass': True, 'passed': 12, 'required': 12, 'comparisons': [{
            'measurement_pass': True, 'functional': {'status': 'pass'},
            'pixel_comparison': {'measurement_status': 'pass', 'numerical_status': 'pass'},
            'dynamics': [{'observable': True, 'pass': True}], 'improvement': [{'pass': True}]}]}
        self.report = {'overall_acceptance': 'pass', 'acceptance_details': self.details,
            'native_run_status': 'pass', 'native_run': {'status': 'pass'},
            'independent_review_status': 'pass', 'review_coverage_status': 'pass',
            'source_review_status': 'pass', 'candidate_sha256': 'source',
            'kiro_run': {'status': 'completed', 'model': 'claude-opus-5.5', 'effort': 'max',
                'response': {'stopReason': 'end_turn'}, 'terminal_cleanup': {'status': 'pass'},
                'stream_configuration': {'generation_configuration_verified': True}}}
        self.records = [recording_fact({'measurement_status': 'ok', 'timing': {'status': 'pass'},
            'functional': {'status': 'pass'}, 'scenario': {'id': 'open'}},
            job_id='final-01', role=role, context='final-01') for role in ('reference', 'before', 'final')]

    def result(self):
        return summarize_acceptance(self.state, self.report, recordings=self.records)

    def test_complete_evidence_is_fully_validated(self):
        result = self.result()
        self.assertEqual('fully_validated', result['summary']['status'])
        self.assertTrue(result['summary']['certified_success'])
        self.assertTrue(all(axis['status'] == 'pass' for axis in result['axes'].values()))

    def test_suite_ground_projects_saved_gates_without_rescoring(self):
        self.details.update(suite_sha256='frozen-suite',preservation={'status':'pass'},comparisons=[{
            'stage':'ground','measurement_pass':True,'numerical_status':'pass',
            'improvement_required':True,'gates':{'improvement':True}}])
        self.assertEqual('pass',self.result()['axes']['observed_behavior']['status'])
        self.details['comparisons'][0]['gates']['improvement']=False
        self.assertEqual('fail',self.result()['axes']['observed_behavior']['status'])

    def test_aggregate_game_pass_does_not_invent_saved_pixel_evidence(self):
        self.details.update(suite_sha256='frozen-suite',comparisons=[{'stage':'jump','pass':True,'measurement_pass':True}])
        self.assertEqual('incomplete',self.result()['axes']['observed_behavior']['status'])

    def test_missing_native_evidence_does_not_erase_behavior_success(self):
        self.report.update(overall_acceptance='fail', native_run_status='fail',
                           native_run={'code': 'invalid_native_evidence', 'message': 'Missing decision'})
        result = self.result()
        self.assertEqual('pass', result['axes']['observed_behavior']['status'])
        self.assertEqual('pass', result['axes']['model_completion']['status'])
        self.assertEqual('invalid', result['axes']['native_evidence']['status'])
        self.assertEqual('not_fully_validated', result['summary']['status'])
        self.assertEqual('fail', result['summary']['legacy_aggregate'])
        self.assertFalse(result['summary']['certified_success'])
        self.assertEqual('unknown', result['axes']['model_completion']['evidence']['root_cause_of_missing_evidence'])

    def test_absent_audit_is_incomplete_never_pass(self):
        self.report.pop('native_run_status')
        self.report.pop('native_run')
        result = self.result()
        self.assertEqual('incomplete', result['axes']['native_evidence']['status'])
        self.assertFalse(result['summary']['certified_success'])

    def test_uploaded_reference_timestamps_are_not_native_capture_status(self):
        self.state['experiment']['reference_mode'] = 'media_bundle'
        self.records[0]['timing_status'] = 'source_timestamps'
        self.assertEqual('pass', self.result()['axes']['measurement_validity']['status'])
        self.assertEqual('source_timestamps', self.result()['axes']['measurement_validity']['evidence']['recordings'][0]['timing_status'])
        self.records[-1]['timing_status'] = 'source_timestamps'
        self.assertEqual('incomplete', self.result()['axes']['measurement_validity']['status'])

    def test_partial_clip_false_aggregate_is_not_motion_failure(self):
        self.details['pass'] = False
        self.details.update(calibration_pass=True, acceptance_status='partial_unobserved_holdout', heldout_status='not_provided')
        self.details.pop('required'); self.details.pop('passed')
        self.state['final']['pass'] = False
        self.report['overall_acceptance'] = 'partial'
        result = self.result()
        self.assertEqual('pass', result['axes']['observed_behavior']['status'])
        self.assertEqual('supplied_clip_calibration_only', result['axes']['observed_behavior']['scope'])
        self.assertEqual('not_evaluated', result['axes']['unseen_validation']['status'])
        self.assertFalse(result['summary']['certified_success'])
        self.assertFalse(self.details['pass'])

    def test_actual_motion_failure_is_separate_from_valid_measurement(self):
        self.details['comparisons'][0]['pixel_comparison']['numerical_status'] = 'fail'
        self.details['pass'] = False
        self.report['overall_acceptance'] = 'fail'
        result = self.result()
        self.assertEqual('pass', result['axes']['measurement_validity']['status'])
        self.assertEqual('fail', result['axes']['observed_behavior']['status'])
        self.assertEqual('behavior_mismatch', result['summary']['status'])

    def test_nested_comparison_measurement_failure_prevents_behavior_verdict(self):
        self.details['comparisons'][0]['pixel_comparison'].update(measurement_status='fail', numerical_status='fail')
        result = self.result()
        self.assertEqual('invalid', result['axes']['measurement_validity']['status'])
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])
        row = self.details['comparisons'][0]
        row['windows'] = [row.pop('pixel_comparison')]
        self.assertEqual('invalid', self.result()['axes']['measurement_validity']['status'])

    def test_unseen_not_provided_is_preserved_in_integrity_evidence(self):
        self.state['experiment']['heldout_status'] = 'not_provided'
        result = self.result()
        self.assertEqual('not_evaluated', result['axes']['unseen_validation']['status'])
        self.assertFalse(result['axes']['application_integrity']['evidence']['unseen_interactions_included'])

    def test_failed_finalization_has_no_executed_unseen_claim(self):
        self.state['final']['status'] = 'failed'
        self.report['acceptance_details'] = {'error': {'code': 'worker_interrupted'}}
        result = self.result()
        self.assertFalse(result['axes']['application_integrity']['evidence']['unseen_interactions_included'])
        self.assertEqual('incomplete', result['axes']['unseen_validation']['status'])

    def test_capture_invalid_with_no_score_is_not_motion_failure(self):
        self.state.update(selected_candidate_id=None, final=None, candidates=[{
            'status': 'failed', 'score': None, 'error': {'code': 'capture_timing_failure'}}])
        self.report['acceptance_details'] = None
        result = self.result()
        self.assertEqual('invalid', result['axes']['measurement_validity']['status'])
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])
        self.assertEqual('measurement_invalid', result['summary']['status'])

    def test_invalid_final_ignores_earlier_good_candidate_records(self):
        self.state['final'] = {'job_id': 'final-02', 'status': 'failed', 'error': {'code': 'tracking_failure'}}
        self.report['acceptance_details'] = {'status': 'failed', 'error': {'code': 'tracking_failure'}}
        result = self.result()
        self.assertEqual([], result['axes']['measurement_validity']['evidence']['recordings'])
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])

    def test_no_selected_no_score_cannot_claim_evaluated_motion(self):
        self.state.update(selected_candidate_id=None, final=None, candidates=[])
        self.report['acceptance_details'] = None
        result = self.result()
        self.assertEqual('not_evaluated', result['axes']['observed_behavior']['status'])
        self.assertEqual('not_evaluated', result['axes']['measurement_validity']['status'])

    def test_functional_failure_does_not_become_pixel_mismatch(self):
        self.details['comparisons'][0]['functional']['status'] = 'fail'
        self.records[-1]['functional_status'] = 'fail'
        self.details['pass'] = False
        self.report['overall_acceptance'] = 'fail'
        result = self.result()
        self.assertEqual('pass', result['axes']['observed_behavior']['status'])
        self.assertEqual('fail', result['axes']['application_integrity']['status'])
        self.assertFalse(result['summary']['certified_success'])

    def test_interrupted_model_is_not_inferred_from_audit_missing(self):
        self.report['kiro_run']['status'] = 'timeout'
        self.report['kiro_run'].pop('response')
        result = self.result()
        self.assertEqual('incomplete', result['axes']['model_completion']['status'])
        self.assertEqual('timeout', result['axes']['model_completion']['evidence']['launch_status'])
        self.assertEqual('pass', result['axes']['observed_behavior']['status'])

    def test_unverified_model_configuration_cannot_pass_completion(self):
        self.report['kiro_run']['stream_configuration']['generation_configuration_verified'] = False
        self.assertEqual('incomplete', self.result()['axes']['model_completion']['status'])

    def test_invalid_manual_or_source_review_blocks_certification(self):
        for field in ('independent_review_status', 'review_coverage_status', 'source_review_status'):
            with self.subTest(field=field):
                original = self.report[field]
                self.report[field] = 'fail'
                self.assertFalse(self.result()['summary']['certified_success'])
                self.report[field] = original

    def test_legacy_failure_never_becomes_certified_by_presentation(self):
        self.report['overall_acceptance'] = 'fail'
        self.assertFalse(self.result()['summary']['certified_success'])

    def test_legacy_game_schema_uses_motion_and_preservation_separately(self):
        self.state['domain'] = 'game'
        self.report['acceptance_details'] = {'pass': False, 'motion_status': 'pass', 'passed': 18, 'required': 18,
            'comparison': {'numerical_status': 'pass'}, 'preservation': {'status': 'fail'}}
        self.report['overall_acceptance'] = 'fail'
        result = self.result()
        self.assertEqual('pass', result['axes']['observed_behavior']['status'])
        self.assertEqual('fail', result['axes']['application_integrity']['status'])

    def test_svg_relationship_failure_remains_observed_failure(self):
        self.state['domain'] = 'svg'
        self.details['comparisons'][0]['svg_geometry_preservation'] = {'pass': False}
        self.assertEqual('fail', self.result()['axes']['observed_behavior']['status'])

    def test_game_visual_boundary_failure_remains_observed_failure(self):
        self.state['domain'] = 'game'
        self.report['acceptance_details'] = {'pass': False, 'motion_status': 'pass', 'visual_wall_status': 'fail',
            'passed': 18, 'required': 18, 'preservation': {'status': 'pass'}}
        self.assertEqual('fail', self.result()['axes']['observed_behavior']['status'])

    def test_unobservable_dynamic_is_not_treated_as_failed_behavior(self):
        self.details['comparisons'][0]['dynamics'].append({'observable': False, 'pass': False})
        self.assertEqual('pass', self.result()['axes']['observed_behavior']['status'])

    def test_presentation_does_not_mutate_saved_inputs(self):
        before = deepcopy((self.state, self.report, self.records))
        self.result()
        self.assertEqual(before, (self.state, self.report, self.records))


class ReportAxesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='axes café ')
        self.root = Path(self.temp.name)
        baseline = self.root/'original'; (baseline/'src').mkdir(parents=True)
        (baseline/'src/panel-motion.js').write_text('export const speed=1;')
        (baseline/'index.html').write_text('protected')
        ref = self.root/'reference'; ref.mkdir(); (ref/'private.js').write_text('private')
        self.session = self.root/'session'
        create_session(self.session, 'web_panel', baseline, ref, 'Match motion',
                       experiment={'capture_lock': str(self.root/'capture.lock')})

    def tearDown(self):
        self.temp.cleanup()

    def complete(self, operation, result):
        session = Session(self.session)
        with patch.object(Session, '_spawn'):
            job = session.submit(operation)
        run_session_job(self.session, job['job_id'], runner=lambda *args: result)
        session.get_job(job['job_id'])

    def test_new_report_retains_raw_verdict_and_prior_report_bytes(self):
        self.complete('prepare', {})
        self.complete('evaluate', {'eligible': True, 'score': .01})
        self.complete('finalize', {'pass': False, 'calibration_pass': True,
                                 'acceptance_status': 'partial_unobserved_holdout'})
        first = reporting.build_report(self.session, encode=False)
        prior = Path(first['data']).read_bytes()
        second = reporting.build_report(self.session, output=self.session/'report-02', encode=False)
        value = read_json(second['data'])
        self.assertEqual(prior, Path(first['data']).read_bytes())
        self.assertFalse(value['numerical']['pass'])
        self.assertEqual('partial', value['overall_acceptance'])
        self.assertFalse(value['acceptance_summary']['certified_success'])
        self.assertEqual('not_evaluated', value['acceptance_axes']['unseen_validation']['status'])
        self.assertIn('Unchanged strict certification aggregate', Path(second['report']).read_text('utf-8'))
        self.assertIn('Separate acceptance checks', Path(second['report']).read_text('utf-8'))
        self.assertFalse(value['tolerance_assessment']['acceptance_changed'])

    def test_tampered_selected_source_still_blocks_new_report(self):
        self.complete('prepare', {})
        self.complete('evaluate', {'eligible': True, 'score': .01})
        self.complete('finalize', {'pass': True})
        (self.session/'candidates/candidate-01/source/src/panel-motion.js').write_text('tampered')
        with self.assertRaises(VisualError) as error:
            reporting.build_report(self.session, output=self.session/'fresh-report', encode=False)
        self.assertEqual('source_changed', error.exception.code)


if __name__ == '__main__':
    unittest.main()
