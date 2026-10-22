"""The final envelope fixes a naming seam without changing recorded behavior."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import realistic_review as review
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_web_final_raw as consumer


def matrix(root, stage='panel'):
    cases, rows = [], []
    for split, count in (('calibration', 8 if stage == 'panel' else 4), ('regression', 12), ('validation', 12)):
        for number in range(count):
            case_id = f'realistic-{stage}-{split}-{number}'
            scenario = {'id': case_id, 'case_id': case_id, 'domain': stage,
                'suite_split': 'known_regression' if split == 'regression' else split,
                'split': 'validation' if split == 'regression' else split,
                'blind_validation': split == 'validation', 'events': [{'time_s': .4, 'action': 'click'}]}
            cases.append({'case_id': case_id, 'role': split, 'stage': stage, 'purpose': 'primary', 'scenario': scenario})
            for role in review.ROLES:
                record_id = f'{role}-{case_id}'
                rows.append({'id': record_id, 'case_id': case_id, 'split_role': split, 'role': role, 'domain': stage,
                    'selection': {'objects': [{'id': 'body'}]},
                    'record': {'scenario': deepcopy(scenario), 'package': str(root / record_id),
                        'source_sha256': 'a' * 64, 'observations': {'objects': [], 'package_sha256': 'b' * 64},
                        'events': {'events': []}, 'actual_input_times': [.4137]}})
    context = {'stage': stage, 'domain': 'web_panel' if stage == 'panel' else 'svg',
        'acceptance_policy': {'id': 'realistic-behavior-v1'}, 'cases': cases,
        'suite_sha256': 'c' * 64, 'candidate_sha256': 'd' * 64,
        'session_directory': str(root / 'session'), 'suite_directory': str(root / 'suite'),
        'selected_candidate_id': 'candidate-01', 'evaluation_job_id': 'evaluation', 'final_job_id': 'final'}
    return context, rows


class EnvelopeSemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc final envelope café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context, self.rows = matrix(self.root)

    def controller(self):
        self.context_patch = patch.object(review, '_session_context', return_value=(self.context, self.rows))
        self.proof_patch = patch.object(consumer, '_canonical_inputs', return_value={'canonical_input_files': {}, 'required_observations': []})
        self.context_patch.start()
        self.proof_patch.start()
        self.addCleanup(self.context_patch.stop)
        self.addCleanup(self.proof_patch.stop)
        return consumer._controller()

    def test_current_frozen_reader_red_seam_rejects_true_known_regression(self):
        regression = next(row for row in self.rows if row['split_role'] == 'regression')
        self.assertEqual(regression['record']['scenario']['suite_split'], 'known_regression')
        self.assertFalse(regression['record']['scenario']['blind_validation'])
        with self.assertRaises(VisualError) as caught:
            review._records([regression], 'panel', 'final')
        self.assertEqual(caught.exception.code, 'invalid_review')
        self.assertIn('held-out images as calibration', str(caught.exception))

    def test_all_four_roles_restore_exact_scenario_inputs_and_observations(self):
        before = deepcopy(self.rows)
        expected = [review.legacy._record(row, number) for number, row in enumerate(self.rows)]
        with self.controller() as binding:
            context, rows = review._session_context(self.root / 'session')
            actual = review._records(rows, 'panel', 'final')
            self.assertEqual(actual, expected)
            self.assertEqual(context['independent_consumer_version'], consumer.VERSION)
            self.assertEqual(binding['normalization']['alias_record_count'], 48)
        self.assertEqual(self.rows, before)

    def test_svg_uses_its_112_record_grid_and_same_exact_alias(self):
        self.context, self.rows = matrix(self.root, 'svg')
        with self.controller() as binding:
            _, rows = review._session_context(self.root / 'session')
            actual = review._records(rows, 'svg', 'final')
            self.assertEqual(len(actual), 112)
            self.assertEqual(binding['normalization']['alias_record_count'], 48)

    def test_legacy_validation_field_alone_is_not_known_regression_proof(self):
        for row in self.rows:
            if row['split_role'] == 'regression':
                row['record']['scenario'].pop('suite_split')
        for case in self.context['cases']:
            if case['role'] == 'regression':
                case['scenario'].pop('suite_split')
        with self.controller():
            with self.assertRaises(VisualError) as caught:
                review._session_context(self.root / 'session')
        self.assertEqual(caught.exception.code, 'unauthorized_regression_alias')

    def test_blind_validation_cannot_be_relabelled_regression(self):
        for row in self.rows:
            if row['split_role'] == 'regression':
                row['record']['scenario']['blind_validation'] = True
        for case in self.context['cases']:
            if case['role'] == 'regression':
                case['scenario']['blind_validation'] = True
        with self.controller():
            with self.assertRaises(VisualError):
                review._session_context(self.root / 'session')

    def test_calibration_cannot_use_known_or_validation_alias(self):
        for declared in ('known_regression', 'validation'):
            context, rows = matrix(self.root)
            context['cases'][0]['scenario']['suite_split'] = declared
            for row in rows[:4]:
                row['record']['scenario']['suite_split'] = declared
            with self.subTest(declared=declared), self.assertRaises(VisualError):
                consumer._matrix(context, rows)

    def test_changed_scenario_or_split_or_duplicate_or_missing_role_is_rejected(self):
        for mutation in ('scenario', 'split', 'duplicate', 'missing'):
            context, rows = matrix(self.root)
            if mutation == 'scenario':
                rows[0]['record']['scenario']['events'][0]['time_s'] = .6
            elif mutation == 'split':
                rows[0]['split_role'] = 'validation'
            elif mutation == 'duplicate':
                rows[-1] = deepcopy(rows[-2])
            else:
                rows.pop()
            with self.subTest(mutation=mutation), self.assertRaises(VisualError):
                consumer._matrix(context, rows)

    def test_same_recipe_other_policy_or_domain_is_rejected(self):
        for changes in ({'domain': 'game'}, {'stage': 'jump'}, {'acceptance_policy': {'id': 'legacy-v1'}}):
            with self.subTest(changes=changes), self.assertRaises(VisualError):
                consumer._matrix(self.context | changes, self.rows)

    def test_controller_is_final_only_and_cannot_process_substituted_record(self):
        with self.controller():
            _, rows = review._session_context(self.root / 'session')
            changed = deepcopy(rows)
            changed[0]['record']['source_sha256'] = 'f' * 64
            for values, phase in ((rows, 'calibration'), (changed, 'final')):
                with self.subTest(phase=phase), self.assertRaises(VisualError):
                    review._records(values, 'panel', phase)

    def test_controller_restores_on_exception_and_rejects_nested_scope(self):
        previous = (review.VERSION, review._session_context, review._records, review._code_sources)
        with self.assertRaisesRegex(RuntimeError, 'extraction failed'):
            with consumer._controller():
                with self.assertRaises(VisualError):
                    with consumer._controller():
                        pass
                raise RuntimeError('extraction failed')
        self.assertEqual((review.VERSION, review._session_context, review._records, review._code_sources), previous)
        self.assertFalse(consumer._active)


class FinalEnvelopeLifecycleTests(unittest.TestCase):
    """Exercise real strict reader/attestation/bytes around a small extraction stub."""
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc final proof café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context, self.rows = matrix(self.root)
        self.authority = self.root / 'session'
        self.authority.mkdir()
        self.pinned = self.authority / 'session.json'
        self.pinned.write_text('{"immutable":"sealed"}', encoding='utf-8')
        self.files = {str(self.pinned): file_sha256(self.pinned)}
        self.proof = {'canonical_input_files': self.files, 'required_observations': [['p', 'o']],
            'native_record_and_source_bindings_replayed': True,
            'native_model_generation_acceptance_granted': False, 'source_code_acceptance_granted': False}
        self.output = self.root / 'fresh-review'
        self.numeric_status = 'pass'
        for owner, name, change in (
            (review, '_session_context', {'return_value': (self.context, self.rows)}),
            (consumer, '_canonical_inputs', {'return_value': self.proof}),
            (review, '_code_sources', {'return_value': self.files}),
            (review, 'prepare_final_review', {'side_effect': self.prepare_pixels})):
            helper = patch.object(owner, name, **change)
            helper.start()
            self.addCleanup(helper.stop)

    def prepare_pixels(self, directory, output):
        context, rows = review._session_context(directory)
        checked = review._records(rows, context['stage'], 'final')
        output.mkdir()
        result = {'kind': 'RealisticIndependentPixelProposal', 'schema_version': 2, 'version': review.VERSION,
            'phase': 'final', 'domain': 'panel', 'numeric_status': self.numeric_status,
            'context': context, 'records': [{'record_id': row['id']} for row in checked], 'sheets': []}
        write_json(output / 'result.json', result)
        write_json(output / 'evidence-receipt.json', {'result_sha256': digest(result), 'source_files': self.files,
            'code_sources': review._code_sources(), 'generated_files': {'result.json': file_sha256(output / 'result.json')},
            'runtime': {'opencv': cv2.__version__, 'numpy': np.__version__, 'opencv_build_sha256': digest(cv2.getBuildInformation())}})
        return {'numeric_status': self.numeric_status, 'sheet_count': 0, 'full_behavior_acceptance': False}

    def test_fresh_controller_replays_bound_seal_archive_and_full_result(self):
        value = consumer.prepare(self.authority, self.output)
        self.assertEqual(value['result'], read_json(self.output / 'result.json'))
        self.assertEqual(value['consumer_binding']['record_count'], 128)
        consumer.attest(self.output, session_directory=self.authority, reviewer='actual test fixture inspection',
            inspected_sheets=[], accepted=True, images_inspected=True)
        verified = consumer.validate(self.output, session_directory=self.authority)
        self.assertEqual(verified['status'], 'pass')
        self.assertEqual(verified['required_observations'], [['p', 'o']])
        self.assertFalse(verified['full_behavior_acceptance'])
        self.assertEqual(verified['native_model_generation_review_status'], 'pending')
        self.assertEqual(read_json(self.pinned), {'immutable': 'sealed'})

    def test_original_reader_cannot_accept_extended_version(self):
        consumer.prepare(self.authority, self.output)
        with self.assertRaises(VisualError) as caught:
            review.validate_review(self.output, require_attested=False)
        self.assertEqual(caught.exception.code, 'stale_review')

    def test_numeric_failure_remains_failure_and_cannot_be_attested_success(self):
        self.numeric_status = 'fail'
        consumer.prepare(self.authority, self.output)
        proposal = consumer.validate(self.output, session_directory=self.authority, require_attested=False)
        self.assertEqual(proposal['numeric_status'], 'fail')
        self.assertEqual(proposal['verified_scopes'], {})
        with self.assertRaises(VisualError):
            consumer.attest(self.output, session_directory=self.authority, reviewer='test fixture',
                inspected_sheets=[], accepted=True, images_inspected=True)
        self.assertFalse((self.output / 'attestation.json').exists())

    def test_producer_archive_or_normalization_tamper_invalidates_verification(self):
        consumer.prepare(self.authority, self.output)
        archive = Path(read_json(self.output / 'consumer-binding.json')['producer_archive'])
        archive.write_text('# substituted consumer', encoding='utf-8')
        with self.assertRaises(VisualError):
            consumer.validate(self.output, session_directory=self.authority, require_attested=False)

    def test_canonical_seal_changed_after_extraction_is_not_rebound(self):
        consumer.prepare(self.authority, self.output)
        self.pinned.write_text('{"immutable":"different"}', encoding='utf-8')
        with self.assertRaises(VisualError):
            consumer.validate(self.output, session_directory=self.authority, require_attested=False)


if __name__ == '__main__':
    unittest.main()
