"""Rest confirmation must not leave a nearly exhausted departure anchor."""
import unittest

from kiroclops.visual.events import extract_events
from kiroclops.visual.schema import VisualError
from tests.test_visual_events import observed


class ConfirmedRestPolicyTests(unittest.TestCase):
    def series(self, ending=None):
        # Fast motion, then 1.5 px of visible slow creep over the rest window.
        # A tiny continued drift must not create a new departure from stale x=3.
        return [0, 0, 3, 3.3, 3.6, 3.9, 4.2, 4.49] + (ending or [4.51]*8)

    def run_series(self, xs, **options):
        return extract_events({'objects': [observed(xs)]}, contract={
            'event_options': {'rest_duration_s': .25, **options}})

    def test_confirmed_rest_rebases_the_next_departure_without_spurious_restart(self):
        result = self.run_series(self.series())
        self.assertEqual(len([e for e in result['events'] if e['type']=='motion_start']), 1)
        self.assertEqual(len([e for e in result['events'] if e['type']=='stop']), 1)
        self.assertEqual(result['motion_start_policy']['version'], 'observed-confirmed-rest-departure-v3')

    def test_a_real_later_departure_still_emits_from_confirmation_image(self):
        result = self.run_series(self.series([4.51]*8+[6.2,8.]))
        starts = [e for e in result['events'] if e['type']=='motion_start']
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[-1]['rest_anchor_frame_index'], 7)
        self.assertEqual(starts[-1]['frame_indices'], [15,16])

    def test_historical_rule_can_be_explicitly_replayed(self):
        result = self.run_series(self.series(), motion_policy='observed-rest-band-departure-v2')
        self.assertEqual(len([e for e in result['events'] if e['type']=='motion_start']), 2)
        self.assertEqual(result['motion_start_policy']['version'], 'observed-rest-band-departure-v2')

    def test_unknown_rule_is_rejected(self):
        with self.assertRaises(VisualError):
            self.run_series(self.series(), motion_policy='pass-everything')

    def test_small_coordinate_perturbation_does_not_cascade_into_extra_episodes(self):
        xs=self.series()
        for delta in (-.005, .005):
            noisy=[x+(delta if i>=8 else 0.) for i,x in enumerate(xs)]
            events=self.run_series(noisy)['events']
            self.assertEqual(len([e for e in events if e['type']=='motion_start']), 1)

    def test_subresolution_order_is_not_claimed_but_lag_is_preserved(self):
        a=observed([0]*5+[3]*12,oid='a')
        b=observed([0]*6+[3]*11,oid='b')
        result=extract_events({'objects':[a,b]})
        row=next(r for r in result['relations'] if r['type']=='relative_event_timing' and r['event_type']=='motion_start')
        self.assertEqual(row['order'], 'overlapping_uncertainty')
        self.assertGreater(row['order_resolution_s'], 0.)
        self.assertAlmostEqual(row['lag_interval_s'][1], .1)

    def test_large_order_remains_observable(self):
        a=observed([0]*5+[3]*15,oid='a')
        b=observed([0]*10+[3]*10,oid='b')
        result=extract_events({'objects':[a,b]})
        row=next(r for r in result['relations'] if r['type']=='relative_event_timing' and r['event_type']=='motion_start')
        self.assertEqual(row['order'], 'before')


if __name__=='__main__':unittest.main()
