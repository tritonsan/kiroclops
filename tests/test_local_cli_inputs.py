import unittest
from kiroclops.local_engine.__main__ import declared_clicks,declared_actions
from kiroclops.visual.schema import VisualError


class DeclaredInputsTests(unittest.TestCase):
    def test_clicks_are_explicit_not_recorder_verified(self):
        result=declared_clicks(['0.4:#open-panel','1.8:button:first-child'])
        self.assertEqual('user_declared',result['provenance'])
        self.assertEqual([.4,1.8],[e['time_s'] for e in result['events']])
        self.assertEqual('button:first-child',result['events'][1]['selector'])

    def test_invalid_or_reordered_times_rejected(self):
        for values in (['NaN:#open'],['-1:#open'],['1:#open','0.5:#close'],['0.5:'],['0.5']):
            with self.subTest(values=values),self.assertRaises(VisualError):declared_clicks(values)

    def test_game_actions_keep_explicit_press_release_and_provenance(self):
        result=declared_actions(['0.5:jump:press','0.55:jump:release'])
        self.assertEqual('user_declared',result['provenance'])
        self.assertEqual([True,False],[e['pressed'] for e in result['events']])
        self.assertEqual([.5,.55],[e['time_s'] for e in result['events']])

    def test_invalid_game_actions_do_not_guess_intent(self):
        for values in (['.5:jump:hold'],['.5:dash:press'],['nan:left:press'],['1:left:press','.5:left:release'],['oops:jump:press']):
            with self.subTest(values=values),self.assertRaises(VisualError):declared_actions(values)


if __name__=='__main__':unittest.main()
