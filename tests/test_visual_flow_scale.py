"""Actual rendered size changes must survive forward/backward flow checks."""
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.tracking import analyze_package


class FlowScaleTests(unittest.TestCase):
    def test_textured_card_scales_and_translates_with_measured_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw';raw.mkdir()
            rng=np.random.default_rng(412);card=rng.integers(35,235,(50,60,3),dtype=np.uint8)
            base=np.full((220,320,3),12,np.uint8);base[70:120,80:140]=card
            paths=[];times=[0,.021,.050,.084,.108,.139,.180,.214,.242]
            for i in range(len(times)):
                s=1+i*.035
                # Scale about the selected card center, then translate. Ground
                # truth belongs to the test image generator, not the analyzer.
                transform=np.array([[s,0,110*(1-s)+i*1.5],[0,s,95*(1-s)-i*.7]],np.float32)
                frame=cv2.warpAffine(base,transform,(320,220),flags=cv2.INTER_LINEAR,borderValue=(12,12,12))
                path=raw/f'{i:06}.png';cv2.imencode('.png',frame)[1].tofile(path);paths.append(path)
            create_package(root/'package',paths,times)
            selection={'schema_version':1,'objects':[{'id':'card','method':'flow','roi':[80,70,60,50],'anchor':[30,25],
                                   'min_features':8,'max_fb_error_px':1.,'min_inlier_ratio':.8}]}
            measured=analyze_package(root/'package',selection)
            self.assertEqual('ok',measured['status']);frames=measured['objects'][0]['frames']
            self.assertTrue(all(f['valid'] for f in frames));last=frames[-1];s=1+8*.035
            self.assertAlmostEqual(122,last['x'],delta=1.)
            self.assertAlmostEqual(89.4,last['y'],delta=1.)
            self.assertAlmostEqual(60*s,last['width'],delta=2.)
            self.assertAlmostEqual(50*s,last['height'],delta=2.)


if __name__=='__main__':unittest.main()
