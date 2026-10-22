"""V4 independent game anchor: the frozen reader plus the mirrored Tiny Crate hat."""
import unittest

import numpy as np

from scripts import prepare_game_raw_review_aid as frozen
from scripts import prepare_game_raw_review_aid_v4 as v4

PINK = (168, 119, 255)


def tiny(x, y, mirrored=False):
    image = np.zeros((128, 228, 3), np.uint8)
    image[y, x:x + 5] = PINK
    if mirrored:
        image[y + 1, x - 1:x + 5] = PINK
    else:
        image[y + 1, x:x + 6] = PINK
    return image


class AnchorV4Tests(unittest.TestCase):
    def test_right_facing_hat_uses_the_frozen_reader(self):
        image = tiny(116, 104)
        self.assertEqual(frozen.anchor(image), v4.anchor(image))
        self.assertEqual([(117., 106., 'pink_hat_top_left')], [(a['x'], a['y'], a['feature']) for a in v4.anchor(image)])

    def test_mirrored_hat_marks_the_same_box_position(self):
        image = tiny(108, 103, mirrored=True)
        self.assertEqual([], frozen.anchor(image))
        found = v4.anchor(image)
        self.assertEqual([(110., 105., 'pink_hat_top_right', [108, 103])],
                         [(a['x'], a['y'], a['feature'], a['feature_xy']) for a in found])

    def test_turning_in_place_keeps_the_point(self):
        # An 8-px box at x=114: the right hat starts at box+2, the in-place mirror at box+1.
        right, left = tiny(116, 104), tiny(115, 104, mirrored=True)
        self.assertEqual([a['x'] for a in v4.anchor(right)], [a['x'] for a in v4.anchor(left)])

    def test_no_hat_or_a_wider_band_gives_no_point(self):
        self.assertEqual([], v4.anchor(np.zeros((128, 228, 3), np.uint8)))
        wide = tiny(108, 103, mirrored=True)
        wide[103, 113] = PINK
        self.assertEqual([], v4.anchor(wide))

    def test_heart_frames_are_unchanged(self):
        image = np.zeros((180, 320, 3), np.uint8)
        self.assertEqual(frozen.anchor(image), v4.anchor(image))


if __name__ == '__main__':
    unittest.main()
