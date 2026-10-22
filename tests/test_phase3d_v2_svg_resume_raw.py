"""Small synthetic resume checks; these tests grant no actual review acceptance."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import realistic_review as review
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_web_final_raw as bridge
from scripts import phase3d_v2_svg_resume_raw as resume


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='svg resume unit café ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.authority = self.root / 'session'
        self.authority.mkdir()
        self.canonical = self.authority / 'session.json'
        write_json(self.canonical, {'unit_only': 'immutable-canonical'})
        self.files = {str(self.canonical): file_sha256(self.canonical)}
        self.proof = {'canonical_input_files': self.files, 'required_observations': [],
                      'native_record_and_source_bindings_replayed': True,
                      'native_model_generation_acceptance_granted': False, 'source_code_acceptance_granted': False}
        self.project = self.root / 'source'
        self.project.mkdir()
        self.source = self.project / 'main.py'
        self.source.write_text('# immutable synthetic source\n', encoding='utf-8')
        self.source_hashes = {'main.py': file_sha256(self.source)}
        image = self.root / 'frame.png'
        cv2.imencode('.png', np.full((80, 120, 3), 40, np.uint8))[1].tofile(str(image))
        self.package = self.root / 'package'
        self.manifest = create_package(self.package, [image, image], [0., .05],
            metadata={'source_hashes': self.source_hashes})
        self.context = {'stage': 'svg', 'domain': 'svg', 'acceptance_policy': {'id': 'realistic-behavior-v1'},
            'cases': [], 'suite_sha256': 'c'*64, 'candidate_sha256': digest(self.source_hashes),
            'session_directory': str(self.authority), 'suite_directory': str(self.root / 'suite'),
            'selected_candidate_id': 'unit-historical', 'evaluation_job_id': 'eval', 'final_job_id': 'final', 'event_contract': {}}
        self.rows = []
        for split, count in (('calibration', 4), ('regression', 12), ('validation', 12)):
            for number in range(count):
                case_id = f'svg-{split}-{number}'
                scenario = {'id': case_id, 'case_id': case_id, 'domain': 'svg',
                    'suite_split': 'known_regression' if split == 'regression' else split,
                    'blind_validation': split == 'validation'}
                self.context['cases'].append({'case_id': case_id, 'role': split, 'scenario': deepcopy(scenario)})
                for role in review.ROLES:
                    self.rows.append({'id': role+'-'+case_id, 'case_id': case_id, 'split_role': split,
                        'role': role, 'domain': 'svg', 'selection': {'objects': [{'id': 'a'}], 'connections': []},
                        'object_map': {'a': 'a'}, 'source_project': str(self.project), 'full_source_hashes': self.source_hashes,
                        'record': {'package': str(self.package), 'package_sha256': self.manifest['package_sha256'],
                                   'source_sha256': digest(self.source_hashes), 'scenario': deepcopy(scenario),
                                   'analysis': str(self.root / 'forbidden-production-analysis.json')}})
        self.patch(review, '_session_context', return_value=(self.context, self.rows))
        self.patch(bridge, '_canonical_inputs', return_value=self.proof)
        self.patch(review, '_code_sources', return_value={str(self.source): file_sha256(self.source)})
        self.partial = self.root / 'old-partial'
        self.partial.mkdir()
        self.output = self.root / 'fresh-resumed'
        raw_points = {name: {'status': 'not_detected', 'boxes': []} for name in 'abcdef'}
        raw_points['a'] = {'x': 20.5, 'y': 25.5, 'color_bounds': [16, 20, 10, 12], 'status': 'unsigned'}
        self.raw_image = {'points': raw_points,
                          'objects': {'a': {'bounds': [10, 10, 50, 50], 'anchor': [20.5, 25.5]}}, 'connections': {}}
        self.raw = {'rows': [{'index': frame['index'], 'time_s': frame['time_s'], 'image_sha256': frame['sha256'],
                             **deepcopy(self.raw_image)} for frame in self.manifest['frames']],
                    'package_sha256': self.manifest['package_sha256'], 'production_coordinates_used': False,
                    'production_events_used': False, 'target_code_or_dom_read': False, 'status': 'unsigned'}
        self.snapshot, _, self.normalized = resume.canonical_snapshot(self.authority)
        self.pins = {}
        for row in self.normalized[:87]:
            path = self.partial / 'raw' / row['id'] / 'points.json'
            write_json(path, self.raw)
            self.pins[row['id']] = file_sha256(path)
        self.prior = self.root / 'completed-panel'
        self.prior.mkdir()
        producer = self.prior / 'consumer-sources' / Path(bridge.__file__).name
        producer.parent.mkdir()
        producer.write_bytes(Path(bridge.__file__).read_bytes())
        consumer = {'version': bridge.VERSION, 'producer_sha256': resume.FROZEN_BRIDGE_SHA256, 'producer_archive': str(producer)}
        prior_result = {'phase': 'final', 'domain': 'panel', 'version': bridge.VERSION}
        write_json(self.prior / 'result.json', prior_result)
        write_json(self.prior / 'records.json', [])
        write_json(self.prior / 'extraction-complete.json', {'unit_only': True})
        write_json(self.prior / 'consumer-binding.json', consumer)
        self.prior_receipt = self.prior / 'evidence-receipt.json'
        write_json(self.prior_receipt, {'kind': 'RealisticPixelEvidenceReceipt', 'schema_version': 2,
            'result_sha256': digest(prior_result), 'consumer_binding_sha256': digest(consumer),
            'code_sources': resume._baseline_sources(), 'runtime': resume._runtime(),
            'generated_files': {p.relative_to(self.prior).as_posix(): file_sha256(p)
                                for p in self.prior.rglob('*') if p.is_file()}})
        self.prior_pin = file_sha256(self.prior_receipt)
        self.patch(resume, 'PRIOR_RECEIPT_SHA256', self.prior_pin)
        self.witness = self.root / 'observed-interruption.json'
        write_json(self.witness, {'kind': 'ObservedInterruptedIndependentSvgExtraction', 'schema_version': 1,
            'session': str(self.authority), 'partial_directory': str(self.partial),
            'complete_points_count': 87, 'expected_record_count': 112,
            'partial_points_sha256': {str(self.partial / 'raw' / name / 'points.json'): sha for name, sha in self.pins.items()},
            'observed_worker_command': 'scripts/'+Path(bridge.__file__).name+' prepare --session '+self.authority.as_posix()+' --output '+self.partial.as_posix(),
            'observed_producer_sha256': resume.FROZEN_BRIDGE_SHA256,
            'previous_completed_source_runtime_receipt': str(self.prior_receipt),
            'previous_completed_source_runtime_receipt_sha256': self.prior_pin,
            'observed_exit_code': 0x40010004, 'windows_last_boot_up_time': '2026-10-03T10:37:15.500000+03:00',
            'point_pins_observed_after_reboot': True, 'pre_interruption_initial_receipt_exists': False,
            'partial_origin': 'unsigned_unattested_incomplete_pixel_proposals', 'accepted_scopes': {}, 'full_behavior_acceptance': False})
        self.witness_pin = file_sha256(self.witness)
        self.patch(resume, 'WITNESS_SHA256', self.witness_pin)
        self.clarification = self.root / 'observed-interruption-clarification.json'
        write_json(self.clarification, {'kind': 'ObservedSvgInterruptionScopeClarification', 'schema_version': 1,
            'witness_path': str(self.witness), 'witness_sha256': self.witness_pin,
            'clarified_field': 'production_measurements_opened_by_original_worker',
            'narrow_claim': 'The _prepare measurement comparison stage was not reached before the interruption.',
            'canonical_context_metadata_read_before_pixel_extraction': True,
            'production_coordinates_used_for_pixel_extraction': False, 'pre_interruption_initial_receipt_exists': False,
            'point_pins_observed_after_reboot': True, 'accepted_scopes': {}, 'full_behavior_acceptance': False})
        self.clarification_pin = file_sha256(self.clarification)
        self.patch(resume, 'CLARIFICATION_SHA256', self.clarification_pin)
        self.inventory_path = self.root / 'external-post-reboot-inventory.json'
        self.inventory = {'kind': 'InterruptedUnsignedSvgExtractionInventory', 'schema_version': 1,
            'session_directory': str(self.authority), 'partial_directory': str(self.partial),
            'prior_completed_receipt': {'path': str(self.prior_receipt), 'sha256': self.prior_pin},
            'raw_points_sha256': self.pins, **self.snapshot,
            'interruption': {'witness': {'path': str(self.witness), 'sha256': self.witness_pin},
                             'clarification': {'path': str(self.clarification), 'sha256': self.clarification_pin}},
            'pre_interruption_receipt_exists': False, 'reused_origin_attested': False,
            'production_coordinates_used_for_extraction': False, 'measurement_stage_reached_before_interruption': False}
        self.pin_inventory()
        self.measure_calls = []
        self.patch(review.generic, '_raw_image', side_effect=lambda image, domain: deepcopy(self.raw_image))
        self.patch(review, '_measurement', side_effect=self.measurement)
        self.patch(review, '_position_checks', return_value=([], []))
        self.patch(review, '_geometry_checks', return_value=([], []))
        self.patch(review, '_controls', return_value=([], []))
        self.patch(review, '_final_svg_events', return_value=([], [], [], []))
        self.patch(review, '_prove_original_events', return_value={'status': 'pass', 'files': {}})
        self.patch(review, '_sheets', return_value=[])
        from kiroclops.local_engine import suite_evidence
        # Unit seam supplies canonical authority state; no real Session is
        # accepted or altered by the synthetic two-frame extraction fixture.
        self.patch(review.Session, '_load', return_value={})
        self.patch(suite_evidence, 'required_bindings', return_value={(self.manifest['package_sha256'], digest({'unit_only': True}))})

    def patch(self, owner, name, *args, **kwargs):
        item = patch.object(owner, name, *args, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def pin_inventory(self):
        write_json(self.inventory_path, self.inventory)
        self.inventory_pin = file_sha256(self.inventory_path)

    def prepare(self):
        return resume.prepare(self.authority, self.output, inventory_path=self.inventory_path, inventory_sha256=self.inventory_pin)

    def preflight(self, pin=None):
        return resume.preflight(self.authority, self.inventory_path, self.inventory_pin if pin is None else pin)

    def measurement(self, bundle, inventory):
        complete = read_json(self.output / 'extraction-complete.json')
        self.assertEqual(len(complete['raw_points_sha256']), 112)
        self.assertEqual(len(list((self.output / 'raw').glob('*/points.json'))), 112)
        self.assertTrue(complete['all_pixel_extraction_finished_before_measurement_read'])
        self.measure_calls.append(bundle['record']['id'])
        bundle['observations'] = {'unit_only': True}

    def assert_invalid(self, code, function, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_87_exact_old_bytes_25_decodes_all112_before_measurement_no_canonical_writes(self):
        protected = {p: p.read_bytes() for root in (self.authority, self.project, self.partial, self.prior)
                     for p in root.rglob('*') if p.is_file()}
        with patch.object(review.generic, 'read_image', wraps=review.generic.read_image) as decoder:
            value = self.prepare()
        self.assertEqual(decoder.call_count, 50)
        self.assertEqual(len(self.measure_calls), 112)
        binding = value['resume_binding']
        self.assertEqual((binding['record_count'], binding['reused_record_count'], binding['freshly_extracted_record_count']), (112, 87, 25))
        self.assertFalse(binding['reused_origin_attested'])
        self.assertFalse(binding['reused_points_freshly_recomputed'])
        self.assertFalse(binding['full_behavior_acceptance'])
        self.assertFalse(binding['ordinary_reader_compatibility_claimed'])
        for name, sha in self.pins.items():
            self.assertEqual(file_sha256(self.output / 'raw' / name / 'points.json'), sha)
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})
        verified = resume.validate(self.output, session_directory=self.authority, require_attested=False)
        self.assertEqual(verified['status'], 'proposal_verified')
        self.assertEqual(verified['verified_scopes'], {})
        self.assertEqual(verified['source_code_review_status'], 'pending')
        self.assertEqual(verified['native_model_generation_review_status'], 'pending')
        self.assertFalse(verified['full_behavior_acceptance'])
        self.assertEqual(verified['version'], resume.VERSION)
        for path in (self.inventory_path, self.prior_receipt, self.witness, self.clarification):
            self.assertEqual(verified['consumed_files'][str(path)], file_sha256(path))
        self.assertEqual(verified['consumed_files_sha256'], digest(verified['consumed_files']))
        with self.assertRaises(VisualError):
            bridge.validate(self.output, session_directory=self.authority, require_attested=False)

    def test_missing_or_wrong_external_inventory_pin(self):
        self.assert_invalid('missing_resume_pin', self.preflight, '')
        self.assert_invalid('stale_resume', self.preflight, '0'*64)
        self.inventory_path.write_text('{}', encoding='utf-8')
        self.assert_invalid('stale_resume', self.preflight)
        self.assertFalse(self.output.exists())

    def test_missing_or_tampered_raw_pins_and_extra_files_reject(self):
        self.inventory['raw_points_sha256'] = dict(list(self.pins.items())[1:])
        self.pin_inventory()
        self.assert_invalid('missing_resume_pin', self.preflight)
        self.inventory['raw_points_sha256'] = self.pins
        self.pin_inventory()
        path = self.partial / 'raw' / next(iter(self.pins)) / 'points.json'
        before = path.read_bytes()
        path.write_bytes(before+b' ')
        self.assert_invalid('stale_resume', self.preflight)
        path.write_bytes(before)
        write_json(self.partial / 'result.json', {'forbidden': 'invented completion'})
        self.assert_invalid('stale_resume', self.preflight)

    def test_prior_external_pin_source_and_runtime_must_match(self):
        original = read_json(self.prior_receipt)
        self.prior_receipt.write_bytes(self.prior_receipt.read_bytes()+b' ')
        self.assert_invalid('stale_resume', self.preflight)
        write_json(self.prior_receipt, original)
        with patch.object(resume, '_runtime', return_value={'changed': True}):
            self.assert_invalid('source_changed', self.preflight)
        with patch.object(review, '_code_sources', return_value={'changed.py': '0'*64}):
            self.assert_invalid('source_changed', self.preflight)
        with patch.object(resume, 'FROZEN_BRIDGE_SHA256', '0'*64):
            self.assert_invalid('source_changed', self.preflight)

    def test_missing_tampered_or_wrong_interruption_and_clarification_pin(self):
        original = deepcopy(self.inventory['interruption'])
        self.inventory['interruption'] = {'unit_only': 'unbound reboot claim'}
        self.pin_inventory()
        self.assert_invalid('missing_resume_pin', self.preflight)
        self.inventory['interruption'] = original
        self.pin_inventory()
        witness_bytes = self.witness.read_bytes()
        self.witness.write_bytes(witness_bytes+b' ')
        self.assert_invalid('stale_resume', self.preflight)
        self.witness.write_bytes(witness_bytes)
        clarification_bytes = self.clarification.read_bytes()
        self.clarification.write_bytes(clarification_bytes+b' ')
        self.assert_invalid('stale_resume', self.preflight)
        self.clarification.write_bytes(clarification_bytes)
        self.inventory['interruption']['clarification']['sha256'] = '0'*64
        self.pin_inventory()
        self.assert_invalid('missing_resume_pin', self.preflight)

    def test_wrong_record_public_selection_order_or_canonical_input_rejects(self):
        for field, value in (('selection', {'objects': []}), ('source_sha256', '0'*64), ('actual_input_times', [.123])):
            with self.subTest(field=field):
                nested = self.rows[0]['record']
                old = deepcopy(nested)
                nested[field] = value
                # Selection is an outer canonical public binding.
                outer = deepcopy(self.rows[0])
                if field == 'selection':
                    self.rows[0]['selection'] = value
                self.assert_invalid('stale_resume', self.preflight)
                self.rows[0] = outer
                self.rows[0]['record'] = old
        self.inventory['record_order'] = list(reversed(self.inventory['record_order']))
        self.pin_inventory()
        self.assert_invalid('stale_resume', self.preflight)
        self.inventory['record_order'] = self.snapshot['record_order']
        self.pin_inventory()
        self.canonical.write_text('{"changed":true}', encoding='utf-8')
        self.assert_invalid('stale_review', self.preflight)

    def test_cached_schema_frame_time_image_and_glyph_stroke_sanity(self):
        changes = [lambda raw: raw['rows'][0].update(index=1),
                   lambda raw: raw['rows'][0].update(time_s=.01),
                   lambda raw: raw['rows'][0].update(image_sha256='0'*64),
                   lambda raw: raw.update(production_coordinates_used=True),
                   lambda raw: raw['rows'][0]['points']['a'].update(x=200.),
                   lambda raw: raw['rows'][0]['objects']['a'].update(anchor=[25., 25.5]),
                   lambda raw: raw['rows'][0]['connections'].update({'a-b': {'start': [5., 5.], 'end': [8., 8.], 'pixels': 10}}),
                   lambda raw: raw['rows'][0]['points'].update(b={'status': 'ambiguous', 'boxes': []})]
        for change in changes:
            raw = deepcopy(self.raw)
            change(raw)
            with self.subTest(change=change), self.assertRaises(VisualError):
                resume._raw_schema(raw, self.manifest)
        mass_failure = deepcopy(self.raw)
        mass_failure['rows'][0]['points']['a'] = {'status': 'ambiguous', 'reason': 'synthetic mass failure'}
        resume._raw_schema(mass_failure, self.manifest)

    def test_png_header_hash_and_record_package_source_verified_for_reuse(self):
        plan = self.preflight()
        row = self.normalized[0]
        output = self.root / 'single-copy'
        wrong = deepcopy(row)
        wrong['source_sha256'] = '0'*64
        self.assert_invalid('stale_resume', resume._reuse, wrong, output, {}, plan)
        frame = self.package / self.manifest['frames'][0]['path']
        frame.write_bytes(b'not PNG')
        self.assert_invalid('package_integrity_failure', resume._reuse, row, output, {}, plan)
        manifest = deepcopy(self.manifest)
        manifest['frames'][0]['sha256'] = file_sha256(frame)
        manifest['package_sha256'] = digest({key: value for key, value in manifest.items() if key != 'package_sha256'})
        write_json(self.package / 'manifest.json', manifest)
        self.assert_invalid('invalid_frame', resume._reuse, row, output, {}, plan)
        self.assertFalse(output.exists())

    def test_parsed_reuse_data_is_the_same_pinned_bytes_copied(self):
        plan = self.preflight()
        row = self.normalized[0]
        original = Path.read_bytes
        source = self.partial / 'raw' / row['id'] / 'points.json'
        def changed_read(path):
            return b'{}' if path == source else original(path)
        with patch.object(Path, 'read_bytes', changed_read):
            self.assert_invalid('stale_resume', resume._reuse, row, self.root / 'single-copy', {}, plan)

    def test_callback_restoration_after_exception_and_nested_scope(self):
        plan = self.preflight()
        before = (review.generic._extract, bridge._extra_sources, review.VERSION, review._session_context, review._records, review._code_sources)
        with self.assertRaisesRegex(RuntimeError, 'interrupted test'):
            with resume._controller(plan):
                self.assert_invalid('stale_resume', review.generic._extract, self.normalized[1], self.root / 'never-created', {})
                with self.assertRaises(VisualError):
                    with resume._controller(plan):
                        pass
                with bridge._controller():
                    raise RuntimeError('interrupted test')
        self.assertEqual(before, (review.generic._extract, bridge._extra_sources, review.VERSION, review._session_context, review._records, review._code_sources))
        self.assertFalse(resume._active)
        self.assertFalse(bridge._active)

    def test_protected_outputs_and_existing_output_are_rejected(self):
        plan = self.preflight()
        for root in resume._protected_roots(plan):
            with self.subTest(root=root), self.assertRaises(VisualError):
                resume._safe_output(root / 'new-write', plan)
        self.output.mkdir()
        self.assert_invalid('output_exists', self.prepare)

    def test_own_archive_scope_and_complete_generated25_barrier_are_bound(self):
        self.prepare()
        complete = read_json(self.output / 'extraction-complete.json')
        fresh_name = self.normalized[-1]['id']
        complete['raw_points_sha256'][fresh_name] = '0'*64
        write_json(self.output / 'extraction-complete.json', complete)
        self.assert_invalid('stale_resume', resume.validate, self.output, session_directory=self.authority, require_attested=False)
        complete['raw_points_sha256'][fresh_name] = file_sha256(self.output / 'raw' / fresh_name / 'points.json')
        write_json(self.output / 'extraction-complete.json', complete)
        binding = read_json(self.output / 'resume-consumer-binding.json')
        archive = Path(binding['producer_archive'])
        archive.write_text('# changed resume producer', encoding='utf-8')
        self.assert_invalid('source_changed', resume.validate, self.output, session_directory=self.authority, require_attested=False)

    def test_scope_claim_cannot_be_promoted_even_by_resealing_binding(self):
        self.prepare()
        binding = read_json(self.output / 'resume-consumer-binding.json')
        binding['full_behavior_acceptance'] = True
        write_json(self.output / 'resume-consumer-binding.json', binding)
        receipt = read_json(self.output / 'evidence-receipt.json')
        receipt['resume_consumer_binding_sha256'] = digest(binding)
        write_json(self.output / 'evidence-receipt.json', receipt)
        self.assert_invalid('stale_resume', resume.validate, self.output, session_directory=self.authority, require_attested=False)


if __name__ == '__main__':
    unittest.main()
