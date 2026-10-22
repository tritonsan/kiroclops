import copy
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import visual_boundary_v4 as boundary
from kiroclops.visual.schema import VisualError,write_json,file_sha256


class FixtureValidityTests(unittest.TestCase):
    def recording(self):
        return {'frames':[{'frame_index':i,'physics_tick':60+i,'actions':['jump'] if i==57 else []} for i in range(240)],
                'observations':[{'frame_index':i,'physics_tick':60+i,'on_floor':not 30<=i<=70,'on_wall':50<=i<=60} for i in range(240)]}

    def case(self,kind='rebound'):
        return next(c for c in boundary.cases() if c['id']=='left-'+kind)

    def test_new_recipe_is_fixed_long_first_hold_and_separate_second_edge(self):
        self.assertEqual(len(boundary.cases()),8)
        for side in ('left','right'):
            row=next(c for c in boundary.cases() if c['id']==side+'-rebound')
            self.assertEqual([(e['tick'],e['pressed']) for e in row['events'] if e['action']=='jump'],[(30,True),(54,False),(57,True),(60,False)])
            self.assertEqual(row['frame_count'],240);self.assertEqual(row['warmup_ticks'],60)

    def test_second_edge_uses_prior_postdraw_contact_not_later_airborne_state(self):
        recording=self.recording();self.assertEqual(boundary._fixture_value(recording,self.case())['status'],'pass')
        recording['observations'][56]['on_floor']=True
        value=boundary._fixture_value(recording,self.case())
        self.assertEqual(value['status'],'invalid_fixture_for_behavior')
        self.assertFalse(value['checks']['second_edge_airborne'])
        self.assertFalse(recording['observations'][57]['on_floor'])

    def test_missing_wall_or_missing_new_input_edge_is_inconclusive_not_motion_failure(self):
        for change in ('wall','edge'):
            recording=self.recording()
            if change=='wall':recording['observations'][56]['on_wall']=False
            else:recording['frames'][56]['actions']=['jump']
            result=boundary._fixture_value(recording,self.case())
            self.assertEqual(result['status'],'invalid_fixture_for_behavior')
            self.assertNotIn('classification',result)

    def test_parallel_fixture_requires_contiguous_airborne_contact_window(self):
        recording=self.recording()
        self.assertEqual(boundary._fixture_value(recording,self.case('parallel'))['status'],'pass')
        for i in (52,56,60):recording['observations'][i]['on_floor']=True
        self.assertEqual(boundary._fixture_value(recording,self.case('parallel'))['status'],'invalid_fixture_for_behavior')

    def test_clock_or_nonboolean_contact_is_capture_error_not_valid_fixture(self):
        for key,value in [('physics_tick',56),('on_floor',0)]:
            recording=self.recording();recording['observations'][56][key]=value
            with self.assertRaises(VisualError):boundary._fixture_value(recording,self.case())

    def test_private_receipt_is_exact_raw_recording_bound_and_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);recording=self.recording();scenario=self.case()
            write_json(root/'recording.json',recording)
            value={**boundary._fixture_value(recording,scenario),'recording_sha256':file_sha256(root/'recording.json')}
            write_json(root/'private-fixture-validity.json',value)
            self.assertEqual(boundary.fixture_validity(root,scenario),value)
            recording['observations'][56]['on_floor']=True;write_json(root/'recording.json',recording)
            with self.assertRaisesRegex(VisualError,'receipt differs'):boundary.fixture_validity(root,scenario)

    def test_pixel_classifier_is_identical_to_preexisting_qualified_algorithm(self):
        from kiroclops.local_engine import visual_boundary
        self.assertIs(boundary.classify,visual_boundary.classify)
        self.assertIs(boundary.visible_wall,visual_boundary.visible_wall)
        self.assertIn('kiroclops\\local_engine\\visual_boundary.py',boundary.policy()['source_hashes'])


if __name__=='__main__':unittest.main()
