"""Ground cohort and proof gates; only temporary synthetic metadata is used."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import ground_benchmark as gb
from kiroclops.integration.experiments import GROUND_ID, get_experiment, run_name
from kiroclops.integration.storage import write_json, sha256


class GroundBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        p = patch.object(gb, 'ROOT', self.root)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(gb.proof, 'ROOT', self.root)
        p.start(); self.addCleanup(p.stop)
        self.cohort = {'schema_version': 1, 'experiment_id': GROUND_ID, 'registered_before_runs': True,
            'cli_session_ids': ['cli-ground-01', 'cli-ground-02', 'cli-ground-03'], 'ide_session_ids': ['ide-ground-01'],
            'baseline_session_ids': [], 'parent_session_id': 'ide-02', 'parent_candidate_id': 'candidate-01',
            'model': 'claude-opus-5', 'effort': 'high', 'max_candidates': 3, 'max_seconds': 1800, 'replacement_runs': False}
        write_json(self.root / 'config/phase2b/cohort.json', self.cohort)
        self.session = self.root / 'sessions/cli-ground-01'
        self.session.mkdir(parents=True)

    def state(self):
        candidate = {'candidate_id': 'candidate-01', 'source_hashes': {'player.gd': 'source'}, 'status': 'finalized'}
        state = {'schema_version': 3, 'experiment_id': GROUND_ID, 'session_id': self.session.name, 'mode': 'tools',
            'candidates': [candidate], 'selected_candidate_id': 'candidate-01', 'baseline_hashes': {'baseline': 'source'},
            'experiment_locks': {'tracking': 'lock'}, 'reference': {}, 'baseline_runs': {}, 'final': {'candidate_id': 'candidate-01'}}
        write_json(self.session / 'session.json', state)
        return state, candidate

    def test_missing_runs_remain_not_run_and_report_writes_only_separate_output(self):
        result = gb.build_benchmark(output='artifacts/phase2b-report/test')
        self.assertEqual([r['status'] for r in result['sessions']], ['not_run'] * 4)
        self.assertFalse(result['cli']['all_pass'])
        self.assertEqual(result['phase_acceptance'], 'pending_or_failed')
        self.assertTrue((self.root / 'artifacts/phase2b-report/test/index.html').is_file())
        self.assertFalse((self.session / 'session.json').exists())
        with self.assertRaises(ValueError):
            gb.build_benchmark(output='sessions/output')

    def test_registered_ids_only_no_replacements_baseline_or_other_experiment(self):
        for ids in (['cli-ground-04'], ['cli-ground-01'] * 2, ['../escape']):
            with self.assertRaises(ValueError): gb.build_benchmark(ids)
        state, _ = self.state()
        state['experiment_id'] = 'legacy-jump-v1'
        write_json(self.session / 'session.json', state)
        with self.assertRaises(ValueError): gb.build_benchmark(['cli-ground-01'])
        self.cohort['baseline_session_ids'] = ['baseline-01']
        write_json(self.root / 'config/phase2b/cohort.json', self.cohort)
        with self.assertRaises(ValueError): gb.build_benchmark()

    def test_malformed_started_metadata_is_reported_without_inventing_success(self):
        (self.session / 'session.json').write_text('{broken', encoding='utf-8')
        row = gb.build_benchmark(['cli-ground-01'])['sessions'][0]
        self.assertEqual(row['status'], 'fail')
        self.assertEqual(row['final_error']['status'], 'unavailable')

    def test_workflow_binding_rejects_legacy_instruction_bytes_and_accepts_crlf_source(self):
        source = self.root / 'power/kiroclops/ground-motion.md'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'ground instructions\r\n')
        lock = {'tracking': {'files': {'power/kiroclops/ground-motion.md': sha256(source)}}}
        config = {'experiment_id': GROUND_ID, 'instruction_sha256': gb.hashlib.sha256(b'ground instructions\n').hexdigest()}
        with patch('kiroclops.integration.kiro._load_config', return_value=config):
            self.assertEqual(gb._configuration_binding(self.session, lock)['status'], 'pass')
            config['instruction_sha256'] = 'legacy instruction'
            self.assertEqual(gb._configuration_binding(self.session, lock)['status'], 'fail')

    def test_session_before_registration_freeze_is_rejected(self):
        state, candidate = self.state()
        state['created_at'] = '2026-09-19T08:00:00+00:00'
        lock = {'tracking': {'created_at': '2026-09-19T09:00:00+00:00'},
                'reference': {'created_at': '2026-09-19T10:00:00+00:00'}}
        gate = gb._state_integrity(self.session, state, candidate, lock)
        self.assertEqual(gate['status'], 'fail')
        self.assertIn('after', gate['reason'])

    def test_complete_same_cli_cohort_and_native_demo_are_separate_from_strict_ide_proof(self):
        write_json(self.root / 'config/phase2b/frozen.json', {})
        identity = {'baseline_hashes': {'p': 'x'}, 'experiment_locks': {'t': 'x'}, 'cli_version': '1', 'kas_version': '2'}
        def collect(session, surface, lock):
            return {'session_id': session.name, 'surface': surface, 'success': surface == 'cli',
                'status': 'pass' if surface == 'cli' else 'unverified',
                'demo_status': 'demonstrated' if surface == 'ide' else 'not_applicable',
                'cohort_identity': deepcopy(identity), 'final_error': {'status': 'available', 'mean_rmse_body': .05}}
        with patch.object(gb, 'verify_ground_lock', return_value={'valid': True}), patch.object(gb, '_collect', side_effect=collect):
            result = gb.build_benchmark()
            self.assertEqual(result['phase_acceptance'], 'demonstrated_with_ide_configuration_limit')
            self.assertEqual(result['ide']['strict_model_configuration'], 'unverified')
            self.assertFalse(result['strict_all_surfaces_acceptance'])
            subset = gb.build_benchmark(['cli-ground-01', 'ide-ground-01'])
            self.assertFalse(subset['registration_complete'])
            self.assertEqual(subset['phase_acceptance'], 'pending_or_failed')
        def differing(session, surface, lock):
            row = collect(session, surface, lock)
            if session.name == 'cli-ground-02': row['cohort_identity']['baseline_hashes'] = {'p': 'different'}
            return row
        with patch.object(gb, 'verify_ground_lock', return_value={'valid': True}), patch.object(gb, '_collect', side_effect=differing):
            self.assertFalse(gb.build_benchmark()['cli']['all_pass'])

    def mocked_collect(self, *, manual='pass', smoke='pass', source_scope='pass', saved_ok=True, surface='cli'):
        if surface == 'ide':
            self.session = self.root / 'sessions/ide-ground-01'
            self.session.mkdir(exist_ok=True)
        state, candidate = self.state()
        launch = {'surface': surface, 'mode': 'tools', 'status': 'completed', 'elapsed_seconds': 300,
                  'effort_requested': 'high', 'cli_version': '1', 'kas_version': '2'}
        write_json(self.session / 'kiro/launch.json', launch)
        fresh = {'ground': {'numerical_status': 'pass'}, 'jump_regression': {'numerical_status': 'pass'},
                 'manual_validation_status': 'pass', 'mean_rmse_body': .08}
        author = {'status': 'verified', 'candidate_id': candidate['candidate_id'], 'source_hashes': candidate['source_hashes']}
        native = {'recomputed_from_original_zip': True, 'authorship': author, 'model': {'reasoning_models': ['claude-opus-5'], 'status': 'limited'},
            'tools': {'calls': [{'success': True, 'power': {'action': 'activate', 'powerName': 'kiroclops'}},
                               {'success': True, 'power': {'action': 'readSkill', 'powerName': 'kiroclops', 'skillName': 'motion-calibration'}}]}}
        complete = {'status': 'pass', 'ui_before': {'status': 'pass'}, 'ui_after': {'status': 'pass'}}
        with ExitStack() as stack:
            for name in ('_state_integrity', '_configuration_binding', '_capture_identity'):
                stack.enter_context(patch.object(gb, name, return_value={'status': 'pass'}))
            stack.enter_context(patch.object(gb, '_evaluation_job', return_value={'status': 'pass'}))
            stack.enter_context(patch.object(gb, '_quality', return_value=({'status': 'pass' if saved_ok else 'fail'}, fresh)))
            stack.enter_context(patch.object(gb, '_manual', return_value={'status': manual, 'manual_validation_status': manual}))
            stack.enter_context(patch.object(gb, '_source_scope', return_value={'status': source_scope}))
            stack.enter_context(patch.object(gb, '_smoke', return_value={'status': smoke}))
            stack.enter_context(patch.object(gb.proof, '_configuration', return_value={'status': 'pass'}))
            stack.enter_context(patch.object(gb.proof, '_model', return_value={'status': 'verified'}))
            stack.enter_context(patch.object(gb.proof, '_launch_completion', return_value=complete))
            stack.enter_context(patch.object(gb.proof, '_ide_completion', return_value=complete))
            stack.enter_context(patch.object(gb.proof, '_ide_evidence', return_value=native))
            stack.enter_context(patch.object(gb.proof, '_ide_job_bindings', return_value={'status': 'pass', 'jobs': {'evaluate': {}, 'finalize': {}}}))
            stack.enter_context(patch('kiroclops.integration.authorship.audit_kiro_authorship', return_value=author))
            return gb._collect(self.session, surface, {'test': True})

    def test_saved_pass_never_overrides_missing_manual_failed_smoke_or_changed_comparison(self):
        self.assertTrue(self.mocked_collect()['success'])
        self.assertFalse(self.mocked_collect(manual='unverified')['success'])
        self.assertEqual(self.mocked_collect(smoke='fail')['status'], 'fail')
        changed = self.mocked_collect(saved_ok=False)
        self.assertFalse(changed['success'])
        self.assertEqual(changed['final_error']['status'], 'unavailable')

    def test_real_ide_demo_can_be_demonstrated_without_invented_strict_effort_proof(self):
        row = self.mocked_collect(surface='ide')
        self.assertEqual(row['demo_status'], 'demonstrated')
        self.assertFalse(row['success'])
        self.assertEqual(row['strict_model_configuration'], 'unverified')
        self.assertEqual(self.mocked_collect(surface='ide', manual='unverified')['demo_status'], 'unverified')

    def test_source_scope_review_is_required_for_cli_and_ide_acceptance(self):
        for status in ('pending', 'fail'):
            with self.subTest(status=status):
                cli = self.mocked_collect(source_scope=status)
                self.assertFalse(cli['success'])
                self.assertEqual(cli['gates']['source_scope'], status)
                expected = 'failed' if status == 'fail' else 'unverified'
                self.assertEqual(self.mocked_collect(surface='ide', source_scope=status)['demo_status'], expected)
        self.assertTrue(self.mocked_collect(source_scope='pass')['success'])

    def test_fresh_comparison_rejects_summary_pass_even_when_files_exist(self):
        state, candidate = self.state()
        expected = {'schema_version': 3, 'numerical_status': 'fail'}
        with patch('kiroclops.ground.comparison.compare_runs', return_value={'numerical_status': 'fail'}), \
             patch('kiroclops.integration.evaluation.final_comparison', return_value={'numerical_status': 'pass'}), \
             patch('kiroclops.integration.ground_worker._jump_drift', return_value={}), \
             patch('kiroclops.integration.ground_worker.combine_comparisons', return_value=expected):
            gate, fresh = gb._quality(self.session, state, candidate, {'numerical_status': 'pass'})
        self.assertEqual(gate['status'], 'fail')
        self.assertEqual(fresh, expected)

    def capture_matrix(self):
        state, candidate = self.state()
        lock = {'tracking': {'reference_source_hashes': {'tiny': 'source'}}}
        state['reference'] = {'jump': {}}
        state['baseline_runs'] = {'jump': {}}
        for mechanic in ('ground', 'jump'):
            for role, game, variant in [('reference', 'tiny', 'reference'), ('baseline', 'slay', 'before'), ('candidate', 'slay', 'after')]:
                for split in ('calibration', 'validation'):
                    if mechanic == 'ground': scenarios = get_experiment(GROUND_ID).scenarios(split)
                    else: scenarios = [{'hold_ticks': h, 'start_tick': 30, 'split': split, 'repeat': r, 'spawn_offset': x}
                        for h in ((3, 24) if split == 'calibration' else (8, 14))
                        for x in ((0,) if split == 'calibration' else (0, 24)) for r in (1, 2, 3)]
                    paths = []
                    for i, scenario in enumerate(scenarios):
                        run = self.session / f'evidence/{mechanic}/{role}/{split}/{i}'
                        expected = lock['tracking']['reference_source_hashes'] if role == 'reference' else state['baseline_hashes'] if role == 'baseline' else candidate['source_hashes']
                        manifest = {'game': game, 'variant': variant, 'scenario': scenario, 'fps': 60, 'frame_count': 240, 'status': 'ok',
                            'provenance': {'full_source_hashes_before': expected, 'full_source_hashes_after': expected}}
                        write_json(run / 'manifest.json', manifest)
                        write_json(run / 'analysis.json', {**manifest, 'provenance': {'manifest_sha256': sha256(run / 'manifest.json')}})
                        if role == 'reference' or mechanic == 'jump' and role == 'baseline':
                            name = run_name(scenario) if mechanic == 'ground' else f"{game}-{variant}-h{scenario['hold_ticks']:02d}-x{scenario['spawn_offset']}-r{scenario['repeat']}"
                            original = self.root / ('artifacts/phase2b/reference' if mechanic == 'ground' else 'artifacts/runs') / name
                            write_json(original / 'manifest.json', manifest)
                        paths.append(str(run))
                    if role == 'candidate': candidate[f'{"jump_" if mechanic == "jump" else ""}{split}_runs'] = paths
                    else:
                        group = state['reference' if role == 'reference' else 'baseline_runs']
                        if mechanic == 'jump': group = group['jump']
                        group[split] = paths
        return state, candidate, lock

    def test_exact_18_ground_and_12_jump_matrix_source_identity_is_required(self):
        state, candidate, lock = self.capture_matrix()
        gate = gb._capture_identity(self.session, state, candidate, lock)
        self.assertEqual((gate['status'], gate['checked_capture_manifests']), ('pass', 126))
        path = Path(candidate['validation_runs'][0]) / 'manifest.json'
        manifest = gb.read_json(path)
        manifest['provenance']['full_source_hashes_before'] = {'different': 'candidate'}
        write_json(path, manifest)
        report_path = path.parent / 'analysis.json'
        report = gb.read_json(report_path)
        report['provenance']['manifest_sha256'] = sha256(path)
        write_json(report_path, report)
        self.assertEqual(gb._capture_identity(self.session, state, candidate, lock)['status'], 'fail')

    def test_duplicate_capture_and_missing_validation_do_not_become_pass(self):
        state, candidate, lock = self.capture_matrix()
        candidate['validation_runs'][1] = candidate['validation_runs'][0]
        self.assertEqual(gb._capture_identity(self.session, state, candidate, lock)['status'], 'fail')
        candidate['validation_runs'].pop()
        self.assertEqual(gb._capture_identity(self.session, state, candidate, lock)['status'], 'unverified')

    def test_selected_eligible_feedback_requires_successful_own_evaluation_job(self):
        _, candidate = self.state()
        candidate['feedback'] = {'eligible': True, 'mean_rmse_body': .08}
        job = {'job_id': 'job-selected', 'session_id': self.session.name, 'action': 'evaluate',
               'candidate_id': 'candidate-01', 'status': 'succeeded',
               'result': {'candidate_id': 'candidate-01', 'status': 'evaluated', 'calibration': candidate['feedback']}}
        path = self.session / 'jobs/job-selected.json'
        write_json(path, job)
        self.assertEqual(gb._evaluation_job(self.session, candidate)['status'], 'pass')
        for status in ('running', 'cancelled', 'failed'):
            job['status'] = status
            write_json(path, job)
            self.assertEqual(gb._evaluation_job(self.session, candidate)['status'], 'fail')
        job['status'] = 'succeeded'
        job['result']['calibration'] = {'eligible': True, 'mean_rmse_body': .01}
        write_json(path, job)
        self.assertEqual(gb._evaluation_job(self.session, candidate)['status'], 'fail')
        job['result']['calibration'] = candidate['feedback']
        write_json(path, job)
        write_json(self.session / 'jobs/job-duplicate.json', {**job, 'job_id': 'job-duplicate'})
        self.assertEqual(gb._evaluation_job(self.session, candidate)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
