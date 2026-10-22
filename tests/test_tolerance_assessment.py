from copy import deepcopy
import math
import unittest

from kiroclops.local_engine.uncertainty import assess_measurement_limits, assess_timing_scope, summarize_same_source_pair


def comparison(scale=25., relative=True):
    return {'windows': [{'reference_package_sha256': 'a', 'candidate_package_sha256': 'b',
        'object_windows': [{'reference_id': 'child', 'window_id': 'input-0', 'segment_index': 0,
            'scale_px': scale, 'position_rmse': .02, 'endpoint_error': .005,
            'relative_to_reference': 'panel' if relative else None, 'endpoint_required': True,
            'limits': {'rmse_limit': .03, 'endpoint_limit': .01}, 'pass': True}]}]}


class ToleranceAssessmentTests(unittest.TestCase):
    def test_pixel_limits_are_saved_scale_not_global_average(self):
        data = comparison(); before = deepcopy(data)
        result = assess_measurement_limits(data, {'position_rmse_fraction': .03})
        row = result['object_windows'][0]
        self.assertEqual(row['current_rmse_limit_px'], .75)
        self.assertEqual(row['current_endpoint_limit_px'], .25)
        self.assertEqual(row['measured_rmse_px'], .5)
        self.assertEqual(row['reference_motion_extent_px'], 25.)
        self.assertEqual(data, before)
        self.assertEqual(result['status'], 'uncertainty_unqualified')
        self.assertFalse(result['acceptance_changed'])

    def test_nominal_rounding_is_not_measured_noise_or_hard_floor(self):
        relative = assess_measurement_limits(comparison(), {})['object_windows'][0]
        absolute = assess_measurement_limits(comparison(relative=False), {})['object_windows'][0]
        self.assertAlmostEqual(relative['nominal_pixel_sensitivity']['independent_nearest_pixel_pair_2d_rms_px'], math.sqrt(2/3))
        self.assertAlmostEqual(absolute['nominal_pixel_sensitivity']['independent_nearest_pixel_pair_2d_rms_px'], math.sqrt(1/3))
        self.assertTrue(relative['nominal_pixel_sensitivity']['not_a_minimum_achievable_error'])
        self.assertEqual(relative['nominal_pixel_sensitivity']['status'], 'hypothetical_not_empirical_uncertainty')

    def test_manual_acceptance_number_cannot_qualify_uncertainty(self):
        result = assess_measurement_limits(comparison(), {}, independent_review={'status': 'pass', 'tolerance_px': 1.5})
        self.assertEqual(result['status'], 'uncertainty_unqualified')
        self.assertNotIn('estimated_noise_px', result)

    def test_static_sentinel_is_not_travel(self):
        result = assess_measurement_limits(comparison(scale=1.), {'position_rmse_fraction': .03})
        self.assertIsNone(result['object_windows'][0]['reference_motion_extent_px'])

    def test_missing_numeric_measurement_stays_unknown(self):
        value = comparison(); value['windows'][0]['object_windows'][0]['scale_px'] = None
        result = assess_measurement_limits(value, {})
        self.assertIsNone(result['object_windows'][0]['current_rmse_limit_px'])
        self.assertIsNone(result['object_windows'][0]['nominal_pixel_sensitivity'])

    def test_existing_gate_is_not_rewritten_by_advisory(self):
        value = comparison(); value['windows'][0]['object_windows'][0]['pass'] = False
        self.assertFalse(assess_measurement_limits(value, {})['object_windows'][0]['original_gate_pass'])

    def _timing_fixture(self):
        manifest = {'package_sha256': 'p', 'frames': [
            {'index': 0, 'time_s': 0., 'sha256': 'same'},
            {'index': 1, 'time_s': .2, 'sha256': 'same'},
            {'index': 2, 'time_s': .25, 'sha256': 'different'}],
            'inputs': {'events': [{'action': 'click', 'time_s': .01}]}}
        obs = {'package_sha256': 'p', 'objects': [{'id': 'o', 'frames': [
            {'index': 0, 'x': 0., 'y': 0., 'valid': True},
            {'index': 1, 'x': 0., 'y': 0., 'valid': True},
            {'index': 2, 'x': 3., 'y': 0., 'valid': True}]}]}
        return manifest, obs

    def test_equal_images_do_not_waive_unobserved_gap(self):
        manifest, obs = self._timing_fixture()
        result = assess_timing_scope(manifest, obs, {'status': 'fail', 'errors': ['motion_frame_gap_exceeded']})
        self.assertEqual(result['largest_intervals'][0]['endpoint_evidence'], 'pixel_endpoints_identical')
        self.assertEqual(result['largest_intervals'][0]['intervening_motion'], 'unknown')
        self.assertEqual(result['saved_timing']['status'], 'fail')
        self.assertEqual(result['gate_waivers'], [])
        self.assertFalse(result['settled_state_certified'])

    def test_missing_track_is_not_inferred_as_stationary(self):
        manifest, obs = self._timing_fixture(); obs['objects'][0]['frames'][2]['valid'] = False
        result = assess_timing_scope(manifest, obs, {})
        self.assertEqual(result['all_intervals'][1]['endpoint_evidence'], 'tracking_incomplete')

    def test_bad_clocks_or_wrong_images_are_rejected(self):
        manifest, obs = self._timing_fixture(); obs['package_sha256'] = 'different'
        with self.assertRaises(ValueError): assess_timing_scope(manifest, obs, {})
        obs['package_sha256'] = 'p'; manifest['frames'][1]['time_s'] = 0.
        with self.assertRaises(ValueError): assess_timing_scope(manifest, obs, {})

    def test_same_source_comparison_requires_two_bound_packages(self):
        first = {'source_sha256': 'source', 'observations': {'package_sha256': 'a'}}
        second = {'source_sha256': 'source', 'observations': {'package_sha256': 'b'}}
        result = summarize_same_source_pair(first, second, comparison())
        self.assertEqual(result['mean_normalized_rmse'], .02)
        self.assertFalse(result['accuracy_inferred'])
        with self.assertRaises(ValueError): summarize_same_source_pair(first, first, comparison())
        second['source_sha256'] = 'changed'
        with self.assertRaises(ValueError): summarize_same_source_pair(first, second, comparison())

    def test_wrong_comparison_cannot_be_relabelled_reproducibility(self):
        first = {'source_sha256': 'source', 'observations': {'package_sha256': 'a'}}
        second = {'source_sha256': 'source', 'observations': {'package_sha256': 'c'}}
        with self.assertRaises(ValueError): summarize_same_source_pair(first, second, comparison())


if __name__ == '__main__': unittest.main()
