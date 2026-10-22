import unittest

from kiroclops.visual.raw_event_review import candidates, relative_timing, CONFIRMED, LEGACY
from kiroclops.visual.schema import VisualError


def rows(xs, dt=.05):
    return [{'index': i, 'time_s': i * dt, 'points': {'a': {'x': x, 'y': 0.}}}
            for i, x in enumerate(xs)]


class RawEventReviewTests(unittest.TestCase):
    def test_residual_drift_does_not_manufacture_a_new_episode(self):
        trace = rows([0., 0., 4., 6., 6.2, 6.5, 6.8, 7., 7.3, 7.6, 7.8, 8., 8., 8., 8., 8., 8., 8.])
        old = candidates(trace, ['a'], policy=LEGACY)
        new = candidates(trace, ['a'], policy=CONFIRMED)
        self.assertEqual(2, sum(e['type'] == 'motion_start' for e in old))
        self.assertEqual(1, sum(e['type'] == 'motion_start' for e in new))
        self.assertTrue(all(e['status'] == 'unsigned' for e in new))

    def test_real_later_departure_is_preserved(self):
        trace = rows([0.] * 5 + [4., 8.] + [8.] * 8 + [12., 16.] + [16.] * 8)
        events = candidates(trace, ['a'])
        self.assertEqual(2, sum(e['type'] == 'motion_start' for e in events))
        self.assertEqual(2, sum(e['type'] == 'settled' for e in events))

    def test_order_respects_observation_resolution(self):
        value = relative_timing([0., .02], [.054, .074], [.016] * 5)
        self.assertEqual('overlapping_uncertainty', value['order'])
        self.assertEqual(.05, value['order_resolution_s'])
        self.assertEqual('before', relative_timing([0., .02], [.08, .10], [.016])['order'])
        self.assertEqual('after', relative_timing([.08, .10], [0., .02], [.016])['order'])
        self.assertEqual('before', relative_timing([0., .02], [.054, .074], [.016], policy=LEGACY)['order'])

    def test_broad_interval_remains_unresolved_even_when_both_agree(self):
        value = relative_timing([0., .06184], [0., .06184], [.016])
        self.assertEqual('unresolved', value['timing_status'])
        self.assertEqual('overlapping_uncertainty', value['order'])
        self.assertGreater(value['uncertainty_s'], .1)

    def test_unknown_definition_and_bad_times_are_rejected(self):
        with self.assertRaises(VisualError):
            candidates(rows([0., 5.]), ['a'], policy='guessed')
        trace = rows([0., 5., 10.]); trace[2]['time_s'] = trace[1]['time_s']
        with self.assertRaises(VisualError):
            candidates(trace, ['a'])
        with self.assertRaises(VisualError):
            relative_timing([1., 0.], [0., 1.], [.02])

    def test_gap_and_missing_anchor_do_not_supply_rest(self):
        trace = rows([0., 4., 4., 4., 4.])
        trace[2]['points']['a'] = {'x': None, 'y': None}
        self.assertFalse([e for e in candidates(trace, ['a']) if e['type'] == 'settled'])
        trace = rows([0., 4., 4., 4., 4.]); trace[2]['time_s'] += .5
        trace[3]['time_s'] += .5; trace[4]['time_s'] += .5
        self.assertFalse([e for e in candidates(trace, ['a']) if e['type'] == 'settled'])


if __name__ == '__main__':
    unittest.main()
