"""Revision provenance checks with saved PNGs and in-memory worker fixtures."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.cloud.results import envelope, legacy_result, make_measurement, request_binding
from kiroclops.local_engine import media_suite, suite_revision as revision, suite_web
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.package import copy_package, create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class Fixture:
    def __init__(self, root):
        self.root = root
        self.image = root / 'image.png'
        cv2.imencode('.png', np.zeros((4, 4, 3), np.uint8))[1].tofile(self.image)
        self.sources = {'kiroclops/visual/tracking.py': digest('tracker'), 'kiroclops/visual/events.py': digest('events')}
        identity = {'opencv_version': 'fixture', 'threads': 1}
        self.engine = {'identity': identity, 'engine_sha256': digest(identity)}
        self.scenario = suite_web.scenarios('panel', 'calibration')[0]
        self.selection = {'objects': [{'id': 'panel', 'roi': [0, 0, 2, 2]}]}
        self.parent = self.project(root / 'parent', 'parent')
        self.original = self.project(root / 'original', 'original')

    @staticmethod
    def project(path, value):
        path.mkdir(parents=True)
        (path / 'index.html').write_text(value, encoding='utf-8')
        return path

    def analyze(self, package, selection, output, check, progress, *, contract, candidate_sha256):
        started_at = time.time()
        check()
        output = Path(output)
        manifest = load_package(package, verify='hashes')
        observed = {'package_sha256': manifest['package_sha256'], 'status': 'ok', 'inputs': manifest['inputs'],
                    'objects': [{'id': obj['id'], 'frames': [{'index': i, 'time_s': frame['time_s'],
                        'valid': True, 'x': 1., 'y': 1., 'width': 2., 'height': 2.}
                        for i, frame in enumerate(manifest['frames'])]} for obj in selection['objects']]}
        events = {'kind': 'EventSet', 'motion': [], 'uncertainty': 'fixture'}
        request = request_binding(manifest['package_sha256'], selection, contract, candidate_sha256)
        delivery = envelope(make_measurement(observed, events), request, logical_job_id='worker1',
                            engine=self.engine, sources=self.sources, timings={'execution_wall_s': 0.})
        result = legacy_result(delivery, request, logical_job_id='worker1')
        write_json(output / 'analysis.json', result)
        write_json(output / 'analysis-operation.json', {'job_id': 'worker1', 'status': 'succeeded', 'request_sha256': digest(request)})
        job = output / 'jobs/jobs/worker1'
        write_json(job / 'state.json', {'status': 'succeeded', 'request_sha256': digest(request),
            'result_sha256': digest(result), 'delivery_sha256': digest(delivery),
            'started_at': started_at, 'finished_at': time.time()})
        write_json(job / 'request.json', request)
        write_json(job / 'delivery-v2.json', delivery)
        return result

    def record(self, path, *, project=None, native=False):
        candidate = digest(source_hashes(project or self.parent))
        metadata = {'scenario': self.scenario, 'source_hashes': source_hashes(project or self.parent)}
        if native:
            metadata['game'] = 'heart'
        create_package(path / 'package', [self.image] * 3, [0., .1, .2],
                       {'provenance': 'recorder_verified', 'events': []}, metadata)
        contract = suite_web.event_contract('panel')
        destination = path / 'measurement'
        if native:
            contract = {'min_valid_ratio': .95, 'require_static_camera': True, 'camera_max_shift_px': 1.}
            destination = destination / 'common-analysis'
        result = self.analyze(path / 'package', self.selection, destination, lambda: None, lambda *args: None,
                              contract=contract, candidate_sha256=candidate)
        value = {'package': str(path / 'package'), 'analysis': str(destination / 'analysis.json'),
                 'scenario': deepcopy(self.scenario), 'source_sha256': candidate, 'measurement_status': 'ok',
                 'selection': deepcopy(self.selection), 'observations': result['observations'], 'events': result['events'],
                 'timing': {'status': 'pass'}, 'functional': {'status': 'pass'},
                 'suite_event_contract': deepcopy(contract), 'independent_review_contract': deepcopy(contract),
                 'event_contract_sha256': digest(contract)}
        if native:
            raw = path / 'raw'
            write_json(raw / 'manifest.json', {'game': 'heart'})
            projection = {'status': 'ok', 'candidate_sha256': candidate, 'provenance': {
                'package_sha256': result['package_sha256'], 'observation_sha256': digest(result['observations'])}}
            write_json(path / 'measurement/analysis.json', projection)
            value.update(analysis=str(path / 'measurement/analysis.json'), raw_run=str(raw),
                         native_renderer_source_sha256=digest('renderer-without-sidecars'))
        return value


class MeasurementSliceTests(unittest.TestCase):
    def test_compare_edit_allowed_but_event_policy_and_import_changes_are_not(self):
        original = ('from copy import deepcopy\nPOLICY="v3"\n'
                    'def event_contract(domain):\n return {"rest_duration_s": .20}\n'
                    'def capture_record():\n return event_contract("panel")\n'
                    'def compare_records():\n return False\n')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'suite_web.py'
            path.write_text(original, encoding='utf-8')
            expected = revision._web_measurement_hash(path)
            path.write_text(original.replace('return False', 'return True'), encoding='utf-8')
            self.assertEqual(expected, revision._web_measurement_hash(path))
            path.write_text(original.replace('.20', '.25'), encoding='utf-8')
            self.assertNotEqual(expected, revision._web_measurement_hash(path))
            path.write_text(original.replace('from copy import deepcopy', 'from copy import copy as deepcopy'), encoding='utf-8')
            self.assertNotEqual(expected, revision._web_measurement_hash(path))

    def test_real_measurement_closure_includes_recorder_and_worker_sources(self):
        sources = revision.measurement_source_hashes('panel')
        for name in ('kiroclops/visual/tracking.py', 'kiroclops/visual/events.py', 'kiroclops/web_local/capture.py',
                     'kiroclops/local_engine/worker.py', 'kiroclops/cloud/results.py'):
            self.assertIn(name, sources)
        self.assertNotIn('kiroclops/local_engine/media_suite.py', sources)
        self.assertIn('kiroclops/local_engine/suite_web.py#measurement_ast', sources)


class RecordRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)
        self.patches = [patch.object(revision, 'measurement_source_hashes', return_value=self.fixture.sources),
                        patch.object(revision, 'service_sources', return_value=self.fixture.sources),
                        patch.object(revision, 'engine_identity', return_value=self.fixture.engine),
                        patch('kiroclops.web_local.selection.select_package_from_pixels', side_effect=lambda *args: deepcopy(self.fixture.selection)),
                        patch('kiroclops.local_engine.evaluation._analysis', side_effect=self.fixture.analyze),
                        patch('kiroclops.local_engine.evaluation._capture_web', side_effect=AssertionError('No capture permitted')),
                        patch('kiroclops.local_engine.suite_game.capture_target', side_effect=AssertionError('No native capture permitted'))]
        self.mocks = [item.start() for item in self.patches]
        for item in reversed(self.patches):
            self.addCleanup(item.stop)

    def revise(self, record, name, **kwargs):
        return revision.revise_record(record, self.root / name, 'panel', **kwargs)

    def test_missing_stamp_reanalyzes_same_pixels_and_keeps_old_identity_unknown(self):
        old = self.fixture.record(self.root / 'old')
        before = source_hashes(self.root / 'old')
        result = self.revise(old, 'new')
        self.assertEqual(before, source_hashes(self.root / 'old'))
        self.assertEqual('reanalyzed_existing_pixels', result['measurement_revision']['action'])
        self.assertEqual('unknown', result['measurement_revision']['previous']['historical_code_identity'])
        self.assertIsNone(result['measurement_revision']['previous']['measurement_source_hashes'])
        self.assertEqual(old['observations']['package_sha256'], result['observations']['package_sha256'])
        revision.verify_measurement_record(result, 'panel', expected_sources=self.fixture.sources)
        self.assertEqual(1, self.mocks[4].call_count)

    def test_verified_current_copy_preserves_original_worker_receipt_and_runs_no_analysis(self):
        measured = self.revise(self.fixture.record(self.root / 'old'), 'measured')
        self.mocks[4].reset_mock()
        copied = self.revise(measured, 'copy')
        self.assertEqual('copied_current_measurement', copied['measurement_revision']['action'])
        self.assertEqual(measured['measurement_provenance'], copied['measurement_provenance'])
        self.assertFalse(self.mocks[4].called)

    def test_valid_old_pipeline_forces_reanalysis_and_preserves_its_stamp(self):
        measured = self.revise(self.fixture.record(self.root / 'old'), 'measured')
        old_sources = deepcopy(measured['measurement_source_hashes'])
        newer = {**old_sources, 'extra-measurement-code.py': digest('new')}
        with patch.object(revision, 'measurement_source_hashes', return_value=newer):
            result = self.revise(measured, 'newer')
        self.assertEqual('reanalyzed_existing_pixels', result['measurement_revision']['action'])
        self.assertEqual(old_sources, result['measurement_revision']['previous']['measurement_source_hashes'])
        self.assertEqual(newer, result['measurement_source_hashes'])

    def test_tampered_saved_stamp_fails_without_reanalysis(self):
        measured = self.revise(self.fixture.record(self.root / 'old'), 'measured')
        measured['events']['motion'].append({'fake': True})
        self.mocks[4].reset_mock()
        with self.assertRaises(VisualError) as caught:
            self.revise(measured, 'tampered')
        self.assertEqual('measurement_provenance_changed', caught.exception.code)
        self.assertFalse(self.mocks[4].called)

    def test_engine_change_reanalyzes_and_preserves_prior_engine_receipt(self):
        measured = self.revise(self.fixture.record(self.root / 'old'), 'measured')
        old_engine = deepcopy(self.fixture.engine)
        identity = {'opencv_version': 'new-fixture', 'threads': 1}
        self.fixture.engine = {'identity': identity, 'engine_sha256': digest(identity)}
        with patch.object(revision, 'engine_identity', return_value=self.fixture.engine):
            result = self.revise(measured, 'engine-revision')
        prior = result['measurement_revision']['previous']['measurement_provenance']['worker']['receipt']['engine']
        self.assertEqual(old_engine, prior)
        self.assertEqual(self.fixture.engine, result['measurement_provenance']['worker']['receipt']['engine'])

    def test_code_change_during_analysis_rejects_publication(self):
        old = self.fixture.record(self.root / 'old')
        with patch.object(revision, 'measurement_source_hashes', side_effect=[self.fixture.sources, self.fixture.sources,
                      {**self.fixture.sources, 'changed.py': digest('changed')}]):
            with self.assertRaises(VisualError) as caught:
                self.revise(old, 'changed')
        self.assertEqual('analysis_source_changed', caught.exception.code)
        self.assertFalse((self.root / 'changed/record.json').exists())

    def test_reanalysis_missing_execution_receipt_is_unrecoverable(self):
        old = self.fixture.record(self.root / 'old')
        def invalid(*args, **kwargs):
            result = self.fixture.analyze(*args, **kwargs)
            (Path(args[2]) / 'jobs/jobs/worker1/delivery-v2.json').unlink()
            return result
        self.mocks[4].side_effect = invalid
        with self.assertRaises(OSError):
            self.revise(old, 'no-receipt')
        self.assertFalse((self.root / 'no-receipt/record.json').exists())

    def test_native_projection_keeps_full_source_identity_including_sidecar(self):
        (self.fixture.parent / 'player.gd.uid').write_text('sidecar', encoding='utf-8')
        old = self.fixture.record(self.root / 'native-old', native=True)
        annotation = self.root / 'annotation.json'
        write_json(annotation, {'camera_max_shift_px': 1.})
        def analyze_native(raw, stage, scenario, output, **kwargs):
            copy_package(old['package'], Path(output) / 'package')
            contract = {'min_valid_ratio': .95, 'require_static_camera': True, 'camera_max_shift_px': 1.}
            common = self.fixture.analyze(Path(output) / 'package', self.fixture.selection, Path(output) / 'common-analysis',
                kwargs['check'], kwargs['progress'], contract=contract, candidate_sha256=kwargs['candidate_sha256'])
            projection = {'status': 'ok', 'candidate_sha256': kwargs['candidate_sha256'], 'provenance': {
                'package_sha256': common['package_sha256'], 'observation_sha256': digest(common['observations'])}}
            write_json(Path(output) / 'analysis.json', projection)
            return {'package': str(Path(output) / 'package'), 'analysis': str(Path(output) / 'analysis.json'),
                    'observations': common['observations'], 'events': common['events'], 'measurement_status': 'ok'}
        with patch('kiroclops.local_engine.suite_game.analyze_record', side_effect=analyze_native), \
                patch('kiroclops.local_engine.game._annotation', return_value=annotation):
            result = revision.revise_record(old, self.root / 'native-new', 'jump')
        self.assertEqual(old['source_sha256'], result['source_sha256'])
        self.assertEqual(digest(source_hashes(self.fixture.parent)), read_json(result['analysis'])['candidate_sha256'])
        self.assertEqual(old['source_sha256'], result['measurement_provenance']['worker']['request']['candidate_sha256'])
        self.assertEqual(old['native_renderer_source_sha256'], result['native_renderer_source_sha256'])
        revision.verify_measurement_record(result, 'jump')

    def test_uploaded_reference_preserves_user_selection_and_candidate_none(self):
        old = self.fixture.record(self.root / 'uploaded-old')
        old.update(reference_kind='uploaded_pixels', intake_binding={'object_map': {'custom': 'panel'}},
                   intake_source_selection={'objects': [{'id': 'custom'}]},
                   intake_provenance={'input_provenance': 'user_declared'})
        old['selection'] = {'objects': [{'id': 'custom', 'roi': [0, 0, 2, 2]}],
                            'lifecycle_bindings': {'custom': [{'start': {'kind': 'clip_start'}, 'end': {'kind': 'clip_end'}}]},
                            'input_origin_s': 0.}
        with patch('kiroclops.local_engine.suite_intake_binding.verify_reference_record', return_value={'status': 'verified'}):
            result = self.revise(old, 'uploaded-new')
            revision.verify_measurement_record(result, 'panel')
        self.assertEqual(old['selection'], result['selection'])
        self.assertEqual(old['intake_provenance'], result['intake_provenance'])
        self.assertEqual(old['intake_source_selection'], result['intake_source_selection'])
        self.assertIsNone(result['measurement_provenance']['worker']['request']['candidate_sha256'])
        self.assertFalse(self.mocks[3].called)

    def test_imported_target_remaps_pixel_ids_and_preserves_full_source_binding(self):
        old = self.fixture.record(self.root / 'uploaded-target-old')
        binding = {'object_map': {'custom': 'panel'}, 'connections': [], 'relative_motion': []}
        old.update(intake_binding=deepcopy(binding), intake_provenance={'input_provenance': 'user_declared'})
        reference = {'intake_binding': binding, 'selection': {'objects': [{'id': 'custom', 'roi': [0, 0, 2, 2]}],
                    'lifecycle_bindings': {'custom': [{'start': {'kind': 'clip_start'}, 'end': {'kind': 'clip_end'}}]}}}
        with patch('kiroclops.local_engine.suite_intake_binding.verify_reference_record', return_value={'status': 'verified'}), \
                patch('kiroclops.local_engine.media_session._preserve_point_semantics', side_effect=lambda ref, obj, manifest: deepcopy(obj)):
            result = self.revise(old, 'uploaded-target-new', reference_record=reference)
        self.assertEqual(['custom'], [obj['id'] for obj in result['selection']['objects']])
        self.assertEqual(old['source_sha256'], result['measurement_provenance']['worker']['request']['candidate_sha256'])
        self.assertEqual(old['intake_provenance'], result['intake_provenance'])

    def test_fresh_stamp_binds_precapture_code_and_worker_chronology_without_analysis(self):
        snapshot = revision.measurement_source_hashes('panel')
        started = time.time()
        captured = self.fixture.record(self.root / 'fresh-captured')
        stamped = revision.stamp_new_measurement(captured, 'panel', expected_sources=snapshot, capture_started_at=started)
        self.assertEqual(stamped['observations'], captured['observations'])
        self.assertNotIn('measurement_source_hashes', captured)
        self.assertEqual(started, stamped['measurement_provenance']['fresh_capture_started_at'])
        self.assertFalse(self.mocks[4].called)
        revision.verify_measurement_record(stamped, 'panel', expected_sources=snapshot)

    def test_fresh_stamp_rejects_a_worker_completed_before_capture_transaction(self):
        historical = self.fixture.record(self.root / 'historical')
        with self.assertRaises(VisualError) as caught:
            revision.stamp_new_measurement(historical, 'panel', expected_sources=self.fixture.sources, capture_started_at=time.time())
        self.assertEqual('fresh_capture_provenance_missing', caught.exception.code)
        self.assertNotIn('measurement_source_hashes', historical)

    def test_fresh_stamp_rejects_changed_precapture_source_identity(self):
        started = time.time()
        captured = self.fixture.record(self.root / 'fresh')
        with self.assertRaises(VisualError) as caught:
            revision.stamp_new_measurement(captured, 'panel', expected_sources={'stale.py': digest('old')}, capture_started_at=started)
        self.assertEqual('analysis_source_changed', caught.exception.code)


class SuiteRevisionTests(unittest.TestCase):
    setUp = RecordRevisionTests.setUp
    def test_suite_revision_preserves_case_matrix_initial_roles_pixels_and_previous_suite(self):
        old = self.fixture.record(self.root / 'old-ref')
        before = self.fixture.record(self.root / 'old-before')
        original = self.fixture.record(self.root / 'old-original', project=self.fixture.original)
        name = old['scenario']['case_id']
        sources = media_suite.analysis_source_hashes()
        with patch.object(media_suite, 'analysis_source_hashes', return_value=sources):
            suite = media_suite.create_suite(self.root / 'old-suite', domain='web_panel', stage='panel',
                records=[{'case_id': name, 'role': 'calibration', 'stage': 'panel', 'record': old}],
                required_cases=[{'case_id': name, 'role': 'calibration'}], original=self.fixture.original,
                authoring_parent=self.fixture.parent, goal='Match this motion', event_contract=suite_web.event_contract('panel'),
                initial_records=[{'case_id': name, 'role': 'before', 'record': before},
                                 {'case_id': name, 'role': 'original', 'record': original}])
            old_hashes = source_hashes(self.root / 'old-suite')
            result = revision.revise_suite(self.root / 'old-suite', self.root / 'revision', self.fixture.parent)
            new = media_suite.load_suite(result['suite'])
        self.assertEqual(3, result['reanalyzed_records'])
        self.assertEqual(old_hashes, source_hashes(self.root / 'old-suite'))
        self.assertEqual(suite['required_cases'], new['required_cases'])
        self.assertEqual(suite['cases'][0]['scenario'], new['cases'][0]['scenario'])
        self.assertEqual(suite['cases'][0]['package_sha256'], new['cases'][0]['package_sha256'])
        self.assertEqual({'before', 'original'}, {item['role'] for item in new['initial_records']})
        self.assertEqual('pending', result['reference_raw_review_status'])
        for item in new['cases'] + new['initial_records']:
            record = media_suite.record_from_suite(result['suite'], item)
            revision.verify_measurement_record(record, 'panel', expected_sources=self.fixture.sources)


class WrapperTests(unittest.TestCase):
    def test_started_session_is_rejected_before_provisioning_or_analysis(self):
        from scripts import phase3d_revision
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(phase3d_revision, 'Session') as session, \
                patch.object(phase3d_revision, 'prepare_runtime') as runtime, \
                patch.object(phase3d_revision, 'revise_suite') as revise:
            session.return_value._load.return_value = {'started_at': 1., 'candidates': [],
                                                       'experiment': {'reference_mode': 'media_suite'}}
            with self.assertRaises(VisualError) as caught:
                phase3d_revision.run(Path(temporary) / 'old-session', 'p03', Path(temporary) / 'runtime')
            self.assertEqual('session_started', caught.exception.code)
            self.assertFalse(runtime.called)
            self.assertFalse(revise.called)


if __name__ == '__main__':
    unittest.main()
