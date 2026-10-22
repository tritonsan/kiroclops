"""OpenCV 5 runtime gate, anchor conventions and sub-pixel peak refinement (V6)."""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual import opencv5
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError
from kiroclops.visual.tracking import analyze_package


class OpenCV5TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'v6 anchor café'
        self.root.mkdir()
        self.rng = np.random.default_rng(20261015)
        self.patch = self.rng.integers(20, 250, (14, 16, 3), dtype=np.uint8)
        self.sequence = 0

    def package(self, images, times=None):
        self.sequence += 1
        directory = self.root/str(self.sequence)
        directory.mkdir()
        paths = []
        for i, image in enumerate(images):
            path = directory/f'raw-{i}.png'
            path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
            paths.append(path)
        output = directory/'package'
        create_package(output, paths, times or [i*.04 for i in range(len(images))])
        return output

    def frame(self, x, y, patch=None):
        patch = self.patch if patch is None else patch
        image = np.zeros((120, 200, 3), np.uint8)
        image[y:y+patch.shape[0], x:x+patch.shape[1]] = patch
        return image

    def selection(self, **kwargs):
        return {'schema_version': 1, 'objects': [{'id': 'a', 'roi': [20, 40, 16, 14], 'method': 'template', **kwargs}]}

    def test_runtime_gate_and_inventory(self):
        self.assertTrue(opencv5.require_opencv5().startswith('5.'))
        runtime = opencv5.opencv_runtime()
        self.assertEqual(runtime['major'], 5)
        self.assertIn('features', runtime['modules'])
        self.assertIn('core', runtime['modules'])
        self.assertTrue(runtime['opencv5_symbols']['ALIKED'])
        self.assertTrue(runtime['opencv5_symbols']['LightGlueMatcher'])
        self.assertTrue(runtime['shared_symbols_present'])
        # Machine-dependent values stay out so replays produce identical records.
        self.assertNotIn('threads', runtime)

    def test_observation_set_records_runtime(self):
        result = analyze_package(self.package([self.frame(20, 40), self.frame(22, 40)]), self.selection(anchor=[5, 6]))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['opencv_runtime']['version'], cv2.__version__)
        self.assertEqual(result['opencv_version'], cv2.__version__)

    def test_default_anchor_mirror_is_symmetric(self):
        images = [self.frame(20, 40), self.frame(24, 40, self.patch[:, ::-1])]
        # No explicit anchor: the default w/2 is a continuous centre, so its mirror is w-w/2.
        result = analyze_package(self.package(images), self.selection(horizontal_mirror=True))
        frames = result['objects'][0]['frames']
        self.assertTrue(frames[1]['mirrored'])
        self.assertEqual([f['x'] for f in frames], [28., 32.])
        self.assertEqual([f['y'] for f in frames], [47., 47.])
        # An explicit anchor is a pixel index and keeps the V1 rule (w-1-ax).
        explicit = analyze_package(self.package(images), self.selection(horizontal_mirror=True, anchor=[8, 7]))
        self.assertEqual([f['x'] for f in explicit['objects'][0]['frames']], [28., 31.])
        # The convention can be named explicitly for either case.
        continuous = analyze_package(self.package(images), self.selection(horizontal_mirror=True, anchor=[8, 7],
                                                                           anchor_convention='continuous'))
        self.assertEqual([f['x'] for f in continuous['objects'][0]['frames']], [28., 32.])
        with self.assertRaisesRegex(VisualError, 'anchor_convention'):
            analyze_package(self.package(images), self.selection(anchor_convention='odd'))

    def test_default_anchor_rotation_is_symmetric(self):
        rotated = np.zeros((120, 200, 3), np.uint8)
        rotated[40:56, 24:38] = np.rot90(self.patch)
        result = analyze_package(self.package([self.frame(20, 40), rotated]), self.selection(rotations=[0, 90]))
        frame = result['objects'][0]['frames'][1]
        self.assertEqual(frame['rotation_deg'], 90)
        # Continuous centre (8,7) of a 16x14 patch turns into (7,8) inside the 14x16 image.
        self.assertEqual((frame['x'], frame['y']), (24+7., 40+8.))

    def shifted_pair(self, dx, dy):
        smooth = cv2.GaussianBlur(self.rng.integers(0, 255, (14, 16, 3), dtype=np.uint8), (3, 3), 0)
        first = self.frame(20, 40, smooth)
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        second = cv2.warpAffine(first, matrix, (200, 120), flags=cv2.INTER_LINEAR)
        return [first, second]

    def test_upsampled_refinement_recovers_subpixel_shift(self):
        images = self.shifted_pair(.3, -.4)
        refined = analyze_package(self.package(images), self.selection(anchor=[5, 6], peak_refinement='upsampled_v1'))
        frames = refined['objects'][0]['frames']
        # Symmetric upsampling leaves a residual pull toward the integer peak of
        # at most about 0.1 px on bilinear-shifted synthetic frames (V6 diagnostic
        # over shifts 0.1-0.5 px: max error 0.07 px); the benchmark tabulates it.
        self.assertAlmostEqual(frames[0]['x'], 25., delta=.05)
        self.assertAlmostEqual(frames[0]['y'], 46., delta=.05)
        self.assertAlmostEqual(frames[1]['x'], 25.3, delta=.12)
        self.assertAlmostEqual(frames[1]['y'], 45.6, delta=.12)
        info = frames[1]['peak_refinement']
        self.assertEqual(info['method'], 'upsampled_v1')
        self.assertGreater(info['score_refined'], frames[1]['score'])
        # The integer peak stays available as the search origin.
        self.assertEqual(frames[1]['left'] - info['dx'], round(frames[1]['left'] - info['dx']))

    def test_parabolic_refinement_moves_toward_the_shift_with_known_bias(self):
        images = self.shifted_pair(.3, -.4)
        refined = analyze_package(self.package(images), self.selection(anchor=[5, 6], peak_refinement='parabolic_v1'))
        frame = refined['objects'][0]['frames'][1]
        info = frame['peak_refinement']
        self.assertEqual(info['method'], 'parabolic_v1')
        self.assertTrue(-.5 <= info['dx'] <= .5 and -.5 <= info['dy'] <= .5)
        self.assertLess(info['curvature_x'], 0)
        self.assertLess(info['curvature_y'], 0)
        # A three-point parabola on the integer response under-estimates a
        # fractional shift (pixel locking); it still moves the right way.
        integer_x, integer_y = frame['x']-info['dx'], frame['y']-info['dy']
        self.assertLess(abs(frame['x']-25.3), abs(integer_x-25.3))
        self.assertLess(abs(frame['y']-45.6), abs(integer_y-45.6))

    def test_refinement_is_opt_in_and_template_only(self):
        images = self.shifted_pair(.3, -.4)
        plain = analyze_package(self.package(images), self.selection(anchor=[5, 6]))
        frame = plain['objects'][0]['frames'][1]
        self.assertNotIn('peak_refinement', frame)
        self.assertEqual(frame['x'], round(frame['x']))
        with self.assertRaisesRegex(VisualError, 'peak_refinement'):
            analyze_package(self.package(images), self.selection(anchor=[5, 6], peak_refinement='cubic'))
        # Resampling never refines a window that touches the image border.
        edge = [self.frame(0, 0), self.frame(0, 0)]
        edge_result = analyze_package(self.package(edge), {'schema_version': 1, 'objects': [
            {'id': 'a', 'roi': [0, 0, 16, 14], 'anchor': [5, 6], 'method': 'template', 'peak_refinement': 'upsampled_v1'}]})
        self.assertEqual(edge_result['objects'][0]['frames'][1]['peak_refinement']['status'], 'border_peak')
        self.assertEqual(edge_result['objects'][0]['frames'][1]['x'], 5.)
        with self.assertRaisesRegex(VisualError, 'peak_refinement'):
            analyze_package(self.package(images), self.selection(anchor=[5, 6], method='flow', peak_refinement='parabolic_v1'))


if __name__ == '__main__':
    unittest.main()
