"""Prepared target pools using genuine replayable native receipt schemas.

Only the native operation producers are replaced by deterministic file-writing
fixtures. The actual process, import, source, frame and input validators run.
No Godot, private desktop, tracking worker or model process starts.
"""
from copy import deepcopy
from pathlib import Path
import ast
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import suite_prepared_target as pools
from kiroclops.local_engine import suite_game, media_suite
from kiroclops.local_engine.session import source_hashes
from kiroclops.portable_v2 import runtime
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from tests.evidence import requires_evidence


class NativeFixture:
    def __init__(self, root):
        self.root, self.selected = root, root / 'selected'
        self.selected.mkdir()
        for name in runtime._REQUIRED:
            (self.selected / name).write_text('fixture\n', encoding='utf-8')
        (self.selected / 'project.godot').write_text('[application]\nconfig/use_custom_user_dir=true\n' + runtime.CUSTOM_DIR + '\n', encoding='utf-8')
        (self.selected / 'player.gd.uid').write_text('uid://selected\n', encoding='utf-8')
        adapter = self.selected / 'kiroclops_heart'
        adapter.mkdir()
        for name in runtime._ADAPTER_FILES:
            (adapter / name).write_bytes((runtime.ROOT / 'adapters/heart-godot4' / name).read_bytes())
        self.full = source_hashes(self.selected)
        self.native = runtime.source_hashes(self.selected)
        self.job = '1' * 32
        self.imports, self.captures, self.processes = 0, [], 0
        self.png = cv2.imencode('.png', np.zeros((180, 320, 3), np.uint8))[1].tobytes()

    def process_receipt(self, project, output, scene, config):
        """Produce the existing actual bridge/private receipt schema, without OS execution."""
        self.processes += 1
        directory = output / ('engine.' + runtime.VERSION)
        process = directory / 'process'
        argv = runtime._argv(project, scene)
        env = {'SystemRoot': os.environ.get('SystemRoot', 'C:\\Windows'),
               'LOCALAPPDATA': str(directory / 'environment/localappdata'),
               'TMP': str(directory / 'environment/tmp')}
        environment_sha = hashlib.sha256(runtime.private.environment_block(env).encode('utf-16-le')).hexdigest()
        identity = {'pid': self.processes, 'primary_thread_id': self.processes + 100,
                    'creation_filetime_100ns': self.processes + 1000}
        common = {'runner': runtime.private.VERSION, 'argv': argv, 'cwd': str(project),
            'executable_sha256': runtime.ENGINE_SHA256, 'policy': deepcopy(runtime.PROCESS_POLICY),
            'desktop_name': f'fixture-{self.processes}', 'started_at': '2026-10-02T00:00:00+00:00',
            'timeout_seconds': 120, 'environment_keys': sorted(env), 'environment_sha256': environment_sha}
        write_json(process / 'process-start.json', {**common, 'status': 'starting'})
        desktop_path = 'WinSta0\\' + common['desktop_name']
        write_json(process / 'process-identity.json', {'runner': runtime.private.VERSION,
            'status': 'suspended_in_job', 'process_identity': identity, 'desktop_path': desktop_path,
            'start_sha256': file_sha256(process / 'process-start.json'),
            'recorded_at': '2026-10-02T00:00:01+00:00'})
        (process / 'stdout.log').write_text(runtime.ENGINE_VERSION, encoding='utf-8')
        (process / 'stderr.log').write_text('', encoding='utf-8')
        write_json(process / 'process.json', {**common, 'status': 'completed', 'exit_code': 0,
            'owned_process_termination_confirmed': True, 'cleanup_errors': [], 'process_identity': identity,
            'desktop_path': desktop_path, 'resumed_at': '2026-10-02T00:00:02+00:00',
            'finished_at': '2026-10-02T00:00:03+00:00', 'saw_owned_window': scene is not None,
            'desktop_samples': [{'name': common['desktop_name'], 'receives_input': False,
                                 'owned_window_count': 1 if scene else 0}],
            'evidence': {name: {'sha256': file_sha256(process / name), 'bytes': (process / name).stat().st_size}
                for name in ('process-start.json', 'process-identity.json', 'stdout.log', 'stderr.log')}})
        sources = runtime.source_hashes(project)
        receipt = {'schema_version': 1, 'runtime': runtime.VERSION, 'status': 'starting',
            'started_at': common['started_at'], 'project': str(project), 'scene': scene,
            'kind': 'capture' if scene else 'import', 'argv': argv,
            'config': {'path': str(config), 'sha256': file_sha256(config)} if config else None,
            'protocol': runtime.protocol_record(), 'source_hashes_before': sources,
            'environment_sha256': environment_sha, 'receipt_path': str(directory / 'bridge.json')}
        write_json(directory / 'bridge-start.json', receipt)
        log = b'\n'.join((process / name).read_bytes() for name in ('stdout.log', 'stderr.log'))
        (output / 'engine.log').write_bytes(log)
        receipt.update(status='completed', finished_at='2026-10-02T00:00:03+00:00', source_hashes_after=sources,
            log_sha256=file_sha256(output / 'engine.log'), evidence_hashes={
                p.relative_to(directory).as_posix(): file_sha256(p)
                for p in [directory / 'bridge-start.json', *sorted(process.glob('*'))] if p.is_file()})
        write_json(directory / 'bridge.json', receipt)
        return receipt

    def imported(self, project, output, **kwargs):
        self.imports += 1
        cache = project / '.godot/imported'
        cache.mkdir(parents=True)
        (cache / 'asset.ctex').write_bytes(b'pinned imported product')
        (project / '.godot/global_script_class_cache.cfg').write_text('fixture', encoding='utf-8')
        receipt = self.process_receipt(project, output, None, None)
        summary = {**receipt, 'import_attempts': [{'path': receipt['receipt_path'],
            'sha256': file_sha256(receipt['receipt_path']), 'status': 'completed'}],
            'import_summary_path': str(output / 'import-result.json')}
        write_json(output / 'import-result.json', summary)
        return summary

    def captured(self, project, raw, scenario, **kwargs):
        self.captures.append((project, raw, deepcopy(scenario)))
        raw.mkdir()
        config = dict(scenario, output_dir=str(raw), game='heart', normal_level=False,
                      protocol=runtime.VERSION, viewport=[320, 180], fps=60)
        write_json(raw / 'capture_config.json', config)
        frames, native, active = [], [], set()
        directory = raw / 'frames'
        directory.mkdir()
        for index in range(240):
            for event in scenario['events']:
                if event['tick'] == index:
                    (active.add if event['pressed'] else active.remove)(event['action'])
            frames.append({'frame_index': index, 'physics_tick': 60 + index,
                           'time_s': index / 60, 'actions': sorted(active)})
            native.append({'frame_index': index, 'physics_tick': 60 + index, 'on_floor': index < 30,
                'on_wall': False, 'moving_up': index >= 30, 'moving_horizontal': False,
                'jump_edge': index == 30, 'hazard_entries': 0, 'ground_incline': False,
                'ground_decline': False, 'ground_rise': False, 'ground_descent': False})
            (directory / f'{index:06d}.png').write_bytes(self.png)
        recording = {'status': 'ok', 'fps': 60, 'viewport': [320, 180], 'frame_count': 240,
            'settled': True, 'normal_level': False, 'active_warmup_ticks': 60,
            'frames': frames, 'observations': native}
        write_json(raw / 'recording.json', recording)
        receipt = self.process_receipt(project, raw, runtime.CAPTURE_SCENE, raw / 'capture_config.json')
        sources, hashes = runtime.source_hashes(project), runtime._frames(raw)
        manifest = {'schema_version': 1, 'run_id': raw.name, 'status': 'ok', 'game': 'heart',
            'variant': scenario['variant'], 'fps': 60, 'viewport': [320, 180], 'frame_count': 240,
            'warmup_ticks': 60, 'scenario': scenario, 'normal_level': False, 'settled': True,
            'frames': frames, 'raw_frame_hashes': hashes, 'raw_frames_sha256': digest(hashes),
            'provenance': {'project': str(project), 'engine': runtime.ENGINE_VERSION,
                'engine_sha256': runtime.ENGINE_SHA256, 'full_source_hashes_before': sources,
                'full_source_hashes_after': sources, 'source_digest_before': digest(sources),
                'source_digest_after': digest(sources), 'runtime_receipt': receipt['receipt_path'],
                'runtime_receipt_sha256': file_sha256(receipt['receipt_path']),
                'recording_sha256': file_sha256(raw / 'recording.json'),
                'capture_config_sha256': file_sha256(raw / 'capture_config.json'),
                'protocol': runtime.protocol_record(), 'coordinates_from_engine': False}}
        write_json(raw / 'manifest.json', manifest)
        return manifest


class PreparedTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = NativeFixture(Path(self.temporary.name))
        self.patches = [patch.object(runtime, '_engine', return_value=runtime.ROOT / runtime.ENGINE_RELATIVE),
            patch.object(runtime, 'import_project', side_effect=self.fixture.imported),
            patch.object(runtime, 'capture_project', side_effect=self.fixture.captured),
            patch.object(runtime.private, 'run_private_desktop', side_effect=AssertionError('No native execution permitted'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.prepared = pools.prepare_target(self.fixture.selected, self.fixture.root / 'pool',
            job_id=self.fixture.job, role='candidate', expected_full_sources=self.fixture.full)

    def verify(self, **kwargs):
        return pools.verify_prepared(self.prepared, self.fixture.selected,
            job_id=kwargs.get('job_id', self.fixture.job), role=kwargs.get('role', 'candidate'),
            expected_full_sources=kwargs.get('expected_full_sources', self.fixture.full))

    def scenario(self, *, tick=30, variant='after'):
        return {'id': 'fresh-jump', 'events': [{'tick': tick, 'action': 'jump', 'pressed': True},
            {'tick': tick + 12, 'action': 'jump', 'pressed': False}], 'split': 'validation',
            'repeat': 1, 'spawn_offset': 0, 'variant': variant, 'warmup_ticks': 60, 'frame_count': 240}

    def capture(self, trial='0' * 12, **kwargs):
        return pools.capture_case(self.prepared, self.fixture.selected, kwargs.get('scenario', self.scenario()),
            job_id=kwargs.get('job_id', self.fixture.job), role=kwargs.get('role', 'candidate'), trial_id=trial,
            expected_full_sources=self.fixture.full)

    def reseal_pool(self, mutate):
        path = Path(self.prepared['receipt_path'])
        receipt = read_json(path)
        mutate(receipt)
        write_json(path, receipt)
        self.prepared['receipt_sha256'] = file_sha256(path)

    def test_one_source_import_and_distinct_fresh_native_trials(self):
        first = self.capture('0' * 12)
        second = self.capture('2' * 12, scenario=self.scenario(tick=35))
        self.assertEqual(1, self.fixture.imports)
        self.assertEqual(2, len(self.fixture.captures))
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.parent / 'project', self.fixture.captures[0][0])
        self.assertEqual(self.fixture.captures[0][0], self.fixture.captures[1][0])
        self.assertTrue((first.parent / 'import/import-result.json').is_file())
        proofs = [read_json(raw / ('engine.' + runtime.VERSION) / 'process/process.json') for raw in (first, second)]
        self.assertNotEqual(proofs[0]['process_identity'], proofs[1]['process_identity'])
        self.assertNotEqual(proofs[0]['environment_sha256'], proofs[1]['environment_sha256'])
        self.assertNotEqual(first / 'capture_config.json', second / 'capture_config.json')
        self.assertEqual([60, 240], [self.fixture.captures[0][2]['warmup_ticks'], self.fixture.captures[0][2]['frame_count']])

    def test_pool_has_source_and_import_receipts_without_case_results(self):
        receipt = self.verify()
        self.assertEqual(self.fixture.full, receipt['full_source_hashes'])
        self.assertEqual(self.fixture.native, receipt['native_source_hashes'])
        self.assertNotEqual(receipt['source_sha256'], receipt['native_source_sha256'])
        self.assertNotIn('scenario', receipt)
        self.assertNotIn('observations', receipt)
        self.assertNotIn('comparison', receipt)
        self.assertEqual(0, len(self.fixture.captures))

    def test_wrong_job_or_role_rejects_before_capture(self):
        for kwargs in ({'job_id': '2' * 32}, {'role': 'before'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(VisualError):
                self.capture(**kwargs)
        self.assertEqual([], self.fixture.captures)

    def test_wrong_trusted_full_source_rejects(self):
        with self.assertRaises(VisualError):
            self.verify(expected_full_sources={'foreign.gd': digest('foreign')})

    def test_selected_uid_change_rejects_when_native_map_stays_equal(self):
        (self.fixture.selected / 'player.gd.uid').write_text('uid://changed', encoding='utf-8')
        self.assertEqual(self.fixture.native, runtime.source_hashes(self.fixture.selected))
        with self.assertRaises(VisualError):
            self.capture()
        self.assertEqual([], self.fixture.captures)

    def test_rendered_uid_or_gameplay_source_changes_reject(self):
        for name in ('player.gd.uid', 'player.gd'):
            with self.subTest(name=name):
                path = self.fixture.root / 'pool/project' / name
                before = path.read_bytes()
                path.write_bytes(b'changed')
                with self.assertRaises(VisualError):
                    self.verify()
                path.write_bytes(before)

    def test_changed_import_product_rejects(self):
        (self.fixture.root / 'pool/project/.godot/imported/asset.ctex').write_bytes(b'changed')
        with self.assertRaises(VisualError):
            self.verify()

    def test_documented_rendering_and_editor_outputs_can_be_created(self):
        for name in ('shader_cache', 'editor'):
            path = self.fixture.root / 'pool/project/.godot' / name
            path.mkdir()
            (path / 'generated').write_bytes(b'generated rendering/editor output')
        self.assertEqual('ready', self.verify()['status'])

    def test_stale_capture_code_rejects(self):
        with patch.object(pools, 'source_closure', return_value={'changed.py': digest('changed')}):
            with self.assertRaises(VisualError):
                self.capture()
        self.assertEqual([], self.fixture.captures)

    def test_import_evidence_change_rejects(self):
        (self.fixture.root / 'pool/import/engine.log').write_text('changed', encoding='utf-8')
        with self.assertRaises(VisualError):
            self.verify()

    def test_coherently_resealed_failed_import_receipt_rejects(self):
        def failed(receipt):
            receipt['import_summary']['status'] = 'failed'
        self.reseal_pool(failed)
        with self.assertRaises(VisualError):
            self.verify()

    def test_existing_trial_cannot_be_replayed_as_a_new_capture(self):
        self.capture()
        with self.assertRaises(VisualError) as caught:
            self.capture()
        self.assertEqual('output_exists', caught.exception.code)
        self.assertEqual(1, len(self.fixture.captures))

    def test_native_role_and_clock_cannot_change(self):
        with self.assertRaises(VisualError):
            self.capture(scenario=self.scenario(variant='before'))
        self.assertEqual([], self.fixture.captures)

    def test_record_binding_replays_trial_and_rejects_wrong_job(self):
        raw = self.capture()
        record = {'raw_run': str(raw), 'source_sha256': digest(self.fixture.full), 'analysis': 'unchanged'}
        before = deepcopy(record)
        bound = pools.attach_record(record, self.prepared, self.fixture.selected,
            job_id=self.fixture.job, role='candidate', expected_full_sources=self.fixture.full)
        self.assertEqual(before, record)
        self.assertEqual('pass', pools.verify_record(bound, expected_job_id=self.fixture.job,
                                                   expected_full_sources=self.fixture.full)['status'])
        with self.assertRaises(VisualError):
            pools.verify_record(bound, expected_job_id='2' * 32, expected_full_sources=self.fixture.full)

    def test_changed_trial_png_rejects_without_native_execution(self):
        raw = self.capture()
        (raw / 'frames/000000.png').write_bytes(b'changed')
        with self.assertRaises((VisualError, runtime.PortableRuntimeError)):
            pools.verify_case(raw, self.prepared, self.fixture.selected, job_id=self.fixture.job,
                              role='candidate', expected_full_sources=self.fixture.full)

    @requires_evidence('reviews/suite_prepared_target_glue.py.txt')
    def test_unapplied_glue_reuses_only_import_and_runs_two_fresh_analyses(self):
        proposal = Path(__file__).resolve().parents[1] / 'reviews/suite_prepared_target_glue.py.txt'
        tree = ast.parse(proposal.read_text(encoding='utf-8'))
        def proposed(name, namespace):
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
            values = dict(namespace)
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(proposal), 'exec'), values)
            return values[name]
        capture_target = proposed('capture_target', suite_game.__dict__)
        capture = proposed('_capture', media_suite.__dict__)
        scenario = suite_game.scenarios('jump', 'calibration')[0]
        case = {'stage': 'jump', 'scenario': scenario}
        shared, analyzed = {}, []
        def analyze(raw, stage, selected_scenario, output, **kwargs):
            self.assertFalse(output.exists())
            output.mkdir(parents=True)
            analyzed.append((raw, output, kwargs['candidate_sha256']))
            payload = {'candidate_sha256': kwargs['candidate_sha256'], 'scenario': selected_scenario}
            write_json(output / 'analysis.json', payload)
            return {'raw_run': str(raw), 'scenario': selected_scenario, 'analysis': str(output / 'analysis.json')}
        capture_target.__globals__['analyze_record'] = analyze
        roots = iter([self.fixture.root / 'job-pool'])
        def short_runtime(*args, **kwargs):
            directory = next(roots)
            directory.mkdir()
            return directory
        with patch.object(suite_game, 'capture_target', capture_target), \
             patch.object(suite_game, '_short_runtime', side_effect=short_runtime), \
             patch.object(suite_game, 'analyze_record', side_effect=analyze):
            records = [capture(self.fixture.selected, case, self.fixture.root / 'measure/candidate' / str(index),
                lambda: None, lambda *args: None, None, 'candidate', {},
                prepared_targets=shared, job_id=self.fixture.job) for index in range(2)]
        self.assertEqual(1, len(shared))
        self.assertEqual(2, self.fixture.imports)  # one setup fixture pool plus one job-local pool
        self.assertEqual(2, len(analyzed))
        self.assertNotEqual(analyzed[0][0], analyzed[1][0])
        self.assertNotEqual(analyzed[0][1], analyzed[1][1])
        self.assertEqual([digest(self.fixture.full)] * 2, [value[2] for value in analyzed])
        self.assertEqual([digest(self.fixture.full)] * 2, [r['source_sha256'] for r in records])
        self.assertTrue(all(r['prepared_target']['job_id'] == self.fixture.job for r in records))


if __name__ == '__main__':
    unittest.main()
