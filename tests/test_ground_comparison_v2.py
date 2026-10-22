"""Integration of explicit v2 speed policy without relaxing other motion gates."""
from copy import deepcopy
import math
import unittest

from kiroclops.ground import comparison
from kiroclops.ground.analysis import SCENARIOS
from kiroclops.integration import ground_comparison_v2 as repair


def report(role, sid='r48', repeat=1, velocity=None):
    height = 7 if role == 'reference' else 24
    velocity = (18.0 if role == 'before' else 7.67) if velocity is None else velocity
    frames = []
    for i in range(240):
        x = math.floor(velocity*height*i/60 + .73)
        frames.append({'frame_index': i, 'time_s': i/60, 'valid': True,
                       'x': x, 'x_body': x/height})
    return {'status': 'ok', 'fps': 60, 'game': 'tiny' if role == 'reference' else 'slay',
            'variant': role, 'run_id': f'{role}-{sid}-{repeat}', 'body_height_px': height,
            'camera_valid': True, 'valid_ratio': 1., 'frames': frames,
            'scenario': {'id': sid, 'repeat': repeat, 'spawn_offset': 0,
                'split': 'calibration' if sid in ('r06', 'r48') else 'validation',
                'events': [{'tick': t, 'action': a, 'pressed': p} for t,a,p in SCENARIOS[sid]]},
            'metrics': {'speed_body_s': 7.04 if role == 'reference' else 7.95,
                        'stop_distance_body': 0., 'end_position_body': 0.}}


def matrix():
    return [[report(role, sid, repeat) for sid in ('r06', 'r48') for repeat in (1,2,3)]
            for role in ('reference', 'before', 'after')]


class GroundComparisonRepairTests(unittest.TestCase):
    def test_calibration_uses_native_speed_evidence_without_rewriting_old_measurements(self):
        groups = matrix()
        untouched = deepcopy(groups)
        self.assertFalse(comparison.calibration_feedback(*groups)['eligible'])
        with repair.repaired_measurements():
            result = comparison.calibration_feedback(*groups)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['numerical_status'], 'pass')
        self.assertEqual(result['manual_validation_status'], 'pending')
        self.assertEqual(result['measurement_policy'], repair.POLICY_ID)
        self.assertEqual(groups, untouched)
        self.assertFalse(comparison.calibration_feedback(*groups)['eligible'])

    def test_short_press_exemption_does_not_exempt_curve(self):
        ref, before, after = [report(role, 'r18') for role in ('reference','before','after')]
        for frame in after['frames'][30:78]:
            frame['x'] += 24
            frame['x_body'] += 1
        row = repair.speed_triplet(comparison._triplet, ref, before, after)
        self.assertFalse(row['speed_eligible'])
        self.assertTrue(row['gates']['speed'])
        self.assertFalse(row['gates']['curve_rmse'])
        self.assertFalse(row['pass'])

    def test_short_press_exemption_does_not_exempt_brake_or_endpoint(self):
        ref, before, after = [report(role, 'reverse24') for role in ('reference','before','after')]
        after['metrics'].update(stop_distance_body=.6, end_position_body=.7)
        row = repair.speed_triplet(comparison._triplet, ref, before, after)
        self.assertFalse(row['speed_eligible'])
        self.assertFalse(row['gates']['stop_distance'])
        self.assertFalse(row['gates']['end_position'])
        self.assertFalse(row['pass'])

    def test_long_hold_speed_mismatch_remains_failure(self):
        row = repair.speed_triplet(comparison._triplet, report('reference'), report('before'),
                                   report('after', velocity=9.2))
        self.assertEqual(row['speed_measurement_status'], 'fail')
        self.assertFalse(row['gates']['speed'])
        self.assertFalse(row['pass'])

    def test_invalid_pixel_to_body_mapping_cannot_be_accepted(self):
        groups = matrix()
        groups[2][-1]['frames'][50]['x'] += 10
        with repair.repaired_measurements():
            result = comparison.calibration_feedback(*groups)
        self.assertEqual(result['numerical_status'], 'fail')
        self.assertEqual(result['speed_measurement_status'], 'invalid')
        self.assertFalse(result['eligible'])
        self.assertTrue(any('invalid v2 speed' in error for error in result['errors']))

    def test_missing_scenario_remains_incomplete(self):
        groups = matrix()
        groups[2].pop()
        with repair.repaired_measurements():
            result = comparison.calibration_feedback(*groups)
        self.assertEqual(result['coverage']['complete_triplets'], 5)
        self.assertEqual(result['numerical_status'], 'pending')
        self.assertEqual(result['speed_measurement_status'], 'incomplete')
        self.assertFalse(result['eligible'])

    def test_only_uncertain_speed_is_pending_not_motion_fail(self):
        result = comparison.compare_runs(*matrix(), split='calibration')
        for row in result['scenario_checks']:
            row['gates'] = {k: k != 'speed' for k in row['gates']}
            row.update(speed_measurement_status='indeterminate', **{'pass': False})
        result = repair.classify_comparison(result)
        self.assertEqual(result['numerical_status'], 'pending')
        self.assertFalse(result['success'])
        self.assertEqual(result['speed_measurement_status'], 'indeterminate')

    def test_pending_speed_does_not_hide_other_gate_failure(self):
        result = comparison.compare_runs(*matrix(), split='calibration')
        for row in result['scenario_checks']:
            row['gates'] = {k: k != 'speed' for k in row['gates']}
            row.update(speed_measurement_status='indeterminate', **{'pass': False})
        result['scenario_checks'][0]['gates']['curve_rmse'] = False
        self.assertEqual(repair.classify_comparison(result)['numerical_status'], 'fail')

    def test_context_restores_entrypoints_after_exception(self):
        from kiroclops.integration import ground_worker
        original = (comparison._triplet, comparison.compare_runs, ground_worker._prepare_feedback,
                    ground_worker.combine_comparisons)
        with self.assertRaises(RuntimeError):
            with repair.repaired_measurements():
                raise RuntimeError('interrupted')
        self.assertEqual(original, (comparison._triplet, comparison.compare_runs, ground_worker._prepare_feedback,
                                   ground_worker.combine_comparisons))


if __name__ == '__main__':
    unittest.main()
