import json
from pathlib import Path
import shutil
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.visual.package import create_package, copy_package, load_package, frame_paths, timing_quality
from kiroclops.visual.schema import VisualError, digest, read_json, safe_path, write_json


class VisualPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiroclops café ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = []
        for index in range(4):
            image = np.zeros((30, 40, 3), np.uint8)
            image[5:12, 5+index:12+index] = (20, 100, 250)
            ok, data = cv2.imencode('.png', image)
            self.assertTrue(ok)
            source = self.root / f'source-{index}.png'
            source.write_bytes(data.tobytes())
            self.sources.append(source)

    def package(self, **kwargs):
        return create_package(self.root / 'package', self.sources, [0, .016, .037, .06], **kwargs)

    def test_vfr_timestamps_and_portability_are_preserved(self):
        original = self.package()
        moved = copy_package(self.root / 'package', self.root / 'café folder')
        self.assertEqual(original, moved)
        self.assertEqual(load_package(self.root / 'café folder'), original)
        self.assertEqual([f['time_s'] for f in original['frames']], [0, .016, .037, .06])
        self.assertNotIn(str(self.root), json.dumps(moved))

    def test_frame_change_cannot_be_accepted(self):
        self.package()
        frame_paths(self.root / 'package')[1].write_bytes(b'not a PNG')
        with self.assertRaises(VisualError) as caught:
            load_package(self.root / 'package')
        self.assertEqual(caught.exception.code, 'package_integrity_failure')

    def test_deleted_frame_cannot_be_accepted(self):
        self.package()
        frame_paths(self.root / 'package')[2].unlink()
        with self.assertRaises(VisualError) as caught:
            load_package(self.root / 'package')
        self.assertEqual(caught.exception.code, 'missing_frame')

    def test_duplicate_time_and_nonfinite_are_rejected(self):
        for times in ([0, .01, .01, .02], [0, .01, float('nan'), .03], [0, .04, .02, .06]):
            with self.subTest(times=times), self.assertRaises(VisualError):
                create_package(self.root / 'bad', self.sources, times)

    def test_asset_escape_and_ads_are_rejected(self):
        for path in ('../a', '/a', 'C:/a', 'a\\b', 'frames/a:stream', 'frames//a'):
            with self.subTest(path=path), self.assertRaises(VisualError):
                safe_path(self.root, path, must_exist=False)

    def test_unknown_cannot_claim_inputs(self):
        with self.assertRaises(VisualError):
            self.package(inputs={'provenance': 'unknown', 'events': [{'time_s': .1, 'action': 'click'}]})

    def test_existing_package_is_immutable(self):
        self.package()
        with self.assertRaises(VisualError) as caught:
            self.package()
        self.assertEqual(caught.exception.code, 'output_exists')

    def test_missing_physics_assumptions_no_longer_required(self):
        manifest = self.package(inputs={'provenance': 'user_declared', 'events': [{'time_s': .02, 'action': 'click'}]})
        self.assertNotIn('fps', manifest)
        self.assertNotIn('game', manifest)
        self.assertEqual(len(manifest['frames']), 4)

    def test_timing_failure_is_separate_from_identical_pixels(self):
        manifest = create_package(self.root / 'same', [self.sources[0]] * 4, [0, .02, .04, .3])
        self.assertEqual(len(load_package(self.root / 'same')['frames']), 4)
        self.assertFalse(timing_quality(manifest)['pass'])

    def test_manifest_hash_does_not_allow_frame_index_substitution(self):
        value = self.package()
        value['frames'][2]['index'] = 1
        value['package_sha256'] = digest({k: v for k, v in value.items() if k != 'package_sha256'})
        write_json(self.root / 'package/manifest.json', value)
        with self.assertRaises(VisualError) as caught:
            load_package(self.root / 'package')
        self.assertEqual(caught.exception.code, 'capture_timing_failure')


if __name__ == '__main__':
    unittest.main()
