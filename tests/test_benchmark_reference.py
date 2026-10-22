import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kiroclops.cloud.benchmark_reference import validate_reference, portable_reference
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class BenchmarkReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'café naïve'
        self.sources = {'tracking.py': 'current-source'}
        self.corpus = {'corpus_sha256': 'corpus', 'items': [{'id': 'clip', 'package_sha256': 'package'}]}
        self.obs = {'package_sha256': 'package', 'objects': []}
        self.measurement = {'observations': self.obs, 'events': {'observation_sha256': digest(self.obs)}}
        self.path = self.root/'clip/measurement.json'
        write_json(self.path, self.measurement)
        identity = {'opencv_version': 'test', 'threads': 4}
        self.reference = {'corpus_sha256': 'corpus', 'analysis_sources': self.sources,
                          'engine': {'identity': identity, 'engine_sha256': digest(identity)},
                          'items': [{'id': 'clip', 'package_sha256': 'package',
                                     'measurement_path': 'clip/measurement.json',
                                     'measurement_file_sha256': file_sha256(self.path),
                                     'measurement_sha256': digest(self.measurement)}]}
        self.save()

    def save(self):
        self.reference.pop('reference_sha256', None)
        self.reference['reference_sha256'] = digest(self.reference)
        write_json(self.root/'reference.json', self.reference)

    def assert_error(self, code):
        with self.assertRaises(VisualError) as error:
            validate_reference(self.root, self.corpus, self.sources)
        self.assertEqual(code, error.exception.code)

    def test_current_source_and_full_links_accepted_with_unicode_path(self):
        self.assertEqual(self.reference, validate_reference(self.root, self.corpus, self.sources))

    def test_historical_tracker_cannot_be_used_as_current_engine_oracle(self):
        self.reference['analysis_sources'] = {'tracking.py': 'old-before-tie-fix'}
        self.save()
        self.assert_error('reference_binding_mismatch')

    def test_different_corpus_rejected(self):
        self.reference['corpus_sha256'] = 'another-corpus'
        self.save()
        self.assert_error('reference_binding_mismatch')

    def test_duplicate_inventory_cannot_replace_missing_reference(self):
        self.reference['items'].append(dict(self.reference['items'][0]))
        self.save()
        self.assert_error('reference_binding_mismatch')

    def test_changed_measurement_bytes_rejected(self):
        self.path.write_text('{}')
        self.assert_error('reference_integrity_failure')

    def test_changed_reference_manifest_rejected(self):
        self.reference['corpus_sha256'] = 'tampered'
        write_json(self.root/'reference.json', self.reference)
        self.assert_error('reference_integrity_failure')

    def test_measurement_cannot_borrow_another_package_link(self):
        self.measurement['observations']['package_sha256'] = 'different-package'
        self.measurement['events']['observation_sha256'] = digest(self.measurement['observations'])
        write_json(self.path, self.measurement)
        self.reference['items'][0].update(measurement_sha256=digest(self.measurement), measurement_file_sha256=file_sha256(self.path))
        self.save()
        self.assert_error('reference_integrity_failure')

    def test_stale_event_observation_link_rejected(self):
        self.measurement['events']['observation_sha256'] = 'old-observations'
        write_json(self.path, self.measurement)
        self.reference['items'][0].update(measurement_sha256=digest(self.measurement), measurement_file_sha256=file_sha256(self.path))
        self.save()
        self.assert_error('reference_integrity_failure')

    def test_engine_identity_required(self):
        self.reference['engine']['engine_sha256'] = 'another-build'
        self.save()
        self.assert_error('reference_integrity_failure')

    def test_portable_reference_keeps_local_installation_paths_local(self):
        self.reference['engine']['installation'] = {'python': 'C:/private/workspace/python.exe'}
        self.reference['engine']['build_information'] = 'local absolute compiler paths'
        self.save()
        output = self.root.parent/'café naïve copy'
        copied = portable_reference(self.root, output, self.corpus, self.sources)
        self.assertNotIn('installation', copied['engine'])
        self.assertNotIn('build_information', copied['engine'])
        self.assertEqual(self.reference['reference_sha256'], copied['source_reference_sha256'])
        self.assertEqual(self.path.read_bytes(), (output/'clip/measurement.json').read_bytes())

    def test_paired_benchmark_stops_before_engine_execution_with_stale_oracle(self):
        from scripts import phase3b_paired_benchmark as runner
        self.reference['analysis_sources'] = {'tracking.py': 'old'}
        self.save()
        output = self.root.parent/'trial'
        args = SimpleNamespace(output=output, corpus=self.root, reference=self.root)
        with patch.object(runner, 'verify_corpus', return_value=self.corpus), \
                patch.object(runner, 'analysis_sources', return_value=self.sources), \
                patch.object(runner.subprocess, 'run') as process:
            with self.assertRaises(VisualError):
                runner.paired(args)
            process.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
