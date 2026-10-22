"""V5 SVG ring-aware endpoint rule and the V4 independent helper (PHASE3D_V5_PLAN.md 4.2)."""
import unittest

import cv2
import numpy as np

from kiroclops.local_engine import realistic_review, suite_generic_raw_review
from kiroclops.visual import tracking
from kiroclops.visual.schema import VisualError
from tests.evidence import requires_evidence

BACKGROUND = (37, 25, 16)       # BGR of the canvas #101925
CARD = (79, 57, 38)             # BGR of the card fill #26394f
STROKE = (176, 149, 128)        # BGR of the connector #8095b0
A = {'left': 30., 'top': 30., 'width': 60., 'height': 30., 'valid': True}
B = {'left': 150., 'top': 140., 'width': 60., 'height': 30., 'valid': True}
SPEC = {'id': 'a-b', 'from_object': 'a', 'to_object': 'b'}


def scene(*, ring=True, stop_short=0):
    """A diagonal connector from a to b. b carries a white-and-dark focus ring painted over the stroke."""
    image = np.full((220, 260, 3), BACKGROUND, np.uint8)
    start = np.array([A['left']+A['width']/2, A['top']+A['height']/2])
    end = np.array([B['left']+B['width']/2, B['top']+B['height']/2])
    unit = (end-start)/np.linalg.norm(end-start)
    # The stroke ends inside b (hidden by the card), or ``stop_short`` pixels before the ring.
    tip = end if stop_short == 0 else end-unit*(32+stop_short)
    cv2.line(image, tuple(np.rint(start).astype(int)), tuple(np.rint(tip).astype(int)), STROKE, 3, cv2.LINE_AA)
    if ring:
        l, t = int(B['left']), int(B['top'])
        r, b = l+int(B['width']), t+int(B['height'])
        cv2.rectangle(image, (l-3, t-3), (r+2, b+2), (16, 16, 16), 2)
        cv2.rectangle(image, (l-1, t-1), (r, b), (255, 255, 255), 1)
    for frame in (A, B):
        l, t = int(frame['left']), int(frame['top'])
        image[t:t+int(frame['height']), l:l+int(frame['width'])] = CARD
    return image


def measure(picture, **kwargs):
    gray = cv2.cvtColor(picture, cv2.COLOR_BGR2GRAY)
    return tracking._line_connection(gray, SPEC, {'a': A, 'b': B}, **kwargs)


def box_distance(point, frame):
    x, y = point
    l, t = frame['left'], frame['top']
    r, b = l+frame['width'], t+frame['height']
    if l <= x <= r and t <= y <= b:
        return 0.
    return float(np.hypot(max(l-x, 0, x-r), max(t-y, 0, y-b)))


class RingAwareEndpointTests(unittest.TestCase):
    def test_colour_callers_use_the_v5_rule_and_grey_callers_keep_v3(self):
        image = scene()
        self.assertEqual('canny_axis_band_ring_aware_visible_end_v3', measure(image, image=image)['method'])
        self.assertEqual('canny_axis_band_visible_support_v2', measure(image)['method'])
        with self.assertRaises(VisualError):
            measure(image, endpoint_method='axis_band_v3_ring_aware')

    def test_a_ring_covered_end_is_flagged_and_stays_outside_the_box(self):
        image = scene()
        value = measure(image, image=image)
        self.assertTrue(value['valid'])
        self.assertTrue(value['end_occluded'])
        self.assertFalse(value['start_occluded'])
        distance = box_distance(value['end'], B)
        self.assertGreater(distance, 0.)
        self.assertLessEqual(distance, 6.)

    def test_a_background_gap_before_the_ring_is_not_occlusion(self):
        image = scene(stop_short=8)
        value = measure(image, image=image)
        self.assertTrue(value['valid'])
        self.assertFalse(value['end_occluded'])
        self.assertGreater(box_distance(value['end'], B), 6.)

    def test_an_achromatic_connector_keeps_the_v3_ends_and_gets_no_flag(self):
        # A ring is separable by chroma only from a tinted stroke; a white line near a node is the line itself.
        image = scene(ring=False)
        image[np.all(image == STROKE, axis=2)] = (255, 255, 255)
        tinted = np.any(np.abs(image.astype(int)-np.array(BACKGROUND)) > 0, axis=2) & ~np.all(image == CARD, axis=2)
        image[tinted] = np.max(image[tinted], axis=1, keepdims=True)
        new, old = measure(image, image=image), measure(image, endpoint_method='axis_band_v2')
        self.assertFalse(new['ring_separable'])
        self.assertFalse(new['start_occluded'] or new['end_occluded'])
        for key in ('start', 'end'):
            self.assertLessEqual(float(np.hypot(*(np.array(new[key])-np.array(old[key])))), .5)

    def test_without_a_ring_the_v5_ends_match_v3(self):
        image = scene(ring=False)
        new, old = measure(image, image=image), measure(image, endpoint_method='axis_band_v2')
        self.assertFalse(new['end_occluded'])
        for key in ('start', 'end'):
            self.assertLessEqual(float(np.hypot(*(np.array(new[key])-np.array(old[key])))), .5)


class HelperV4Tests(unittest.TestCase):
    def setUp(self):
        _, self.geometry, _ = suite_generic_raw_review._svg_helpers()

    def end(self, ring_pixels):
        image = np.zeros((100, 120, 3), np.uint8)
        image[:] = BACKGROUND
        line = np.zeros((100, 120), bool)
        end_x = 49 - ring_pixels
        line[50, 20:end_x + 1] = True
        image[50, 20:end_x + 1] = STROKE
        image[50, end_x + 1:50] = (210, 160, 90)
        image[40:60, 50:80] = CARD
        return self.geometry.occluded_end(image, line, (float(end_x), 50.), np.asarray([1., 0.]),
                                          [50, 40, 80, 60], np.asarray(BACKGROUND))

    @requires_evidence('artifacts/phase3a/tools')
    def test_review_tools_load_the_v4_helper_with_an_eight_step_walk(self):
        self.assertTrue(self.geometry.__file__.endswith('raw_svg_geometry_aid_v4.py'))
        self.assertEqual(8, self.geometry.OCCLUSION_MAX_STEPS)
        self.assertTrue(any(p.endswith('raw_svg_geometry_aid_v4.py') for p in realistic_review._code_sources()))

    @requires_evidence('artifacts/phase3a/tools')
    def test_occluders_up_to_seven_pixels_wide_are_crossed(self):
        self.assertTrue(self.end(4))
        self.assertTrue(self.end(7))
        self.assertFalse(self.end(8))


if __name__ == '__main__':
    unittest.main()
