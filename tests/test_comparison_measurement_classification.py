"""Missing temporal support must not certify motion or improvement."""
from copy import deepcopy
import unittest

from tests.test_local_evaluation import record
from kiroclops.local_engine.evaluation import _compare_web
from kiroclops.local_engine.media_session import _compare, VIEWPORT_ALIGNMENT
from kiroclops.local_engine.comparison_diagnostics import explain_comparison


def with_gap(value):
    value = deepcopy(value)
    for obj in value['observations']['objects']:
        obj['frames'] = [f for f in obj['frames'] if not .55 < f['time_s'] < .85]
        for index, frame in enumerate(obj['frames']):
            frame['index'] = index
    return value


class ComparisonMeasurementClassificationTests(unittest.TestCase):
    def test_benchmark_missing_temporal_support_is_measurement_failure(self):
        ref = record()
        result = _compare_web(ref, ref, with_gap(ref), improvement=False)
        self.assertFalse(result['pass'])
        self.assertFalse(result['measurement_pass'])
        self.assertEqual('comparison_coverage_failure', result['failure_class'])

    def test_benchmark_incomplete_baseline_cannot_certify_improvement(self):
        ref = record()
        result = _compare_web(ref, with_gap(record(amplitude=65)), ref, improvement=True)
        self.assertFalse(result['pass'])
        self.assertFalse(result['baseline_measurement_pass'])
        self.assertFalse(result['measurement_pass'])
        self.assertEqual('baseline_comparison_coverage_failure', result['failure_class'])

    def test_motion_error_with_complete_pixels_stays_measurable(self):
        result = _compare_web(record(), record(), record(amplitude=65), improvement=False)
        self.assertFalse(result['pass'])
        self.assertTrue(result['measurement_pass'])

    def test_media_incomplete_baseline_cannot_certify_improvement(self):
        ref = record(static=False)
        binding = {'scenario': ref['scenario'], 'spatial_alignment': VIEWPORT_ALIGNMENT,
                   'input_provenance': 'recorder_verified', 'claim': 'recorded_input_response'}
        result = _compare(ref, with_gap(record(amplitude=65, static=False)), ref,
                          binding, require_improvement=True)
        self.assertFalse(result['pass'])
        self.assertEqual('insufficient_observability', result['measurement_status'])
        self.assertEqual('baseline_comparison_coverage_failure', result['failure_class'])
        self.assertFalse(result['baseline_measurement_pass'])

    def test_null_metrics_under_coverage_failure_are_not_motion_mismatch(self):
        result = explain_comparison({'pass': False, 'windows': [{'object_windows': [{
            'gates': {'coverage': False, 'position': False, 'size': False, 'endpoint': False},
            'position_rmse': None, 'size_rmse': None, 'endpoint_error': None,
            'time_window_s': [0, .00055], 'valid_time_ratio': 0.,
            'scale_px': 1., 'limits': {'rmse_limit': .03},
        }]}]})
        self.assertTrue(all(v['category'] == 'measurement_coverage' for v in result['failed_gates']))
        self.assertEqual(result['failed_gates'][0]['time_window_s'], [0, .00055])


if __name__ == '__main__':
    unittest.main()
