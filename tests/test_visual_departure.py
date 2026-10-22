"""Observable departure must not depend on differentiating tiny point errors."""
import unittest

from kiroclops.visual.events import extract_events, MOTION_START_POLICY


def track(xs, times=None, *, identifier='a', ys=None):
    times = times or [i * .05 for i in range(len(xs))]
    ys = ys or [0.] * len(xs)
    return {'id': identifier, 'frames': [
        {'index': i, 'time_s': t, 'valid': x is not None,
         'x': x, 'y': y if x is not None else None, 'width': 4., 'height': 4.}
        for i, (x, y, t) in enumerate(zip(xs, ys, times))]}


def starts(result):
    return [e for e in result['events'] if e['type'] == 'motion_start']


class VisibleDepartureTests(unittest.TestCase):
    def test_slow_visible_departure_is_the_first_sampled_band_crossing(self):
        times = [i * .05 for i in range(16)]
        result = extract_events({'objects': [track([4.9*t for t in times], times)]})
        event = starts(result)[0]
        self.assertEqual(event['frame_indices'], [6, 7])
        self.assertEqual(event['time_interval_s'], [times[6], times[7]])
        self.assertLess(event['observed_pair_speed_px_s'], 5.)
        self.assertEqual(event['rest_anchor_frame_index'], 0)
        self.assertEqual(event['rest_anchor_px'], [0., 0.])
        self.assertGreater(event['departure_distance_px'], event['rest_radius_px'])
        self.assertEqual(event['predicate_version'], MOTION_START_POLICY['version'])
        self.assertFalse(event['instantaneous_speed_required'])
        self.assertFalse(event['physical_motion_onset_inferred'])
        self.assertEqual(result['motion_start_policy'], MOTION_START_POLICY)

    def test_tiny_speed_threshold_flip_keeps_the_visible_crossing(self):
        brackets = []
        for last in (1.532, 1.535):
            result = extract_events({'objects': [track([0., 1.45, last], [0., .08, .09667])]})
            event = starts(result)[0]
            brackets.append(event['frame_indices'])
        self.assertEqual(brackets, [[1, 2], [1, 2]])

    def test_identical_positions_with_different_valid_cadence_keep_first_crossing(self):
        xs = [0., .4, .8, 1.2, 1.6, 2.]
        for times in ([i*.1 for i in range(6)], [i*.025 for i in range(6)],
                      [0., .08, .11, .20, .24, .31]):
            result = extract_events({'objects': [track(xs, times)]})
            self.assertEqual(starts(result)[0]['frame_indices'], [3, 4])

    def test_direction_speed_setting_cannot_suppress_spatial_departure(self):
        result = extract_events({'objects': [track([0., .5, 1., 1.6])]},
                                contract={'event_options': {'speed_threshold_px_s': 500.}})
        self.assertEqual(starts(result)[0]['frame_indices'], [2, 3])

    def test_rest_band_is_strict_and_stationary_jitter_does_not_start(self):
        self.assertEqual(starts(extract_events({'objects': [track([0., 1.5, 0., 1.5, .4])]})), [])
        result = extract_events({'objects': [track([0., 1.5, 1.5001])]})
        self.assertEqual(starts(result)[0]['frame_indices'], [1, 2])

    def test_gap_and_invalid_observation_cannot_bridge_a_departure(self):
        for obj in (track([0., .2, 10., 10.4], [0., .05, .3, .35]),
                    track([0., None, 10., 10.4])):
            self.assertEqual(starts(extract_events({'objects': [obj]})), [])
        result = extract_events({'objects': [track([0., .2, 10., 12.], [0., .05, .3, .35])]})
        event = starts(result)[0]
        self.assertEqual(event['frame_indices'], [2, 3])
        self.assertEqual(event['start_context'], 'clip_or_gap_censored')
        self.assertEqual(event['rest_anchor_frame_index'], 2)

    def test_relative_child_uses_the_same_spatial_predicate(self):
        parent = track([20., 21., 22., 23.], identifier='parent')
        child = track([25., 26.5, 28., 29.6], identifier='child')
        result = extract_events({'objects': [parent, child]},
                                selection={'relative_motion': [{'object_id': 'child', 'relative_to': 'parent'}]},
                                contract={'event_options': {'speed_threshold_px_s': 500.}})
        event = next(e for e in starts(result) if e['object_ids'] == ['child', 'parent'])
        self.assertEqual(event['frame_indices'], [2, 3])
        self.assertEqual(event['rest_anchor_px'], [5., 0.])
        self.assertFalse(event['rigid_body_translation_inferred'])

    def test_wide_actual_crossing_bracket_remains_unresolved(self):
        result = extract_events({'objects': [track([0., 2.], [0., .11])]})
        event = starts(result)[0]
        self.assertEqual(event['time_interval_s'], [0., .11])
        self.assertEqual(event['timing_status'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
