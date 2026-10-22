"""A self-match cannot establish precision absent from the recorded pixels."""
import unittest
from unittest.mock import patch
from kiroclops.local_engine.web_reference import check_reference_comparison_coverage


class ReferenceEventObservability(unittest.TestCase):
    def run_reference(self, events):
        record={'scenario':{'id':'precision-control'},'functional':{'status':'pass'},
                'observations':{'package_sha256':'image-bound'},'events':events}
        comparison={'pass':True,'pixel_comparison':{'object_windows':[]},'dynamics':[],'relative_timing':[]}
        with patch('kiroclops.local_engine.evaluation._compare_web',return_value=comparison):
            return check_reference_comparison_coverage(record)

    def test_perfect_self_match_does_not_hide_unresolved_relative_event(self):
        result=self.run_reference({'events':[],'relations':[{'type':'relative_event_timing',
            'object_ids':['handle','star'],'uncertainty_s':.134,'timing_status':'unresolved'}]})
        self.assertEqual('fail',result['status'])
        self.assertEqual(1,len(result['unresolved_timing']))

    def test_resolved_observation_keeps_existing_comparator_verdict(self):
        result=self.run_reference({'events':[{'type':'motion_start','timing_status':'resolved'}],
            'relations':[{'type':'relative_event_timing','timing_status':'resolved'}]})
        self.assertEqual('pass',result['status'])
        self.assertEqual([],result['unresolved_timing'])

    def test_unresolved_event_is_not_a_model_motion_failure(self):
        result=self.run_reference({'events':[{'type':'motion_start','timing_status':'unresolved'}],'relations':[]})
        self.assertEqual('fail',result['status'])
        self.assertFalse(result['behavior_transfer_success_claimed'])
        self.assertEqual('event',result['unresolved_timing'][0]['scope'])

    def test_non_temporal_geometry_is_not_mistaken_for_missing_timing(self):
        result=self.run_reference({'events':[],'relations':[{'type':'object_pair_geometry','distance_px':5}]})
        self.assertEqual('pass',result['status'])

if __name__=='__main__':unittest.main()
