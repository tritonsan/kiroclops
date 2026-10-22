import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.selection import propose_objects


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'café space'
        self.root.mkdir()
        self.rng=np.random.default_rng(814)
        self.patch=self.rng.integers(20,250,(14,16,3),dtype=np.uint8)
        self.sequence=0

    def package(self,images,times=None):
        self.sequence+=1
        directory=self.root/str(self.sequence);directory.mkdir()
        paths=[]
        for i,image in enumerate(images):
            path=directory/f'raw-{i}.png'
            path.write_bytes(cv2.imencode('.png',image)[1].tobytes());paths.append(path)
        output=directory/'package'
        create_package(output,paths,times or [i*.04 for i in range(len(images))])
        return output

    def images(self,positions,patch=None):
        patch=self.patch if patch is None else patch
        result=[]
        for x,y in positions:
            frame=np.zeros((120,200,3),np.uint8)
            frame[y:y+patch.shape[0],x:x+patch.shape[1]]=patch
            result.append(frame)
        return result

    def selection(self,**kwargs):
        return {'schema_version':1,'objects':[{'id':'a','roi':[20,40,16,14],
                 'anchor':[5,6],'method':'template',**kwargs}]}

    def test_template_native_unicode_and_actual_times(self):
        times=[0,.023,.079,.1]
        package=self.package(self.images([(20,40),(23,38),(28,37),(30,36)]),times)
        progress=[]
        result=analyze_package(package,self.selection(),progress=lambda *a:progress.append(a))
        self.assertEqual(result['status'],'ok')
        self.assertEqual([f['x'] for f in result['objects'][0]['frames']],[25,28,33,35])
        self.assertEqual([f['time_s'] for f in result['objects'][0]['frames']],times)
        self.assertEqual(progress[-1],('tracking',4,4))
        self.assertEqual(result['measurement_source'],'pixels_only')

    def test_ambiguous_identical_patches_fail_margin(self):
        images=self.images([(20,40)]*3)
        for image in images:image[40:54,44:60]=self.patch
        result=analyze_package(self.package(images),self.selection(search_radius=50))
        self.assertEqual(result['status'],'rejected')
        self.assertTrue(all(f['x'] is None for f in result['objects'][0]['frames']))

    def test_textureless_patch_rejected(self):
        package=self.package([np.zeros((120,200,3),np.uint8)]*2)
        with self.assertRaisesRegex(VisualError,'no spatial texture'):
            analyze_package(package,self.selection())

    def test_multiple_objects_occlusion_has_no_predicted_coordinates(self):
        other=self.rng.integers(20,250,(14,16,3),dtype=np.uint8)
        images=self.images([(20,40),(22,40),(24,40)])
        for i in (0,2):images[i][75:89,100:116]=other
        selection=self.selection()
        selection['objects'].append({'id':'b','roi':[100,75,16,14],'anchor':[8,7]})
        result=analyze_package(self.package(images),selection)
        self.assertEqual(result['objects'][0]['valid_ratio'],1)
        self.assertFalse(result['objects'][1]['frames'][1]['valid'])
        self.assertIsNone(result['objects'][1]['frames'][1]['x'])
        self.assertEqual(result['status'],'rejected')

    def test_fixed_background_detects_camera_translation(self):
        image=self.images([(20,40)])[0]
        image[10:26,140:156]=self.rng.integers(0,255,(16,16,3),dtype=np.uint8)
        moved=cv2.warpAffine(image,np.float32([[1,0,4],[0,1,0]]),(200,120))
        selection=self.selection();selection['background_rois']=[[140,10,16,16]]
        result=analyze_package(self.package([image,moved]),selection,{'require_static_camera':True})
        self.assertEqual(result['camera'][1]['status'],'fail')
        self.assertEqual(result['objects'][0]['frames'][1]['reason'],'camera_motion')

    def test_required_static_camera_does_not_fake_verification(self):
        result=analyze_package(self.package(self.images([(20,40)]*2)),self.selection(),{'require_static_camera':True})
        self.assertEqual(result['status'],'rejected')

    def test_mirror_anchor_transforms_with_fixed_template(self):
        images=self.images([(20,40),(24,40)])
        images[1][40:54,24:40]=self.patch[:,::-1]
        result=analyze_package(self.package(images),self.selection(horizontal_mirror=True))
        frame=result['objects'][0]['frames'][1]
        self.assertTrue(frame['valid']);self.assertTrue(frame['mirrored'])
        self.assertEqual(frame['x'],34)

    def test_quarter_rotation_preserves_physical_anchor(self):
        images=self.images([(20,40)])[0:1]
        rotated=np.zeros_like(images[0]);rotated[40:56,24:38]=np.rot90(self.patch)
        images.append(rotated)
        result=analyze_package(self.package(images),self.selection(rotations=[0,90]))
        frame=result['objects'][0]['frames'][1]
        self.assertTrue(frame['valid']);self.assertEqual(frame['rotation_deg'],90)
        self.assertEqual((frame['x'],frame['y']),(30,50))

    def test_template_is_bound_to_owned_package_and_external_paths_rejected(self):
        reference=self.package(self.images([(20,40),(23,42)]))
        result=analyze_package(reference,self.selection())
        self.assertEqual(result['status'],'ok')
        self.assertEqual(len(result['objects'][0]['template_source']['frame_sha256']),64)
        with self.assertRaisesRegex(VisualError,'not portable'):
            analyze_package(reference,self.selection(template_package='D:/outside'))

    def test_flow_translation_and_failure_are_observed(self):
        patch=self.rng.integers(0,255,(32,32,3),dtype=np.uint8)
        images=self.images([(20,40),(22,41),(24,42)],patch)
        selection=self.selection(method='flow',roi=[20,40,32,32],anchor=[16,16])
        result=analyze_package(self.package(images),selection)
        self.assertEqual(result['status'],'ok')
        self.assertAlmostEqual(result['objects'][0]['frames'][2]['x'],40,delta=.3)
        broken=images[:2]+[np.zeros_like(images[0]),images[-1]]
        result=analyze_package(self.package(broken),selection)
        self.assertFalse(result['objects'][0]['frames'][-1]['valid'])
        self.assertIsNone(result['objects'][0]['frames'][-1]['x'])

    def test_contour_measures_size_change(self):
        images=[]
        for x,w in [(20,30),(22,35),(24,40)]:
            image=np.zeros((120,200,3),np.uint8)
            cv2.rectangle(image,(x,40),(x+w-1,59),(230,230,230),-1);images.append(image)
        result=analyze_package(self.package(images),self.selection(method='contour',roi=[20,40,30,20],anchor=[15,10]))
        self.assertEqual(result['status'],'ok')
        frames=result['objects'][0]['frames']
        self.assertAlmostEqual(frames[-1]['width']-frames[0]['width'],10,delta=1)

    def test_icon_identity_has_separately_measured_panel_boundary(self):
        images=[]
        for x in [20,23,26]:
            frame=np.zeros((120,200,3),np.uint8)
            cv2.rectangle(frame,(x,35),(x+59,64),(180,180,180),1)
            frame[40:54,x+8:x+24]=self.patch;images.append(frame)
        spec=self.selection(roi=[28,40,16,14],geometry={'method':'contour','roi':[20,35,60,30],'min_area_ratio':.7})
        result=analyze_package(self.package(images),spec)
        self.assertEqual(result['status'],'ok')
        last=result['objects'][0]['frames'][-1]
        self.assertEqual(last['x'],39)
        self.assertAlmostEqual(last['left'],26,delta=2)
        self.assertGreater(last['width'],55)

    def test_presence_intervals_do_not_count_expected_absence_as_bad_tracking(self):
        images=[np.zeros((120,200,3),np.uint8)]+self.images([(20,40),(22,40)])+[np.zeros((120,200,3),np.uint8)]
        result=analyze_package(self.package(images),self.selection(frame_index=1,active_intervals_s=[[.04,.08]]))
        obj=result['objects'][0]
        self.assertEqual(obj['valid_ratio'],1)
        self.assertIs(obj['frames'][0]['present'],False)
        self.assertFalse(obj['frames'][0]['expected_active'])

    def test_inactive_interval_does_not_hide_visible_object(self):
        result=analyze_package(self.package(self.images([(20,40)]*4)),self.selection(active_intervals_s=[[0,.08]]))
        self.assertIs(result['objects'][0]['frames'][-1]['present'],True)

    def test_pixel_only_connection_endpoints_follow_visible_line(self):
        a=self.patch;b=self.rng.integers(0,255,a.shape,dtype=np.uint8)
        frames=[]
        for start in [36,45]:
            frame=np.zeros((120,200,3),np.uint8)
            cv2.line(frame,(start,47),(99,47),(255,255,255),1)
            frame[40:54,20:36]=a;frame[40:54,100:116]=b;frames.append(frame)
        selection=self.selection(anchor=[8,7])
        selection['objects'].append({'id':'b','roi':[100,40,16,14],'anchor':[8,7]})
        selection['connections']=[{'id':'ab','from_object':'a','to_object':'b'}]
        result=analyze_package(self.package(frames),selection)
        observed=result['connections'][0]['frames']
        self.assertTrue(all(f['valid'] for f in observed))
        self.assertGreater(observed[1]['start'][0]-observed[0]['start'][0],5)

    def test_cancellation_and_raw_integrity(self):
        package=self.package(self.images([(20,40)]*2))
        def cancel():raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):analyze_package(package,self.selection(),check=cancel)
        (package/'frames/000001.png').write_bytes(b'changed')
        with self.assertRaises(VisualError):analyze_package(package,self.selection())

    def test_candidates_need_confirmation_and_are_not_semantic_labels(self):
        result=propose_objects(self.package(self.images([(20,40),(35,40),(45,40)])))
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertTrue(result['proposals'])
        self.assertTrue(all('roi' in p for p in result['proposals']))


if __name__=='__main__':unittest.main()
