"""Local report/CLI contracts, mocked audits only; no model/engine/encoding."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts import phase2c_review as cli
from kiroclops.integration.storage import read_json, write_json, sha256


class ReportWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry_path = self.root / 'config/phase2c/cohorts/heart-test.json'
        self.registry = {'root': str(self.root), 'cohort_id': 'heart-test',
            'chains': [{'chain_id': f'heart-test-{i:02}',
                        'jump_session': f'heart-heart-test-{i:02}-jump',
                        'ground_session': f'heart-heart-test-{i:02}-ground'} for i in (1, 2, 3)]}
        write_json(self.registry_path, self.registry)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.registry_verify = self.stack.enter_context(patch.object(cli.cohort, 'verify_registry', return_value=self.registry))
        self.verdicts = {}
        self.review_verify = self.stack.enter_context(patch.object(cli.review, 'verify_review', side_effect=self.verify))
        self.builder = self.stack.enter_context(patch.object(cli.reporting, 'build_session_report', side_effect=self.build_standard))
        self.stack.enter_context(patch.object(cli.reporting.subprocess, 'run', side_effect=AssertionError('No process allowed')))

    def directory(self, chain=0, stage='jump'):
        return self.root / 'sessions' / self.registry['chains'][chain][stage + '_session']

    def make_session(self, chain=0, stage='jump', *, verdict='pending', lifecycle='finished', numerical='pass'):
        directory = self.directory(chain, stage)
        state = {'session_id': directory.name, 'experiment_id': 'heart-' + stage + '-v1', 'status': lifecycle,
            'candidates': [{'candidate_id': f'candidate-{i:02}', 'status': 'evaluated'} for i in (1, 2, 3)],
            'selected_candidate_id': 'candidate-03'}
        project = directory / 'candidates/candidate-03/project'
        project.mkdir(parents=True)
        (project / 'player.gd').write_text('fixture player', encoding='utf-8')
        source_hashes = cli.project_hashes(project)
        state['baseline_hashes'] = source_hashes
        state['original_baseline'] = {'source_hashes': source_hashes}
        write_json(directory / 'candidates/candidate-03/candidate.json', {
            'candidate_id': 'candidate-03', 'project_path': str(project),
            'source_hashes': source_hashes, 'source_sha256': cli.digest(source_hashes)})
        if stage == 'ground':
            parent_dir = self.directory(chain, 'jump')
            parent_record = parent_dir / 'candidates/candidate-03/candidate.json'
            state['baseline_hashes'] = source_hashes
            state['parent'] = {'session': str(parent_dir), 'candidate_id': 'candidate-03',
                'source_hashes': source_hashes, 'source_sha256': cli.digest(source_hashes),
                'candidate_record_sha256': sha256(parent_record) if parent_record.is_file() else None}
        write_json(directory / 'session.json', state)
        write_json(directory / 'kiro/launch.json', {'status': 'completed' if lifecycle == 'finished' else 'running',
                                                  'finished_at': '2026-09-21T01:05:00+00:00',
                                                  'elapsed_seconds': 321.5 if lifecycle == 'finished' else None})
        for name in ('stdout.jsonl', 'tool-audit.jsonl', 'outbound.jsonl'):
            (directory / 'kiro' / name).write_text('{}\n', encoding='utf-8')
        write_json(directory / 'final/comparison.json', {'numerical_status': numerical,
            'automatic_integrity_status': 'pass', 'normal_gameplay_status': 'pass', 'measurement_status': 'pending',
            'comparisons': {stage: {'passed': 12 if stage == 'jump' else 18,
                                  'required': 12 if stage == 'jump' else 18, 'numerical_status': numerical}}})
        write_json(directory / 'jobs/job-test.json', {'job_id': 'job-test', 'action': 'evaluate',
            'candidate_id': 'candidate-03', 'status': 'succeeded', 'started_at': '2026-09-21T01:00:00+00:00',
            'finished_at': '2026-09-21T01:01:10+00:00'})
        if verdict != 'pending':
            write_json(directory / 'final' / cli.review.DESTINATION, {'status': 'pass',
                'gates': {'independent_source_review': {'status': 'pass'}, 'independent_visual': {'status': 'pass'}}})
        self.verdicts[directory.name] = verdict
        return directory

    def source_record(self, directory, stage='jump', verdict='pass'):
        candidate = read_json(directory / 'candidates/candidate-03/candidate.json')
        path = directory / 'review/source-review.json'
        checks = sorted(cli.review.original.CHECKS[stage])
        value = {'source': 'assistant_visual', 'verdict': verdict,
            'candidate_id': 'candidate-03', 'source_sha256': candidate['source_sha256'],
            'reviewed_at': '2026-09-21T01:06:00+00:00', 'trace_sha256': sha256(directory / 'kiro/stdout.jsonl'),
            'inspected_files': candidate['source_hashes'],
            'findings': [{'check': check, 'verdict': verdict if check == checks[0] else 'pass',
                          'evidence': 'Independent fixture source inspection'} for check in checks]}
        write_json(path, value)
        return path

    def addendum(self, directory, stage='jump'):
        prior = self.source_record(directory, stage)
        candidate_path = directory / 'candidates/candidate-03/candidate.json'
        candidate = read_json(candidate_path)
        evidence = [candidate_path, Path(candidate['project_path']) / 'player.gd',
                    directory / 'kiro/stdout.jsonl', directory / 'kiro/launch.json', prior]
        value = {'schema_version': 1, 'review_kind': 'immutable_source_scope_addendum',
            'finding_id': 'wall-coyote-overwrite-v1', 'verdict': 'fail', 'effective_source_verdict': 'fail',
            'session_id': directory.name, 'stage': stage, 'candidate_id': 'candidate-03',
            'source_sha256': candidate['source_sha256'], 'reviewed_at': '2026-09-21T01:07:00+00:00',
            'affected_checks': ['wall_jump_preserved'],
            'prior_review': {'path': str(prior), 'sha256': sha256(prior), 'verdict': 'pass'},
            'evidence_files': {str(p): sha256(p) for p in evidence},
            'findings': [{'check': 'wall_jump_preserved', 'verdict': 'fail', 'evidence': 'Conditional overwrite in the same callback'}],
            'runtime_confirmation': {'status': 'not_run'}}
        path = directory / 'review/source-review-addendum.json'
        write_json(path, value)
        return path

    def manual_inputs(self, directory, stage='jump'):
        candidate = read_json(directory / 'candidates/candidate-03/candidate.json')
        comparison = read_json(directory / 'final/comparison.json')
        comparison.update(session_id=directory.name, candidate_id='candidate-03',
                          source_sha256=candidate['source_sha256'], runs={})
        observations = {'candidate_id': 'candidate-03', 'source_sha256': candidate['source_sha256'], 'groups': {}}
        for mechanic in (('jump',) if stage == 'jump' else ('ground', 'jump')):
            comparison['runs'][mechanic] = {}; observations['groups'][mechanic] = {}
            for role in ('reference', 'before', 'after'):
                paths = []
                for repeat in range(12 if mechanic == 'jump' else 18):
                    run = directory / 'final' / mechanic / role / str(repeat)
                    manifest = run / 'manifest.json'
                    write_json(manifest, {'provenance': {'full_source_hashes_before': candidate['source_hashes'],
                                                        'full_source_hashes_after': candidate['source_hashes']}})
                    analysis = run / 'analysis.json'
                    write_json(analysis, {'run_id': str(run), 'body_height_px': 16,
                        'frames': [{'x': i, 'y': 0, 'valid': True, 'mirrored': i >= 10} for i in range(240)],
                        'phases': [{'name': 'coast', 'start': 0, 'end': 10}, {'name': 'reversal', 'start': 10, 'end': 240}],
                        'events': {'takeoff_frame': 1, 'apex_frame': 10, 'landing_frame': 18},
                        'provenance': {'manifest_sha256': sha256(manifest)}})
                    paths.append(str(analysis))
                run = Path(paths[0]).parent
                frames = run / 'frames'; frames.mkdir()
                for i in range(21):
                    (frames / f'{i:06d}.png').write_bytes(f'fixture-pixels-{mechanic}-{role}-{i}'.encode())
                points = [{'run': str(run), 'frame_index': i, 'x': i, 'y': 0, 'source': 'assistant_visual',
                    'frame_sha256': sha256(frames / f'{i:06d}.png'),
                    'categories': (['brake'] if i < 10 else ['reverse', 'flip']) if mechanic == 'ground' else []} for i in range(20)]
                events = [{'run': str(run), 'source': 'assistant_visual', 'takeoff_frame': 1, 'apex_frame': 10,
                    'landing_frame': 18, 'frame_hashes': {str(i): sha256(frames / f'{i:06d}.png') for i in range(21)}}] if mechanic == 'jump' else []
                comparison['runs'][mechanic][role] = paths
                observations['groups'][mechanic][role] = {'points': points, 'events': events,
                    'analysis_files': {p: sha256(p) for p in paths}}
        write_json(directory / 'final/comparison.json', comparison)
        observations['comparison_sha256'] = sha256(directory / 'final/comparison.json')
        path = directory / 'review/observations.json'
        write_json(path, observations)
        saved = read_json(directory / 'final' / cli.review.DESTINATION)
        saved['observations_path'] = str(path)
        write_json(directory / 'final' / cli.review.DESTINATION, saved)
        return path, saved

    def verify(self, directory):
        status = self.verdicts.get(Path(directory).name, 'pending')
        result = {'status': status}
        if status == 'pass':
            result['audit_sha256'] = sha256(Path(directory) / 'final' / cli.review.DESTINATION)
        return result

    @staticmethod
    def build_standard(directory):
        folder = Path(directory) / 'report'; (folder / 'media').mkdir(parents=True, exist_ok=True)
        (folder / 'media/native.mp4').write_bytes(b'fixture media only')
        body = ('<p class="pending">Independent acceptance: pending</p><video controls src="media/native.mp4"></video>'
                '<script>const runs=[{"videos":{"reference":"media/native.mp4","before":"media/native.mp4",'
                '"after":"media/native.mp4"}}];const roles=[];</script>')
        (folder / 'index.html').write_text(cli.reporting._document(Path(directory).name, body), encoding='utf-8')
        write_json(folder / 'summary.json', {'status': 'pending'})
        return folder / 'index.html'

    def build(self):
        path = cli.build_report(self.registry_path)
        return path, read_json(path.parent / 'summary.json')

    def test_six_preregistered_views_remain_pending_without_six_completed_reviews(self):
        path, summary = self.build()
        self.assertEqual(summary['status'], 'pending')
        self.assertEqual((summary['passed_stages'], summary['required_stages']), (0, 6))
        self.assertEqual(len(summary['chains']), 3)
        for chain in summary['chains']:
            for stage in chain['stages'].values():
                self.assertTrue((path.parent / stage['report']).is_file())
                self.assertEqual(stage['status'], 'pending')
                self.assertFalse((self.root / 'sessions' / stage['session_id']).exists())
        self.assertEqual(self.registry_verify.call_count, 2)
        self.builder.assert_not_called()

    def test_verified_pass_is_distinct_from_preserved_old_parser_rejection(self):
        directory = self.make_session(verdict='pass')
        write_json(directory / 'final/independent-review.json', {'status': 'fail', 'reason': 'Orphan completion'})
        self.build_standard(directory)
        originals = {p: sha256(p) for p in [directory / 'report/index.html', directory / 'report/summary.json',
                                          directory / 'final/independent-review.json']}
        path, summary = self.build()
        row = summary['chains'][0]['stages']['jump']
        self.assertEqual(row['status'], 'pass')
        self.assertEqual(row['original_review_status'], 'fail')
        self.assertEqual((row['source_review'], row['manual_review']), ('pass', 'pass'))
        self.assertEqual((row['candidate_count'], row['elapsed_seconds']), (3, 321.5))
        self.assertEqual(row['job_durations'][0]['seconds'], 70)
        self.assertEqual(summary['status'], 'pending')
        text = (path.parent / row['report']).read_text(encoding='utf-8')
        self.assertIn('Post-experiment review', text)
        self.assertIn('index.html', text)
        self.assertIn('media/native.mp4', text)
        self.assertTrue(all(sha256(p) == digest for p, digest in originals.items()))
        self.builder.assert_not_called()

    def test_saved_pass_flag_without_successful_replay_is_failure(self):
        self.make_session(verdict='fail')
        _, summary = self.build()
        self.assertEqual(summary['chains'][0]['stages']['jump']['status'], 'fail')
        self.assertEqual(summary['passed_stages'], 0)

    def test_explicit_numerical_failure_is_never_relabeled_success(self):
        self.make_session(verdict='pass', numerical='fail')
        _, summary = self.build()
        self.assertEqual(summary['chains'][0]['stages']['jump']['status'], 'fail')

    def test_live_stage_is_pending_and_does_not_trigger_heavy_replay_or_standard_render(self):
        self.make_session(lifecycle='evaluating')
        _, summary = self.build()
        self.assertEqual(summary['chains'][0]['stages']['jump']['status'], 'pending')
        self.review_verify.assert_not_called()
        self.builder.assert_not_called()

    def test_failed_parent_blocked_child_is_failed_not_an_unattempted_success(self):
        run = {'chains': [{'chain_id': self.registry['chains'][0]['chain_id'],
                          'stages': {'ground': {'status': 'not_run', 'reason': 'jump_not_promotable'}}}]}
        write_json(self.root / 'artifacts/phase2c/cohorts/heart-test/run.json', run)
        _, summary = self.build()
        child = summary['chains'][0]['stages']['ground']
        self.assertEqual(child['status'], 'fail')
        self.assertEqual(child['candidate_count'], 0)
        self.assertEqual(child['reason'], 'jump_not_promotable')

    def test_registry_rejection_precedes_output_creation(self):
        self.registry_verify.side_effect = ValueError('Frozen source changed')
        with self.assertRaisesRegex(ValueError, 'Frozen source'):
            self.build()
        self.assertFalse((self.root / 'artifacts/phase2c/reports').exists())

    def test_registry_is_rechecked_after_packaging(self):
        self.registry_verify.side_effect = [self.registry, ValueError('Registry changed')]
        with self.assertRaisesRegex(ValueError, 'Registry changed'):
            self.build()
        self.assertFalse((self.root / 'artifacts/phase2c/reports/heart-test-posthoc-v2/summary.json').exists())

    def test_original_report_dynamic_video_reference_must_exist(self):
        directory = self.make_session()
        self.build_standard(directory)
        index = directory / 'report/index.html'
        index.write_text(index.read_text().replace('"after":"media/native.mp4"', '"after":"media/missing.mp4"'))
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.build()

    def test_nonlocal_or_escaping_html_media_is_rejected(self):
        directory = self.make_session(); self.build_standard(directory)
        index = directory / 'report/index.html'
        for link in ('file:///private.mp4', '../../outside.mp4', 'https://example.test/remote.mp4', 'media\\native.mp4'):
            index.write_text('<html><main><video src="' + link + '"></video></main></html>', encoding='utf-8')
            with self.subTest(link=link), self.assertRaises(ValueError):
                cli.validate_links(index.parent)

    def test_hardlinked_report_asset_is_rejected(self):
        directory = self.make_session(); self.build_standard(directory)
        asset = directory / 'report/media/native.mp4'
        os.link(asset, directory / 'report/media/linked.mp4')
        with self.assertRaisesRegex(ValueError, 'independent regular'):
            self.build()

    def test_source_report_change_during_copy_is_rejected(self):
        directory = self.make_session(); self.build_standard(directory)
        original_copy = cli._copy
        def changing_copy(source, destination, **kwargs):
            digest = original_copy(source, destination, **kwargs)
            if Path(source) == directory / 'report/index.html':
                with Path(source).open('a', encoding='utf-8') as stream:
                    stream.write('\n<!-- concurrent edit -->')
            return digest
        with patch.object(cli, '_copy', side_effect=changing_copy), self.assertRaisesRegex(ValueError, 'Source report changed'):
            self.build()

    def test_review_receipt_changed_after_replay_does_not_display_pass(self):
        directory = self.make_session(verdict='pass')
        def changed_receipt(path):
            value = self.verify(path)
            write_json(directory / 'final' / cli.review.DESTINATION, {'status': 'changed'})
            return value
        self.review_verify.side_effect = changed_receipt
        with self.assertRaisesRegex(ValueError, 'Replayed review receipt changed'):
            self.build()

    def test_six_finished_reviews_are_replayed_with_maximum_three_concurrent_calls(self):
        for chain in range(3):
            for stage in ('jump', 'ground'):
                self.make_session(chain, stage, verdict='pass')
        lock = threading.Lock(); active = 0; maximum = 0
        def verify(directory):
            nonlocal active, maximum
            with lock:
                active += 1; maximum = max(maximum, active)
            time.sleep(.025)
            result = self.verify(directory)
            with lock:
                active -= 1
            return result
        self.review_verify.side_effect = verify
        _, summary = self.build()
        self.assertEqual(summary['status'], 'pass')
        self.assertEqual(summary['passed_stages'], 6)
        self.assertEqual(self.review_verify.call_count, 6)
        self.assertLessEqual(maximum, 3)
        self.assertGreater(maximum, 1)
        self.assertEqual(summary['review_replay_max_workers'], 3)

    def test_diagnosis_is_copied_and_linked_with_bound_bytes(self):
        source = self.root / 'artifacts/phase2c/cohorts/heart-test/audit-native-completion-diagnosis.json'
        write_json(source, {'original_rejection_preserved': True})
        path, summary = self.build()
        self.assertEqual(summary['copied_source_evidence'][str(source)], sha256(source))
        text = path.read_text(encoding='utf-8')
        self.assertIn(source.name, text)
        self.assertEqual(summary['link_validation']['status'], 'pass')

    def test_existing_bundle_not_overwritten(self):
        path, _ = self.build(); before = sha256(path)
        with self.assertRaisesRegex(ValueError, 'fresh report snapshot'):
            self.build()
        self.assertEqual(sha256(path), before)

    def test_bound_addendum_changes_effective_status_without_rewriting_historical_pass(self):
        directory = self.make_session(verdict='pass')
        addendum = self.addendum(directory)
        audit_path = directory / 'final' / cli.review.DESTINATION
        original = sha256(audit_path)
        path, summary = self.build()
        row = summary['chains'][0]['stages']['jump']
        self.assertEqual((row['status'], row['source_review'], row['numerical_status']), ('fail', 'fail', 'pass'))
        self.assertEqual(row['historical_v2_replay_status'], 'pass')
        self.assertEqual(row['historical_source_review'], 'pass')
        self.assertTrue(row['source_addendum']['valid'])
        self.assertEqual(row['source_addendum']['sha256'], sha256(addendum))
        self.assertEqual(sha256(audit_path), original)
        view = (path.parent / row['report']).read_text(encoding='utf-8')
        self.assertIn('source-review-addendum.json', view)
        self.assertIn('Current independent acceptance', view)

    def test_addendum_fails_closed_for_wrong_source_tampered_proof_or_chronology(self):
        directory = self.make_session(verdict='pass')
        path = self.addendum(directory)
        original = read_json(path)
        state = read_json(directory / 'session.json')
        variants = [dict(original, source_sha256='f' * 64),
                    dict(original, reviewed_at='2026-09-21T01:00:00+00:00'),
                    dict(original, verdict='pass'), dict(original, evidence_files={})]
        for value in variants:
            with self.subTest(value=value):
                write_json(path, value)
                result = cli._source_addendum(directory, state, 'jump')
                self.assertEqual(result['status'], 'fail')
                self.assertFalse(result['valid'])
        write_json(path, original)
        (directory / 'kiro/stdout.jsonl').write_text('changed', encoding='utf-8')
        self.assertFalse(cli._source_addendum(directory, state, 'jump')['valid'])

    def test_ground_with_own_pass_inherits_registered_parent_effective_failure(self):
        parent = self.make_session(verdict='pass')
        self.addendum(parent)
        self.make_session(stage='ground', verdict='pass')
        _, summary = self.build()
        row = summary['chains'][0]['stages']['ground']
        self.assertEqual(row['status'], 'fail')
        self.assertEqual(row['own_acceptance_status'], 'pass')
        self.assertEqual(row['source_review'], 'pass')
        self.assertEqual(row['parent_jump']['binding_status'], 'pass')
        self.assertEqual(row['parent_jump']['effective_acceptance'], 'fail')

    def test_ground_cannot_pass_with_unreviewed_or_different_parent(self):
        self.make_session(verdict='pending')
        ground = self.make_session(stage='ground', verdict='pass')
        row, state, _ = cli._session_summary(ground, 'ground', {}, self.verify(ground))
        parent = self.directory()
        parent_row, parent_state, _ = cli._session_summary(parent, 'jump', {}, self.verify(parent))
        cli._inherit_parent(row, state, parent_row, parent_state, ground)
        self.assertEqual(row['status'], 'pending')
        state['parent']['source_sha256'] = '0' * 64
        row['status'] = 'pass'
        cli._inherit_parent(row, state, dict(parent_row, status='pass'), parent_state, ground)
        self.assertEqual(row['status'], 'fail')
        self.assertEqual(row['parent_jump']['binding_status'], 'fail')

    def test_original_bound_failed_source_review_surfaces_even_when_audit_has_no_gates(self):
        directory = self.make_session(verdict='fail')
        source = self.source_record(directory, verdict='fail')
        write_json(directory / 'final' / cli.review.DESTINATION, {'status': 'fail', 'source_review_path': str(source)})
        _, summary = self.build()
        row = summary['chains'][0]['stages']['jump']
        self.assertEqual(row['bound_source_record_verdict'], 'fail')
        self.assertEqual(row['source_review'], 'fail')
        self.assertEqual(row['status'], 'fail')

    def test_source_failure_and_fresh_visual_pass_are_independent_axes(self):
        directory = self.make_session(verdict='fail')
        self.source_record(directory, verdict='fail')
        observations, _ = self.manual_inputs(directory)
        _, summary = self.build()
        row = summary['chains'][0]['stages']['jump']
        self.assertEqual((row['status'], row['source_review'], row['manual_review']), ('fail', 'fail', 'pass'))
        self.assertEqual(row['historical_v2_replay_status'], 'fail')
        self.assertEqual(row['historical_v2_manual_gate'], 'pending')
        self.assertEqual(set(row['independent_visual_replay']['groups']), {'jump/reference', 'jump/before', 'jump/after'})
        self.assertEqual(row['independent_visual_replay']['observations_sha256'], sha256(observations))

    def test_visual_replay_rejects_wrong_candidate_hash_comparison_or_missing_group(self):
        directory = self.make_session(verdict='fail')
        path, saved = self.manual_inputs(directory)
        state, original = read_json(directory / 'session.json'), read_json(path)
        variants = [dict(original, candidate_id='candidate-99'), dict(original, source_sha256='f'*64),
                    dict(original, comparison_sha256='f'*64), dict(original, groups={})]
        for value in variants:
            with self.subTest(value=value):
                write_json(path, value)
                checked = cli._visual_replay(directory, state, 'jump', saved)
                self.assertEqual(checked['status'], 'fail')

    def test_visual_replay_keeps_point_event_and_raw_hash_requirements(self):
        directory = self.make_session(verdict='fail')
        path, saved = self.manual_inputs(directory)
        state = read_json(directory / 'session.json')
        for mode in ('points', 'events', 'analysis_hash', 'raw_hash'):
            value = read_json(path)
            original = json.loads(json.dumps(value))
            group = value['groups']['jump']['after']
            if mode == 'points': group['points'] = group['points'][:19]
            if mode == 'events': group['events'] = []
            if mode == 'analysis_hash': group['analysis_files'][next(iter(group['analysis_files']))] = 'f'*64
            if mode == 'raw_hash': group['points'][0]['frame_sha256'] = 'f'*64
            write_json(path, value)
            with self.subTest(mode=mode):
                self.assertEqual(cli._visual_replay(directory, state, 'jump', saved)['status'], 'fail')
            write_json(path, original)

    def test_ground_visual_replay_requires_brake_reverse_and_flip_categories(self):
        self.make_session(verdict='pass')
        directory = self.make_session(stage='ground', verdict='fail')
        path, saved = self.manual_inputs(directory, 'ground')
        state = read_json(directory / 'session.json')
        checked = cli._visual_replay(directory, state, 'ground', saved)
        self.assertEqual(checked['status'], 'pass', checked.get('reason'))
        self.assertEqual(len(checked['groups']), 6)
        value = read_json(path)
        for point in value['groups']['ground']['after']['points']:
            point['categories'] = [c for c in point['categories'] if c != 'flip']
        write_json(path, value)
        self.assertEqual(cli._visual_replay(directory, state, 'ground', saved)['status'], 'fail')


class CommandTests(unittest.TestCase):
    def test_audit_stdout_is_compact_and_full_saved_record_is_not_changed(self):
        saved = {'status': 'pass', 'audit_version': 'review-version', 'candidate_id': 'candidate-02',
                 'source_sha256': 'a' * 64, 'evidence_files': {f'path-{i}': 'b' * 64 for i in range(10000)}}
        capture = io.StringIO()
        with patch.object(cli.review, 'audit_session', return_value=saved), redirect_stdout(capture):
            self.assertEqual(cli.main(['audit', 'session', '--observations', 'points.json', '--source-review', 'source.json']), 0)
        output = json.loads(capture.getvalue())
        self.assertEqual(set(output), {'status', 'audit_version', 'candidate_id', 'source_sha256', 'receipt_path'})
        self.assertEqual(output['candidate_id'], 'candidate-02')
        self.assertLess(len(capture.getvalue()), 1000)
        self.assertEqual(len(saved['evidence_files']), 10000)

    def test_audit_delegates_exact_paths_and_returns_failure_exit(self):
        output = io.StringIO()
        with patch.object(cli.review, 'audit_session', return_value={'status': 'fail', 'reason': 'Bound proof failed'}) as audit, redirect_stdout(output):
            code = cli.main(['audit', 'session', '--observations', 'points.json', '--source-review', 'source.json',
                             '--original-rejection', 'diagnosis.json'])
        self.assertEqual(code, 2)
        self.assertEqual(audit.call_args.args, (Path('session').resolve(), Path('points.json').resolve(), Path('source.json').resolve()))
        self.assertEqual(audit.call_args.kwargs['original_rejection_path'], Path('diagnosis.json').resolve())
        self.assertEqual(json.loads(output.getvalue())['reason'], 'Bound proof failed')

    def test_verify_never_creates_audit_receipt(self):
        with patch.object(cli.review, 'verify_review', return_value={'status': 'pending'}) as verify, \
             patch.object(cli.review, 'audit_session', side_effect=AssertionError('No write')), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['verify', 'session']), 0)
        verify.assert_called_once_with(Path('session').resolve())

    def test_report_identifier_resolves_frozen_registry_without_running_cohort(self):
        with patch.object(cli, 'build_report', return_value=Path('snapshot/index.html')) as build, \
             patch.object(cli.cohort, 'run_cohort', side_effect=AssertionError('No model')), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['report', 'heart-test']), 0)
        build.assert_called_once_with(cli.ROOT / 'config/phase2c/cohorts/heart-test.json', output=None)


if __name__ == '__main__':
    unittest.main()
