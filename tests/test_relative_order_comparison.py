"""Order summaries must retain the uncertainty of the observed intervals."""
import unittest
from unittest.mock import patch

from kiroclops.local_engine.evaluation import _relative_timing_checks


class RelativeOrderComparisonTests(unittest.TestCase):
    def check(self, a, b, *, status='resolved', missing=False):
        def event(value, resolved='resolved'):
            return {'type':'relative_event_timing', 'event_type':'motion_start',
                    'object_ids':['title','content'], 'relative_to_object_id':'panel',
                    'occurrence':0, 'order':value[0], 'lag_interval_s':value[1],
                    'timing_status':resolved}
        records=[{'relations':[event(a)]}, {'relations':[] if missing else [event(b,status)]}]
        item={'observations':{}, 'selection':{'relative_motion':[{},{}]}}
        with patch('kiroclops.visual.events.extract_events', side_effect=records):
            return _relative_timing_checks(item,item,{})[0]

    def test_same_behavior_across_order_resolution_boundary(self):
        a=('overlapping_uncertainty',[.04957,.08547]);b=('before',[.05107,.08458])
        for x,y in ((a,b),(b,a)):
            row=self.check(x,y)
            self.assertTrue(row['pass'])
            self.assertEqual(row['order_comparison'],'overlapping_lag_intervals')
            self.assertEqual(row['lag_tolerance_s'],.1)

    def test_negative_lag_boundary_is_symmetric(self):
        self.assertTrue(self.check(('after',[-.085,-.051]),('overlapping_uncertainty',[-.086,-.049]))['pass'])

    def test_disjoint_ambiguous_interval_does_not_certify_resolved_order(self):
        self.assertFalse(self.check(('before',[.09,.12]),('overlapping_uncertainty',[.01,.04]))['pass'])

    def test_reversed_order_is_rejected_even_within_lag_tolerance(self):
        self.assertFalse(self.check(('before',[.051,.07]),('after',[-.07,-.051]))['pass'])

    def test_unresolved_or_missing_onset_is_not_accepted(self):
        a=('before',[.06,.08])
        self.assertFalse(self.check(a,a,status='unresolved')['pass'])
        self.assertFalse(self.check(a,a,missing=True)['pass'])

    def test_same_label_still_requires_original_lag_tolerance(self):
        self.assertFalse(self.check(('before',[.06,.08]),('before',[.25,.28]))['pass'])


if __name__=='__main__':unittest.main()
