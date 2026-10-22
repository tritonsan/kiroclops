import unittest
import numpy as np

from kiroclops.visual.ncc_peak import exact_ncc, resolve_peak


class NccPeakStabilityTests(unittest.TestCase):
    def test_native_float_rounding_cannot_reverse_exact_integer_tie(self):
        template=np.tile(np.array([0,255,0],np.uint8),(3,1))
        image=np.tile(np.array([0,127,127,0],np.uint8),(3,1))
        score=exact_ncc(image[:,:3],template)
        self.assertEqual(score,exact_ncc(image[:,1:],template))
        for response in (np.array([[score,score+.0000004]],np.float32),np.array([[score+.0000004,score]],np.float32)):
            peak=int(response.argmax())
            x,y,new_score=resolve_peak(image,template,response,peak,0,float(response.max()))
            self.assertEqual((0,0),(x,y));self.assertEqual(score,new_score)

    def test_genuine_better_observed_patch_wins(self):
        template=np.tile(np.array([0,255,0],np.uint8),(3,1))
        image=np.tile(np.array([0,0,255,0],np.uint8),(3,1))
        x,y,score=resolve_peak(image,template,np.array([[.9999999,1]],np.float32),0,0,1.)
        self.assertEqual((1,0,1.),(x,y,score))

    def test_constant_patch_not_assigned_a_fabricated_correlation(self):
        self.assertIsNone(exact_ncc(np.zeros((3,3),np.uint8),np.zeros((3,3),np.uint8)))


if __name__=='__main__':unittest.main()
