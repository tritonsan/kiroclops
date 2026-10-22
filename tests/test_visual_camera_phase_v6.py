"""V6 plan WP1.5: sub-pixel camera shift by phase correlation, reported next to the unchanged integer camera gate."""
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.tracking import analyze_package

SHIFTS = [(0., 0.), (.25, -.1), (.5, .3), (-.4, .45), (1.3, -.7)]
# Phase correlation on a cropped (non-circular) patch leans toward whole pixels;
# 0.2 px covers the windowed estimate on these 112-px patches.
TOLERANCE_PX = .2


def scene(rng, size=(320, 240)):
    texture = cv2.GaussianBlur(rng.random((size[1], size[0])).astype(np.float32), (9, 9), 0)
    background = cv2.cvtColor(cv2.normalize(texture, None, 30, 220, cv2.NORM_MINMAX).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    cv2.rectangle(background, (200, 90), (250, 140), (40, 90, 200), -1)
    cv2.putText(background, 'K7', (207, 125), cv2.FONT_HERSHEY_SIMPLEX, .8, (250, 250, 250), 2, cv2.LINE_AA)
    return background


class CameraPhaseTests(unittest.TestCase):
    def test_subpixel_camera_shift_is_reported_and_the_gate_is_unchanged(self):
        base = scene(np.random.default_rng(15))
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for i, (dx, dy) in enumerate(SHIFTS):
                frame = cv2.warpAffine(base, np.float32([[1, 0, dx], [0, 1, dy]]), (320, 240), flags=cv2.INTER_CUBIC,
                                       borderMode=cv2.BORDER_REFLECT)
                path = Path(temp)/f'{i}.png'
                path.write_bytes(cv2.imencode('.png', frame)[1].tobytes())
                paths.append(path)
            package = Path(temp)/'package'
            create_package(package, paths, [i*.04 for i in range(len(paths))])
            selection = {'schema_version': 1, 'objects': [{'id': 'card', 'roi': [196, 86, 58, 58], 'anchor': [29, 29]}],
                         'background_rois': [[16, 16, 112, 112], [16, 120, 112, 112]]}
            result = analyze_package(package, selection, {'min_valid_ratio': .5, 'require_static_camera': True})
        for frame, (dx, dy) in zip(result['camera'], SHIFTS):
            with self.subTest(index=frame['index']):
                self.assertEqual(2, len(frame['phase_shifts_px']))
                for x, y, response in frame['phase_shifts_px']:
                    self.assertAlmostEqual(dx, x, delta=TOLERANCE_PX)
                    self.assertAlmostEqual(dy, y, delta=TOLERANCE_PX)
                    self.assertGreater(response, .5)
        # The camera gate is still the integer rule (shift up to 1.5 px passes).
        self.assertEqual(['pass']*len(SHIFTS), [frame['status'] for frame in result['camera']])

    def test_no_background_means_no_phase_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'0.png'
            path.write_bytes(cv2.imencode('.png', scene(np.random.default_rng(16)))[1].tobytes())
            package = Path(temp)/'package'
            create_package(package, [path, path], [0., .04])
            result = analyze_package(package, {'schema_version': 1, 'objects': [{'id': 'card', 'roi': [196, 86, 58, 58]}]})
        self.assertTrue(all('phase_shifts_px' not in frame and frame['status'] == 'unverified' for frame in result['camera']))


if __name__ == '__main__':
    unittest.main()
