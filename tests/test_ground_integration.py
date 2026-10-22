"""Ground experiment isolation/dispatch; no models, Godot or real captures."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import experiments as ex, ground_worker as gw
from kiroclops.integration import session, storage, runner, worker, ground_runner, evaluation
from kiroclops.ground import comparison


class GroundSpecTests(unittest.TestCase):
    def test_immutable_spec_exact_matrix_and_release_before_reversal(self):
        spec = ex.get_experiment(ex.GROUND_ID)
        self.assertEqual((len(spec.scenarios('calibration')), len(spec.scenarios('validation'))), (6, 18))
        with self.assertRaises(FrozenInstanceError):
            spec.max_seconds = 1
        rows = spec.scenarios('validation')
        reversal = next(s for s in rows if s['id'] == 'reverse24')
        self.assertEqual(reversal['events'][1:3], [{'tick': 54, 'action': 'right', 'pressed': False},
                                                  {'tick': 54, 'action': 'left', 'pressed': True}])
        rows[0]['events'].clear()
        self.assertTrue(spec.scenarios('validation')[0]['events'])
        record = spec.record()
        self.assertEqual(record['mechanic'], 'ground_horizontal')
        self.assertEqual(record['reference_package'], 'artifacts/phase2b/reference')
        self.assertEqual(record['acceptance']['ground']['minimum_valid_ratio'], .95)
        self.assertEqual(record['acceptance']['ground']['minimum_improvement_applies_to'], 'validation only')
        self.assertEqual(record['acceptance']['jump_regression']['improvement_denominator'], 'artifacts/runs/slay-before-*')
        self.assertEqual(len(record['definition_sha256']), 64)
        record['acceptance']['ground']['minimum_valid_ratio'] = 0
        self.assertEqual(spec.record()['acceptance']['ground']['minimum_valid_ratio'], .95)
        self.assertNotIn('acceptance', ex.get_experiment().record())
        self.assertEqual(ex.get_experiment().experiment_id, 'legacy-jump-v1')
        with self.assertRaises(storage.IntegrationError):
            ex.get_experiment('jump-v1')
        with patch.object(gw, 'provision_session') as provision:
            with self.assertRaises(storage.IntegrationError):
                session.prepare_session('bad', mode='baseline', experiment_id=ex.GROUND_ID)
            provision.assert_not_called()


class GroundSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'café ground'
        self.root.mkdir()
        for module in (ex, gw, session, storage, runner):
            mocked = patch.object(module, 'ROOT', self.root)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.parent = self.root / 'sessions/ide-02/candidates/candidate-01/project'
        self.put(self.parent / 'project.godot', '[application]\n')
        self.put(self.parent / 'source/characters/player/player.gd', 'extends Node\n# learned jump\n')
        self.put(self.parent / 'kiroclops_capture/capture.gd', 'original jump fixture')
        self.put(self.parent / '.kiro/agents/old.json', 'old credentials/config not copied')
        self.put(self.parent / '.kiroclops-reference/profile.json', 'old profile not copied')
        self.parent_hashes = storage.project_hashes(self.parent)
        parent = {'project': self.parent.relative_to(self.root).as_posix(), 'source_hashes': self.parent_hashes}
        self.lock = {'tracking': {'parent': parent, 'phase1_frozen_files': {'legacy': 'locked'}},
                     'tracking_lock_sha256': 'tracking', 'reference_lock_sha256': 'reference'}
        storage.write_json(self.root / 'config/sources.json', {'slay': {'commit': 'approved'}})
        self.verify = patch.object(gw, 'verify_ground_lock', return_value=self.lock)
        self.verify.start()
        self.addCleanup(self.verify.stop)

    @staticmethod
    def put(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def provision(self):
        def prepare(project, game='slay'):
            self.put(Path(project) / 'kiroclops_ground/capture.gd', 'new ground fixture')
        with patch.object(ground_runner, 'prepare_project', side_effect=prepare), \
             patch.object(ground_runner, 'import_project') as imported, \
             patch.object(gw, '_export_raw_calibration'):
            directory = session.prepare_session('ground-test', configure=False, experiment_id=ex.GROUND_ID)
            imported.assert_called_once()
        return session.Session(directory)

    def ready(self):
        selected = self.provision()
        selected.update(lambda s: s.update(status='ready', reference={'calibration': ['ref-ground'],
                            'jump': {'calibration': ['ref-jump']}}, baseline_runs={'calibration': ['before-ground'],
                            'jump': {'calibration': ['before-jump']}}))
        return selected

    def test_fresh_parent_copy_keeps_learned_jump_and_omits_old_agent_reference_data(self):
        selected = self.provision()
        state = selected.read()
        self.assertEqual(state['schema_version'], 3)
        self.assertEqual(state['experiment_id'], ex.GROUND_ID)
        self.assertEqual(state['parent_source_hashes'], self.parent_hashes)
        self.assertNotEqual(state['baseline_hashes'], self.parent_hashes)
        for name in ('baseline', 'target'):
            project = selected.directory / name
            self.assertIn('learned jump', (project / 'source/characters/player/player.gd').read_text())
            self.assertTrue((project / 'kiroclops_capture/capture.gd').exists())
            self.assertTrue((project / 'kiroclops_ground/capture.gd').exists())
            self.assertFalse((project / '.kiro').exists())
            self.assertFalse((project / '.kiroclops-reference').exists())
        self.assertEqual(storage.project_hashes(self.parent), self.parent_hashes)
        self.assertEqual(selected.inspect()['experiment_id'], ex.GROUND_ID)

    def test_changed_parent_at_copy_or_session_baseline_is_rejected(self):
        self.put(self.parent / 'source/characters/player/player.gd', 'changed')
        with patch.object(ground_runner, 'prepare_project') as prepare:
            with self.assertRaises(storage.IntegrationError):
                gw.provision_session('parent-mismatch', configure=False)
            prepare.assert_not_called()
        self.put(self.parent / 'source/characters/player/player.gd', 'extends Node\n# learned jump\n')
        selected = self.provision()
        self.put(selected.directory / 'baseline/project.godot', 'tampered')
        with self.assertRaises(storage.IntegrationError):
            gw.verify_session(selected)

    def test_sealed_validation_and_changed_spec_fail_before_reference_access(self):
        selected = self.provision()
        with patch.object(gw, '_copy_raw') as copied:
            with self.assertRaises(storage.IntegrationError):
                gw.prepare_reference(selected, 'validation', lambda: None)
            copied.assert_not_called()
        selected.update(lambda s: s['experiment_spec'].update(max_seconds=9999))
        with self.assertRaises(storage.IntegrationError):
            gw.verify_session(selected)

    def test_prepare_dispatch_exposes_only_calibration_and_fresh_profile(self):
        selected = self.provision()
        profile = {'status': 'ok', 'family': 'accel_cap_linear_brake', 'max_speed_body_s': 4}
        with patch.object(gw, 'prepare_reference', return_value={'runs': ['cal-r06', 'cal-r48'], 'profile': profile,
                    'profile_path': 'fresh'}) as ref, \
             patch.object(gw, '_ground_group', return_value=['before-cal']) as group, \
             patch.object(evaluation, 'prepare_reference', return_value={'runs': ['jump-cal']}) as jump, \
             patch.object(gw, '_jump_before', return_value=['original-before-cal']), \
             patch.object(gw, '_prepare_feedback', return_value={'reference_body_height_px': 11, 'target_body_height_px': 31,
                 'units': {'x_body': 'body heights'}, 'before_calibration': {'mean_rmse_body': .2}}):
            result = gw.run_action(selected, {'action': 'prepare'}, cancel=threading.Event(), check=lambda: None, progress=Mock())
        self.assertEqual(ref.call_args.args[1], 'calibration')
        self.assertEqual(jump.call_args.kwargs['split'], 'calibration')
        self.assertEqual(group.call_args.args[4], 'calibration')
        self.assertEqual(result['reference_profile'], profile)
        self.assertEqual(result['reference_body_height_px'], 11)
        self.assertEqual(result['target_body_height_px'], 31)
        self.assertEqual(result['before_calibration']['mean_rmse_body'], .2)
        self.assertEqual(selected.read()['preparation_feedback']['units'], {'x_body': 'body heights'})
        self.assertNotIn('validation', selected.read()['reference'])
        self.assertEqual(selected.read()['baseline_runs']['jump']['calibration'], ['original-before-cal'])

    def test_reference_cancel_and_model_mismatch_stop_before_capture(self):
        selected = self.provision()
        with patch.object(gw, '_copy_raw') as copied:
            def cancelled():
                raise storage.IntegrationError('cancelled', 'test cancellation')
            with self.assertRaises(storage.IntegrationError) as caught:
                gw.prepare_reference(selected, 'calibration', cancelled)
            self.assertEqual(caught.exception.code, 'cancelled')
            copied.assert_not_called()
        with patch.object(gw, '_copy_raw', return_value=Path('raw')), \
             patch.object(gw, '_ground_annotation', return_value=Path('annotation')), \
             patch('kiroclops.ground.analysis.analyze_run', return_value={
                 'status': 'ok', 'valid_ratio': 1, 'camera_valid': True}), \
             patch('kiroclops.ground.fitting.fit_profile', return_value={'status': 'mismatch'}) as fitted, \
             patch.object(gw, '_ground_group') as captured:
            with self.assertRaises(storage.IntegrationError) as caught:
                gw.run_action(selected, {'action': 'prepare'}, cancel=threading.Event(),
                              check=lambda: None, progress=Mock())
            self.assertEqual(caught.exception.code, 'model_mismatch')
            self.assertEqual(len(fitted.call_args.args[0]), 6)
            captured.assert_not_called()

    def test_capture_source_mutation_is_rejected_before_analysis(self):
        selected = self.ready()
        state = selected.read()
        project = Path(state['baseline'])
        def tamper(*args, **kwargs):
            self.put(project / 'project.godot', 'changed during capture')
        with patch.object(ground_runner, 'capture_project', side_effect=tamper), \
             patch('kiroclops.ground.analysis.analyze_run') as analyzed:
            with self.assertRaises(storage.IntegrationError):
                gw._ground_group(selected, project, selected.directory / 'test-runs', 'before',
                    'calibration', state['baseline_hashes'], threading.Event(), lambda: None, Mock())
            analyzed.assert_not_called()

    def evaluate(self, selected, *, ground_eligible=True, jump_pass=True, smoke='passed'):
        selected.update(lambda s: s['candidates'].append({'candidate_id': 'candidate-01', 'status': 'queued'}))
        feedback = {'status': 'ok', 'eligible': ground_eligible, 'diagnostic_gates_pass': True, 'mean_rmse_body': .08}
        with patch.object(ground_runner, 'import_project'), \
             patch.object(gw, '_ground_group', return_value=['new-ground-cal']), \
             patch.object(gw, '_jump_group', return_value=['new-jump-cal']), \
             patch.object(comparison, 'calibration_feedback', return_value=feedback), \
             patch.object(evaluation, 'calibration_feedback', return_value={'diagnostic_gates_pass': jump_pass}), \
             patch.object(ground_runner, 'smoke_project', return_value={'status': smoke, 'full_source_hashes_before': {'large': 'map'},
                 'full_source_hashes_after': {'large': 'map'}, 'runs': {'normal': {'path': 'normal', 'checks': {'runtime': True}}}}), \
             patch.object(gw, '_jump_drift', return_value={'status': 'diagnostic'}), \
             patch.object(gw, 'export_calibration_evidence', return_value={'label': 'candidate-01', 'index_path': 'workspace/calibration/index.json'}):
            return gw.run_action(selected, {'action': 'evaluate', 'candidate_id': 'candidate-01'},
                                 cancel=threading.Event(), check=lambda: None, progress=Mock())

    def test_ground_success_requires_jump_smoke_and_manual_eligibility(self):
        selected = self.ready()
        result = self.evaluate(selected)
        self.assertTrue(result['calibration']['eligible'])
        self.assertNotIn('full_source_hashes_before', result['gameplay'])
        self.assertIn('full_source_hashes_before', selected.read()['candidates'][0]['smoke'])
        self.assertEqual(result['calibration_evidence']['label'], 'candidate-01')
        self.assertEqual(selected.read()['candidates'][0]['calibration_evidence'], result['calibration_evidence'])
        for ground_ok, jump_ok, smoke in ((False, True, 'passed'), (True, False, 'passed'), (True, True, 'failed')):
            selected.update(lambda s: s.update(candidates=[]))
            # Snapshot output is immutable: each isolated negative case uses a new session name.
            # Reuse the measured candidate only through mocked snapshot creation.
            from kiroclops.integration import session as session_module
            candidate_root = selected.directory / 'candidates/candidate-01'
            with patch.object(session_module, 'create_candidate_snapshot', return_value=candidate_root):
                result = self.evaluate(selected, ground_eligible=ground_ok, jump_pass=jump_ok, smoke=smoke)
            self.assertFalse(result['calibration']['eligible'])

    def test_finalization_keeps_manual_pending_and_records_both_matrices(self):
        selected = self.ready()
        self.evaluate(selected)
        selected.update(lambda s: s.update(selected_candidate_id='candidate-01', sealed_at=storage.utc_now()))
        numeric = {'numerical_status': 'pass', 'manual_validation_status': 'pending',
                   'validation_scenarios': [], 'coverage': {'complete_triplets': 18}, 'mean_rmse_body': .08}
        with patch.object(gw, 'prepare_reference', return_value={'runs': ['ref-val']}), \
             patch.object(gw, '_ground_group', side_effect=[['before-val'], ['after-val']]), \
             patch.object(evaluation, 'prepare_reference', return_value={'runs': ['jump-ref-val']}), \
             patch.object(gw, '_jump_before', return_value=['old-jump-before-val']), \
             patch.object(gw, '_jump_group', return_value=['new-jump-after-val']), \
             patch.object(comparison, 'compare_runs', return_value=numeric), \
             patch.object(evaluation, 'final_comparison', return_value=numeric), \
             patch.object(gw, '_jump_drift', return_value={'status': 'diagnostic'}):
            result = gw.run_action(selected, {'action': 'finalize', 'candidate_id': 'candidate-01'},
                                 cancel=threading.Event(), check=lambda: None, progress=Mock())
        self.assertEqual(result['numerical_status'], 'pass')
        self.assertEqual(result['overall_status'], 'pending')
        self.assertFalse(result['validation']['success'])
        self.assertEqual(selected.read()['candidates'][0]['jump_validation_runs'], ['new-jump-after-val'])
        self.assertIn('ground', result['validation'])
        self.assertIn('jump_regression', result['validation'])

    def test_worker_dispatch_preserves_failure_accounting_and_candidate_budget(self):
        selected = self.ready()
        process = Mock(pid=98765)
        with patch.object(session.subprocess, 'Popen', return_value=process), \
             patch.object(session, 'process_identity', return_value='same'), \
             patch.object(worker, 'process_identity', return_value='same'), \
             patch.object(gw, 'run_action', side_effect=storage.IntegrationError('tracking_failure', 'test')) as action, \
             patch.object(worker.traceback, 'print_exc'):
            for i in range(3):
                job = selected.submit('evaluate')
                self.assertEqual(worker.run_job(selected.directory, job['job_id']), 1)
                self.assertEqual(selected.read()['candidates'][i]['error']['code'], 'tracking_failure')
            self.assertEqual(action.call_count, 3)
            with self.assertRaises(storage.IntegrationError):
                selected.submit('evaluate')
        self.assertIsNone(selected.read()['active_job_id'])

    def measured_runs(self, selected, game, variant, body_height):
        runs = []
        for scenario in ex.get_experiment(ex.GROUND_ID).scenarios('calibration'):
            run = selected.directory / 'measurements' / f'{game}-{variant}' / ex.run_name(scenario)
            manifest = {'game': game, 'variant': variant, 'scenario': scenario, 'fps': 60,
                        'frame_count': 240, 'viewport': [228, 128]}
            storage.write_json(run / 'manifest.json', manifest)
            frames = [{'frame_index': i, 'time_s': i / 60, 'physics_tick': 60 + i, 'actions': [],
                       'x_body': i / 100, 'valid': True, 'score': .99, 'margin': .2,
                       'camera_valid': True, 'camera_shift_px': 0, 'mirrored': False} for i in range(240)]
            report = {**manifest, 'status': 'ok', 'body_height_px': body_height, 'valid_ratio': 1,
                      'camera_valid': True, 'frames': frames, 'events': {'first_input_tick': 30},
                      'phases': [{'name': 'input', 'start': 30, 'end': 36}],
                      'metrics': {'speed_body_s': 6, 'stop_distance_body': .5, 'end_position_body': 2.39,
                                  'diagnostics': {'pixel_rest_frame': 84}},
                      'annotation_validation': {'status': 'pending'},
                      'provenance': {'manifest_sha256': storage.sha256(run / 'manifest.json')}}
            storage.write_json(run / 'analysis.json', report)
            (run / 'video.mp4').write_bytes(b'raw test video')
            runs.append(str(run))
        return runs

    @staticmethod
    def fake_contact_sheet(run, frames, crop, output, **kwargs):
        Path(output).write_bytes(b'raw contact test image')

    def test_public_evidence_has_all_native_curves_and_measured_nonstandard_scales(self):
        selected = self.provision()
        refs = self.measured_runs(selected, 'tiny', 'reference', 11)
        before = self.measured_runs(selected, 'slay', 'before', 31)
        original = {str(Path(r) / name): storage.sha256(Path(r) / name)
                    for r in refs + before for name in ('analysis.json', 'manifest.json', 'video.mp4')}
        source_before = storage.project_hashes(selected.directory / 'target')
        profile = {'fit_diagnostics': {'ambiguous': True, 'rmse_body': .04, 'integration': 'input then move'}}
        with patch('scripts.inspect_frames.contact_sheet', side_effect=self.fake_contact_sheet), \
             patch('kiroclops.ground.comparison.curve_rmse', return_value=.25):
            result = gw._prepare_feedback(selected, {'runs': refs, 'profile': profile}, before, lambda: None)
        self.assertEqual((result['reference_body_height_px'], result['target_body_height_px']), (11, 31))
        self.assertEqual((result['fps'], result['dt_s']), (60, 1/60))
        self.assertTrue(result['confidence']['fit']['ambiguous'])
        self.assertEqual(result['before_calibration']['mean_rmse_body'], .25)
        self.assertEqual(len(result['before_calibration']['runs']), 6)
        for role in ('reference', 'baseline'):
            package = result['calibration_evidence'][role]
            self.assertTrue(Path(package['index_path']).is_relative_to(selected.directory / 'target'))
            self.assertEqual(len(package['runs']), 6)
            for row in package['runs']:
                measured = storage.read_json(row['measurement_path'])
                self.assertEqual(len(measured['frames']), 240)
                self.assertEqual(measured['frames'][123]['x_body'], 1.23)
                self.assertEqual(measured['frames'][123]['time_s'], 123/60)
                self.assertEqual(row['confidence']['manual_validation_status'], 'pending')
                self.assertEqual(row['measurement_sha256'], storage.sha256(row['measurement_path']))
                self.assertIn('diagnostics', measured['metrics'])
                self.assertNotIn('frames', row)
            self.assertEqual(sum('video_path' in r for r in package['runs']), 2)
        self.assertEqual(storage.project_hashes(selected.directory / 'target'), source_before)
        self.assertEqual({path: storage.sha256(path) for path in original}, original)

    def test_export_refuses_validation_incomplete_or_inconsistent_body_scale(self):
        selected = self.provision()
        runs = self.measured_runs(selected, 'slay', 'after', 19)
        with self.assertRaises(storage.IntegrationError):
            gw.export_calibration_evidence(selected, 'candidate-01', runs[:-1])
        path = Path(runs[0]) / 'analysis.json'
        report = storage.read_json(path)
        report['body_height_px'] = 20
        storage.write_json(path, report)
        with self.assertRaises(storage.IntegrationError):
            gw.export_calibration_evidence(selected, 'candidate-01', runs)
        report['body_height_px'] = 19
        report['scenario'] = ex.get_experiment(ex.GROUND_ID).scenarios('validation')[0]
        storage.write_json(path, report)
        with self.assertRaises(storage.IntegrationError):
            gw.export_calibration_evidence(selected, 'candidate-01', runs)
        self.assertFalse((selected.directory / 'target/.kiroclops-evidence').exists())

    def test_candidate_export_is_calibration_only_and_cannot_overwrite_evidence(self):
        selected = self.provision()
        runs = self.measured_runs(selected, 'slay', 'after', 19)
        with patch('scripts.inspect_frames.contact_sheet', side_effect=self.fake_contact_sheet):
            result = gw.export_calibration_evidence(selected, 'candidate-01', runs)
        self.assertEqual(result['body_height_px'], 19)
        original = Path(result['index_path']).read_bytes()
        with self.assertRaises(storage.IntegrationError):
            gw.export_calibration_evidence(selected, 'candidate-01', runs)
        self.assertEqual(Path(result['index_path']).read_bytes(), original)

    def test_export_cancelled_before_first_write_keeps_workspace_clean(self):
        selected = self.provision()
        runs = self.measured_runs(selected, 'tiny', 'reference', 11)
        def cancel(): raise storage.IntegrationError('cancelled', 'stopped')
        with self.assertRaises(storage.IntegrationError):
            gw.export_calibration_evidence(selected, 'reference', runs, cancel)
        self.assertFalse((selected.directory / 'target/.kiroclops-evidence').exists())


class GroundLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'reference-project'
        GroundSessionTests.put(self.project / 'project.godot', 'frozen reference source')
        self.source_hashes = storage.project_hashes(self.project)
        self.files = {'ground.py': 'frozen-code'}
        for method, value in (('_tracking_files', self.files), ('ground_parent', {'source_hashes': {'parent': 'x'}}),
                              ('_parent_jump_files', {'parent-run': 'x'})):
            p = patch.object(ex, method, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('scripts.run_suite.Suite.verify_frozen', return_value={'files': {'old': 'frozen'}})
        p.start()
        self.addCleanup(p.stop)

    def raw_pack(self):
        for split in ('calibration', 'validation'):
            for scenario in ex.get_experiment(ex.GROUND_ID).scenarios(split):
                run = self.root / 'artifacts/phase2b/reference' / ex.run_name(scenario)
                storage.write_json(run / 'manifest.json', {'status': 'ok', 'game': 'tiny', 'variant': 'reference',
                    'fps': 60, 'frame_count': 240, 'scenario': scenario,
                    'provenance': {'full_source_hashes_before': self.source_hashes,
                                   'full_source_hashes_after': self.source_hashes}})
                (run / 'frames').mkdir()
                for i in range(240):
                    (run / 'frames' / f'{i:06d}.png').write_bytes(b'hash-only-test-pixel')

    def test_two_step_freeze_complete_raw_pack_is_immutable(self):
        ex.freeze_ground_tracking(self.project, root=self.root)
        self.raw_pack()
        with patch('kiroclops.ground.analysis.analyze_run') as analyze:
            path = ex.freeze_ground_reference(root=self.root)
            analyze.assert_not_called()
        before = path.read_bytes()
        self.assertEqual(len(ex.verify_ground_lock(root=self.root)['reference']['reference_files']), 24 * 241)
        with self.assertRaises(storage.IntegrationError):
            ex.freeze_ground_reference(root=self.root)
        self.assertEqual(path.read_bytes(), before)
        run = self.root / 'artifacts/phase2b/reference/r18-x0-r1/frames/000100.png'
        run.write_bytes(b'changed pixels')
        with self.assertRaises(storage.IntegrationError):
            ex.verify_ground_lock(root=self.root)

    def test_holdout_before_tracking_freeze_and_missing_pack_fail_closed(self):
        run = self.root / 'artifacts/phase2b/reference/r18-x0-r1'
        GroundSessionTests.put(run / 'manifest.json', '{}')
        with self.assertRaises(storage.IntegrationError) as caught:
            ex.freeze_ground_tracking(self.project, root=self.root)
        self.assertEqual(caught.exception.code, 'holdout_before_freeze')
        (run / 'manifest.json').unlink()
        ex.freeze_ground_tracking(self.project, root=self.root)
        with self.assertRaises(storage.IntegrationError):
            ex.freeze_ground_reference(root=self.root)
        self.assertFalse((self.root / 'config/phase2b/frozen.json').exists())

    def test_source_and_tracking_changes_invalidate_before_frame_access(self):
        ex.freeze_ground_tracking(self.project, root=self.root)
        self.files['ground.py'] = 'changed-code'
        # The frozen JSON keeps the original hash even if the mock's dict changes.
        with patch.object(ex, '_reference_files') as pixels:
            with self.assertRaises(storage.IntegrationError):
                ex.freeze_ground_reference(root=self.root)
            pixels.assert_not_called()
