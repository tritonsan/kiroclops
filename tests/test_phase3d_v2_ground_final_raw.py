"""Canonical independent consumer keeps gates, provenance and failure scopes."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.local_engine import suite_final_raw_review as final, suite_generic_raw_review as raw
from scripts import phase3d_v2_ground_final_raw as consumer


class CanonicalGroundControllerTests(unittest.TestCase):
    def setUp(self):
        self.parent_binding = patch.object(consumer, '_parent_chain', return_value={'path': 'pinned-parent'})
        self.parent_mock = self.parent_binding.start()
        self.addCleanup(self.parent_binding.stop)

    def context(self, **changes):
        value = {'domain': 'game', 'stage': 'ground', 'kind': 'SealedSessionFinalRawMatrix',
                 'cases': [{} for _ in range(72)]}
        value.update(changes)
        return value, [{} for _ in range(296)]

    def test_context_is_canonical_complete_and_parent_bound(self):
        with patch.object(final, '_context_session', return_value=self.context()):
            with consumer._controller('parent.json', 'a' * 64):
                context, rows = final._context_session('session')
                self.assertEqual(len(rows), 296)
                self.assertEqual(context['independent_consumer_version'], consumer.VERSION)
                self.assertEqual(context['prospective_parent_chain'], {'path': 'pinned-parent'})
        self.parent_mock.assert_called_once()

    def test_jump_or_historical_failed_seal_is_rejected(self):
        for changed in ({'stage': 'jump'}, {'kind': 'UnchangedGameMixedEpochSupplementalRawMatrix'}, {'domain': 'svg'}):
            with self.subTest(changed=changed):
                with patch.object(final, '_context_session', return_value=self.context(**changed)):
                    with consumer._controller('parent.json', 'a' * 64):
                        with self.assertRaises(VisualError) as caught:
                            final._context_session('session')
                        self.assertEqual(caught.exception.code, 'wrong_final_seal')

    def test_missing_role_record_is_rejected(self):
        context, rows = self.context()
        rows.pop()
        with patch.object(final, '_context_session', return_value=(context, rows)):
            with consumer._controller('parent.json', 'a' * 64):
                with self.assertRaises(VisualError):
                    final._context_session('session')

    def test_all_callbacks_are_restored_on_exception_and_numeric_gates_stay_same(self):
        prior = (final.VERSION, final._context_session, final._code_sources, final._extract_boundary,
                 raw._raw_events, raw._compare_events)
        untouched = (raw._positions, raw._jump, final._boundary_events, raw._sheets)
        with self.assertRaisesRegex(RuntimeError, 'deliberate failure'):
            with consumer._controller('parent.json', 'a' * 64):
                self.assertEqual((raw._positions, raw._jump, final._boundary_events, raw._sheets), untouched)
                raise RuntimeError('deliberate failure')
        self.assertEqual((final.VERSION, final._context_session, final._code_sources, final._extract_boundary,
                          raw._raw_events, raw._compare_events), prior)
        self.assertFalse(consumer._active)

    def test_nested_controller_is_rejected_and_outer_still_restores(self):
        previous = final.VERSION
        with consumer._controller('parent.json', 'a' * 64):
            with self.assertRaises(VisualError) as caught:
                with consumer._controller('other.json', 'b' * 64):
                    pass
            self.assertEqual(caught.exception.code, 'controller_active')
            self.assertEqual(final.VERSION, consumer.VERSION)
        self.assertEqual(final.VERSION, previous)

    @staticmethod
    def bundle(provenance='unknown'):
        return {'record': {'id': 'fresh-sealed-record', 'domain': 'game'},
            'manifest': {'inputs': {'provenance': provenance, 'events': []}},
            'mapping': {'player': 'character'}, 'rows': [
                {'index': i, 'time_s': i / 60, 'points': {'player': {'x': 10., 'y': 10.}}} for i in range(4)]}

    def test_unknown_input_does_not_gain_known_provenance_or_suppress_unbound_overshoot(self):
        bundle = self.bundle()
        raw_event = {'type': 'overshoot', 'object_ids': ['player'], 'indices': [0, 1],
                     'observed_interval_s': [0., 1 / 60], 'timing_status': 'resolved', 'uncertainty_s': 1 / 60}
        def compare(data, contract):
            events, relations = raw._raw_events(data, contract)
            return {'raw_events': events, 'relation_checks': relations}, [{'code': 'existing_numeric_failure'}]
        policy = {'rest_duration_s': .2, 'rest_radius_px': 1.5, 'max_gap_s': .12}
        with patch.object(raw, '_raw_events', return_value=([raw_event], [])), patch.object(raw, '_compare_events', side_effect=compare):
            with consumer._controller('parent.json', 'a' * 64):
                compared, failures = raw._compare_events(bundle, policy)
        self.assertEqual(compared['raw_events'], [raw_event])
        self.assertEqual(compared['public_input_episode_review']['public_input_provenance'], 'unknown')
        self.assertEqual(bundle['manifest']['inputs']['provenance'], 'unknown')
        self.assertIn('existing_numeric_failure', [f['code'] for f in failures])
        self.assertIn('raw_input_episode_binding_unresolved', [f['code'] for f in failures])

    def test_relation_checks_are_not_removed_by_public_input_filter(self):
        bundle = self.bundle('recorder_verified')
        relations = [{'type': 'a-required-relationship', 'index': 2}]
        policy = {'rest_duration_s': .2, 'rest_radius_px': 1.5, 'max_gap_s': .12}
        with patch.object(raw, '_raw_events', return_value=([], relations)):
            with consumer._controller('parent.json', 'a' * 64):
                events, actual = raw._raw_events(bundle, policy)
        self.assertEqual(events, [])
        self.assertEqual(actual, relations)

    def test_unknown_or_declared_inputs_fail_canonical_provenance_even_without_overshoot(self):
        policy = {'rest_duration_s': .2, 'rest_radius_px': 1.5, 'max_gap_s': .12}
        def compare(data, contract):
            events, relations = raw._raw_events(data, contract)
            return {'raw_events': events, 'relation_checks': relations}, []
        for provenance in ('unknown', 'user_declared'):
            with self.subTest(provenance=provenance):
                bundle = self.bundle(provenance)
                with patch.object(raw, '_raw_events', return_value=([], [])), patch.object(raw, '_compare_events', side_effect=compare):
                    with consumer._controller('parent.json', 'a' * 64):
                        compared, failures = raw._compare_events(bundle, policy)
                self.assertIn('raw_input_history_not_recorder_verified', [item['code'] for item in failures])
                self.assertEqual(compared['public_input_episode_review']['public_input_provenance'], provenance)


class FreshStaticWallExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc fresh wall café ')
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        image = np.zeros((128, 256, 3), dtype=np.uint8)
        image[8:121, 192:209] = [90, 110, 150]
        self.images = [image.copy() for _ in range(5)]
        self.paths = []
        for i in range(5):
            path = self.output / f'{i}.png'
            path.write_bytes(bytes([i, 42]))
            self.paths.append(path)
        self.rows = [{'index': i, 'time_s': i / 60, 'image_sha256': file_sha256(path),
            'points': {'character': {'x': 90., 'y': 90.}}, 'connections': {}}
            for i, path in enumerate(self.paths)]
        self.bundle = {'record': {'id': 'fresh-wall', 'package': self.output, 'scenario': {'wall_side': 'right'}},
            'manifest': {'package_sha256': 'a' * 64,
                'inputs': {'provenance': 'recorder_verified', 'events': [{'time_s': .4}]},
                'frames': [{'time_s': i / 60} for i in range(5)]}, 'raw': {'rows': self.rows}}

    def extract(self):
        with patch.object(consumer, 'frame_paths', return_value=self.paths), \
             patch.object(consumer, 'read_image', side_effect=self.images):
            consumer._extract_static_wall(self.bundle, self.output, {})
        return read_json(self.output / 'raw/fresh-wall/points.json')

    def test_fresh_geometry_selects_inner_face_without_production_coordinates(self):
        result = self.extract()
        self.assertEqual(result['wall_geometry']['x'], 191.5)
        self.assertTrue(all(row['wall']['x'] == 191.5 for row in result['rows']))
        self.assertFalse(result['production_coordinates_used'])
        self.assertFalse(result['production_events_used'])

    def test_shifted_preinput_wall_stays_unobservable_and_records_reason(self):
        self.images[-1] = np.roll(self.images[-1], 1, axis=1)
        result = self.extract()
        self.assertEqual(result['wall_geometry_error']['code'], 'raw_camera_motion')
        self.assertTrue(all(row['wall']['status'] == 'unobservable' for row in result['rows']))
        self.assertTrue(all('independent_wall_edge' not in row['connections'] for row in result['rows']))

    def test_unknown_input_does_not_invent_preinput_support(self):
        self.bundle['manifest']['inputs']['provenance'] = 'unknown'
        result = self.extract()
        self.assertIsNotNone(result['wall_geometry_error'])
        self.assertTrue(all(row['wall']['status'] == 'unobservable' for row in result['rows']))


class ProspectiveParentChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc chain café ')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.session, self.suite = root / 'session', root / 'suite'
        parent_hashes, original_hashes = {'player.gd': 'a' * 64}, {'player.gd': 'b' * 64}
        manifest = {'kind': 'ReferenceMediaSuite', 'parent': parent_hashes, 'original': original_hashes}
        write_json(self.suite / 'suite.json', manifest)
        write_json(self.suite / 'suite-binding.json', {'suite_sha256': digest(manifest)})
        write_json(self.session / 'session.json', {'session_id': 'new-session', 'started_at': 123})
        initial = root / 'initial-session.json'
        write_json(initial, {'session_id': 'new-session', 'started_at': None, 'candidates': [],
                            'experiment': {'suite_sha256': digest(manifest)}})
        producer = root / 'producer.py'
        producer.write_text('# pinned parent bridge')
        self.prior = root / 'earlier-proof.txt'
        self.prior.write_text('immutable earlier evidence')
        consumed = {str(self.prior): file_sha256(self.prior)}
        parent = {'stage': 'ground', 'parent_stage': 'jump',
            'parent_source_sha256': digest(parent_hashes), 'original_source_sha256': digest(original_hashes),
            'parent_acceptance_mode': 'append_only_mixed_epoch_selected_code_revalidation'}
        self.chain = {'kind': 'ProspectiveGroundParentChainReceipt', 'schema_version': 1, 'status': 'pass',
            'parent': parent, 'parent_sha256': digest(parent), 'consumed_files': consumed,
            'consumed_files_sha256': digest(consumed), 'session': str(self.session), 'session_id': 'new-session',
            'suite': str(self.suite), 'suite_sha256': digest(manifest),
            'suite_file_sha256': file_sha256(self.suite / 'suite.json'),
            'suite_binding_sha256': file_sha256(self.suite / 'suite-binding.json'),
            'initial_session_archive': str(initial), 'initial_session_archive_sha256': file_sha256(initial),
            'producer_archive': str(producer), 'producer_sha256': file_sha256(producer),
            'canonical_session_promoted': False, 'historical_session_rewritten': False,
            'ground_behavior_acceptance': False, 'model_started': False}
        self.path = root / 'chain.json'
        write_json(self.path, self.chain)
        self.trusted_sha = file_sha256(self.path)
        self.context = {'directory': str(self.session), 'suite': str(self.suite), 'suite_sha256': digest(manifest),
            'authoring_parent_source_hashes': parent_hashes, 'original_source_hashes': original_hashes}

    def test_exact_pinned_chain_allows_current_session_to_have_started(self):
        binding = consumer._parent_chain(self.path, self.trusted_sha, self.context)
        self.assertEqual(binding['file_sha256'], self.trusted_sha)
        self.assertFalse(binding['canonical_history_promoted'])

    def test_external_parent_chain_identity_cannot_be_rebound(self):
        changed = deepcopy(self.chain)
        changed['session_id'] = 'forged'
        write_json(self.path, changed)
        with self.assertRaises(VisualError) as caught:
            consumer._parent_chain(self.path, self.trusted_sha, self.context)
        self.assertEqual(caught.exception.code, 'wrong_parent_chain')

    def test_underlying_declared_evidence_tamper_fails(self):
        self.prior.write_text('mutated raw proof')
        with self.assertRaises(VisualError) as caught:
            consumer._parent_chain(self.path, self.trusted_sha, self.context)
        self.assertEqual(caught.exception.code, 'stale_review')

    def test_other_current_session_id_cannot_reuse_same_suite_and_source(self):
        write_json(self.session / 'session.json', {'session_id': 'other-session'})
        with self.assertRaises(VisualError) as caught:
            consumer._parent_chain(self.path, self.trusted_sha, self.context)
        self.assertEqual(caught.exception.code, 'wrong_parent_chain')

    def test_changed_original_improvement_denominator_is_rejected(self):
        context = deepcopy(self.context)
        context['original_source_hashes'] = {'player.gd': 'c' * 64}
        with self.assertRaises(VisualError) as caught:
            consumer._parent_chain(self.path, self.trusted_sha, context)
        self.assertEqual(caught.exception.code, 'wrong_parent_chain')


class StandaloneConsumerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc archive café ')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output, self.session = root / 'output', root / 'session'
        self.session.mkdir()
        helper = root / 'helper.py'
        helper.write_text('# exact independent helper')
        self.extra = {str(helper): file_sha256(helper)}
        archive = self.output / 'consumer-sources/helper.py'
        archive.parent.mkdir(parents=True)
        archive.write_bytes(helper.read_bytes())
        self.parent = root / 'parent-chain.json'
        write_json(self.parent, {'unit_test_fixture': True})
        self.parent_sha = file_sha256(self.parent)
        self.binding = {'kind': 'CanonicalGroundRawConsumerBinding', 'schema_version': 1,
            'version': consumer.VERSION, 'record_count': 296, 'fresh_all_current_records': True,
            'historical_measurement_reuse': False, 'full_behavior_acceptance': False, 'new_model_generation': False,
            'session_directory': str(self.session), 'parent_chain': str(self.parent),
            'parent_chain_sha256': self.parent_sha, 'extra_sources': self.extra,
            'archives': [{'origin': str(helper), 'sha256': file_sha256(helper), 'archive': str(archive)}]}
        self.save()

    def save(self):
        write_json(self.output / 'consumer-binding.json', self.binding)
        write_json(self.output / 'evidence-receipt.json', {'consumer_binding_sha256': digest(self.binding)})

    def validate(self):
        return consumer.validate(self.output, session_directory=self.session,
                                 parent_chain=self.parent, parent_chain_sha256=self.parent_sha)

    def test_source_or_archive_byte_tamper_fails_before_seal_validation(self):
        Path(self.binding['archives'][0]['archive']).write_text('# changed archive')
        with patch.object(consumer, '_extra_sources', return_value=self.extra), patch.object(final, 'validate_review') as sealed:
            with self.assertRaises(VisualError) as caught:
                self.validate()
        self.assertEqual(caught.exception.code, 'stale_review')
        sealed.assert_not_called()

    def test_external_path_cannot_replace_owned_producer_archive(self):
        self.binding['archives'][0]['archive'] = self.binding['archives'][0]['origin']
        self.save()
        with patch.object(consumer, '_extra_sources', return_value=self.extra), patch.object(final, 'validate_review') as sealed:
            with self.assertRaises(VisualError) as caught:
                self.validate()
        self.assertEqual(caught.exception.code, 'unsafe_path')
        sealed.assert_not_called()

    def test_missing_helper_archive_fails_before_seal_validation(self):
        self.binding['archives'] = []
        self.save()
        with patch.object(consumer, '_extra_sources', return_value=self.extra), patch.object(final, 'validate_review') as sealed:
            with self.assertRaises(VisualError):
                self.validate()
        sealed.assert_not_called()

    def test_standalone_proof_carries_replayable_source_and_receipt_inventory(self):
        root = self.output.parent
        suite = root / 'suite'
        write_json(suite / 'suite.json', {'unit_test_suite': True})
        write_json(suite / 'suite-binding.json', {'unit_test_binding': True})
        origin = self.binding['archives'][0]['origin']
        sha = self.extra[origin]
        write_json(self.parent, {'consumed_files': self.extra, 'suite': str(suite),
            'suite_file_sha256': file_sha256(suite / 'suite.json'),
            'suite_binding_sha256': file_sha256(suite / 'suite-binding.json'),
            'initial_session_archive': origin, 'initial_session_archive_sha256': sha,
            'producer_archive': origin, 'producer_sha256': sha})
        self.parent_sha = file_sha256(self.parent)
        self.binding['parent_chain_sha256'] = self.parent_sha
        self.save()
        receipt = read_json(self.output / 'evidence-receipt.json')
        receipt.update({'source_files': self.extra, 'code_sources': self.extra,
                        'generated_files': {'consumer-binding.json': file_sha256(self.output / 'consumer-binding.json')}})
        write_json(self.output / 'evidence-receipt.json', receipt)
        write_json(self.output / 'attestation.json', {'fixture_reviewed': True})
        with patch.object(consumer, '_extra_sources', return_value=self.extra), \
             patch.object(final, 'validate_review', return_value={'status': 'pass', 'record_count': 296}):
            proof = self.validate()
        inventory = proof['consumed_files']
        for path in (self.parent, self.output / 'consumer-binding.json', self.output / 'evidence-receipt.json',
                     self.output / 'attestation.json', Path(self.binding['archives'][0]['archive'])):
            self.assertEqual(inventory[str(path)], file_sha256(path))
        self.assertEqual(proof['consumed_files_sha256'], digest(inventory))
        self.assertEqual(proof['kind'], 'VerifiedCanonicalGroundRawInspection')
        self.assertTrue(proof['standalone_validator_required'])
        self.assertFalse(proof['full_behavior_acceptance'])


class FreshConsumerWrapperSeamTests(unittest.TestCase):
    def test_prepare_archives_exact_added_semantics_and_binds_generated_receipt(self):
        with TemporaryDirectory(prefix='kc prepare seam café ') as name:
            root = Path(name)
            helper, output = root / 'helper.py', root / 'raw-review'
            helper.write_text('# inspected prospective helper')
            extra = {str(helper): file_sha256(helper)}
            def review(session, target):
                self.assertEqual(final.VERSION, consumer.VERSION)
                self.assertEqual(final._code_sources(), extra)
                result = {'version': final.VERSION, 'records': [{} for _ in range(296)]}
                write_json(target / 'result.json', result)
                write_json(target / 'evidence-receipt.json', {'existing_core_binding': 'preserved'})
                return result
            with patch.object(consumer, '_extra_sources', return_value=extra), \
                 patch.object(final, '_code_sources', return_value={}), \
                 patch.object(final, 'review_session', side_effect=review):
                consumer.prepare(root / 'session', output, parent_chain=root / 'chain.json',
                                 parent_chain_sha256='a' * 64)
            binding = read_json(output / 'consumer-binding.json')
            receipt = read_json(output / 'evidence-receipt.json')
            self.assertEqual(binding['extra_sources'], extra)
            self.assertEqual(len(binding['archives']), 1)
            archived = Path(binding['archives'][0]['archive'])
            self.assertEqual(archived.read_bytes(), helper.read_bytes())
            self.assertEqual(receipt['consumer_binding_sha256'], digest(binding))
            self.assertEqual(receipt['existing_core_binding'], 'preserved')
            self.assertEqual(set(receipt['generated_files']),
                {'result.json', 'consumer-binding.json', archived.relative_to(output).as_posix()})
            self.assertTrue(binding['fresh_all_current_records'])
            self.assertFalse(binding['ordinary_reader_compatibility_claimed'])

    def test_ordinary_final_reader_rejects_custom_consumer_version(self):
        with TemporaryDirectory(prefix='kc ordinary reject ') as name:
            output = Path(name)
            write_json(output / 'result.json', {'kind': 'FinalOnlyUnsignedGameSVGReviewAid',
                                               'version': consumer.VERSION})
            write_json(output / 'evidence-receipt.json', {})
            write_json(output / 'attestation.json', {})
            self.assertNotEqual(final.VERSION, consumer.VERSION)
            with patch.object(final, '_context_session', return_value=({}, [])):
                with self.assertRaisesRegex(VisualError, 'Final raw inspection is pending, failed or belongs to another seal'):
                    final.validate_review(output, session_directory=output / 'session')


if __name__ == '__main__':
    unittest.main()
