"""V6 plan WP5.2: the error-table statistics separate a constant bias from the scatter around it."""
import math
import unittest

from scripts.v6_marks_error_table import METHODS, summarise


class SummariseTests(unittest.TestCase):
    def test_constant_offset_is_bias_not_scatter(self):
        row = summarise([[0., -1.04]]*39, 2.)
        self.assertEqual((39, 39), (row['points'], row['measured']))
        self.assertAlmostEqual(-1.04, row['bias_px'][1])
        self.assertAlmostEqual(0., row['scatter_px'])
        self.assertAlmostEqual(1.04, row['median_px'])
        self.assertEqual(39, row['within_floor'])

    def test_opposite_offsets_cancel_in_the_bias_and_show_as_scatter(self):
        row = summarise([[0., -1.]]*2+[[0., 1.]]*2+[None], 2.)
        self.assertEqual((5, 4), (row['points'], row['measured']))
        self.assertEqual([0., 0.], row['bias_px'])
        self.assertAlmostEqual(1., row['scatter_px'])
        self.assertEqual((1., 1.), (row['median_px'], row['max_px']))

    def test_distances_floor_and_unmeasured(self):
        row = summarise([[3., 4.], [.3, .4], [0., 0.]], 2.)
        self.assertAlmostEqual(.5, row['median_px'])
        self.assertAlmostEqual(5., row['max_px'])
        self.assertEqual(2, row['within_floor'])
        self.assertTrue(math.isclose(row['bias_length_px'], math.hypot(1.1, 1.4666666666666666)))
        self.assertEqual({'points': 2, 'measured': 0}, summarise([None, None], 2.))
        self.assertEqual(('accepted', 'features_v1'), (METHODS[0], METHODS[-1]))


if __name__ == '__main__':
    unittest.main()
