import unittest
import cv2
import numpy as np
from kiroclops.local_engine.visual_boundary import cases,classify,visible_wall
from kiroclops.visual.schema import VisualError


def inputs(xs,ys,distances):
    frames=[{'index':30+i,'valid':True,'x':x,'y':y} for i,(x,y) in enumerate(zip(xs,ys))]
    return ({'status':'ok','objects':[{'frames':frames}]},
            {'relations':[{'index':30+i,'type':'boundary_distance','boundary_id':'wall','distance_px':d} for i,d in enumerate(distances)]})


class VisibleBoundaryTests(unittest.TestCase):
    def test_eight_mirrored_input_only_cases(self):
        rows=cases();self.assertEqual(len(rows),8)
        for row in rows:
            self.assertEqual(row['frame_count'],240)
            self.assertEqual(row['warmup_ticks'],60)
            self.assertEqual(sorted(row['events'],key=lambda e:(e['tick'],e['pressed'],e['action'])),row['events'])
            self.assertFalse(any('velocity' in key or 'position' in key for key in row))

    def test_approach_means_pixel_proximity_not_physics_collision(self):
        distances=[max(7,30-i*2) for i in range(35)]
        result=classify(*inputs([100-d for d in distances],[20]*35,distances))
        self.assertEqual(result['classification'],'visible_approach')
        self.assertFalse(result['physics_collision_inferred'])

    def test_parallel_motion_and_distant_negative(self):
        result=classify(*inputs([80]*30,list(range(30)),[7]*30))
        self.assertEqual(result['classification'],'visible_parallel_motion')
        distant=classify(*inputs([80]*30,list(range(30)),[30]*30))
        self.assertEqual(distant['classification'],'distant_motion')

    def test_departure_is_not_labeled_for_an_unobserved_gap(self):
        distances=[max(6,25-i*2) for i in range(15)]+[6+i*2 for i in range(20)]
        a,b=inputs([100-d for d in distances],[20]*35,distances)
        result=classify(a,b)
        self.assertEqual(result['classification'],'visible_departure_after_approach')
        a['status']='rejected'
        self.assertEqual(classify(a,b)['status'],'unobservable')

    def test_pixel_edge_selection_and_no_visible_wall_rejection(self):
        image=np.zeros((180,320,3),np.uint8)
        cv2.rectangle(image,(208,64),(223,159),(220,220,220),-1)
        result=visible_wall(image,'right')
        self.assertAlmostEqual(result['points'][0][0],208,delta=1)
        with self.assertRaises(VisualError):visible_wall(np.zeros_like(image),'right')


if __name__=='__main__':unittest.main()
