"""Reference gate binding checks using small JSON only, never CV or a browser.

The raw-pack loader and numerical comparator are isolated protocol doubles.
These tests establish acceptance wiring/integrity, not visual adequacy.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.reference_acceptance import validate_reference_gate
from kiroclops.local_engine.qualification import analyzer_sources as protocol_source_inventory
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.web_local.specs import scenarios


class ReferenceAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='reference gate café ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / 'raw-manifest.json'
        write_json(self.manifest, {'kind': 'TestRawManifest', 'scope': 'loader-isolated JSON fixture'})
        self.manifest_sha = file_sha256(self.manifest)
        self.mirror = self.root / 'source-mirror'
        self.sources = {}
        # Follow the current production inventory, including the gate producer;
        # do not let a new required module disappear behind a frozen test list.
        self.required_source_names = tuple(protocol_source_inventory())
        for name in sorted(set(self.required_source_names) | {
                'kiroclops/local_engine/web_reference.py',
                'kiroclops/local_engine/session.py', 'artifacts/gate-producer.py'}):
            path = self.mirror / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixed test source\n', encoding='utf-8')
            self.sources[name] = file_sha256(path)
        self.splits = {}
        self.rows = []
        self.app_sources = {'app.js': digest('fixed-test-application')}
        for domain in ('panel', 'svg'):
            for split in ('calibration', 'validation'):
                raw = []
                for scenario in scenarios(domain, split):
                    package_sha = digest({'domain': domain, 'scenario': scenario})
                    record = {'scenario': scenario, 'package': str(self.root / 'raw' / domain / scenario['id']),
                        'reference_package_sha256': package_sha, 'source_sha256': digest(self.app_sources),
                        'timing': {'status': 'pass'}, 'functional': {'status': 'pass'}}
                    raw.append(record)
                    directory = self.root / 'measurement' / domain / scenario['id']
                    selection = {'schema_version': 1, 'kind': 'ObjectSelection', 'package_sha256': package_sha,
                        'objects': [{'id': 'object', 'roi': [4, 5, 10, 10], 'anchor': [5, 5]}]}
                    observations = {'schema_version': 1, 'kind': 'ObservationSet', 'package_sha256': package_sha,
                        'selection': selection, 'objects': [{'id': 'object', 'frames': [{'x': 5, 'y': 5}]}]}
                    events = {'schema_version': 1, 'kind': 'EventSet', 'package_sha256': package_sha,
                        'observation_sha256': digest(observations), 'events': []}
                    analysis = {'kind': 'AnalysisResult', 'schema_version': 1, 'measurement_status': 'ok',
                        'package_sha256': package_sha, 'candidate_sha256': record['source_sha256'],
                        'observations': observations, 'events': events}
                    comparison = self.replay({**record, 'observations': observations, 'events': events, 'selection': selection})
                    write_json(directory / 'selection.json', selection)
                    write_json(directory / 'analysis.json', analysis)
                    write_json(directory / 'self-comparison.json', comparison)
                    row = {'domain': domain, 'split': split, 'scenario_id': scenario['id'],
                        'status': 'pass', 'camera_status': 'pass', 'temporal_support_status': 'pass',
                        'comparator_self_consistency_status': 'pass', 'errors': [],
                        'reference_pack_sha256': self.manifest_sha, 'reference_package_sha256': package_sha,
                        'scenario_sha256': digest(scenario), 'analysis_path': str(directory / 'analysis.json'),
                        'analysis_sha256': file_sha256(directory / 'analysis.json'),
                        'observations_sha256': digest(observations), 'events_sha256': digest(events),
                        'self_comparison_path': str(directory / 'self-comparison.json'),
                        'self_comparison_sha256': file_sha256(directory / 'self-comparison.json'),
                        'evidence_hashes': {str(path): file_sha256(path) for path in directory.glob('*.json')}}
                    self.rows.append(row)
                self.splits[domain, split] = raw
        self.proof = {'kind': 'ReferenceObservabilityGateV2', 'status': 'pass', 'fatal_error': None,
            'count': 32, 'passed': 32, 'actual_comparator_self_consistency_passed': 32, 'required_count': 32,
            'manifest_sha256': self.manifest_sha, 'source_hashes': deepcopy(self.sources),
            'completed_source_hashes': deepcopy(self.sources), 'source_binding_status': 'pass', 'records': self.rows}
        self.gate = self.root / 'gate.json'
        self.save()
        bindings = [
            patch('kiroclops.local_engine.reference_acceptance.ROOT', self.mirror),
            patch('kiroclops.local_engine.qualification.analyzer_sources',
                  side_effect=lambda: {name: self.sources[name] for name in self.required_source_names}),
            patch('kiroclops.local_engine.session.source_hashes', return_value=self.app_sources),
            patch('kiroclops.local_engine.web_reference.verify_reference_pack', side_effect=self.verify_pack),
            patch('kiroclops.local_engine.web_reference.check_reference_comparison_coverage', side_effect=self.replay),
            patch('kiroclops.visual.tracking.analyze_package', side_effect=AssertionError('CV is forbidden in gate replay')),
            patch('kiroclops.local_engine.backend.LocalAnalysisBackend.submit', side_effect=AssertionError('Worker execution is forbidden')),
            patch('kiroclops.web_local.capture.capture_run', side_effect=AssertionError('Browser recording is forbidden')),
        ]
        self.mocks = [binding.start() for binding in bindings]
        for binding in bindings:
            self.addCleanup(binding.stop)

    @staticmethod
    def replay(record):
        return {'kind': 'ReferenceComparatorSelfConsistency', 'status': 'pass',
            'scenario_id': record['scenario']['id'],
            'reference_package_sha256': record['observations']['package_sha256'],
            'reference_observations_sha256': digest(record['observations'])}

    def load_split(self, manifest, domain, split, *, expected_source_hashes, expected_scenarios):
        self.assertEqual(Path(manifest), self.manifest)
        self.assertEqual(expected_source_hashes, self.app_sources)
        self.assertEqual(expected_scenarios, scenarios(domain, split))
        return deepcopy(self.splits[domain, split])

    def verify_pack(self, manifest, *, include_splits=False):
        self.assertEqual(Path(manifest), self.manifest)
        self.assertTrue(include_splits)
        verified = {(domain, split): self.load_split(manifest, domain, split,
            expected_source_hashes=self.app_sources,
            expected_scenarios=scenarios(domain, split))
            for domain in ('panel', 'svg') for split in ('calibration', 'validation')}
        return {'manifest_sha256': self.manifest_sha,
                'verified_records': sum(map(len, verified.values())),
                'verified_splits': verified}

    def save(self):
        write_json(self.gate, self.proof)

    def reject(self):
        with self.assertRaises(VisualError) as error:
            validate_reference_gate(self.manifest, self.gate)
        self.assertEqual(error.exception.code, 'reference_not_qualified')

    def rebind_analysis(self, mutate):
        row = self.rows[0]
        path = Path(row['analysis_path'])
        analysis = read_json(path)
        mutate(analysis)
        write_json(path, analysis)
        row['analysis_sha256'] = row['evidence_hashes'][str(path)] = file_sha256(path)
        row['observations_sha256'] = digest(analysis['observations'])
        row['events_sha256'] = digest(analysis['events'])
        self.save()

    def test_missing_gate_rejected_without_comparator_or_pixel_work(self):
        with self.assertRaises(VisualError) as error:
            validate_reference_gate(self.manifest, None)
        self.assertEqual(error.exception.code, 'reference_not_qualified')
        self.mocks[4].assert_not_called()

    def test_old_kind_cannot_supply_32_boolean_pass_flags(self):
        self.proof['kind'] = 'ReferenceObservabilityGate'
        self.save()
        self.reject()

    def test_thirty_one_rows_rejected_even_when_counters_claim_32(self):
        self.rows.pop()
        self.save()
        self.reject()

    def test_duplicate_scenario_cannot_replace_missing_row(self):
        self.rows[-1] = deepcopy(self.rows[0])
        self.save()
        self.reject()

    def test_wrong_raw_corpus_rejected(self):
        self.proof['manifest_sha256'] = '0' * 64
        self.save()
        self.reject()

    def test_wrong_package_row_rejected(self):
        self.rows[0]['reference_package_sha256'] = '0' * 64
        self.save()
        self.reject()

    def test_changed_current_source_rejected(self):
        (self.mirror / self.required_source_names[0]).write_text('changed source\n')
        self.reject()

    def test_a_missing_required_source_cannot_be_hidden_by_matching_summary_hashes(self):
        missing = 'kiroclops/local_engine/reference_observability.py'
        self.assertIn(missing, self.required_source_names)
        self.proof['source_hashes'].pop(missing)
        self.proof['completed_source_hashes'].pop(missing)
        self.save()
        self.reject()

    def test_changed_selection_bytes_rejected(self):
        selection = Path(self.rows[0]['analysis_path']).parent / 'selection.json'
        value = read_json(selection)
        value['objects'][0]['roi'][0] += 1
        write_json(selection, value)
        self.reject()

    def test_changed_analysis_bytes_rejected(self):
        path = Path(self.rows[0]['analysis_path'])
        value = read_json(path)
        value['observations']['objects'][0]['frames'][0]['x'] += 1
        write_json(path, value)
        self.reject()

    def test_observations_must_match_raw_package_even_after_receipt_rehash(self):
        self.rebind_analysis(lambda analysis: analysis['observations'].update(package_sha256='0' * 64))
        self.reject()

    def test_foreign_selection_rehash_does_not_match_original_observations(self):
        row = self.rows[0]
        selection = Path(row['analysis_path']).parent / 'selection.json'
        value = read_json(selection)
        value['package_sha256'] = '0' * 64
        write_json(selection, value)
        row['evidence_hashes'][str(selection)] = file_sha256(selection)
        self.save()
        self.reject()

    def test_eventset_must_bind_the_exact_observations(self):
        self.rebind_analysis(lambda analysis: analysis['events'].update(observation_sha256='0' * 64))
        self.reject()

    def test_eventset_must_bind_the_actual_raw_package(self):
        self.rebind_analysis(lambda analysis: analysis['events'].update(package_sha256='0' * 64))
        self.reject()

    def test_worker_result_must_bind_the_reference_source(self):
        self.rebind_analysis(lambda analysis: analysis.update(candidate_sha256='0' * 64))
        self.reject()

    def test_failed_current_comparator_cannot_reuse_stored_pass(self):
        self.mocks[4].side_effect = lambda record: {**self.replay(record), 'status': 'fail'}
        self.reject()

    def test_complete_32_rows_replay_once_each_without_cv(self):
        result = validate_reference_gate(self.manifest, self.gate)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['records'], 32)
        self.assertEqual(result['sha256'], file_sha256(self.gate))
        self.assertEqual(result['manifest_sha256'], self.manifest_sha)
        self.assertEqual(self.mocks[3].call_count, 1)
        self.assertEqual(self.mocks[4].call_count, 32)
        for forbidden in self.mocks[5:]:
            forbidden.assert_not_called()
        self.assertIn('no transfer', result['scope'])

    def test_cohort_binding_requires_gate_and_keeps_its_hash(self):
        from kiroclops.local_engine.cohort import web_reference_binding
        with self.assertRaises(VisualError):
            web_reference_binding(self.manifest)
        value = web_reference_binding(self.manifest, self.gate)
        self.assertEqual(self.mocks[3].call_count, 1)
        self.assertEqual(value['observability']['sha256'], file_sha256(self.gate))
        self.assertEqual(value['manifest_sha256'], self.manifest_sha)

    def test_gate_rejects_incomplete_or_foreign_full_pack_replay(self):
        original = self.mocks[3].side_effect
        for changed in ({'verified_records': 31}, {'manifest_sha256': '0' * 64}):
            with self.subTest(changed=changed):
                self.mocks[3].side_effect = lambda path, *, include_splits: {
                    **original(path, include_splits=include_splits), **changed}
                self.reject()
        self.mocks[3].side_effect = original

    def test_cohort_cannot_create_registration_before_missing_gate_rejection(self):
        from kiroclops.local_engine.cohort import register
        output = self.root / 'registration'
        with patch('kiroclops.local_engine.cohort.validate_qualification', return_value={}):
            with self.assertRaises(VisualError) as error:
                register(output, self.root / 'qualification.json', web_references=self.manifest)
        self.assertEqual(error.exception.code, 'reference_not_qualified')
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
