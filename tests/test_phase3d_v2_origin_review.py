"""Source-axis fixtures only; historical validator is explicitly mocked."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import phase3d_v2_origin_review as origin_review
from scripts import phase3d_v2_unchanged as unchanged
from tests import test_phase3d_v2_unchanged as unchanged_fixtures
from kiroclops.local_engine.session import JOB_BINDING_FIELDS, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class ExistingOriginSourceAxisTests(unittest.TestCase):
    def setUp(self):
        fixture = unchanged_fixtures.UnchangedV2Tests('test_svg_and_panel_use_canonical_jobs_and_never_claim_new_generation')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.output = fixture.root / 'source-axis'
        self.make_historical_review()
        self.materialize_stage()

    def make_historical_review(self):
        f = self.fixture
        f.history.state.update(selected_candidate_id='candidate-01', baseline_hashes={**f.state['baseline_hashes'], f.entry: '0' * 64})
        f.history._save()
        f.origin_value['historical_binding_files'][str(f.historical / 'session.json')] = file_sha256(f.historical / 'session.json')
        write_json(f.origin, f.origin_value)
        f.origin_sha = file_sha256(f.origin)
        write_json(f.historical / 'kiro/launch.json', {'model': 'claude-opus-5.5', 'effort': 'max',
            'cli_version': 'fixture historical CLI', 'kas_version': 'fixture historical KAS'})
        write_json(f.historical / 'kiro/config.json', {'fixture': 'historical config'})
        for name in ('kiro/run/stdout.jsonl', 'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl'):
            path = f.historical / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture historical evidence', encoding='utf8')
        native = {'status': 'pass', 'model': 'claude-opus-5.5', 'effort': 'max', 'native_session_id': 'fixture-native-history',
            'evidence_hashes': {name: file_sha256(f.historical / name) for name in
                ('kiro/launch.json', 'kiro/config.json', 'kiro/run/stdout.jsonl', 'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl')}}
        self.source_review = f.root / 'historical-source-review.json'
        value = {'kind': 'IndependentSourceReview', 'status': 'pass', 'source_hashes': f.state['baseline_hashes'],
            'candidate_sha256': digest(f.state['baseline_hashes']), 'session_directory': str(f.historical),
            'full_behavior_acceptance': False, 'native_validation': native}
        write_json(self.source_review, {key: item for key, item in value.items() if key != 'native_validation'})
        self.validated_historical_review = value

    def materialize_stage(self):
        f = self.fixture
        self.stage, _ = f.invoke()
        f.session.state.update(status='finalized', final={'job_id': f.session.state['final_job_id'], 'status': 'succeeded', 'pass': f.final_pass})
        f.session._save()
        for item in self.stage['completed_jobs']:
            path = f.directory / 'jobs' / item['job_id']
            job = {key: deepcopy(value) for key, value in item.items() if key != 'result'}
            job.update(schema_version=1, source_hashes=f.state['baseline_hashes'],
                source_path=str(f.directory / 'baseline' if item['operation'] == 'prepare' else f.directory / 'candidates/candidate-01/source'),
                result_sha256=digest(item['result']))
            write_json(path / 'job.json', job)
            write_json(path / 'binding.json', {'sha256': digest({key: job[key] for key in JOB_BINDING_FIELDS})})
            write_json(path / 'result.json', item['result'])
        self.rebind_stage()

    def rebind_stage(self):
        f = self.fixture
        files = {str(f.directory / name): file_sha256(f.directory / name)
            for name in ('session.json', 'binding.json', 'admission.json', 'unchanged-stage-intent.json')}
        for item in self.stage['completed_jobs']:
            files.update({str(f.directory / 'jobs' / item['job_id'] / name): file_sha256(f.directory / 'jobs' / item['job_id'] / name)
                for name in ('job.json', 'binding.json', 'result.json')})
        files.update({str(f.directory / 'candidates/candidate-01/source' / name): sha
                      for name, sha in f.state['baseline_hashes'].items()})
        write_json(f.output / 'canonical-bindings.json', {'kind': 'NoCodeChangeCanonicalEvidence', 'schema_version': 1,
            'status': 'complete', 'errors': [], 'files': files})
        write_json(f.output / 'result.json', self.stage)
        output_files = source_hashes(f.output)
        output_files.pop('result-binding.json', None)
        write_json(f.output / 'result-binding.json', {'kind': 'NoCodeChangeStageResultReceipt', 'schema_version': 1,
            'result_sha256': digest(self.stage), 'output_file_hashes': output_files})

    def invoke(self, validated=None, validation_error=None):
        f = self.fixture
        def factory(path):
            if Path(path) == f.directory:
                return f.session
            if Path(path) == f.historical:
                return f.history
            raise AssertionError('No unrelated Sessions or job reconciliation')
        with patch.object(origin_review, 'Session', side_effect=factory), \
             patch.object(unchanged, 'Session', side_effect=factory), \
             patch.object(origin_review.review, 'validate_source_review', side_effect=validation_error,
                 return_value=deepcopy(validated if validated is not None else self.validated_historical_review)) as validation:
            result = origin_review.run(f.directory, f.output, f.origin, f.origin_sha, self.source_review, self.output)
        return result, validation

    def test_historical_authorship_plus_new_exact_snapshot_is_source_axis_only(self):
        result, validation = self.invoke()
        f = self.fixture
        self.assertEqual('pass', result['source_axis_status'])
        self.assertEqual('candidate-01', result['selected_measurement_candidate_id'])
        self.assertEqual('candidate-01', result['historical_generation_scope']['historical_candidate_id'])
        self.assertTrue(result['historical_generation_scope']['historical_only'])
        self.assertEqual('pass', result['historical_generation_scope']['historical_native_replay_status'])
        self.assertNotEqual(result['historical_clean_baseline_hashes'], result['new_measurement_baseline_hashes'])
        self.assertEqual('pass', result['numerical_behavior_status'])
        for key in ('new_model_generation', 'new_session_native_generation_audited', 'model_started',
                    'app_or_capture_started', 'code_written', 'full_behavior_acceptance'):
            self.assertFalse(result[key])
        validation.assert_called_once_with(self.source_review, f.historical, candidate_sha256=digest(f.state['baseline_hashes']))
        self.assertFalse((f.directory / 'source_review.json').exists())
        receipt = read_json(self.output / 'receipt.json')
        self.assertEqual(digest(result), receipt['source_axis_sha256'])
        self.assertFalse(receipt['full_behavior_acceptance'])

    def test_numerical_failure_can_pass_source_axis_without_being_accepted(self):
        f = self.fixture
        self.stage['numerical_pass'] = False
        self.stage['status'] = 'numerical_failure'
        self.stage['completed_jobs'][-1]['result']['pass'] = False
        path = f.directory / 'jobs' / f.session.state['final_job_id']
        write_json(path / 'result.json', self.stage['completed_jobs'][-1]['result'])
        job = read_json(path / 'job.json')
        job['result_sha256'] = digest(self.stage['completed_jobs'][-1]['result'])
        write_json(path / 'job.json', job)
        f.session.state['final']['pass'] = False
        f.session._save()
        self.rebind_stage()
        result, _ = self.invoke()
        self.assertEqual('pass', result['source_axis_status'])
        self.assertEqual('fail', result['numerical_behavior_status'])
        self.assertFalse(result['full_behavior_acceptance'])

    def test_panel_historical_review_needs_no_relabeling_of_old_receipt(self):
        f = self.fixture
        f.make_fixture('panel')
        self.make_historical_review()
        self.materialize_stage()
        self.assertNotIn('historical_only', read_json(self.source_review))
        result, _ = self.invoke()
        self.assertEqual('pass', result['source_axis_status'])
        self.assertTrue(result['historical_generation_scope']['historical_only'])
        self.assertNotIn('historical_only', read_json(self.source_review))
        self.assertFalse(result['full_behavior_acceptance'])

    def test_changed_selected_source_or_target_is_rejected(self):
        f = self.fixture
        (f.directory / 'target' / f.entry).write_text('changed target', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('source_changed', error.exception.code)
        self.assertFalse(self.output.exists())
        (f.directory / 'target' / f.entry).write_text('existing Kiro behavior code', encoding='utf8')
        (f.directory / 'candidates/candidate-01/source' / f.entry).write_text('changed selected snapshot', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('source_changed', error.exception.code)

    def test_own_model_launch_or_false_generation_claim_is_rejected(self):
        f = self.fixture
        write_json(f.directory / 'kiro/launch.json', {'status': 'completed'})
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('model_already_started', error.exception.code)
        (f.directory / 'kiro/launch.json').unlink()
        self.stage['new_model_generation'] = True
        self.rebind_stage()
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('incomplete_unchanged_stage', error.exception.code)

    def test_wrong_job_snapshot_is_rejected_even_with_rebound_output(self):
        self.stage['completed_jobs'][-1]['source_sha256'] = '0' * 64
        self.rebind_stage()
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('wrong_job', error.exception.code)

    def test_historical_validator_failure_is_not_an_accepted_origin(self):
        with self.assertRaises(VisualError) as error:
            self.invoke(validation_error=VisualError('invalid_native_evidence', 'fixture stale historical trace'))
        self.assertEqual('invalid_native_evidence', error.exception.code)
        self.assertFalse(self.output.exists())
        wrong = deepcopy(self.validated_historical_review)
        wrong['source_hashes'] = {'different': '0' * 64}
        with self.assertRaises(VisualError) as error:
            self.invoke(validated=wrong)
        self.assertEqual('historical_source_not_qualified', error.exception.code)

    def test_no_source_axis_can_transfer_old_full_behavior_claim(self):
        wrong = deepcopy(self.validated_historical_review)
        wrong['full_behavior_acceptance'] = True
        with self.assertRaises(VisualError) as error:
            self.invoke(validated=wrong)
        self.assertEqual('historical_source_not_qualified', error.exception.code)
        self.assertFalse(self.output.exists())

    def test_historical_native_files_and_stage_receipt_are_byte_bound(self):
        f = self.fixture
        (f.historical / 'kiro/run/stdout.jsonl').write_text('changed old native trace', encoding='utf8')
        with self.assertRaises(VisualError) as error:
            self.invoke()
        self.assertEqual('source_changed', error.exception.code)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
