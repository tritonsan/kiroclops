from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import cv2
import numpy as np

from kiroclops.visual.package import create_package, load_package, read_image
from kiroclops.visual.schema import digest, write_json, read_json, VisualError
from kiroclops.cloud.corpus import freeze, portable_manifest
from kiroclops.cloud.identity import computation_key
from kiroclops.cloud.results import envelope, legacy_result, make_measurement, request_binding, validate_delivery
from kiroclops.cloud.transport import pack, unpack, png_dimensions
from kiroclops.cloud.history import inventory


class CloudFoundations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kiroclops-café ')
        self.root = Path(self.temp.name)
        frames = []
        for i in range(3):
            p = self.root / f'{i}.png'
            p.write_bytes(cv2.imencode('.png', np.full((24, 32, 3), i * 80, np.uint8))[1].tobytes())
            frames.append(p)
        self.manifest = create_package(self.root / 'input', frames, [0, .05, .1], metadata={'private_path': 'C:\\private\\secret'})
        self.engine = {'identity': {'build': 'one', 'threads': 4}}
        self.engine['engine_sha256'] = digest(self.engine['identity'])
        self.sources = {'tracking.py': 'a' * 64}

    def tearDown(self):
        self.temp.cleanup()

    def test_transport_relocates_and_excludes_private_metadata_and_unlisted_files(self):
        (self.root / 'input/secret.txt').write_text('SECRET', encoding='utf-8')
        receipt = pack(self.root / 'input', self.root / 'package.zip')
        manifest = unpack(self.root / 'package.zip', self.root / 'moved', receipt['package_sha256'])
        self.assertEqual({}, manifest['metadata'])
        self.assertEqual([f['sha256'] for f in self.manifest['frames']], [f['sha256'] for f in manifest['frames']])
        self.assertFalse((self.root / 'moved/secret.txt').exists())
        self.assertEqual('C:\\private\\secret', read_json(self.root / 'input/manifest.json')['metadata']['private_path'])

    def test_archive_is_identical_after_relocation_and_filesystem_timestamp_change(self):
        import os,shutil
        first=pack(self.root/'input',self.root/'one.zip')
        shutil.copytree(self.root/'input',self.root/'relocated')
        for path in (self.root/'relocated').rglob('*'):
            if path.is_file():os.utime(path,(1000000000,1000000000))
        second=pack(self.root/'relocated',self.root/'two.zip')
        self.assertEqual(first['archive_sha256'],second['archive_sha256'])
        self.assertEqual((self.root/'one.zip').read_bytes(),(self.root/'two.zip').read_bytes())

    def test_png_bomb_rejected_before_decoder(self):
        p = self.root / 'input/frames/000000.png'
        data = bytearray(p.read_bytes())
        data[16:24] = struct.pack('>II', 65535, 65535)
        data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]) & 0xffffffff)
        p.write_bytes(data)
        with patch('cv2.imdecode', side_effect=AssertionError('must not decode')):
            with self.assertRaises(VisualError) as error:
                pack(self.root / 'input', self.root / 'bad.zip')
        self.assertEqual('unsupported_input', error.exception.code)

    def test_crc_and_invalid_header_rejected(self):
        for header in (b'x' * 33, b'\x89PNG\r\n\x1a\n' + b'x' * 25):
            with self.assertRaises(VisualError):
                png_dimensions(header)

    def test_hash_validation_preserves_integrity_without_decoding(self):
        with patch('cv2.imdecode', side_effect=AssertionError('no redundant decode')):
            self.assertEqual(self.manifest, load_package(self.root / 'input', verify='hashes'))
        frame = self.root / 'input/frames/000001.png'
        frame.write_bytes(frame.read_bytes() + b'changed')
        with self.assertRaises(VisualError) as error:
            load_package(self.root / 'input', verify='hashes')
        self.assertEqual('package_integrity_failure', error.exception.code)

    def test_local_decoder_also_checks_png_dimensions_before_decode(self):
        p=self.root/'bomb.png';data=bytearray((self.root/'0.png').read_bytes())
        data[16:24]=struct.pack('>II',65535,65535)
        data[29:33]=struct.pack('>I',zlib.crc32(data[12:29]) & 0xffffffff);p.write_bytes(data)
        with patch('cv2.imdecode',side_effect=AssertionError('unsafe decode')):
            with self.assertRaises(VisualError) as error:read_image(p)
        self.assertEqual('unsupported_input',error.exception.code)

    def test_archive_traversal_rejected_before_writes(self):
        receipt = pack(self.root / 'input', self.root / 'good.zip')
        with zipfile.ZipFile(self.root / 'good.zip', 'a') as archive:
            archive.writestr('../outside.png', b'bad')
        with self.assertRaises(VisualError):
            unpack(self.root / 'good.zip', self.root / 'moved', receipt['package_sha256'])
        self.assertFalse((self.root / 'moved').exists())

    def test_duplicate_and_unlisted_archive_entries_rejected(self):
        receipt = pack(self.root / 'input', self.root / 'good.zip')
        with zipfile.ZipFile(self.root / 'good.zip', 'a') as archive:
            archive.writestr('unlisted.png', b'bad')
        with self.assertRaises(VisualError):
            unpack(self.root / 'good.zip', self.root / 'moved', receipt['package_sha256'])

    def test_measurement_stable_receipt_changes_and_legacy_is_bound(self):
        sha = self.manifest['package_sha256']
        measurement = make_measurement({'status': 'ok', 'package_sha256': sha, 'objects': []}, {'events': []})
        request = request_binding(sha, {}, {}, 'a' * 64)
        first = envelope(measurement, request, logical_job_id='one', engine=self.engine, sources=self.sources, timings={'wall': 1})
        second = envelope(measurement, request, logical_job_id='one', engine=self.engine, sources=self.sources, timings={'wall': 9}, origin={'kind': 'exact_reuse'})
        self.assertEqual(first['measurement_sha256'], second['measurement_sha256'])
        self.assertNotEqual(first['receipt_sha256'], second['receipt_sha256'])
        self.assertEqual(legacy_result(first, request, logical_job_id='one'), legacy_result(second, request, logical_job_id='one'))
        changed = dict(request, candidate_sha256='b' * 64)
        with self.assertRaises(VisualError):
            legacy_result(first, changed, logical_job_id='one')
        rebound = envelope(measurement, changed, logical_job_id='two', engine=self.engine, sources=self.sources, timings={})
        self.assertEqual(first['receipt']['computation_key'], rebound['receipt']['computation_key'])
        self.assertNotEqual(first['receipt']['request_sha256'], rebound['receipt']['request_sha256'])

    def test_delivery_rejects_missing_receipt_tamper_wrong_job_and_version(self):
        sha = self.manifest['package_sha256']; req = request_binding(sha, {}, {})
        delivery = envelope(make_measurement({'status':'ok','package_sha256':sha}, {}), req,
                            logical_job_id='one', engine=self.engine, sources=self.sources, timings={})
        corrupt = deepcopy(delivery); corrupt['measurement']['measurement_status'] = 'fake'
        missing = deepcopy(delivery); missing.pop('receipt')
        for value in (corrupt, missing):
            with self.assertRaises(VisualError):
                validate_delivery(value, req, logical_job_id='one')
        with self.assertRaises(VisualError):
            validate_delivery(delivery, req, logical_job_id='other')
        with self.assertRaises(VisualError):
            validate_delivery(delivery, req, logical_job_id='one', expected_engine_sha256='other')
        with self.assertRaises(VisualError):
            validate_delivery(delivery, req, logical_job_id='one', expected_sources={'tracking.py':'b'*64})

    def test_cache_key_invalidation(self):
        sha = self.manifest['package_sha256']
        key = computation_key(sha, {}, {}, self.sources, self.engine)
        for args in [('b'*64, {}, {}, self.sources, self.engine),
                     (sha, {'roi':[1,2,3,4]}, {}, self.sources, self.engine),
                     (sha, {}, {'min_valid_ratio':1}, self.sources, self.engine),
                     (sha, {}, {}, {'tracking.py':'b'*64}, self.engine)]:
            self.assertNotEqual(key, computation_key(*args))
        engine = {'identity': {'build': 'two', 'threads': 4}}
        engine['engine_sha256'] = digest(engine['identity'])
        self.assertNotEqual(key, computation_key(sha, {}, {}, self.sources, engine))
        with self.assertRaises(VisualError):
            computation_key(sha, {}, {}, {}, self.engine)

    def test_freeze_rejects_holdout_before_creating_directory(self):
        rows = [{'id':f'{domain}-{i}', 'domain':domain, 'split':'calibration'}
                for domain in ('game','panel','svg') for i in range(4)]
        rows[-1]['split'] = 'heldout'
        with self.assertRaises(VisualError):
            freeze(self.root, self.root / 'out', rows)
        self.assertFalse((self.root / 'out').exists())

    def test_history_deduplicates_jobs_and_does_not_enroll_cache(self):
        result = {'status':'ok'}
        for store in ('first','copy'):
            directory = self.root / store / 'jobs/job1'
            write_json(directory / 'result.json', result)
            write_json(directory / 'state.json', {'job_id':'job1','status':'succeeded','result_sha256':digest(result),
                                                 'request_sha256':'r','started_at':10,'finished_at':12})
        value = inventory([self.root / 'first',self.root / 'copy'], self.root / 'inventory.json')
        self.assertEqual(1,value['unique_jobs'])
        self.assertEqual(1,value['copied_jobs_ignored'])
        self.assertEqual(0,value['same_request_same_result_repeats'])


if __name__ == '__main__':
    unittest.main()
