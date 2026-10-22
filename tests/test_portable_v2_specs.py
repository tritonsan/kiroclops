"""Version isolation and preservation of both historical source maps."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.storage import IntegrationError, read_json, sha256, write_json
from kiroclops.portable import specs as previous
from kiroclops.portable_v2 import specs


class SourceFreezeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='KiroClops frozen sources ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.old61 = self.make_sources('kiroclops/frozen_legacy', 61)
        self.old20 = self.make_sources('kiroclops/portable', 20)
        write_json(self.root / 'artifacts/model-trials/autonomous-v4-cohort.json',
                   {'source_hashes': self.old61})
        write_json(self.root / 'config/phase2c/cohorts/heart-02.json',
                   {'protocol_id': 'heart-transfer-chain-v1', 'protocol_sources': self.old20})
        self.registry_path = self.root / 'config/phase2c/cohorts/heart-02.json'
        # The production registry has a literal historical digest. Isolate its
        # identity check here while hashing every test source from real bytes.
        self.hash_patch = patch.object(specs, 'sha256', side_effect=lambda path:
            '59ff1fd9787597a98b36ae48791271e412676272438cc3e4fdbbfa9a5e22300d'
            if Path(path) == self.registry_path else sha256(path))
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        mocked = patch.object(specs, 'ROOT', self.root)
        mocked.start()
        self.addCleanup(mocked.stop)

    def make_sources(self, directory, count):
        result = {}
        for index in range(count):
            relative = f'{directory}/frozen_{index:02}.py'
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'# Original source {directory}/{index}\n', encoding='utf-8')
            result[relative] = sha256(path)
        return result

    def test_both_complete_historical_maps_are_returned(self):
        self.assertEqual(specs.verify_legacy(), {**self.old61, **self.old20})
        self.assertEqual(len(specs.verify_legacy()), 81)

    def test_either_old_generation_mutation_is_rejected(self):
        for mapping in (self.old61, self.old20):
            path = self.root / next(iter(mapping))
            original = path.read_bytes()
            with self.subTest(path=path):
                path.write_bytes(original + b'# Mutation\n')
                with self.assertRaises(IntegrationError):
                    specs.verify_legacy()
                path.write_bytes(original)

    def test_missing_previous_portable_source_is_rejected(self):
        (self.root / next(iter(self.old20))).unlink()
        with self.assertRaises(IntegrationError):
            specs.verify_legacy()

    def test_incomplete_previous_portable_registry_is_rejected(self):
        path = self.root / 'config/phase2c/cohorts/heart-02.json'
        value = read_json(path)
        value['protocol_sources'].pop(next(iter(self.old20)))
        write_json(path, value)
        with self.assertRaises(IntegrationError):
            specs.verify_legacy()

    def test_unpinned_previous_registry_cannot_supply_a_new_source_map(self):
        self.hash_patch.stop()
        with self.assertRaises(IntegrationError) as caught:
            specs.verify_legacy()
        self.assertEqual(caught.exception.code, 'legacy_registry_changed')


class ExperimentIdentityTests(unittest.TestCase):
    def test_explicit_v2_experiment_identity_keeps_existing_motion_thresholds(self):
        for stage in ('jump', 'ground'):
            with self.subTest(stage=stage):
                new, old = specs.experiment(stage), previous.experiment(stage)
                self.assertEqual(new['experiment_id'], f'heart-{stage}-v2')
                self.assertEqual(new['schema_version'], 5)
                self.assertNotEqual(new['definition_sha256'], old['definition_sha256'])
                self.assertEqual(new['acceptance'], old['acceptance'])
                for key in ('calibration', 'validation', 'max_candidates', 'max_seconds'):
                    self.assertEqual(new[key], old[key])

    def test_v2_output_roots_are_separate(self):
        self.assertNotEqual(specs.CONFIG, previous.CONFIG)
        self.assertNotEqual(specs.ARTIFACTS, previous.ARTIFACTS)
        self.assertEqual(specs.CONFIG.name, 'phase2c-v2')
        self.assertEqual(specs.ARTIFACTS.name, 'phase2c-v2')

    def test_new_source_map_binds_v2_modules_and_preserves_old_capture_adapter(self):
        sources = specs.protocol_sources()
        self.assertIn('kiroclops/portable_v2/session.py', sources)
        self.assertIn('kiroclops/portable_v2/integrity.py', sources)
        self.assertIn('kiroclops/portable_v2/audit.py', sources)
        self.assertIn('adapters/heart-godot4/capture.gd', sources)
        self.assertTrue(any(path.startswith('adapters/heart-wall-coyote-godot4-v2/')
                            for path in sources))
        self.assertIn('adapters/heart-integrity-godot4/integrity.gd', sources)
        self.assertFalse(any(path.startswith('kiroclops/portable/') for path in sources))
        self.assertNotEqual(sources, previous.protocol_sources())


if __name__ == '__main__':
    unittest.main()
