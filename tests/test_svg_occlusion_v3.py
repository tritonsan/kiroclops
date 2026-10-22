"""V3 focus-ring rule: a covered stroke end is unobservable, not a detachment.

The V3 helper is loaded from its own file. Since V5 the review tools load the V4
helper (eight-step occlusion walk); tests/test_svg_ring_v5.py covers it.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np

from kiroclops.local_engine import realistic_review, suite_generic_raw_review
from kiroclops.local_engine.geometry_acceptance import svg_preservation

from tests.test_local_svg_preservation import observations, specs
from tests.evidence import requires_evidence

BACKGROUND = np.asarray([37, 25, 16])
RING = (210, 160, 90)
BOX = [50, 40, 80, 60]


def scene(gap_colors, end_x=46):
    """One row: a stroke ending at ``end_x``, then ``gap_colors`` from ``end_x + 1``, then the node box at x=50."""
    image = np.zeros((100, 120, 3), np.uint8)
    image[:] = BACKGROUND
    line = np.zeros((100, 120), bool)
    line[50, 20:end_x + 1] = True
    image[50, 20:end_x + 1] = (176, 149, 128)
    for offset, color in enumerate(gap_colors):
        x = end_x + 1 + offset
        if color == "stroke":
            line[50, x] = True
            image[50, x] = (176, 149, 128)
        elif color is not None:
            image[50, x] = color
    l, t, r, b = BOX
    image[t:b, l:r] = (79, 57, 38)
    return image, line


def v3_helper():
    suite_generic_raw_review._svg_helpers()  # loads the shared raw_svg_sequence_aid dependency
    path = Path(__file__).resolve().parents[1] / "artifacts/phase3a/tools/raw_svg_geometry_aid_v3.py"
    if "raw_svg_geometry_aid_v3" not in sys.modules:
        spec = importlib.util.spec_from_file_location("raw_svg_geometry_aid_v3", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["raw_svg_geometry_aid_v3"] = module
        spec.loader.exec_module(module)
    return sys.modules["raw_svg_geometry_aid_v3"]


class OccludedEndTests(unittest.TestCase):
    def setUp(self):
        self.geometry = v3_helper()

    def end(self, image, line, x=46.):
        return self.geometry.occluded_end(image, line, (x, 50.), np.asarray([1., 0.]), BOX, BACKGROUND)

    @requires_evidence('artifacts/phase3a/tools')
    def test_this_is_the_v3_helper_and_the_review_tools_now_load_v4(self):
        self.assertTrue(self.geometry.__file__.endswith("raw_svg_geometry_aid_v3.py"))
        self.assertEqual(4, self.geometry.OCCLUSION_MAX_STEPS)
        sources = realistic_review._code_sources()
        self.assertTrue(any(path.endswith("raw_svg_geometry_aid_v4.py") for path in sources))
        self.assertFalse(any(path.endswith(("raw_svg_geometry_aid.py", "raw_svg_geometry_aid_v3.py")) for path in sources))

    @requires_evidence('artifacts/phase3a/tools')
    def test_painted_gap_reaching_the_box_is_occluded(self):
        image, line = scene([RING, RING, RING])
        self.assertTrue(self.end(image, line))
        image, line = scene([RING], end_x=48)
        self.assertTrue(self.end(image, line, x=48.))

    @requires_evidence('artifacts/phase3a/tools')
    def test_background_pixel_in_the_gap_is_a_real_gap(self):
        image, line = scene([RING, None, RING])
        self.assertFalse(self.end(image, line))

    @requires_evidence('artifacts/phase3a/tools')
    def test_low_contrast_gap_is_background(self):
        faint = tuple(int(v) for v in BACKGROUND + 8)
        image, line = scene([RING, faint, RING])
        self.assertFalse(self.end(image, line))

    @requires_evidence('artifacts/phase3a/tools')
    def test_another_stroke_pixel_means_the_end_was_not_the_visible_end(self):
        image, line = scene([RING, "stroke", RING])
        self.assertFalse(self.end(image, line))

    @requires_evidence('artifacts/phase3a/tools')
    def test_end_touching_the_box_needs_no_occlusion(self):
        image, line = scene([], end_x=49)
        self.assertFalse(self.end(image, line, x=49.))

    @requires_evidence('artifacts/phase3a/tools')
    def test_box_beyond_four_steps_is_not_occlusion(self):
        image, line = scene([RING] * 4, end_x=45)
        self.assertFalse(self.end(image, line, x=45.))

    @requires_evidence('artifacts/phase3a/tools')
    def test_leaving_the_image_is_not_occlusion(self):
        image, line = scene([RING] * 3)
        self.assertFalse(self.geometry.occluded_end(image, line, (118., 50.), np.asarray([1., 0.]), BOX, BACKGROUND))


class PreservationOcclusionTests(unittest.TestCase):
    def detached(self):
        obs = observations()
        candidate = deepcopy(obs)
        for frame in candidate["connections"][0]["frames"]:
            frame["end"] = [frame["end"][0] - 5., frame["end"][1]]
        return obs, candidate

    def test_short_end_without_occlusion_is_a_violation(self):
        reference, candidate = self.detached()
        result = svg_preservation(reference, candidate, specs(reference))
        self.assertFalse(result["pass"])
        self.assertEqual("preservation_violation", result["failure_class"])

    def test_occluded_short_end_is_attached_and_reported(self):
        reference, candidate = self.detached()
        for frame in candidate["connections"][0]["frames"]:
            frame["end_occluded"] = True
        result = svg_preservation(reference, candidate, specs(reference))
        self.assertTrue(result["pass"], result)
        self.assertEqual("ok", result["measurement_status"])
        samples = result["visible_connections"][0]["samples"]
        self.assertTrue(all(sample["occluded_ends"] == [False, True] for sample in samples))
        self.assertTrue(all("occluded_ends" not in sample for sample in result["visible_connections"][1]["samples"]))

    def test_only_a_boolean_true_flag_counts(self):
        reference, candidate = self.detached()
        for frame in candidate["connections"][0]["frames"]:
            frame["end_occluded"] = 1
        self.assertFalse(svg_preservation(reference, candidate, specs(reference))["pass"])

    def test_occlusion_cannot_stand_in_for_a_missing_end(self):
        reference, candidate = self.detached()
        for frame in candidate["connections"][0]["frames"]:
            frame["end_occluded"] = True
            frame["end"] = None
        result = svg_preservation(reference, candidate, specs(reference))
        self.assertFalse(result["pass"])
        self.assertEqual("insufficient_observability", result["measurement_status"])


if __name__ == "__main__":
    unittest.main()
