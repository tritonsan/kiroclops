"""Prospective recipes, clock binding and fixed acceptance gates; no engines run."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import suite_game as suite
from kiroclops.visual.schema import VisualError, digest, write_json


def recipe(stage='jump', split='validation', sid=None):
    return next(s for s in suite.scenarios(stage, split) if sid is None or s['id'] == sid)


def manifest(scenario, game='tiny', legacy=False):
    raw = suite.native_scenario(scenario['mechanic'], scenario)
    if legacy:
        raw['split'] = 'validation' if scenario['split'] == 'regression' else scenario['split']
        if scenario['mechanic'] == 'jump':
            raw = {k: raw[k] for k in ('hold_ticks', 'start_tick', 'split', 'repeat', 'spawn_offset')}
    active, frames = set(), []
    for i in range(240):
        for e in scenario['events']:
            if e['tick'] == i:
                (active.add if e['pressed'] else active.remove)(e['action'])
        frames.append({'frame_index': i, 'physics_tick': 60 + i, 'time_s': i / 60, 'actions': sorted(active)})
    return {'status': 'ok', 'fps': 60, 'frame_count': 240, 'warmup_ticks': 60, 'settled': True,
        'normal_level': False, 'game': game, 'variant': 'reference' if game == 'tiny' else 'before',
        'viewport': [228, 128] if game == 'tiny' else [320, 180], 'scenario': raw, 'frames': frames,
        'provenance': {'source_commit': suite.TINY_COMMIT, 'controller_hashes': deepcopy(suite.TINY_CONTROLLERS)}}


def jump_report(scenario, amplitude=1., *, role='reference'):
    values = []
    for i in range(240):
        height = amplitude * ((i - 30) / 20 if i <= 50 else (80 - i) / 30) if 30 <= i <= 80 else 0.
        values.append({'frame_index': i, 'physics_tick': 60 + i, 'time_s': i / 60,
                       'valid': True, 'height_body': height, 'x_body': 0.})
    return {'status': 'ok', 'fps': 60, 'frames': values, 'valid_ratio': 1., 'camera_valid': True,
        'body_height_px': 16., 'scenario': deepcopy(scenario), 'game': 'tiny' if role == 'reference' else 'heart',
        'variant': role, 'events': {'takeoff_frame': 30, 'apex_frame': 50, 'landing_frame': 80},
        'metrics': {'height_body': amplitude, 'rise_s': 20 / 60, 'fall_s': 30 / 60}}


def ground_report(scenario, error=0., *, role='reference'):
    native = manifest(scenario)['frames']
    frames = [dict(f, valid=True, x=0. if i < 30 else (i - 30) / 6, y=40.) for i, f in enumerate(native)]
    plan, metrics, errors = suite._measure_ground(frames, scenario, 16.)
    assert not errors
    # A constant post-input displacement error has unchanged measurable speed/coast.
    for f in frames[30:]: f['x_body'] += error
    metrics['end_position_body'] += error
    return {'status': 'ok', 'fps': 60, 'frames': frames, 'valid_ratio': 1., 'camera_valid': True,
        'body_height_px': 16., 'scenario': deepcopy(scenario), 'game': 'tiny' if role == 'reference' else 'heart',
        'variant': role, 'metrics': metrics, 'phases': plan}


class SuiteGameTests(unittest.TestCase):
    def test_complete_disjoint_registered_splits(self):
        expected = {'jump': (6, 12, 12), 'ground': (6, 18, 18)}
        all_cases = []
        for stage, counts in expected.items():
            for split, count in zip(suite.SPLITS, counts):
                rows = suite.scenarios(stage, split)
                self.assertEqual(len(rows), count)
                self.assertEqual({s['repeat'] for s in rows}, {1, 2, 3})
                self.assertEqual({s['spawn_offset'] for s in rows}, {0} if split == 'calibration' else {0, 24})
                for s in rows:
                    native = suite.native_scenario(stage, s)
                    self.assertNotIn('case_id', native)
                    self.assertNotIn('fps', native)
                    self.assertEqual(native['frame_count'], 240)
                    self.assertEqual(native['warmup_ticks'], 60)
                all_cases.extend(rows)
        self.assertEqual(len(all_cases), 72)
        self.assertEqual(len({s['case_id'] for s in all_cases}), 72)
        self.assertEqual({s['hold_ticks'] for s in suite.scenarios('jump', 'validation')}, {10, 18})

    def test_exact_new_reversal_and_release_before_press(self):
        s = recipe('ground', 'validation', 'reverse27')
        self.assertEqual(s['events'], [
            {'tick': 30, 'action': 'right', 'pressed': True},
            {'tick': 57, 'action': 'right', 'pressed': False},
            {'tick': 57, 'action': 'left', 'pressed': True},
            {'tick': 78, 'action': 'left', 'pressed': False}])
        self.assertEqual(suite.phases(s), [
            {'name': 'held', 'start': 30, 'end': 57, 'direction': 1},
            {'name': 'reversal', 'start': 57, 'end': 78, 'direction': -1},
            {'name': 'coast', 'start': 78, 'end': 109, 'direction': 0}])

    def test_mutation_id_only_alias_and_cross_split_are_rejected(self):
        s = recipe()
        s['events'][-1]['tick'] += 1
        with self.assertRaises(VisualError): suite.native_scenario('jump', s)
        s = recipe(); s['split'] = 'calibration'
        with self.assertRaises(VisualError): suite.canonical_scenario('jump', s)
        s = recipe(); s.pop('case_id')
        with self.assertRaises(VisualError): suite.canonical_scenario('jump', s)
        self.assertEqual(recipe()['hold_ticks'], 10)

    def test_legacy_receipts_relabel_only_known_regressions(self):
        for stage, sid in (('jump', 'h14'), ('ground', 'reverse24')):
            s = recipe(stage, 'regression', sid); raw = manifest(s, legacy=True)
            suite._bind_manifest(raw, stage, s, legacy=True)
            self.assertEqual(raw['scenario']['split'], 'validation')
            with self.assertRaises(VisualError): suite._bind_manifest(raw, stage, s)
            raw['frames'][57]['actions'] = ['jump']
            with self.assertRaises(VisualError): suite._bind_manifest(raw, stage, s, legacy=True)

    def test_native_clock_telemetry_and_controller_tampering_are_rejected(self):
        s = recipe()
        for mutate in (lambda m: m['frames'][80].update(x=1.),
                       lambda m: m['frames'][80].update(physics_tick=141),
                       lambda m: m['frames'][80].update(time_s=2.),
                       lambda m: m['provenance']['controller_hashes'].update({'src/actor/Player.gd': 'a' * 64}),
                       lambda m: m.update(viewport=[640, 360])):
            raw = manifest(s); mutate(raw)
            with self.assertRaises(VisualError): suite._bind_manifest(raw, 'jump', s)
        target = manifest(s, game='heart')
        suite._bind_manifest(target, 'jump', s)

    def test_fresh_ground_metrics_reuse_frozen_formula_and_no_native_position(self):
        s = recipe('ground', 'validation', 'l33')
        raw = manifest(s)
        obs = {'package_sha256': 'a' * 64, 'objects': [{'frames': [
            {'index': i, 'time_s': i / 60, 'x': 100 - max(0, min(i - 30, 33)), 'y': 30.,
             'valid': True, 'score': 1., 'margin': .2} for i in range(240)]}],
             'camera': [{'status': 'pass', 'max_shift_px': 0.} for _ in range(240)]}
        with patch('kiroclops.ground.analysis.analyze_run', side_effect=AssertionError('native/old tracker forbidden')):
            result = suite.observation_to_motion(obs, raw, {'body_height_px': 16.}, 'ground', s)
        self.assertEqual(result['status'], 'ok')
        self.assertAlmostEqual(result['metrics']['end_position_body'], -33 / 16)
        self.assertAlmostEqual(result['metrics']['speed_body_s'], -60 / 16)
        self.assertEqual(result['metrics']['stop_distance_body'], -1 / 16)
        obs['objects'][0]['frames'][22]['valid'] = False
        self.assertEqual(suite.observation_to_motion(obs, raw, {'body_height_px': 16.}, 'ground', s)['status'], 'rejected')

    def test_improvement_uses_original_not_already_good_parent(self):
        s = recipe()
        ref, original = jump_report(s), jump_report(s, 1.8, role='before')
        candidate = jump_report(s, 1.03, role='after')
        row = suite.compare_records(ref, original, candidate, 'jump')
        self.assertTrue(row['pass'])
        self.assertGreater(row['improvement_fraction'], .95)
        parent = jump_report(s, 1.02, role='before')
        self.assertFalse(suite.compare_records(ref, parent, candidate, 'jump')['gates']['improvement'])
        preserved = suite.compare_records(parent, parent, candidate, 'jump', require_improvement=False)
        self.assertTrue(preserved['pass'])
        self.assertEqual(preserved['baseline_role'], 'authoring_parent')

    def test_lower_error_is_lower_candidate_selection_score(self):
        s = recipe(); ref = jump_report(s); original = jump_report(s, 1.8, role='before')
        good = suite.compare_records(ref, original, jump_report(s, 1.02, role='after'), 'jump')
        worse = suite.compare_records(ref, original, jump_report(s, 1.08, role='after'), 'jump')
        self.assertLess(good['score'], worse['score'])
        self.assertEqual(good['score'], good['after_rmse_body'])

    def test_fresh_ground_acceptance_and_common_coverage(self):
        s = recipe('ground', 'validation', 'reverse27')
        ref, old = ground_report(s), ground_report(s, .6, role='before')
        new = ground_report(s, .05, role='after')
        row = suite.compare_records(ref, old, new, 'ground')
        self.assertTrue(row['pass']); self.assertAlmostEqual(row['after_rmse_body'], .05)
        new['frames'][57]['valid'] = False
        new['frames'][58]['valid'] = False
        with self.assertRaises(VisualError): suite.compare_records(ref, old, new, 'ground')

    def test_fixed_height_and_exact_native_duration_gates(self):
        s = recipe(); ref = jump_report(s); old = jump_report(s, 1.8, role='before')
        new = jump_report(s, 1.11, role='after')
        self.assertFalse(suite.compare_records(ref, old, new, 'jump')['gates']['height'])
        new = jump_report(s, role='after')
        new['events']['apex_frame'] = 53
        new['metrics'].update(rise_s=23 / 60, fall_s=27 / 60)
        row = suite.compare_records(ref, old, new, 'jump')
        self.assertFalse(row['gates']['rise'])
        self.assertTrue(row['gates']['fall'])

    def test_target_dispatch_is_private_native_recipe_with_source_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); project = base / 'parent'; project.mkdir()
            directory = base / 'native'; directory.mkdir()
            scenario = recipe(); hashes = {'player.gd': 'a' * 64}
            record = {'scenario': scenario, 'analysis': 'mock.json'}
            with patch.object(suite.runtime, 'source_hashes', return_value=hashes), \
                 patch('kiroclops.local_engine.session.source_hashes', return_value=hashes), \
                 patch.object(suite, '_short_runtime', return_value=directory), \
                 patch.object(suite, 'copy_project') as copy, \
                 patch.object(suite.runtime, 'import_project') as imported, \
                 patch.object(suite.runtime, 'capture_project') as captured, \
                 patch.object(suite, 'analyze_record', return_value=record):
                result = suite.capture_target(project, scenario, base / 'measured', lambda: None, lambda *a: None, None, variant='final')
            self.assertEqual(captured.call_args.args[2], suite.native_scenario('jump', scenario, variant='after'))
            self.assertEqual(result['source_sha256'], digest(hashes))
            self.assertEqual(result['capture_role'], 'final')
            self.assertEqual(copy.call_args.args[0], project.resolve())
            imported.assert_called_once()

    def test_profile_cannot_include_validation_and_exports_calibration_only(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'profile.json'
            rows = [jump_report(s) for s in suite.scenarios('jump', 'calibration')]
            with patch.object(suite.measurement, 'fit_reference', return_value={'family': 'held_velocity', 'v0_body_s': 10., 'fit_diagnostics': {}}) as fit:
                value = suite.profile(rows, 'jump', output)
            self.assertEqual(value['family'], 'held_velocity')
            self.assertEqual(len(fit.call_args.args[1]), 6)
            self.assertEqual(value['confidence']['independent_pixel_review'], 'pending')
            rows[-1] = jump_report(recipe())
            with self.assertRaises(VisualError): suite.profile(rows, 'jump', Path(temp) / 'bad.json')

    def test_long_actual_tmp_path_is_rejected_before_import_or_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / ('long' * 55)
            with patch.object(suite, '_short_runtime', return_value=directory), \
                 patch.object(suite.runtime, 'source_hashes', return_value={'player.gd': 'a' * 64}), \
                 patch('kiroclops.local_engine.session.source_hashes', return_value={'player.gd': 'a' * 64}), \
                 patch.object(suite, 'copy_project'), patch.object(suite.runtime, 'import_project') as imported, \
                 patch.object(suite.runtime, 'capture_project') as captured:
                with self.assertRaises(VisualError): suite.capture_target(Path(temp) / 'parent', recipe(), Path(temp) / 'measured')
            imported.assert_not_called(); captured.assert_not_called()

    def test_adapter_changes_only_capture_transport(self):
        root = Path(__file__).resolve().parents[1]
        old = (root / 'adapters/ground-godot3/capture.gd').read_text('utf-8')
        new = (root / 'adapters/phase3d-tiny/capture.gd').read_text('utf-8')
        self.assertEqual(old.splitlines()[3:], new.splitlines()[3:])
        self.assertNotIn('"x":', new); self.assertNotIn('"y":', new)
        self.assertIn('next_capture_tick = tick + 1 - warmup', new)

    def test_preservation_routes_short_native_files_into_bound_small_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); native = base / 'n'; native.mkdir()
            hashes = {'player.gd': 'a' * 64}
            def preserved(project, parent, output, stage, check, cancel):
                output.mkdir()
                write_json(output / 'comparison.json', {'status': 'pass', 'case_count': 40})
                write_json(output / 'source.json', hashes)
                return {'status': 'pass', 'evidence_path': str(output / 'comparison.json')}
            with patch.object(suite, '_short_runtime', return_value=native), \
                 patch('kiroclops.local_engine.session.source_hashes', return_value=hashes), \
                 patch.object(suite.game, '_preservation', side_effect=preserved):
                result = suite.preservation(base / 'candidate', base / 'parent', base / 'requested', 'jump')
            from kiroclops.visual.schema import read_json, file_sha256
            index = read_json(result['native_evidence_index']['path'])
            self.assertEqual(result['source_sha256'], digest(hashes))
            self.assertEqual(result['native_evidence_index']['file_count'], 2)
            self.assertEqual(result['native_evidence_index']['sha256'], file_sha256(base / 'requested/native-evidence-index.json'))
            self.assertEqual(result['native_evidence_sha256'], digest(index['file_hashes']))
            self.assertNotIn('native_evidence_hashes', result)
            self.assertEqual(Path(result['evidence_path']).parent, native / 'preservation')

    def test_failed_private_transport_cannot_count_as_pixels(self):
        proof = {'runner': suite.private.VERSION, 'status': 'completed', 'exit_code': 0,
            'owned_process_termination_confirmed': True, 'cleanup_errors': [], 'policy': suite.runtime.PROCESS_POLICY,
            'argv': ['godot.exe'], 'cwd': 'project', 'executable_sha256': suite.TINY_ENGINE_SHA256}
        # Failure is rejected before opening any recording/process file.
        proof['cleanup_errors'] = ['close failed']
        with self.assertRaises(VisualError): suite._verify_private(proof, Path('unused'), ['godot.exe'], Path('project'), {})
        proof['cleanup_errors'] = []; proof['status'] = 'timeout'
        with self.assertRaises(VisualError): suite._verify_private(proof, Path('unused'), ['godot.exe'], Path('project'), {})

    def reference_snapshot(self, root, scenario, *, kind='known_pixels'):
        import cv2
        import numpy as np
        from kiroclops.visual.schema import file_sha256
        raw = root / 'raw'; (raw / 'frames').mkdir(parents=True)
        png = cv2.imencode('.png', np.zeros((128, 228, 3), np.uint8))[1].tobytes()
        for i in range(240): (raw / 'frames' / f'{i:06d}.png').write_bytes(png)
        value = manifest(scenario, legacy=kind == 'known_pixels')
        write_json(raw / 'manifest.json', value)
        hashes = suite._frame_hashes(raw, [228, 128])
        return {'scenario': scenario, 'reference_kind': kind, 'raw_run': str(raw),
            'source_manifest_sha256': file_sha256(raw / 'manifest.json'), 'raw_frames_sha256': digest(hashes),
            'source_sha256': digest({'source_commit': suite.TINY_COMMIT, 'controller_hashes': suite.TINY_CONTROLLERS})}

    def test_read_only_reference_validation_binds_known_regression_png_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            record = self.reference_snapshot(Path(temp), recipe('jump', 'regression', 'h14'))
            raw = Path(record['raw_run']); original = (raw / 'manifest.json').read_bytes()
            result = suite.verify_reference_record(record, 'jump')
            self.assertEqual(result['status'], 'pass')
            self.assertFalse(result['private_transport_verified'])
            self.assertFalse(result['historical_pixel_seal_present'])
            self.assertEqual((raw / 'manifest.json').read_bytes(), original)
            (raw / 'frames/000057.png').write_bytes((raw / 'frames/000057.png').read_bytes() + b'changed')
            with self.assertRaises(VisualError): suite.verify_reference_record(record, 'jump')

    def test_fresh_reference_cannot_pass_without_bound_private_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            record = self.reference_snapshot(Path(temp), recipe('jump', 'validation', 'h10'), kind='fresh_pinned_capture')
            with self.assertRaises((VisualError, OSError)): suite.verify_reference_record(record, 'jump')


if __name__ == '__main__':
    unittest.main()
