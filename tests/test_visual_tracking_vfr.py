"""Timestamp-scaled pixel search must not relax identity or invent positions."""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.tracking import analyze_package


class VariableIntervalTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = np.random.default_rng(417).integers(40,120,(14,16,3),dtype=np.uint8)
        self.times = [0.,.02,.04,.12,.14,.16,.18]
        self.positions = [20,30,40,80,90,100,110]
        self.counter = 0

    def analyze(self, *, times=None, mutation=None):
        self.counter += 1
        directory=self.root/str(self.counter);directory.mkdir()
        sources=[]
        for i,x in enumerate(self.positions):
            image=np.zeros((100,320,3),np.uint8)
            image[40:54,x:x+16]=self.patch
            if mutation:mutation(i,image,x)
            path=directory/f'{i}.png'
            path.write_bytes(cv2.imencode('.png',image)[1].tobytes());sources.append(path)
        package=directory/'package'
        create_package(package,sources,times or self.times)
        selection={'schema_version':1,'objects':[{'id':'glyph','roi':[20,40,16,14],
                    'anchor':[8,7],'search_radius':14,'min_score':.75,'min_margin':.03,
                    'max_color_rmse':12}]}
        return analyze_package(package,selection)

    def test_real_long_interval_uses_larger_search_without_time_rescaling(self):
        result=self.analyze()
        frames=result['objects'][0]['frames']
        self.assertEqual(result['status'],'ok')
        self.assertEqual([f['x'] for f in frames],[x+8 for x in self.positions])
        self.assertEqual([f['time_s'] for f in frames],self.times)
        self.assertAlmostEqual(frames[3]['search_radius_px'],56)
        self.assertAlmostEqual(frames[4]['search_radius_px'],14)
        self.assertFalse(result['search_timing']['predicted_positions_emitted'])

    def test_same_displacement_without_long_interval_remains_out_of_bounds(self):
        result=self.analyze(times=[i*.02 for i in range(7)])
        frame=result['objects'][0]['frames'][3]
        self.assertFalse(frame['valid'])
        self.assertIsNone(frame['x'])
        self.assertEqual(result['status'],'rejected')

    def test_wider_window_still_rejects_identical_competing_identity(self):
        def duplicate(i,image,x):
            if i==3:image[40:54,52:68]=self.patch
        result=self.analyze(mutation=duplicate)
        frame=result['objects'][0]['frames'][3]
        self.assertFalse(frame['valid'])
        self.assertLess(frame['margin'],.03)
        self.assertIsNone(frame['x'])

    def test_wider_window_keeps_color_identity_gate(self):
        def wrong_identity(i,image,x):
            if i==3:image[40:54,x:x+16]=self.patch+65
        result=self.analyze(mutation=wrong_identity)
        frame=result['objects'][0]['frames'][3]
        self.assertFalse(frame['valid'])
        self.assertIsNone(frame['x'])
        self.assertEqual(result['status'],'rejected')

    def test_occlusion_does_not_emit_prediction_or_accumulate_search_extent(self):
        def missing(i,image,x):
            if i in (3,4):image[:]=0
        result=self.analyze(mutation=missing)
        frames=result['objects'][0]['frames']
        for frame in frames[3:]:
            self.assertFalse(frame['valid'])
            self.assertIsNone(frame['x'])
            self.assertIsNone(frame['y'])
        self.assertAlmostEqual(frames[4]['search_radius_px'],14)
        self.assertAlmostEqual(frames[5]['search_radius_px'],14)

    def test_observed_cadence_not_assumed_fps_sets_extent(self):
        a=self.analyze()
        b=self.analyze(times=[5+3*t for t in self.times])
        for first,second in zip(a['objects'][0]['frames'],b['objects'][0]['frames']):
            self.assertEqual(first['x'],second['x'])
            self.assertAlmostEqual(first['search_radius_px'],second['search_radius_px'])


if __name__=='__main__':unittest.main()
