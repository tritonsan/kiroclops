import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.pixel_anchor import prepare_anchor, observe_anchor
from kiroclops.visual.schema import VisualError
from kiroclops.visual.tracking import analyze_package


FG = np.array([70, 180, 240.])
BG = np.array([20, 35, 50.])
OPTIONS = {'method': 'foreground_color_mass_v1', 'foreground_rgb': FG[::-1].astype(int).tolist()}


def rendered(cx, cy, size=(80, 60)):
    """Exact pixel-square coverage of two disjoint analytic rectangles.

    Truth is the analytic first moment, not an OpenCV warp or tracker value.
    Pixel centers have integer coordinates.
    """
    yy, xx = np.indices((size[1], size[0]), dtype=float)
    mass = np.zeros(xx.shape)
    for left, top, width, height in [(cx-6, cy-5, 12, 10), (cx+6, cy, 2, 4)]:
        x = np.maximum(0, np.minimum(xx+.5, left+width)-np.maximum(xx-.5, left))
        y = np.maximum(0, np.minimum(yy+.5, top+height)-np.maximum(yy-.5, top))
        mass += x*y
    return np.rint(BG+mass[..., None]*(FG-BG)).astype(np.uint8), (cx+7*8/128, cy+2*8/128)


class PixelAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, images):
        paths = []
        for i, image in enumerate(images):
            path = self.root/f'{i}.png'
            path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
            paths.append(path)
        create_package(self.root/'package', paths, [i*.04 for i in range(len(paths))])
        return self.root/'package'

    def selection(self, roi=None, **kwargs):
        return {'schema_version': 1, 'objects': [{'id': 'illustration', 'roi': roi or [20, 15, 25, 23],
                  'anchor': [12, 11], 'method': 'template', 'min_margin': .015,
                  'search_radius': 20, 'pixel_anchor': OPTIONS, **kwargs}]}

    def test_fractional_positions_preserve_true_transverse_and_endpoint_motion(self):
        positions = [(31+p, 26+p/3) for p in [0, .125, .25, .5, .875, 1.125, 3]]
        frames = [rendered(*p) for p in positions]
        observed = analyze_package(self.package([f[0] for f in frames]), self.selection())
        values = observed['objects'][0]['frames']
        self.assertEqual(observed['status'], 'ok')
        for result, (_, truth) in zip(values, frames):
            self.assertLess(np.linalg.norm(np.array([result['x'], result['y']])-truth), .04)
            self.assertIsNone(result['pixel_anchor_accuracy_uncertainty_px'])
        self.assertAlmostEqual(values[-1]['x']-values[0]['x'], 3, delta=.04)
        self.assertAlmostEqual(values[-1]['y']-values[0]['y'], 1, delta=.04)

    def test_crop_parity_and_independent_first_phase_do_not_define_anchor(self):
        base, _ = rendered(31, 26)
        shifted, expected = rendered(31.375, 26.75)
        outputs = []
        for image, roi in [(base, [20, 15, 25, 23]), (shifted, [21, 16, 24, 22])]:
            x, y, w, h = roi
            reference = prepare_anchor(image[y:y+h, x:x+w], OPTIONS)
            value = observe_anchor(shifted, x, y, w, h, reference)
            self.assertTrue(value['valid'], value)
            outputs.append([value['x'], value['y']])
        np.testing.assert_allclose(outputs[0], outputs[1], atol=1e-12)
        np.testing.assert_allclose(outputs[0], expected, atol=.04)

    def test_clipping_and_missing_contrast_fail_closed(self):
        image, _ = rendered(31, 26)
        with self.assertRaisesRegex(VisualError, 'clipped'):
            prepare_anchor(image[20:32, 26:38], OPTIONS)
        with self.assertRaisesRegex(VisualError, 'foreground'):
            prepare_anchor(np.tile(BG.astype(np.uint8), (20, 20, 1)), OPTIONS)

    def test_partial_occlusion_and_shape_change_are_not_subpixel_success(self):
        image, _ = rendered(31, 26)
        reference = prepare_anchor(image[15:38, 20:45], OPTIONS)
        occluded = image.copy(); occluded[22:29, 25:29] = BG
        value = observe_anchor(occluded, 20, 15, 25, 23, reference)
        self.assertFalse(value['valid'])
        self.assertIsNone(value['x'])

    def test_ncc_duplicate_identity_gate_still_precedes_centroid(self):
        image, _ = rendered(31, 26)
        image[15:38, 48:73] = image[15:38, 20:45]
        selection = self.selection(search_radius=70)
        result = analyze_package(self.package([image, image]), selection)
        self.assertEqual(result['status'], 'rejected')
        self.assertTrue(all(f['x'] is None for f in result['objects'][0]['frames']))

    def test_refinement_does_not_feed_matching_origin_into_outer_geometry(self):
        image, _ = rendered(31, 26)
        moved, _ = rendered(32.25, 26.5)
        selection = self.selection(geometry={'method': 'contour', 'roi': [15, 10, 35, 35]})
        calls = []
        def contour(gray, origin, base_size, radius, options, image):
            calls.append(origin)
            return [(10, 8, 42, 40, 1.)]
        with patch('kiroclops.visual.tracking._contours', side_effect=contour):
            result = analyze_package(self.package([image, moved]), selection)
        self.assertEqual(calls[0], (15., 10.))
        self.assertTrue(all(float(v).is_integer() for v in calls[1]))
        self.assertTrue(all(f['width'] == 42 for f in result['objects'][0]['frames']))
        self.assertNotEqual(result['objects'][0]['frames'][1]['x'], 32+12)

    def test_unsupported_method_and_invalid_rgb_rejected(self):
        image, _ = rendered(31, 26)
        for options in [{'method': 'unknown', 'foreground_rgb': [1, 2, 3]},
                        {'method': OPTIONS['method'], 'foreground_rgb': [1, 2, 999]}]:
            with self.assertRaises(VisualError):
                prepare_anchor(image[15:38, 20:45], options)

    def test_default_template_keeps_original_anchor_and_no_new_metadata(self):
        image, _ = rendered(31, 26)
        selection = self.selection()
        del selection['objects'][0]['pixel_anchor']
        result = analyze_package(self.package([image, image]), selection)
        obj = result['objects'][0]
        self.assertNotIn('pixel_anchor', obj)
        self.assertEqual([f['x'] for f in obj['frames']], [32., 32.])
        self.assertFalse(any(any(k.startswith('pixel_anchor') for k in f) for f in obj['frames']))


if __name__ == '__main__':
    unittest.main()
