"""Dual jump intake proof tests; no engine, subprocess, model or desktop runs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import cv2
import numpy as np

from kiroclops.common import ROOT
from kiroclops.cloud.identity import service_sources
from kiroclops.cloud.results import envelope, make_measurement, request_binding, legacy_result
from kiroclops.local_engine import suite_game_intake_binding as bridge, suite_intake, game_media
from kiroclops.local_engine.intake import prepare_intake, resume_selection, detect_project
from kiroclops.local_engine.session import source_hashes, copy_project
from kiroclops.portable_v2 import runtime
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json, file_sha256
from tests.evidence import requires_evidence


class GameIntakeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='user game bridge ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / 'parent'; self.original = self.root / 'original'
        copy_project(ROOT / 'vendor/heart', self.parent); copy_project(self.parent, self.original)
        with (self.original / 'player.gd').open('a', encoding='utf-8') as stream:
            stream.write('\n# distinct explicit original role\n')
        self.intake = self.root / 'intake'; self.output = self.root / 'adapter'
        suite_intake.draft(self.intake, 'game', 'Match the supplied short and long jumps.', ['short_jump', 'long_jump'])
        self.engine = {'identity': {'fixture': 'unit-test-no-engine'}, 'engine_sha256': digest({'fixture': 'unit-test-no-engine'})}
        self.engine_patch = patch('kiroclops.cloud.identity.engine_identity', return_value=self.engine)
        self.engine_patch.start(); self.addCleanup(self.engine_patch.stop)
        self.count = 0
        self.no_native = patch.object(runtime, 'capture_project', side_effect=AssertionError('No native capture in unit tests'))
        self.no_native.start(); self.addCleanup(self.no_native.stop)
        self.no_import = patch.object(runtime, 'import_project', side_effect=AssertionError('No native import in unit tests'))
        self.no_import.start(); self.addCleanup(self.no_import.stop)

    @staticmethod
    def height(index, start=30, amplitude=20):
        phase = (index - start) / 54
        return max(0, round(amplitude * 4 * phase * (1 - phase))) if 0 <= phase <= 1 else 0

    def clip(self, name, behavior, hold, *, role='calibration', origin=7., provenance='user_declared',
             timing_shift=None, amplitude=20):
        self.count += 1
        raw = self.root / f'raw-{self.count}'; raw.mkdir()
        rng = np.random.default_rng(self.count)
        glyph = rng.integers(20, 240, (8, 10, 3), dtype=np.uint8)
        background = rng.integers(20, 240, (12, 12, 3), dtype=np.uint8)
        paths = []
        for index in range(240):
            image = np.zeros((100, 160, 3), np.uint8)
            height = self.height(index, amplitude=amplitude)
            image[60-height:68-height, 55:65] = glyph
            image[80:92, 10:22] = background
            path = raw / f'{index:06d}.png'; path.write_bytes(cv2.imencode('.png', image)[1].tobytes()); paths.append(path)
        inputs = {'provenance': provenance, 'events': [] if provenance == 'unknown' else [
            {'action': 'jump', 'pressed': True, 'time_s': origin + .5},
            {'action': 'jump', 'pressed': False, 'time_s': origin + (30+hold)/60}]}
        times = [origin + i/60 for i in range(240)]
        if timing_shift is not None:
            times[20] += timing_shift
        package = self.root / f'package-{self.count}'
        create_package(package, paths, times, inputs, {'unit_amplitude': amplitude})
        single = self.root / f'single-{self.count}'
        with patch('kiroclops.local_engine.intake._qualify_automatic', return_value=(None, {'status': 'needs_confirmation'})), \
             patch('kiroclops.local_engine.intake.propose_objects', return_value={'status': 'needs_confirmation', 'proposals': []}):
            prepare_intake(package, self.parent, 'Match this jump.', single)
        manifest = load_package(single / 'package')
        selection = {'schema_version': 1, 'kind': 'ObjectSelection', 'package_sha256': manifest['package_sha256'],
            'objects': [{'id': 'my-hero', 'roi': [55, 60, 10, 8], 'anchor': [5, 4], 'body_height_px': 8,
                'frame_index': 0, 'template_frame_sha256': manifest['frames'][0]['sha256'], 'method': 'template', 'search_radius': 12}],
            'background_rois': [[10, 80, 12, 12]]}
        resume_selection(single, selection)
        result = suite_intake.add_clip(self.intake, single, name, [behavior], {'my-hero': 'character'}, role)
        return manifest, selection, result

    def fake_analysis(self, package, selection, output, check, progress, *, contract, candidate_sha256=None):
        manifest = load_package(package)
        obj = selection['objects'][0]
        x, y, w, h = obj['roi']; amplitude = manifest['metadata'].get('unit_amplitude', 20)
        body = obj.get('body_height_px', 8)
        frames = [{'index': f['index'], 'time_s': f['time_s'], 'valid': True, 'x': x+w/2,
                   'y': y+h/2-self.height(f['index'], amplitude=amplitude)*body/8, 'width': w, 'height': h}
                  for f in manifest['frames']]
        observations = {'kind': 'Observations', 'status': 'ok', 'package_sha256': manifest['package_sha256'],
            'image_size': manifest['image_size'], 'inputs': deepcopy(manifest['inputs']), 'selection': deepcopy(selection),
            'objects': [{'id': obj['id'], 'method': obj['method'], 'frames': frames}],
            'camera': [{'index': f['index'], 'time_s': f['time_s'], 'status': 'pass', 'max_shift_px': 0.}
                       for f in manifest['frames']]}
        events = extract_events(observations, selection, contract)
        request = request_binding(manifest['package_sha256'], selection, contract, candidate_sha256)
        job_id = uuid.uuid4().hex
        delivery = envelope(make_measurement(observations, events), request, logical_job_id=job_id,
                            engine=self.engine, sources=service_sources(), timings={'analysis_s': 0.1})
        result = legacy_result(delivery, request, logical_job_id=job_id)
        output = Path(output); job = output / 'jobs/jobs' / job_id
        write_json(job / 'request.json', request); write_json(job / 'delivery-v2.json', delivery)
        write_json(job / 'state.json', {'job_id': job_id, 'status': 'succeeded', 'request_sha256': digest(request),
            'package_sha256': manifest['package_sha256'], 'candidate_sha256': candidate_sha256,
            'result_sha256': digest(result), 'delivery_sha256': digest(delivery)})
        write_json(output / 'analysis-operation.json', {'job_id': job_id, 'status': 'succeeded', 'request_sha256': digest(request)})
        write_json(output / 'analysis.json', result)
        return result

    def bind(self, **kwargs):
        with patch('kiroclops.local_engine.evaluation._analysis', side_effect=self.fake_analysis) as analyze:
            result = bridge.bind_intake(self.intake, self.output, original=self.original, authoring_parent=self.parent, **kwargs)
        return result, analyze

    def ready(self, *, validation=False, provenance='user_declared', short=3, long=24):
        self.clip('short', 'short_jump', short, provenance=provenance)
        self.clip('long', 'long_jump', long, provenance=provenance)
        if validation:
            self.clip('chosen-validation', 'long_jump', 19, role='validation', provenance=provenance)
        suite_intake.freeze(self.intake)
        return self.bind()

    def reference(self, name='short'):
        return read_json(self.output / 'cases' / name / 'record.json')

    def code(self, expected, fn, *args, **kwargs):
        with self.assertRaises(VisualError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code)

    @requires_evidence('vendor/heart')
    def test_exact_input_origin_pixels_and_explicit_original_parent_roles(self):
        frozen_original, frozen_parent = source_hashes(self.original), source_hashes(self.parent)
        result, analyze = self.ready()
        self.assertEqual(result['status'], 'suite_bound'); self.assertEqual(analyze.call_count, 2)
        self.assertTrue(result['runtime_adapter_ready']); self.assertFalse(result['execution_ready'])
        self.assertEqual(result['session_routing'], 'not_integrated')
        self.assertFalse(result['behavior_accepted']); self.assertFalse(result['benchmark_qualified'])
        record = self.reference()
        self.assertEqual(record['scenario']['id'], 'short')
        self.assertEqual([30, 33], [e['tick'] for e in record['scenario']['events']])
        self.assertEqual(record['observations']['inputs']['events'][0]['time_s'], 7.5)
        self.assertEqual(record['observations']['objects'][0]['frames'][0]['time_s'], 7.)
        self.assertIsNone(read_json(record['analysis'])['candidate_sha256'])
        self.assertEqual(record['intake_binding']['claim'], 'conditional_on_user_declared_inputs')
        self.assertNotIn('raw_run', record)
        suite = bridge.load_suite(self.output)
        self.assertNotEqual(suite['source_roles']['original'], suite['source_roles']['authoring_parent'])
        self.assertEqual(suite['source_hashes']['original'], frozen_original)
        self.assertEqual(suite['source_hashes']['authoring_parent'], frozen_parent)
        self.assertEqual(source_hashes(self.original), frozen_original); self.assertEqual(source_hashes(self.parent), frozen_parent)

    @requires_evidence('vendor/heart')
    def test_distinct_user_validation_retains_declared_role_and_authority(self):
        result, analyze = self.ready(validation=True, provenance='recorder_verified')
        self.assertEqual(analyze.call_count, 3)
        self.assertEqual(result['validation_basis'], 'user_designated_distinct_pixels')
        row = self.reference('chosen-validation')
        self.assertEqual(row['scenario']['split'], 'validation')
        self.assertEqual(row['intake_binding']['input_provenance'], 'recorder_verified')
        self.assertEqual(row['intake_binding']['claim'], 'recorded_input_response')
        self.assertFalse(row['blind_validation']); self.assertFalse(row['benchmark_qualified'])

    @requires_evidence('vendor/heart')
    def test_unknown_inputs_stay_pending_without_worker_or_target(self):
        manifest, selection, _ = self.clip('short', 'short_jump', 3, provenance='unknown')
        clip = {'behaviors': ['short_jump'], 'role': 'calibration', 'object_map': {'my-hero': 'character'}}
        binding = bridge._binding(detect_project(self.parent), manifest, selection, clip, {})
        self.assertEqual(binding['status'], 'needs_input_binding')
        result, analyze = self.bind()
        self.assertEqual(result['status'], 'needs_reference'); self.assertEqual(analyze.call_count, 0)
        self.assertFalse(self.output.exists())

    @requires_evidence('vendor/heart')
    def test_variable_rate_and_extra_inputs_fail_explicitly(self):
        manifest, selection, _ = self.clip('short', 'short_jump', 3, timing_shift=.001)
        clip = {'behaviors': ['short_jump'], 'role': 'calibration', 'object_map': {'my-hero': 'character'}}
        self.code('unsupported_game_media_timing', bridge._binding, detect_project(self.parent), manifest, selection, clip, {})
        manifest['frames'][20]['time_s'] -= .001
        manifest['inputs']['events'].append({'action': 'right', 'pressed': True, 'time_s': 8.})
        self.code('invalid_input_binding', bridge._binding, detect_project(self.parent), manifest, selection, clip, {})

    @requires_evidence('vendor/heart')
    def test_dual_jump_requires_both_calibration_examples(self):
        self.clip('short', 'short_jump', 3)
        self.clip('long', 'long_jump', 24, role='validation')
        suite_intake.freeze(self.intake)
        result, analyze = self.bind()
        self.assertEqual(result['missing_calibration_behaviors'], ['long_jump'])
        self.assertEqual(analyze.call_count, 0); self.assertFalse(self.output.exists())

    @requires_evidence('vendor/heart')
    def test_equal_or_reversed_declared_hold_lengths_are_rejected(self):
        self.clip('short', 'short_jump', 24); self.clip('long', 'long_jump', 3)
        suite_intake.freeze(self.intake)
        with patch('kiroclops.local_engine.evaluation._analysis', side_effect=AssertionError('must not measure invalid pair')):
            self.code('invalid_input_binding', bridge.bind_intake, self.intake, self.output,
                      original=self.original, authoring_parent=self.parent)
        self.assertFalse(self.output.exists())

    @requires_evidence('vendor/heart')
    def test_unobservable_reference_jump_does_not_become_ready(self):
        self.clip('short', 'short_jump', 3, amplitude=0); self.clip('long', 'long_jump', 24)
        suite_intake.freeze(self.intake)
        with patch('kiroclops.local_engine.evaluation._analysis', side_effect=self.fake_analysis):
            self.code('unobservable_behavior', bridge.bind_intake, self.intake, self.output,
                      original=self.original, authoring_parent=self.parent)
        self.assertFalse((self.output / 'suite.json').exists())

    @requires_evidence('vendor/heart')
    def test_parent_cannot_be_replaced_with_original(self):
        self.clip('short', 'short_jump', 3); self.clip('long', 'long_jump', 24); suite_intake.freeze(self.intake)
        self.code('wrong_authoring_parent', bridge.bind_intake, self.intake, self.output,
                  original=self.original, authoring_parent=self.original)
        self.assertFalse(self.output.exists())

    @requires_evidence('vendor/heart')
    def test_reference_cannot_acquire_native_or_candidate_identity(self):
        self.ready(); row = self.reference()
        self.code('measurement_binding_changed', bridge._verify_measurement, row, 'c'*64)
        row['raw_run'] = str(self.root / 'invented-native')
        self.code('binding_changed', bridge.verify_reference_record, row)
        row = self.reference(); result = read_json(row['analysis']); result['candidate_sha256'] = 'a'*64
        write_json(row['analysis'], result)
        self.code('measurement_binding_changed', bridge.verify_reference_record, row)

    @requires_evidence('vendor/heart')
    def test_saved_worker_operation_request_and_source_receipts_are_required(self):
        self.ready(); row = self.reference(); analysis = Path(row['analysis']); operation = read_json(analysis.parent / 'analysis-operation.json')
        job = analysis.parent / 'jobs/jobs' / operation['job_id']
        for name, field, value in (('state.json', 'status', 'failed'), ('state.json', 'candidate_sha256', 'b'*64),
                                   ('request.json', 'candidate_sha256', 'b'*64), ('analysis-operation.json', 'job_id', uuid.uuid4().hex)):
            path = analysis.parent / name if name == 'analysis-operation.json' else job / name
            before = path.read_bytes(); changed = read_json(path); changed[field] = value; write_json(path, changed)
            with self.subTest(name=name, field=field), self.assertRaises((VisualError, FileNotFoundError)):
                bridge.verify_reference_record(row)
            path.write_bytes(before)
        delivery = read_json(job / 'delivery-v2.json')
        request = read_json(job / 'request.json')
        changed = envelope(delivery['measurement'], request, logical_job_id=operation['job_id'],
                           engine=self.engine, sources={'foreign.py': 'a'*64}, timings={})
        result = legacy_result(changed, request, logical_job_id=operation['job_id'])
        state = read_json(job / 'state.json'); state.update(result_sha256=digest(result), delivery_sha256=digest(changed))
        write_json(job / 'delivery-v2.json', changed); write_json(job / 'state.json', state); write_json(analysis, result)
        self.code('result_binding_mismatch', bridge.verify_reference_record, row)

    @requires_evidence('vendor/heart')
    def test_inline_observations_and_recomputed_events_cannot_diverge(self):
        self.ready(); row = self.reference()
        row['observations']['objects'][0]['frames'][40]['y'] += 1
        self.code('measurement_binding_changed', bridge.verify_reference_record, row)
        row = self.reference(); row['events']['events'] = []
        self.code('measurement_binding_changed', bridge.verify_reference_record, row)

    @requires_evidence('vendor/heart')
    def test_pixel_template_and_code_binding_mutations_are_rejected(self):
        self.ready(); row = self.reference()
        row['intake_source_selection']['objects'][0]['template_frame_sha256'] = 'a'*64
        self.code('selection_binding_mismatch', bridge.verify_reference_record, row)
        row = self.reference(); row['measurement_source_hashes']['foreign.py'] = 'a'*64
        self.code('measurement_source_changed', bridge.verify_reference_record, row)
        package = load_package(row['package']); frame = Path(row['package']) / package['frames'][0]['path']
        frame.write_bytes(frame.read_bytes() + b'changed')
        with self.assertRaises(VisualError): bridge.verify_reference_record(self.reference())

    @requires_evidence('vendor/heart')
    def test_missing_background_is_pending_and_camera_frame_failure_rejected(self):
        manifest, selection, _ = self.clip('short', 'short_jump', 3)
        clip = {'behaviors': ['short_jump'], 'role': 'calibration', 'object_map': {'my-hero': 'character'}}
        selection['background_rois'] = []
        self.code('needs_camera_binding', bridge._binding, detect_project(self.parent), manifest, selection, clip, {})
        self.clip('long', 'long_jump', 24); suite_intake.freeze(self.intake)
        self.bind(); row = self.reference()
        row['observations']['camera'][10]['status'] = 'unverified'
        # Coherent worker artifacts still cannot upgrade unverified camera
        # frames to a numerical reference, even if outer status says "ok".
        analysis = Path(row['analysis']); result = read_json(analysis)
        result['observations'] = deepcopy(row['observations'])
        result['events'] = extract_events(result['observations'], row['selection'], bridge.CONTRACT)
        row['events'] = deepcopy(result['events'])
        operation = read_json(analysis.parent / 'analysis-operation.json'); job = analysis.parent / 'jobs/jobs' / operation['job_id']
        request = read_json(job / 'request.json')
        delivery = envelope(make_measurement(result['observations'], result['events']), request,
            logical_job_id=operation['job_id'], engine=self.engine, sources=service_sources(), timings={})
        result = legacy_result(delivery, request, logical_job_id=operation['job_id'])
        state = read_json(job / 'state.json'); state.update(result_sha256=digest(result), delivery_sha256=digest(delivery))
        write_json(job / 'delivery-v2.json', delivery); write_json(job / 'state.json', state); write_json(analysis, result)
        self.code('camera_unverified', bridge.verify_reference_record, row)

    @requires_evidence('vendor/heart')
    def test_frozen_source_changes_and_false_suite_claims_are_rejected(self):
        self.ready()
        source = self.output / 'sources/original/player.gd'; before = source.read_bytes(); source.write_bytes(before+b'\nchanged\n')
        self.code('source_changed', bridge.load_suite, self.output); source.write_bytes(before)
        suite = read_json(self.output / 'suite.json'); suite['benchmark_qualified'] = True
        write_json(self.output / 'suite.json', suite); write_json(self.output / 'suite-binding.json', {'suite_sha256': digest(suite)})
        self.code('binding_changed', bridge.load_suite, self.output)

    @requires_evidence('vendor/heart')
    def test_reference_record_paths_must_remain_owned(self):
        self.ready(); row = self.reference(); copied = self.root / 'foreign-record.json'; write_json(copied, row)
        suite = read_json(self.output / 'suite.json'); suite['cases'][0]['record'] = '../foreign-record.json'
        write_json(self.output / 'suite.json', suite); write_json(self.output / 'suite-binding.json', {'suite_sha256': digest(suite)})
        self.code('unsafe_path', bridge.load_suite, self.output)

    def source_scope(self):
        expected = source_hashes(self.parent)
        prepared, receipt = game_media.prepare_baseline(self.parent, self.root / 'source-scope/prepared')
        rendered = self.root / 'source-scope/native/project'; copy_project(prepared, rendered)
        scope = {'kind': 'UserGameCaptureSourceScope', 'selected': str(self.parent), 'prepared': str(prepared),
                 'rendered': str(rendered), 'selected_sources': expected, 'selected_sha256': digest(expected),
                 'prepared_sources': source_hashes(prepared), 'native_sources': runtime.source_hashes(rendered),
                 'rendered_sources': source_hashes(rendered), 'preparation': receipt}
        return {'capture_source_scope': scope, 'source_sha256': digest(expected)}, expected

    @requires_evidence('vendor/heart')
    def test_exact_source_overlay_replays_and_gameplay_or_uid_scope_cannot_be_relabeled(self):
        row, expected = self.source_scope()
        bridge._verify_source_scope(row, expected)
        rendered = Path(row['capture_source_scope']['rendered'])
        (rendered / 'generated.gd.uid').write_text('uid://generated', encoding='utf-8')
        self.code('source_changed', bridge._verify_source_scope, row, expected)
        # Simulate a legitimate import-sidecar snapshot made by the producer,
        # then require it to remain byte-for-byte immutable afterward.
        row['capture_source_scope']['rendered_sources'] = source_hashes(rendered)
        bridge._verify_source_scope(row, expected)
        (rendered / 'player.gd').write_text('# changed gameplay', encoding='utf-8')
        self.code('source_changed', bridge._verify_source_scope, row, expected)

    @requires_evidence('vendor/heart')
    def test_settings_controller_preparation_receipt_and_foreign_source_fail_closed(self):
        row, expected = self.source_scope()
        prepared = Path(row['capture_source_scope']['prepared'])
        receipt_path = prepared.parent / 'prepared-preparation.json'
        receipt_bytes = receipt_path.read_bytes(); receipt = read_json(receipt_path); receipt['controller_unchanged'] = False
        write_json(receipt_path, receipt)
        self.code('source_changed', bridge._verify_source_scope, row, expected); receipt_path.write_bytes(receipt_bytes)
        config = prepared / 'project.godot'; before = config.read_bytes(); config.write_bytes(before+b'\n[foreign]\n')
        row['capture_source_scope']['prepared_sources'] = source_hashes(prepared)
        self.code('source_changed', bridge._verify_source_scope, row, expected)
        self.code('source_changed', bridge._verify_source_scope, row, source_hashes(self.original))

    @requires_evidence('vendor/heart')
    def test_no_missing_or_foreign_case_can_shrink_aggregate_matrix(self):
        self.ready()
        self.code('suite_recipe_mismatch', bridge.compare_suite, self.output, {'short': {}}, {'short': {}},
                  expected_candidate_sources=source_hashes(self.parent))
        self.code('suite_recipe_mismatch', bridge.compare_suite, self.output,
                  {'short': {}, 'long': {}, 'foreign': {}}, {'short': {}, 'long': {}, 'foreign': {}},
                  expected_candidate_sources=source_hashes(self.parent))

    @requires_evidence('vendor/heart')
    def test_one_case_failure_cannot_hide_in_a_passing_mean(self):
        self.ready()
        rows = [{'pass': True, 'score': 0.}, {'pass': False, 'score': .2}]
        with patch.object(bridge, 'compare_records', side_effect=rows) as compare:
            result = bridge.compare_suite(self.output, {'short': {}, 'long': {}}, {'short': {}, 'long': {}},
                                          expected_candidate_sources=source_hashes(self.parent))
        self.assertFalse(result['numerical_pass']); self.assertEqual(compare.call_count, 2)
        self.assertTrue(all(call.kwargs['require_improvement'] is True for call in compare.call_args_list))
        self.assertFalse(result['behavior_accepted']); self.assertEqual(result['independent_raw_review'], 'required_separately')

    @requires_evidence('vendor/heart')
    def test_real_jump_metrics_require_original_gain_and_visible_completed_jump(self):
        self.ready(); reference = self.reference()
        original, candidate = deepcopy(reference), deepcopy(reference)
        original.update(source_role='original'); candidate.update(source_role='candidate')
        for frame in original['observations']['objects'][0]['frames']:
            frame['y'] = 64 + (frame['y'] - 64) * .7
        for frame in candidate['observations']['objects'][0]['frames']:
            frame['y'] = 64 + (frame['y'] - 64) * .95
        # This test isolates comparison math; source/worker verification is
        # exercised independently above and actual native transport is unrun.
        with patch.object(bridge, 'verify_target_record', return_value={'status': 'verified'}) as verify:
            good = bridge.compare_records(reference, original, candidate,
                expected_original_sources=source_hashes(self.original), expected_candidate_sources=source_hashes(self.parent))
            self.assertTrue(good['pass']); self.assertEqual(good['baseline_role'], 'original')
            self.assertEqual(verify.call_args_list[0].kwargs['source_role'], 'original')
            self.assertFalse(good['behavior_accepted']); self.assertFalse(good['benchmark_qualified'])
            for frame in candidate['observations']['objects'][0]['frames']: frame['y'] = 64
            bad = bridge.compare_records(reference, original, candidate,
                expected_original_sources=source_hashes(self.original), expected_candidate_sources=source_hashes(self.parent))
            self.assertFalse(bad['pass']); self.assertFalse(bad['metric_gates']['jump_observed'])
            exact = deepcopy(reference); exact.update(source_role='original')
            perfect = deepcopy(reference); perfect.update(source_role='candidate')
            no_gain = bridge.compare_records(reference, exact, perfect,
                expected_original_sources=source_hashes(self.original), expected_candidate_sources=source_hashes(self.parent))
            self.assertFalse(no_gain['pass']); self.assertIsNone(no_gain['phases'][0]['improvement_fraction'])


if __name__ == '__main__':
    unittest.main()
