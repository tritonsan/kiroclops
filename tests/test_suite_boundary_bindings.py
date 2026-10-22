"""Boundary proof validation with synthetic PNGs and genuine receipt schemas.

No engine, capture process or tracking worker starts. Observations describe
known pixels drawn by this fixture; production event extraction is exercised.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.cloud.identity import service_sources
from kiroclops.cloud.results import envelope, legacy_result, make_measurement, request_binding
from kiroclops.local_engine import suite_boundary, visual_boundary_v4 as boundary
from kiroclops.local_engine.session import source_hashes
from kiroclops.portable_v2 import runtime
from kiroclops.visual.compat import import_legacy_run
from kiroclops.visual.events import extract_events
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class BoundaryFixture:
    def __init__(self, root):
        self.root = root
        self.native = root / 'native'
        self.selected = root / 'selected'
        self.selected.mkdir()
        (self.selected / 'player.gd').write_text('extends CharacterBody2D\n', encoding='utf-8')
        (self.selected / 'player.gd.uid').write_text('uid://fixed\n', encoding='utf-8')
        self.full = source_hashes(self.selected)
        self.sources = runtime.source_hashes(self.selected)
        self.worker_sources = service_sources()
        identity = {'opencv_version': 'fixture', 'threads': 1}
        self.engine = {'identity': identity, 'engine_sha256': digest(identity)}
        self.policy = {'version': 'synthetic-boundary-fixture', 'source_hashes': {'boundary.py': digest('fixture')}}
        self.rendered = self.native / 'project'
        shutil.copytree(self.selected, self.rendered)
        overlay = self.rendered / boundary.OVERLAY
        overlay.mkdir()
        for name in ('Boundary.tscn', 'boundary.gd'):
            shutil.copyfile(boundary.ADAPTER / name, overlay / name)
        self.rendered_sources = runtime.source_hashes(self.rendered)
        self.rows, summaries = [], []
        labels = {'approach': 'visible_approach', 'parallel': 'visible_parallel_motion',
                  'rebound': 'visible_departure_after_approach', 'near_miss': 'distant_motion'}
        for number, scenario in enumerate(boundary.cases(), 1):
            row, measured = self.case(scenario, number)
            self.rows.append(row)
            summaries.append({'id': scenario['id'], 'expected': labels[scenario['kind']],
                              'actual': measured['classification'], 'pass': measured['classification'] == labels[scenario['kind']],
                              'fixture_status': 'pass', 'package': row['package'],
                              'measurement': str(Path(row['observations']).parent),
                              'runtime_receipt': str(Path(row['raw_run']) / 'runtime-receipt.json'),
                              'fixture_validity_path': str(Path(row['raw_run']) / 'private-fixture-validity.json')})
        assert all(item['pass'] for item in summaries), summaries
        self.result_path = self.native / 'result.json'
        self.rows_path = self.native / 'candidate.json'
        write_json(self.native / 'qualification-start.json', {'protocol': self.policy,
            'original_sources': self.sources, 'cases': boundary.cases()})
        write_json(self.result_path, {'protocol': self.policy, 'status': 'pass', 'candidate_id': 'selected1',
            'original_sources': self.sources, 'project_sources': self.rendered_sources,
            'physics_collision_inferred': False, 'rows': summaries})
        write_json(self.rows_path, self.rows)
        self.receipt_path = root / 'boundary.json'
        write_json(self.receipt_path, {'kind': 'ReferenceSuiteBoundaryEvidence', 'candidate_id': 'selected1',
            'status': 'pass', 'required': 8, 'passed': 8, 'physics_collision_inferred': False,
            'source_sha256': digest(self.full), 'full_source_hashes': self.full, 'source_hashes': self.sources,
            'selected_source': str(self.selected), 'native_root': str(self.native),
            'evidence_path': str(self.result_path), 'records_path': str(self.rows_path),
            'evidence_sha256': file_sha256(self.result_path), 'file_hashes': self.inventory()})
        self.originals = {path: path.read_bytes() for path in root.rglob('*.json')}
        self.originals[self.selected / 'player.gd.uid'] = (self.selected / 'player.gd.uid').read_bytes()
        self.originals[overlay / 'boundary.gd'] = (overlay / 'boundary.gd').read_bytes()

    def inventory(self):
        return {path.relative_to(self.native).as_posix(): file_sha256(path)
                for path in sorted(self.native.rglob('*')) if path.is_file()}

    def reset(self):
        for path, content in self.originals.items():
            path.write_bytes(content)
        foreign = self.root / 'foreign'
        if foreign.exists():
            # Only this fixture-owned temporary directory is removed.
            shutil.rmtree(foreign)

    def seal(self):
        receipt = read_json(self.receipt_path)
        receipt['file_hashes'] = self.inventory()
        receipt['evidence_sha256'] = file_sha256(receipt['evidence_path'])
        write_json(self.receipt_path, receipt)

    @staticmethod
    def update(path, mutate):
        value = read_json(path)
        mutate(value)
        write_json(path, value)

    def worker_paths(self, row):
        common = Path(row['observations']).parent / 'common-analysis'
        job = common / 'jobs/jobs' / read_json(common / 'analysis-operation.json')['job_id']
        return common, job

    def process(self, raw):
        """Actual private-desktop receipt shape, populated without a process."""
        directory = raw / 'private-process'
        argv = [str(runtime.ROOT / runtime.ENGINE_RELATIVE), '--path', str(self.rendered),
                '--fixed-fps', '60', '--resolution', '320x180', '--rendering-method',
                'gl_compatibility', '--windowed', '--disable-vsync', boundary.SCENE]
        common = {'runner': runtime.private.VERSION, 'argv': argv, 'cwd': str(self.rendered),
                  'executable_sha256': runtime.ENGINE_SHA256, 'policy': deepcopy(runtime.PROCESS_POLICY),
                  'desktop_name': 'fixture', 'started_at': '2026-10-02T00:00:00+00:00',
                  'timeout_seconds': 120, 'environment_keys': ['KIROCLOPS_CAPTURE_CONFIG'],
                  'environment_sha256': digest('synthetic environment')}
        identity = {'pid': 1, 'primary_thread_id': 2, 'creation_filetime_100ns': 3}
        write_json(directory / 'process-start.json', {**common, 'status': 'starting'})
        write_json(directory / 'process-identity.json', {'runner': runtime.private.VERSION,
            'status': 'suspended_in_job', 'process_identity': identity, 'desktop_path': 'WinSta0\\fixture',
            'start_sha256': file_sha256(directory / 'process-start.json'),
            'recorded_at': '2026-10-02T00:00:01+00:00'})
        for name, value in (('stdout.log', runtime.ENGINE_VERSION), ('stderr.log', '')):
            (directory / name).write_text(value, encoding='utf-8')
        proof = {**common, 'status': 'completed', 'exit_code': 0,
            'owned_process_termination_confirmed': True, 'cleanup_errors': [],
            'process_identity': identity, 'desktop_path': 'WinSta0\\fixture',
            'resumed_at': '2026-10-02T00:00:02+00:00', 'finished_at': '2026-10-02T00:00:03+00:00',
            'desktop_samples': [{'name': 'fixture', 'receives_input': False, 'owned_window_count': 1}],
            'saw_owned_window': True, 'evidence': {name: {'sha256': file_sha256(directory / name),
                'bytes': (directory / name).stat().st_size} for name in
                ('process-start.json', 'process-identity.json', 'stdout.log', 'stderr.log')}}
        write_json(directory / 'process.json', proof)
        return argv

    def worker(self, row, *, candidate=None, sources=None, engine=None):
        observed, selection = read_json(row['observations']), read_json(row['selection'])
        manifest = load_package(row['package'], verify='hashes')
        contract = {'require_static_camera': True, 'min_valid_ratio': .95}
        request = request_binding(manifest['package_sha256'], selection, contract,
                                  digest(self.sources) if candidate is None else candidate)
        common = Path(row['observations']).parent / 'common-analysis'
        number = boundary.cases().index(row['scenario']) + 1
        job_id = f'{number:032x}'
        delivery = envelope(make_measurement(observed, extract_events(observed, selection, contract)), request,
            logical_job_id=job_id, engine=engine or self.engine, sources=sources or self.worker_sources,
            timings={'execution_wall_s': 0.})
        result = legacy_result(delivery, request, logical_job_id=job_id)
        write_json(common / 'analysis.json', result)
        write_json(common / 'analysis-operation.json', {'schema_version': 1, 'logical_job_id': 'operation',
            'job_id': job_id, 'status': 'succeeded', 'request_sha256': digest(request)})
        job = common / 'jobs/jobs' / job_id
        write_json(job / 'request.json', request)
        write_json(job / 'delivery-v2.json', delivery)
        # Match LocalAnalysisBackend's actual top-level state schema.
        write_json(job / 'state.json', {'schema_version': 1, 'kind': 'AnalysisJob', 'job_id': job_id,
            'status': 'succeeded', 'request_sha256': digest(request), 'package_sha256': manifest['package_sha256'],
            'candidate_sha256': request['candidate_sha256'], 'result_sha256': digest(result),
            'delivery_sha256': digest(delivery), 'delivery_schema_version': 2})

    def case(self, scenario, number):
        raw = self.native / 'raw' / scenario['id']
        raw.mkdir(parents=True)
        frames, observed_frames, native_observations, active = [], [], [], set()
        frame_dir = raw / 'frames'
        frame_dir.mkdir()
        wall = 40 if scenario['wall_side'] == 'left' else 280
        pixels = {}
        for index in range(240):
            for event in scenario['events']:
                if event['tick'] == index:
                    (active.add if event['pressed'] else active.remove)(event['action'])
            x, y = 80, 100
            if scenario['kind'] in ('approach', 'rebound'):
                x -= 2 * min(15, max(0, index - 30))
                if scenario['kind'] == 'rebound':
                    x += 2 * min(15, max(0, index - 57))
            elif scenario['kind'] == 'parallel':
                x = 50
                y -= 2 * min(30, max(0, index - 30))
            else:
                x = 100
                y -= min(30, max(0, index - 30))
            if scenario['wall_side'] == 'right':
                x = 320 - x
            if (x, y) not in pixels:
                image = np.zeros((180, 320, 3), np.uint8)
                image[:, wall:wall+1] = 128
                image[y-4:y+4, x-4:x+4] = 255
                pixels[x, y] = cv2.imencode('.png', image)[1].tobytes()
            (frame_dir / f'{index:06d}.png').write_bytes(pixels[x, y])
            frames.append({'frame_index': index, 'physics_tick': 60 + index, 'time_s': index / 60, 'actions': sorted(active)})
            observed_frames.append({'index': index, 'time_s': index / 60, 'valid': True,
                                    'x': x, 'y': y, 'width': 8., 'height': 8., 'score': 1., 'margin': .5})
            native_observations.append({'frame_index': index, 'physics_tick': 60 + index,
                'on_floor': index < 30, 'on_wall': 30 <= index <= 60, 'moving_up': 30 <= index < 60,
                'moving_horizontal': 30 <= index < 60,
                'jump_edge': any(e['tick'] == index and e['action'] == 'jump' and e['pressed'] for e in scenario['events']),
                'hazard_entries': 0, 'ground_incline': False, 'ground_decline': False,
                'ground_rise': False, 'ground_descent': False})
        write_json(raw / 'recording.json', {'status': 'ok', 'fps': 60, 'viewport': [320, 180],
            'frame_count': 240, 'settled': True, 'normal_level': False, 'active_warmup_ticks': 60,
            'frames': frames, 'observations': native_observations})
        validity = {**boundary._fixture_value(read_json(raw / 'recording.json'), scenario),
                    'recording_sha256': file_sha256(raw / 'recording.json')}
        assert validity['status'] == 'pass', validity
        write_json(raw / 'private-fixture-validity.json', validity)
        argv = self.process(raw)
        write_json(raw / 'capture_config.json', {**scenario, 'normal_level': False, 'output_dir': str(raw),
                                               'viewport': [320, 180], 'fps': 60, 'game': 'heart', 'variant': 'before'})
        hashes = {f'{index:06d}.png': file_sha256(frame_dir / f'{index:06d}.png') for index in range(240)}
        manifest = {'schema_version': 1, 'status': 'ok', 'game': 'heart', 'variant': 'before', 'fps': 60,
            'viewport': [320, 180], 'frame_count': 240, 'warmup_ticks': 60, 'frames': frames, 'scenario': scenario,
            'raw_frame_hashes': hashes, 'provenance': {'protocol': self.policy, 'source_hashes': self.rendered_sources,
                'process_sha256': file_sha256(raw / 'private-process/process.json'),
                'config_sha256': file_sha256(raw / 'capture_config.json')}}
        write_json(raw / 'manifest.json', manifest)
        write_json(raw / 'runtime-receipt.json', {'status': 'pass', 'protocol': self.policy,
            'project': str(self.rendered), 'argv': argv, 'process': str(raw / 'private-process/process.json'),
            'non_input_desktop': True, 'owned_cleanup_verified': True, 'source_hashes': self.rendered_sources,
            'manifest_sha256': file_sha256(raw / 'manifest.json'), 'raw_frame_hashes': hashes,
            'process_sha256': file_sha256(raw / 'private-process/process.json'),
            'fixture_validity_sha256': file_sha256(raw / 'private-fixture-validity.json')})
        package = self.native / 'packages' / scenario['id']
        package_manifest = import_legacy_run(raw, package)
        measurement = self.native / 'measurement' / scenario['id']
        selection = {'objects': [{'id': 'character', 'roi': [observed_frames[0]['x']-4, 96, 8, 8]}],
                     'boundaries': [{'id': 'wall', 'points': [[wall, 0], [wall, 180]]}]}
        observed = {'schema_version': 1, 'kind': 'ObservationSet', 'status': 'ok', 'package_sha256': package_manifest['package_sha256'],
            'image_size': [320, 180], 'inputs': package_manifest['inputs'], 'selection': deepcopy(selection),
            'objects': [{'id': 'character', 'frames': observed_frames}], 'camera': [], 'errors': []}
        events = extract_events(observed, selection, {'event_options': {'boundary_tolerance_px': 12}})
        measured = boundary.classify(observed, events)
        for name, value in (('selection', selection), ('observations', observed), ('events', events), ('result', measured)):
            write_json(measurement / (name + '.json'), value)
        row = {'scenario': deepcopy(scenario), 'package': str(package), 'raw_run': str(raw),
            'selection': str(measurement / 'selection.json'), 'observations': str(measurement / 'observations.json'),
            'events': str(measurement / 'events.json'), 'analysis': str(measurement / 'result.json'),
            'measurement_status': 'ok', 'candidate_id': 'selected1', 'source_hashes': deepcopy(self.sources)}
        self.worker(row)
        return row, measured

    def rebind_manifest(self, row):
        manifest_path = Path(row['package']) / 'manifest.json'
        manifest = read_json(manifest_path)
        manifest['metadata']['source_manifest_sha256'] = file_sha256(Path(row['raw_run']) / 'manifest.json')
        manifest['package_sha256'] = digest({key: value for key, value in manifest.items() if key != 'package_sha256'})
        write_json(manifest_path, manifest)
        self.update(row['observations'], lambda value: value.update(package_sha256=manifest['package_sha256']))
        write_json(row['events'], extract_events(read_json(row['observations']), read_json(row['selection']),
                                              {'event_options': {'boundary_tolerance_px': 12}}))
        self.worker(row)


class BoundaryBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.fixture = BoundaryFixture(Path(cls.temporary.name))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.fixture.reset()
        self.patches = [patch.object(boundary, 'policy', return_value=self.fixture.policy),
                        patch('kiroclops.cloud.identity.service_sources', return_value=self.fixture.worker_sources),
                        patch('kiroclops.cloud.identity.engine_identity', return_value=self.fixture.engine),
                        patch('kiroclops.local_engine.suite_boundary.capture', side_effect=AssertionError('No captures permitted'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def verify(self):
        return suite_boundary.bindings(self.fixture.receipt_path,
            candidate_sha256=digest(self.fixture.full), candidate_id='selected1')

    def rejects(self, *, reseal=True):
        if reseal:
            self.fixture.seal()
        with self.assertRaises((VisualError, OSError, KeyError)):
            self.verify()

    def test_real_schemas_bind_eight_sources_with_candidate_in_result_not_observations(self):
        values = self.verify()
        self.assertEqual(8, len(values))
        self.assertNotEqual(digest(self.fixture.full), digest(self.fixture.sources))
        for row in self.fixture.rows:
            observed = read_json(row['observations'])
            common, _ = self.fixture.worker_paths(row)
            self.assertNotIn('candidate_sha256', observed)
            self.assertEqual(digest(self.fixture.sources), read_json(common / 'analysis.json')['candidate_sha256'])

    def test_worker_failed_status_rejects_even_with_matching_output_hashes(self):
        _, job = self.fixture.worker_paths(self.fixture.rows[0])
        self.fixture.update(job / 'state.json', lambda value: value.update(status='failed'))
        self.rejects()

    def test_saved_worker_request_must_equal_actual_reconstructed_binding(self):
        _, job = self.fixture.worker_paths(self.fixture.rows[0])
        self.fixture.update(job / 'request.json', lambda value: value.update(candidate_sha256=digest('wrong')))
        self.rejects()

    def test_worker_state_package_candidate_and_request_are_required(self):
        _, job = self.fixture.worker_paths(self.fixture.rows[0])
        for field in ('package_sha256', 'candidate_sha256', 'request_sha256'):
            with self.subTest(field=field):
                self.fixture.reset()
                self.fixture.update(job / 'state.json', lambda value: value.update({field: digest('wrong')}))
                self.rejects()

    def test_worker_state_job_identity_must_match_operation(self):
        _, job = self.fixture.worker_paths(self.fixture.rows[0])
        self.fixture.update(job / 'state.json', lambda value: value.update(job_id='f' * 32))
        self.rejects()

    def test_operation_request_and_status_cannot_describe_another_request(self):
        common, _ = self.fixture.worker_paths(self.fixture.rows[0])
        for field, value in (('request_sha256', digest('wrong')), ('status', 'queued')):
            with self.subTest(field=field):
                self.fixture.reset()
                self.fixture.update(common / 'analysis-operation.json', lambda data: data.update({field: value}))
                self.rejects()

    def test_delivery_receipt_tampering_rejects_after_outer_inventory_is_resealed(self):
        _, job = self.fixture.worker_paths(self.fixture.rows[0])
        self.fixture.update(job / 'delivery-v2.json', lambda value: value['receipt'].update(candidate_sha256=digest('wrong')))
        self.rejects()

    def test_internally_consistent_stale_code_delivery_rejects(self):
        self.fixture.worker(self.fixture.rows[0], sources={'old-worker.py': digest('old')})
        self.rejects()

    def test_internally_consistent_stale_engine_delivery_rejects(self):
        identity = {'opencv_version': 'older', 'threads': 1}
        self.fixture.worker(self.fixture.rows[0], engine={'identity': identity, 'engine_sha256': digest(identity)})
        self.rejects()

    def test_candidate_identity_requires_native_projection_not_full_sidecar_digest(self):
        self.fixture.worker(self.fixture.rows[0], candidate=digest(self.fixture.full))
        self.rejects()

    def test_stored_boundary_events_are_recomputed_with_actual_policy(self):
        self.fixture.update(self.fixture.rows[0]['events'], lambda value: value.update(relations=[]))
        self.rejects()

    def test_selected_sidecar_change_rejects_even_when_native_projection_is_identical(self):
        (self.fixture.selected / 'player.gd.uid').write_text('uid://different\n', encoding='utf-8')
        self.assertEqual(self.fixture.sources, runtime.source_hashes(self.fixture.selected))
        self.rejects()

    def test_wrong_native_producer_receipt_source_rejects(self):
        raw = Path(self.fixture.rows[0]['raw_run'])
        self.fixture.update(raw / 'runtime-receipt.json', lambda value: value.update(source_hashes={'foreign.gd': digest('foreign')}))
        self.rejects()

    def test_duplicate_registered_case_rejects(self):
        rows = read_json(self.fixture.rows_path)
        rows[1]['scenario'] = deepcopy(rows[0]['scenario'])
        write_json(self.fixture.rows_path, rows)
        self.rejects()

    def test_distinct_declared_case_cannot_alias_another_cases_pixels_and_worker(self):
        rows = read_json(self.fixture.rows_path)
        for key in ('package', 'raw_run', 'observations', 'selection', 'events', 'analysis'):
            rows[1][key] = rows[0][key]
        write_json(self.fixture.rows_path, rows)
        self.rejects()

    def test_top_level_evidence_path_cannot_escape_owned_inventory(self):
        foreign = self.fixture.root / 'foreign'
        foreign.mkdir()
        shutil.copyfile(self.fixture.result_path, foreign / 'result.json')
        self.fixture.update(self.fixture.receipt_path, lambda value: value.update(evidence_path=str(foreign / 'result.json')))
        self.rejects()

    def test_measurement_paths_cannot_escape_owned_inventory(self):
        rows = read_json(self.fixture.rows_path)
        original = Path(rows[0]['observations']).parent
        foreign = self.fixture.root / 'foreign'
        shutil.copytree(original, foreign)
        for key in ('observations', 'selection', 'events', 'analysis'):
            rows[0][key] = str(foreign / Path(rows[0][key]).name)
        write_json(self.fixture.rows_path, rows)
        self.rejects()

    def test_rendered_fixture_source_must_equal_selected_native_plus_exact_fixed_overlay(self):
        altered = self.fixture.rendered / boundary.OVERLAY / 'boundary.gd'
        altered.write_text('extends Node2D\n# alternate fixture\n', encoding='utf-8')
        wrong = runtime.source_hashes(self.fixture.rendered)
        self.fixture.update(self.fixture.result_path, lambda value: value.update(project_sources=wrong))
        for row in self.fixture.rows:
            raw = Path(row['raw_run'])
            self.fixture.update(raw / 'manifest.json', lambda value: value['provenance'].update(source_hashes=wrong))
            self.fixture.update(raw / 'runtime-receipt.json', lambda value: value.update(
                source_hashes=wrong, manifest_sha256=file_sha256(raw / 'manifest.json')))
            self.fixture.rebind_manifest(row)
        self.rejects()

    def test_saved_classification_cannot_be_rewritten_independently_of_observations(self):
        self.fixture.update(self.fixture.rows[0]['analysis'], lambda value: value.update(classification='distant_motion'))
        self.rejects()

    def test_result_case_pass_and_aggregate_cannot_be_rewritten(self):
        self.fixture.update(self.fixture.result_path, lambda value: value['rows'][0].update(actual='distant_motion', **{'pass': False}))
        self.rejects()

    def test_coherently_resealed_process_receipt_with_wrong_command_rejects(self):
        row = self.fixture.rows[0]
        raw = Path(row['raw_run'])
        self.fixture.update(raw / 'private-process/process.json', lambda value: value.update(argv=['different-engine']))
        self.fixture.update(raw / 'manifest.json', lambda value: value['provenance'].update(
            process_sha256=file_sha256(raw / 'private-process/process.json')))
        self.fixture.update(raw / 'runtime-receipt.json', lambda value: value.update(
            process_sha256=file_sha256(raw / 'private-process/process.json'),
            manifest_sha256=file_sha256(raw / 'manifest.json')))
        self.fixture.rebind_manifest(row)
        self.rejects()

    def test_coherently_resealed_capture_config_with_wrong_recipe_rejects(self):
        row = self.fixture.rows[0]
        raw = Path(row['raw_run'])
        self.fixture.update(raw / 'capture_config.json', lambda value: value.update(events=[]))
        self.fixture.update(raw / 'manifest.json', lambda value: value['provenance'].update(
            config_sha256=file_sha256(raw / 'capture_config.json')))
        self.fixture.update(raw / 'runtime-receipt.json', lambda value: value.update(
            manifest_sha256=file_sha256(raw / 'manifest.json')))
        self.fixture.rebind_manifest(row)
        self.rejects()


if __name__ == '__main__':
    unittest.main()
