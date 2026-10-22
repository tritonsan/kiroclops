import unittest
from pathlib import Path
import tempfile
import cv2
import numpy as np

from kiroclops.visual.polyline import visible_polyline
from kiroclops.visual.package import create_package
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.events import extract_events


class PolylineTests(unittest.TestCase):
    def scene(self,*,detached=0,branch=False,broken=False):
        image=np.zeros((150,260,3),np.uint8)
        objects={'a':{'valid':True,'x':30.,'y':55.,'left':10.,'top':35.,'width':40.,'height':40.},
                 'b':{'valid':True,'x':225.,'y':100.,'left':205.,'top':80.,'width':40.,'height':40.}}
        for frame in objects.values():cv2.rectangle(image,(int(frame['left']),int(frame['top'])),(int(frame['left']+frame['width']),int(frame['top']+frame['height'])),(90,45,20),-1)
        points=np.asarray([[50+detached,55],[120,55],[120,100],[205-detached,100]],np.int32)
        cv2.polylines(image,[points],False,(190,200,220),3,cv2.LINE_8)
        if branch:cv2.line(image,(120,78),(170,78),(190,200,220),3)
        if broken:cv2.rectangle(image,(115,72),(125,82),(0,0,0),-1)
        spec={'from_object':'a','to_object':'b','stroke_rgb':[220,200,190],'stroke_tolerance':5}
        return image,spec,objects

    def test_real_three_segment_stroke_endpoints_are_measured(self):
        measured=visible_polyline(*self.scene())
        self.assertTrue(measured['valid']);self.assertFalse(measured['node_boundary_snapping'])
        self.assertAlmostEqual(50,measured['start'][0],delta=2);self.assertAlmostEqual(55,measured['start'][1],delta=1)
        self.assertAlmostEqual(205,measured['end'][0],delta=2);self.assertAlmostEqual(100,measured['end'][1],delta=1)

    def test_detached_endpoint_is_not_extended_to_the_node(self):
        measured=visible_polyline(*self.scene(detached=12))
        self.assertTrue(measured['valid']);self.assertGreater(measured['start'][0],58);self.assertLess(measured['end'][0],197)

    def test_branch_or_broken_stroke_is_not_a_valid_connection(self):
        for kwargs in ({'branch':True},{'broken':True}):
            with self.subTest(kwargs=kwargs):self.assertFalse(visible_polyline(*self.scene(**kwargs))['valid'])

    def test_unknown_appearance_and_invalid_node_are_unobservable(self):
        image,spec,objects=self.scene();spec.pop('stroke_rgb')
        self.assertFalse(visible_polyline(image,spec,objects)['valid'])

    def test_pipeline_reports_visible_detachment_from_multipart_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);rng=np.random.default_rng(893)
            patches={k:rng.integers(0,140,(40,40,3),dtype=np.uint8) for k in ('a','b')};paths=[]
            for index,gap in enumerate((0,12)):
                image,spec,objects=self.scene(detached=gap)
                for key,frame in objects.items():
                    x,y=int(frame['left']),int(frame['top']);image[y:y+40,x:x+40]=patches[key]
                path=root/f'{index}.png';cv2.imencode('.png',image)[1].tofile(path);paths.append(path)
            create_package(root/'package',paths,[0,.025])
            selected={'schema_version':1,'objects':[{'id':k,'method':'template','roi':[int(o['left']),int(o['top']),40,40],'anchor':[20,20]} for k,o in objects.items()],
                      'connections':[{**spec,'id':'ab','method':'polyline'}]}
            observations=analyze_package(root/'package',selected)
            self.assertTrue(all(f['valid'] for f in observations['connections'][0]['frames']))
            events=extract_events(observations,selected)
            relations=[r for r in events['relations'] if r['type']=='connection_attachment']
            self.assertTrue(relations[0]['visually_attached']);self.assertFalse(relations[1]['visually_attached'])
        spec['stroke_rgb']=[220,200,190];objects['b']['valid']=False
        self.assertFalse(visible_polyline(image,spec,objects)['valid'])


if __name__=='__main__':unittest.main()
