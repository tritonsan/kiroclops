"""Prospective normal consumer authority, raw-only controls and byte receipts."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_ground_normal_raw as normal


class NormalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kc normal café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / 'session'
        self.suite = self.root / 'suite'
        self.selected = self.session / 'candidates/candidate-03/source'
        self.selected.mkdir(parents=True)
        (self.selected / 'player.gd').write_text('# sealed ground controller')
        self.source_map = {'player.gd': file_sha256(self.selected / 'player.gd')}
        self.source_sha = digest(self.source_map)
        self.jobs = ('prepare-01', 'evaluate-03', 'final-01')
        self.normal_directory = self.session / 'measurements/final-01/normal-level'
        self.native = self.root / 'native'
        (self.native / 'project').mkdir(parents=True)
        (self.native / 'project/player.gd').write_bytes((self.selected / 'player.gd').read_bytes())
        self.routes = []
        for name in ('normal-combined', 'normal-slope'):
            run = self.native / name
            frames, hashes = [], {}
            for index in range(240):
                path = run / 'frames' / f'{index:06d}.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes([index]))
                hashes[path.name] = file_sha256(path)
                frames.append({'time_s': index / 60, 'engine_coordinate_should_not_be_used': 9999})
            write_json(run / 'manifest.json', {'frames': frames, 'raw_frame_hashes': hashes})
            video = self.normal_directory / (name + '.mp4')
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b'bound fixture video')
            write_json(video.with_suffix('.source.json'), {'fixture_encoder_receipt': True})
            self.routes.append({'id': name, 'scenario': {'events': [
                {'tick': 30, 'action': 'right', 'pressed': True},
                {'tick': 72, 'action': 'right', 'pressed': False}]},
                'raw_run': str(run), 'video': str(video), 'video_sha256': file_sha256(video),
                'gameplay': {'status': 'pass'}})
        self.normal_result = {'kind': 'ReferenceSuiteNormalLevelEvidence', 'status': 'pass',
            'mandatory_route': 'normal-combined', 'slope_diagnostic_status': 'pass',
            'selected_source': str(self.selected), 'source_sha256': self.source_sha,
            'full_source_hashes': self.source_map, 'native_root': str(self.native),
            'native_source_hashes': self.source_map, 'scenarios': self.routes,
            'file_hashes': {path.relative_to(self.native).as_posix(): file_sha256(path)
                            for path in self.native.rglob('*') if path.is_file()}}
        write_json(self.normal_directory / 'result.json', self.normal_result)
        self.context = {'kind': 'SealedSessionFinalRawMatrix', 'domain': 'game', 'stage': 'ground',
            'cases': [{} for _ in range(72)], 'selected_candidate_id': 'candidate-03',
            'suite': str(self.suite), 'suite_sha256': 'a' * 64,
            'sealed_source': str(self.selected), 'sealed_source_hashes': self.source_map,
            'sealed_source_sha256': self.source_sha, 'authoring_parent': str(self.root / 'parent'),
            'original': str(self.root / 'original')}
        chosen = {'candidate_id': 'candidate-03', 'eligible': True, 'status': 'succeeded',
                  'source_sha256': self.source_sha, 'job_id': self.jobs[1]}
        self.state = {'status': 'finalized', 'active_job_id': None, 'session_id': 'prospective-ground',
            'candidates': [chosen], 'selected_candidate_id': 'candidate-03', 'sealed_at': 123,
            'prepare_job_id': self.jobs[0], 'final_job_id': self.jobs[2],
            'final': {'status': 'succeeded', 'job_id': self.jobs[2], 'pass': True}}
        self.results = {'prepare': {'suite_sha256': 'a' * 64},
            'evaluate': {'suite_sha256': 'a' * 64, 'eligible': True},
            'finalize': {'suite_sha256': 'a' * 64, 'pass': True,
                         'normal_level': self.normal_result, 'normal_level_status': 'pass'}}
        for name, value in (('session.json', self.state), ('binding.json', {'fixture': True})):
            write_json(self.session / name, value)
        for name in ('suite.json', 'suite-binding.json'):
            write_json(self.suite / name, {'fixture': True})
        for job in self.jobs:
            for name in ('job.json', 'binding.json', 'result.json'):
                write_json(self.session / 'jobs' / job / name, {'fixture_job': job, 'fixture_file': name})
        self.chain = self.root / 'chain.json'
        self.producer = self.root / 'parent-producer.py'
        self.producer.write_text('# parent producer fixture')
        self.initial = self.root / 'initial-session.json'
        write_json(self.initial, {'fixture_initial': True})
        write_json(self.chain, {'consumed_files': {str(self.producer): file_sha256(self.producer)},
            'suite': str(self.suite), 'suite_file_sha256': file_sha256(self.suite / 'suite.json'),
            'suite_binding_sha256': file_sha256(self.suite / 'suite-binding.json'),
            'initial_session_archive': str(self.initial), 'initial_session_archive_sha256': file_sha256(self.initial),
            'producer_archive': str(self.producer), 'producer_sha256': file_sha256(self.producer)})
        self.chain_sha = file_sha256(self.chain)
        self.patches = [patch.object(normal, 'Session', return_value=SimpleNamespace(_load=lambda: self.state)),
            patch.object(normal.final, '_context_session', side_effect=lambda path: (deepcopy(self.context), [{} for _ in range(296)])),
            patch.object(normal.ground, '_parent_chain', return_value={'path': str(self.chain), 'file_sha256': self.chain_sha}),
            patch.object(normal.evidence, '_completed_job', side_effect=lambda directory, state, job, operation, candidate: self.results[operation]),
            patch.object(normal.demo, 'verify', side_effect=lambda path, source_sha256: self.normal_result),
            patch.object(normal.runtime, 'source_hashes', return_value=self.source_map)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def authority(self):
        return normal._authority(self.session, self.chain, self.chain_sha)


class CanonicalNormalAuthorityTests(NormalFixture):
    def test_complete_ground_authority_derives_exact_final_normal_path(self):
        authority, actual, inventory = self.authority()
        self.assertEqual(authority['normal_directory'], str(self.normal_directory))
        self.assertEqual(authority['source_sha256'], self.source_sha)
        self.assertEqual(actual, self.normal_result)
        self.assertIn(str(self.session / 'jobs/final-01/binding.json'), inventory)
        self.assertIn(str(self.native / 'normal-combined/frames/000000.png'), inventory)
        self.assertIn(str(self.routes[1]['video']), inventory)
        self.assertFalse(authority['full_behavior_acceptance'])

    def test_jump_historical_or_incomplete_matrix_is_rejected(self):
        for field, value in (('stage', 'jump'), ('kind', 'MixedEpochHistoricalSeal'), ('cases', [{}])):
            with self.subTest(field=field):
                previous = self.context[field]
                self.context[field] = value
                with self.assertRaises(VisualError) as caught:
                    self.authority()
                self.assertEqual(caught.exception.code, 'wrong_final_seal')
                self.context[field] = previous

    def test_unfinished_or_failed_canonical_session_is_rejected(self):
        self.state['status'] = 'finalization_failed'
        with self.assertRaises(VisualError):
            self.authority()

    def test_canonical_job_suite_or_selected_result_mismatch_is_rejected(self):
        self.results['finalize']['suite_sha256'] = 'b' * 64
        with self.assertRaises(VisualError) as caught:
            self.authority()
        self.assertEqual(caught.exception.code, 'wrong_final_seal')

    def test_normal_source_path_or_full_map_substitution_is_rejected(self):
        for field, value in (('selected_source', str(self.root / 'other-candidate')), ('full_source_hashes', {})):
            with self.subTest(field=field):
                previous = self.normal_result[field]
                self.normal_result[field] = value
                with self.assertRaises(VisualError) as caught:
                    self.authority()
                self.assertEqual(caught.exception.code, 'wrong_candidate')
                self.normal_result[field] = previous

    def test_renderer_projection_substitution_is_rejected(self):
        self.normal_result['native_source_hashes'] = {'other.gd': 'c' * 64}
        with self.assertRaises(VisualError) as caught:
            self.authority()
        self.assertEqual(caught.exception.code, 'wrong_candidate')

    def test_mandatory_failure_is_rejected_but_slope_failure_is_diagnostic(self):
        self.normal_result['slope_diagnostic_status'] = 'fail'
        write_json(self.normal_directory / 'result.json', self.normal_result)
        authority, _, _ = self.authority()
        self.assertEqual(authority['slope_diagnostic_status'], 'fail')
        self.normal_result['status'] = 'fail'
        with self.assertRaises(VisualError):
            self.authority()

    def test_parent_pin_and_nested_native_bytes_are_checked(self):
        with self.assertRaises(VisualError):
            normal._authority(self.session, self.chain, '0' * 64)
        frame = self.native / 'normal-combined/frames/000000.png'
        frame.write_bytes(b'tampered native raster')
        with self.assertRaises(VisualError) as caught:
            self.authority()
        self.assertEqual(caught.exception.code, 'source_changed')


class FreshNormalRouteTests(NormalFixture):
    def route(self, *, observable=True):
        output = self.root / 'route-output'
        output.mkdir(exist_ok=True)
        def save(path, image):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'bound contact fixture')
        with patch.object(normal, 'read_image', return_value=np.zeros((2, 2, 3), np.uint8)), \
             patch.object(normal.glyph, 'anchor', return_value=[{'x': 4., 'y': 7.}] if observable else []), \
             patch.object(normal.generic, '_tile', return_value=np.zeros((230, 600, 3), np.uint8)), \
             patch.object(normal.common, '_save_image', side_effect=save):
            return normal._route(self.routes[0], self.source_sha, output, {})

    def test_all_240_raw_frames_and_twenty_distinct_glyph_controls_are_bound(self):
        record, sheets, failures = self.route()
        self.assertEqual(len(record['frames']), 240)
        self.assertEqual(record['distinct_image_count'], 240)
        self.assertEqual(record['distinct_glyph_control_count'], 240)
        self.assertGreaterEqual(len({row['image_sha256'] for row in record['selected_images']
                                    if 'distinct_raw_glyph_control' in row['reasons']}), 20)
        self.assertEqual(failures, [])
        self.assertTrue(sheets)
        self.assertFalse(record['physics_landing_inferred'])
        self.assertFalse(record['physics_braking_inferred'])
        self.assertEqual(record['frames'][0]['point'], {'x': 4., 'y': 7.})

    def test_unobservable_glyphs_remain_failure_without_synthetic_points(self):
        record, _, failures = self.route(observable=False)
        self.assertIn('normal_raw_controls_insufficient', [item['code'] for item in failures])
        self.assertTrue(all('x' not in row['point'] for row in record['frames']))

    def test_public_input_neighborhoods_are_present_and_engine_values_are_not_used(self):
        record, _, _ = self.route()
        row = next(row for row in record['selected_images'] if row['index'] == 30)
        self.assertIn('public_input:right press', row['reasons'])
        self.assertNotIn('engine_coordinate_should_not_be_used', record['frames'][0])
        self.assertEqual(record['public_input_provenance'], 'recorder_verified')

    def test_corrupt_frame_is_not_replaced_with_a_proposal(self):
        (self.native / 'normal-combined/frames/000003.png').write_bytes(b'wrong raster')
        with self.assertRaises(VisualError):
            self.route()

    def test_contact_planner_rejects_nonfinite_or_non_numeric_glyph_coordinates(self):
        for value in (float('inf'), float('nan'), '4', True):
            with self.subTest(value=value):
                with self.assertRaises(VisualError):
                    normal._contacts([{'index': 0, 'time_s': 0., 'image_sha256': 'a' * 64,
                                       'point': {'x': value, 'y': 7.}}], [])


class CanonicalNormalConsumerTests(NormalFixture):
    def prepare(self):
        self.output = self.root / 'normal-review'
        sources = {str(self.producer): file_sha256(self.producer)}
        self.source_patch = patch.object(normal, '_code_sources', return_value=sources)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        def save(path, image):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'bound contact fixture')
        with patch.object(normal, 'read_image', return_value=np.zeros((2, 2, 3), np.uint8)), \
             patch.object(normal.glyph, 'anchor', return_value=[{'x': 4., 'y': 7.}]), \
             patch.object(normal.generic, '_tile', return_value=np.zeros((230, 600, 3), np.uint8)), \
             patch.object(normal.common, '_save_image', side_effect=save):
            self.aid = normal.prepare(self.session, self.output, parent_chain=self.chain, parent_chain_sha256=self.chain_sha)
        return self.aid

    def attest(self):
        return normal.attest(self.output, reviewer='actual-test-fixture-inspector',
            inspected_sheets=[sheet['path'] for sheet in self.aid['sheets']], accepted=True, images_inspected=True)

    def validate(self):
        return normal.validate(self.output, session_directory=self.session,
                               parent_chain=self.chain, parent_chain_sha256=self.chain_sha)

    def rewrite_pending(self, *, edit_aid=lambda aid: None, edit_receipt=lambda receipt: None):
        aid = read_json(self.output / 'result.json')
        edit_aid(aid)
        write_json(self.output / 'result.json', aid)
        receipt = read_json(self.output / 'evidence-receipt.json')
        receipt['result_sha256'] = digest(aid)
        receipt['generated_files']['result.json'] = file_sha256(self.output / 'result.json')
        edit_receipt(receipt)
        write_json(self.output / 'evidence-receipt.json', receipt)
        self.aid = aid

    def test_fresh_prepare_attest_validate_proof_is_standalone_and_replayable(self):
        self.prepare()
        self.attest()
        proof = self.validate()
        self.assertEqual(proof['kind'], 'VerifiedCanonicalGroundNormalRawInspection')
        self.assertEqual(proof['frame_count'], 480)
        self.assertFalse(proof['full_behavior_acceptance'])
        self.assertFalse(proof['ordinary_reader_compatibility_claimed'])
        self.assertTrue(proof['standalone_validator_required'])
        self.assertEqual(proof['consumed_files_sha256'], digest(proof['consumed_files']))
        for path in (self.chain, self.output / 'evidence-receipt.json', self.output / 'attestation.json',
                     self.native / 'normal-combined/frames/000002.png'):
            self.assertEqual(proof['consumed_files'][str(path)], file_sha256(path))

    def test_real_image_inspection_is_required_not_inferred_from_numeric_pass(self):
        self.prepare()
        with self.assertRaises(VisualError):
            normal.attest(self.output, reviewer='not-an-inspection', inspected_sheets=[], accepted=True)

    def test_source_sheet_or_producer_archive_tamper_rejects_proof(self):
        self.prepare()
        self.attest()
        receipt = read_json(self.output / 'evidence-receipt.json')
        path = self.output / receipt['producer_archives'][0]['archive']
        path.write_text('# changed archived producer')
        with self.assertRaises(VisualError):
            self.validate()

    def test_wrong_candidate_or_parent_pin_cannot_reuse_normal_review(self):
        self.prepare()
        self.attest()
        with self.assertRaises(VisualError):
            normal.validate(self.output, session_directory=self.session, parent_chain=self.chain, parent_chain_sha256='0' * 64)

    def test_immutable_source_tree_cannot_be_used_as_review_output(self):
        with self.assertRaises(VisualError) as caught:
            normal.prepare(self.session, self.suite / 'unsafe-review', parent_chain=self.chain, parent_chain_sha256=self.chain_sha)
        self.assertEqual(caught.exception.code, 'unsafe_path')

    def test_custom_version_is_distinct_from_historical_normal_reader(self):
        from scripts import phase3d_v2_game_normal_review as historical
        self.prepare()
        self.attest()
        with patch.object(historical.supplemental, 'verify', return_value={'source_sha256': self.source_sha}):
            with self.assertRaisesRegex(VisualError, 'Exact actual normal-level image inspection is required'):
                historical.validate(self.normal_directory, self.output)

    def test_rebound_pending_aid_cannot_omit_public_input_neighborhood(self):
        self.prepare()
        def omit(aid):
            aid['records'][0]['selected_images'] = [row for row in aid['records'][0]['selected_images'] if row['index'] != 30]
        self.rewrite_pending(edit_aid=omit)
        self.attest()
        with self.assertRaisesRegex(VisualError, 'omit input neighborhoods'):
            self.validate()

    def test_rebound_selected_point_must_equal_its_original_raw_frame(self):
        self.prepare()
        def substitute(aid):
            aid['records'][0]['selected_images'][0]['point']['x'] = 900.
        self.rewrite_pending(edit_aid=substitute)
        self.attest()
        with self.assertRaisesRegex(VisualError, 'exact frame/tile identities'):
            self.validate()

    def test_missing_generated_sheet_binding_rejects_even_fresh_attestation(self):
        self.prepare()
        sheet = self.aid['sheets'][0]['path']
        self.rewrite_pending(edit_receipt=lambda receipt: receipt['generated_files'].pop(sheet))
        self.attest()
        with self.assertRaisesRegex(VisualError, 'sheet or archive byte binding is missing'):
            self.validate()

    def test_missing_generated_archive_binding_rejects_even_fresh_attestation(self):
        self.prepare()
        archive = read_json(self.output / 'evidence-receipt.json')['producer_archives'][0]['archive']
        self.rewrite_pending(edit_receipt=lambda receipt: receipt['generated_files'].pop(archive))
        self.attest()
        with self.assertRaisesRegex(VisualError, 'sheet or archive byte binding is missing'):
            self.validate()

    def test_video_binding_native_transport_and_tile_renderer_are_in_source_closure(self):
        with patch.object(normal.final, '_code_sources', return_value={}), \
             patch.object(normal.ground, '_extra_sources', return_value={}), \
             patch.object(normal, 'file_sha256', return_value='a' * 64):
            sources = normal._code_sources()
        for module in (normal.storage, normal.runtime.private, normal.generic, normal.reporting):
            self.assertIn(str(Path(module.__file__).resolve()), sources)


if __name__ == '__main__':
    unittest.main()
