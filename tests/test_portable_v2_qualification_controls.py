"""Known wall/coyote qualification fixtures; native execution is always mocked."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.integration import storage
from kiroclops.portable_v2 import integrity, qualification_controls as controls


class QualificationControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'original'
        self.original.mkdir()
        (self.original / 'player.gd').write_text('original protected controller\n', encoding='utf-8')
        (self.original / 'DefaultMovementData.tres').write_text('original resource\n', encoding='utf-8')
        self.original_sources = storage.project_hashes(self.original)
        self.output = self.root / 'controls'
        self.expected_controls = {}
        self.historical = {}
        for (stage, chain), (candidate_id, _) in controls.CONTROLS.items():
            project = self.root / f'sessions/heart-heart-02-{chain}-{stage}/candidates/{candidate_id}/project'
            project.mkdir(parents=True)
            (project / 'player.gd').write_text(f'fixture {stage} {chain}\n', encoding='utf-8')
            (project / 'DefaultMovementData.tres').write_text('original resource\n', encoding='utf-8')
            self.historical[stage, chain] = project
            self.expected_controls[stage, chain] = candidate_id, controls.digest(storage.project_hashes(project))
        self.source_maps = {str(project): storage.project_hashes(project) for project in self.historical.values()}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(controls, 'ROOT', self.root))
        self.stack.enter_context(patch.object(controls, 'CONTROLS', self.expected_controls))
        self.run = self.stack.enter_context(patch.object(integrity, 'run_checks', side_effect=self.run_checks))
        self.replay = self.stack.enter_context(patch.object(integrity, 'verify_checks', side_effect=self.verify_checks))

    def run_checks(self, candidate, baseline, directory, stage, *, cancel=None):
        """Only create tiny deterministic evidence files, never engine input/output."""
        chain = directory.name.rsplit('-', 1)[1]
        status = 'pass' if chain == '02' else 'fail'
        sources = {'baseline': storage.project_hashes(baseline), 'candidate': storage.project_hashes(candidate)}
        component = directory / 'wall-coyote/comparison.json'
        storage.write_json(component, {'stage': stage, 'status': status, 'sources': sources})
        raw = directory / 'wall-coyote/raw-frame.fixture'
        raw.write_bytes(('mock-native-raw-' + stage + chain).encode())
        storage.write_json(directory / 'native-proof.fixture.json', {'raw_path': str(raw), 'raw_sha256': storage.sha256(raw)})
        result = {'stage': stage, 'status': status, 'sources': sources,
                  'wall_coyote': {'path': str(component), 'sha256': storage.sha256(component)}}
        storage.write_json(directory / 'comparison.json', result)
        return result

    @staticmethod
    def verify_checks(directory, *, expected_candidate_sources, expected_baseline_sources):
        """Model the strict native verifier boundary, including changed raw bytes."""
        proof = storage.read_json(directory / 'native-proof.fixture.json')
        result = storage.read_json(directory / 'comparison.json')
        raw = Path(proof['raw_path'])
        valid = (raw.is_file() and storage.sha256(raw) == proof['raw_sha256']
                 and result['sources'] == {'baseline': expected_baseline_sources, 'candidate': expected_candidate_sources})
        return {'status': 'pass' if valid else 'fail', 'preservation_status': result['status']}

    def collect(self, **kwargs):
        return Path(controls.collect_controls(self.original, self.output, progress=lambda _: None, **kwargs))

    def verify(self, path=None, **kwargs):
        return controls.verify_controls(path or self.output / 'controls.json',
                                        expected_original_sources=kwargs.get('sources', self.original_sources))

    def mutate(self, change):
        path = self.output / 'controls.json'
        receipt = storage.read_json(path)
        change(receipt)
        storage.write_json(path, receipt)
        return receipt

    def rebind_comparisons(self, receipt, row):
        directory = Path(row['path'])
        component = directory / 'wall-coyote/comparison.json'
        result = storage.read_json(directory / 'comparison.json')
        result['wall_coyote'] = {'path': str(component), 'sha256': storage.sha256(component)}
        storage.write_json(directory / 'comparison.json', result)
        row['comparison_sha256'] = storage.sha256(directory / 'comparison.json')
        row['wall_comparison_sha256'] = storage.sha256(component)
        storage.write_json(self.output / 'controls.json', receipt)

    def test_collects_exact_six_known_sources_and_expected_verdicts(self):
        path = self.collect()
        receipt = storage.read_json(path)
        self.assertEqual(len(self.run.call_args_list), 6)
        self.assertEqual({(r['stage'], r['chain']) for r in receipt['controls']}, set(self.expected_controls))
        self.assertEqual({r['id']: r['expected'] for r in receipt['controls']},
                         {f'{stage}-{chain}': 'pass' if chain == '02' else 'fail' for stage, chain in self.expected_controls})
        self.assertIs(receipt['engineering_only'], True)
        self.assertIs(receipt['model_input'], False)
        result = self.verify(path)
        self.assertEqual(result['status'], 'pass', result.get('reason'))
        self.assertEqual((result['count'], result['required']), (6, 6))
        self.assertEqual(len(self.replay.call_args_list), 6)

    def test_original_and_historical_sources_are_unchanged_and_only_copies_run(self):
        self.collect()
        self.assertEqual(storage.project_hashes(self.original), self.original_sources)
        self.assertEqual({str(p): storage.project_hashes(p) for p in self.historical.values()}, self.source_maps)
        for call in self.run.call_args_list:
            candidate, baseline, directory, stage = call.args
            self.assertTrue(candidate.is_relative_to(self.output / 'controllers'))
            self.assertNotIn(candidate, self.historical.values())
            self.assertTrue(directory.is_relative_to(self.output / 'runs'))
            self.assertEqual(baseline, self.original if stage == 'jump' else self.output / 'controllers/jump-02')

    def test_ground_controls_use_protected_jump02_parent_even_for_bad01_and03(self):
        self.collect()
        expected = storage.project_hashes(self.output / 'controllers/jump-02')
        self.assertEqual(controls.digest(expected), self.expected_controls['jump', '02'][1])
        result = self.verify()
        self.assertEqual(result['status'], 'pass')
        ground_calls = [c for c in self.replay.call_args_list if c.args[0].name.startswith('ground-')]
        self.assertEqual(len(ground_calls), 3)
        self.assertTrue(all(c.kwargs['expected_baseline_sources'] == expected for c in ground_calls))

    def test_changed_pinned_historical_source_rejected_before_any_native_call(self):
        (self.historical['jump', '01'] / 'player.gd').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'historical control source changed'):
            self.collect()
        self.run.assert_not_called()

    def test_copy_corruption_rejected_before_native_calls(self):
        real_copy = controls.copy_project
        def corrupt(source, destination):
            copied = real_copy(source, destination)
            (copied / 'player.gd').write_text('bad copy', encoding='utf-8')
            return copied
        with patch.object(controls, 'copy_project', side_effect=corrupt), self.assertRaisesRegex(ValueError, 'copy changed'):
            self.collect()
        self.run.assert_not_called()

    def test_existing_and_source_overlapping_output_are_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'new output'):
            self.collect()
        with self.assertRaisesRegex(ValueError, 'separate'):
            controls.collect_controls(self.original, self.original / 'nested')
        self.run.assert_not_called()

    def test_cancel_token_forwarded_and_native_failure_leaves_no_success_receipt(self):
        token = threading.Event()
        self.collect(cancel=token)
        self.assertTrue(all(c.kwargs['cancel'] is token for c in self.run.call_args_list))
        self.output = self.root / 'interrupted'
        self.run.side_effect = RuntimeError('cancelled native fixture')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.collect(cancel=token)
        self.assertFalse((self.output / 'controls.json').exists())

    def test_missing_wall_component_cannot_produce_receipt(self):
        def incomplete(candidate, baseline, directory, stage, *, cancel=None):
            storage.write_json(directory / 'comparison.json', {'stage': stage, 'status': 'fail'})
            return {'status': 'fail'}
        self.run.side_effect = incomplete
        with self.assertRaisesRegex(ValueError, 'Missing wall/coyote'):
            self.collect()
        self.assertFalse((self.output / 'controls.json').exists())

    def test_original_baseline_binding_rejects_changed_bytes_or_wrong_expected_map(self):
        self.collect()
        self.assertEqual(self.verify(sources={'player.gd': '0'*64})['status'], 'fail')
        (self.original / 'player.gd').write_text('changed', encoding='utf-8')
        self.assertEqual(self.verify()['status'], 'fail')

    def test_visibility_schema_and_version_must_match_exactly(self):
        self.collect()
        path = self.output / 'controls.json'; original = storage.read_json(path)
        for changes in ({'version': 'other'}, {'schema_version': 2}, {'engineering_only': False}, {'model_input': True}):
            storage.write_json(path, dict(original, **changes))
            with self.subTest(changes=changes):
                self.assertEqual(self.verify()['status'], 'fail')

    def test_missing_duplicate_and_swapped_control_identities_are_rejected(self):
        self.collect(); path = self.output / 'controls.json'; original = storage.read_json(path)
        variants = []
        missing = deepcopy(original); missing['controls'].pop(); variants.append(missing)
        duplicate = deepcopy(original); duplicate['controls'][-1] = deepcopy(duplicate['controls'][0]); variants.append(duplicate)
        renamed = deepcopy(original); renamed['controls'][0]['id'] = 'jump-02'; variants.append(renamed)
        expected = deepcopy(original); expected['controls'][0]['expected'] = 'pass'; variants.append(expected)
        for value in variants:
            storage.write_json(path, value)
            with self.subTest(value=value['controls'][0]):
                self.assertEqual(self.verify()['status'], 'fail')

    def test_candidate_path_swap_and_source_change_are_rejected(self):
        self.collect(); path = self.output / 'controls.json'; original = storage.read_json(path)
        changed = deepcopy(original)
        changed['controls'][0]['candidate_project'] = changed['controls'][1]['candidate_project']
        storage.write_json(path, changed)
        self.assertEqual(self.verify()['status'], 'fail')
        storage.write_json(path, original)
        candidate = Path(original['controls'][0]['candidate_project'])
        (candidate / 'player.gd').write_text('other source', encoding='utf-8')
        self.assertEqual(self.verify()['status'], 'fail')

    def test_ground_cannot_use_original_or_bad_jump_parent(self):
        self.collect(); path = self.output / 'controls.json'; original = storage.read_json(path)
        for baseline in (self.original, self.output / 'controllers/jump-01'):
            value = deepcopy(original)
            next(r for r in value['controls'] if r['stage'] == 'ground')['baseline_project'] = str(baseline)
            storage.write_json(path, value)
            with self.subTest(baseline=baseline):
                self.assertEqual(self.verify()['status'], 'fail')

    def test_run_path_swap_and_changed_comparison_hash_are_rejected(self):
        self.collect(); path = self.output / 'controls.json'; original = storage.read_json(path)
        changed = deepcopy(original); changed['controls'][0]['path'] = changed['controls'][1]['path']
        storage.write_json(path, changed)
        self.assertEqual(self.verify()['status'], 'fail')
        storage.write_json(path, original)
        comparison = Path(original['controls'][0]['path']) / 'comparison.json'
        comparison.write_text('{}', encoding='utf-8')
        self.assertEqual(self.verify()['status'], 'fail')

    def test_component_source_or_stage_substitution_fails_even_when_hashes_rebound(self):
        self.collect(); path = self.output / 'controls.json'; original = storage.read_json(path)
        row = original['controls'][0]; component = Path(row['path']) / 'wall-coyote/comparison.json'
        wall = storage.read_json(component)
        for changed in (dict(wall, stage='ground'), dict(wall, sources={'candidate': {}, 'baseline': {}})):
            receipt = deepcopy(original)
            storage.write_json(component, changed)
            self.rebind_comparisons(receipt, receipt['controls'][0])
            self.assertEqual(self.verify()['status'], 'fail')

    def test_composite_failure_does_not_substitute_for_wall_component_failure(self):
        self.collect(); receipt = storage.read_json(self.output / 'controls.json')
        row = next(r for r in receipt['controls'] if r['id'] == 'jump-01')
        component = Path(row['path']) / 'wall-coyote/comparison.json'
        value = storage.read_json(component); value['status'] = 'pass'
        storage.write_json(component, value)
        self.rebind_comparisons(receipt, row)
        result = self.verify()
        self.assertEqual(result['status'], 'fail')
        self.assertIn('gate itself', result['reason'])

    def test_protected02_requires_both_composite_and_wall_pass(self):
        self.collect(); receipt = storage.read_json(self.output / 'controls.json')
        row = next(r for r in receipt['controls'] if r['id'] == 'ground-02')
        component = Path(row['path']) / 'wall-coyote/comparison.json'
        value = storage.read_json(component); value['status'] = 'fail'
        storage.write_json(component, value)
        self.rebind_comparisons(receipt, row)
        self.assertEqual(self.verify()['status'], 'fail')

    def test_composite_must_link_exact_wall_component(self):
        self.collect(); receipt = storage.read_json(self.output / 'controls.json')
        row = receipt['controls'][0]; path = Path(row['path']) / 'comparison.json'
        value = storage.read_json(path); value['wall_coyote']['path'] = str(self.root / 'other.json')
        storage.write_json(path, value); row['comparison_sha256'] = storage.sha256(path)
        storage.write_json(self.output / 'controls.json', receipt)
        result = self.verify()
        self.assertEqual(result['status'], 'fail')
        self.assertIn('not bound', result['reason'])

    def test_changed_raw_native_evidence_is_rejected_through_fresh_replay(self):
        self.collect()
        (self.output / 'runs/ground-03/wall-coyote/raw-frame.fixture').write_bytes(b'changed raw bytes')
        result = self.verify()
        self.assertEqual(result['status'], 'fail')
        self.assertIn('native/source replay failed', result['reason'])
        self.assertEqual(len(self.replay.call_args_list), 6)

    def test_native_replay_unverified_or_wrong_preservation_verdict_cannot_pass(self):
        self.collect()
        for response in ({'status': 'unverified', 'preservation_status': 'fail'},
                         {'status': 'pass', 'preservation_status': 'pass'}):
            self.replay.side_effect = None; self.replay.return_value = response
            with self.subTest(response=response):
                self.assertEqual(self.verify()['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
