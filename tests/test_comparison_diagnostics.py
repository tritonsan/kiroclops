import unittest
from copy import deepcopy

from kiroclops.local_engine.comparison_diagnostics import explain_comparison


class ComparisonDiagnosticTests(unittest.TestCase):
    def test_small_child_error_retains_original_failure_and_pixel_units(self):
        value = {'pass': False, 'windows': [{'object_windows': [{
            'reference_id': 'star', 'relative_to_reference': 'handle', 'window_id': 'input-1',
            'scale_px': 12., 'position_rmse': .04, 'limits': {'rmse_limit': .03},
            'gates': {'position': False, 'endpoint': True, 'coverage': True},
        }]}]}
        original = deepcopy(value)
        result = explain_comparison(value)
        row = result['failed_gates'][0]
        self.assertEqual(row['measured_px'], .48)
        self.assertEqual(row['limit_px'], .36)
        self.assertEqual(row['relative_to'], 'handle')
        self.assertFalse(result['original_pass'])
        self.assertFalse(result['acceptance_changed'])
        self.assertEqual(value, original)

    def test_interruption_history_and_unscored_drag_are_visible(self):
        ref = [{'action': 'click', 'time_s': .4}, {'action': 'drag_start', 'time_s': .7},
               {'action': 'drag_end', 'time_s': 1.}]
        candidate = deepcopy(ref)
        candidate[1]['time_s'] = .72
        result = explain_comparison({'pass': True, 'windows': [{'object_windows': [{'window_id': 'input-0'}]}]}, ref, candidate)
        self.assertAlmostEqual(result['input_history'][1]['previous_interval_difference_s'], .02)
        self.assertFalse(result['input_history'][1]['has_scored_response_window'])
        self.assertTrue(result['original_pass'])

    def test_unobservable_dynamics_are_not_misreported_as_motion_failure(self):
        row = {'observable': False, 'pass': False, 'reason': 'reference_interrupted_or_unsettled'}
        result = explain_comparison({'pass': True, 'dynamics': [row]})
        self.assertEqual(result['failed_gates'], [])
        self.assertEqual(result['unobservable_dynamics'], [row])

    def test_missing_inputs_and_missing_numeric_values_are_not_fabricated(self):
        result = explain_comparison({'windows': [{'object_windows': [{'gates': {'coverage': False}}]}]},
                                    [{'action': 'click', 'time_s': 1}], [])
        self.assertFalse(result['input_count_matches'])
        self.assertIsNone(result['failed_gates'][0]['measured_px'])

    def test_gain_is_only_failure_when_required(self):
        c = {'improvement_gates': [{'pass': False}]}
        self.assertEqual(explain_comparison(c)['failed_gates'], [])
        c['improvement_required'] = True
        self.assertEqual(explain_comparison(c)['failed_gates'][0]['category'], 'insufficient_improvement')


if __name__ == '__main__':
    unittest.main()
