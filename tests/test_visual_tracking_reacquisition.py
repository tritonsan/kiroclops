"""Opt-in pixel reacquisition preserves identity gates and historical defaults."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual import tracking
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, file_sha256
from kiroclops.web_local.selection import select_from_pixels


class TemplateReacquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'café search'
        self.root.mkdir()
        self.glyph=np.random.default_rng(709).integers(30,140,(14,16,3),dtype=np.uint8)
        self.sequence=0

    def image(self, positions=(), glyph=None):
        result=np.zeros((140,400,3),np.uint8)
        glyph=self.glyph if glyph is None else glyph
        for x,y in positions:
            result[y:y+glyph.shape[0],x:x+glyph.shape[1]]=glyph
        return result

    def package(self, images, times=None):
        self.sequence+=1
        root=self.root/str(self.sequence)
        root.mkdir()
        paths=[]
        for index,image in enumerate(images):
            path=root/f'{index}.png'
            path.write_bytes(cv2.imencode('.png',image)[1].tobytes())
            paths.append(path)
        output=root/'package'
        create_package(output,paths,times or [index*.02 for index in range(len(paths))])
        return output

    def selection(self, policy='full_image_unique_v1', **extra):
        spec={'id':'glyph','method':'template','roi':[20,40,16,14],
              'anchor':[5,6],'search_radius':12,'min_score':.75,'min_margin':.03,
              'max_color_rmse':12,**extra}
        if policy is not None:spec['reacquisition_policy']=policy
        return {'schema_version':1,'objects':[spec]}

    def frames(self, package, selection=None):
        return tracking.analyze_package(package,selection or self.selection())['objects'][0]['frames']

    def test_fast_displacement_reacquires_actual_pixels_then_resumes_local_search(self):
        package=self.package([self.image([(20,40)]),self.image([(260,45)]),self.image([(266,45)])])
        selection=self.selection();original=deepcopy(selection)
        hashes={p:file_sha256(p) for p in package.rglob('*') if p.is_file()}
        frames=self.frames(package,selection)
        self.assertEqual([(f['x'],f['y']) for f in frames],[(25,46),(265,51),(271,51)])
        self.assertEqual(frames[1]['template_search'],'full_image_reacquisition')
        self.assertEqual(frames[1]['reacquisition_policy'],'full_image_unique_v1')
        self.assertLess(frames[1]['local_score'],.75)
        self.assertGreaterEqual(frames[1]['score'],.75)
        self.assertGreaterEqual(frames[1]['margin'],.03)
        self.assertNotIn('template_search',frames[0])
        self.assertNotIn('template_search',frames[2])
        self.assertEqual(selection,original)
        self.assertEqual(hashes,{p:file_sha256(p) for p in hashes})

    def test_local_success_never_triggers_global_scan_or_new_provenance(self):
        package=self.package([self.image([(20,40)]),self.image([(25,40)]),self.image([(30,40)])])
        with patch.object(tracking,'_template_match',wraps=tracking._template_match) as matcher:
            opted=self.frames(package)
        self.assertEqual(matcher.call_count,3)
        self.assertFalse(any(call.kwargs.get('whole_image') for call in matcher.call_args_list))
        legacy=self.frames(package,self.selection(policy=None))
        self.assertEqual(opted,legacy)
        self.assertTrue(all('reacquisition_policy' not in f for f in opted))

    def test_missing_policy_preserves_historical_local_only_failure(self):
        package=self.package([self.image([(20,40)]),self.image([(260,40)])])
        implicit=self.frames(package,self.selection(policy=None))
        explicit=self.frames(package,self.selection(policy='local_only_v1'))
        self.assertEqual(implicit,explicit)
        self.assertFalse(implicit[1]['valid'])
        self.assertIsNone(implicit[1]['x'])
        self.assertNotIn('template_search',implicit[1])

    def test_duplicate_images_and_irregular_timestamps_do_not_prevent_reacquisition(self):
        times=[0.,.02,.021,.04,.041,.06]
        positions=[20,24,24,250,250,258]
        package=self.package([self.image([(x,40)]) for x in positions],times)
        frames=self.frames(package)
        self.assertEqual([f['time_s'] for f in frames],times)
        self.assertEqual([f['x'] for f in frames],[x+5 for x in positions])
        self.assertEqual(frames[3]['template_search'],'full_image_reacquisition')
        self.assertNotIn('template_search',frames[4])
        self.assertAlmostEqual(frames[3]['search_radius_px'],12)

    def test_two_indistinguishable_global_candidates_reject_unique_identity(self):
        package=self.package([self.image([(20,40)]),self.image([(170,40),(300,40)])])
        frame=self.frames(package)[1]
        self.assertEqual(frame['template_search'],'full_image_reacquisition')
        self.assertGreaterEqual(frame['score'],.75)
        self.assertLess(frame['margin'],.03)
        self.assertFalse(frame['valid'])
        self.assertIsNone(frame['x'])

        self.assertIsNone(frame['y'])

    def test_occluded_frames_stay_invalid_and_are_not_filled_after_recovery(self):
        package=self.package([self.image([(20,40)]),self.image(),self.image(),self.image([(300,70)])])
        frames=self.frames(package)
        for frame in frames[1:3]:
            self.assertFalse(frame['valid'])
            self.assertEqual(frame['reason'],'tracking_failure')
            self.assertIsNone(frame['x'])
            self.assertIsNone(frame['y'])
            self.assertEqual(frame['template_search'],'full_image_reacquisition')
        self.assertTrue(frames[3]['valid'])
        self.assertEqual((frames[3]['x'],frames[3]['y']),(305,76))
        self.assertAlmostEqual(frames[3]['search_radius_px'],12)

    def test_global_retry_cannot_bypass_color_or_confidence(self):
        replacements=[self.glyph+70,np.random.default_rng(981).integers(30,140,self.glyph.shape,dtype=np.uint8)]
        for glyph in replacements:
            with self.subTest(kind=int(glyph.mean())):
                package=self.package([self.image([(20,40)]),self.image([(270,40)],glyph)])
                frame=self.frames(package)[1]
                self.assertFalse(frame['valid'])
                self.assertIsNone(frame['x'])
                self.assertLess(frame['score'],.75)

    def test_fixed_mirror_bank_reacquires_with_transformed_anchor(self):
        package=self.package([self.image([(20,40)]),self.image([(270,50)],self.glyph[:,::-1])])
        frame=self.frames(package,self.selection(horizontal_mirror=True))[1]
        self.assertTrue(frame['valid'])
        self.assertTrue(frame['mirrored'])
        self.assertEqual((frame['x'],frame['y']),(280,56))

    def test_opt_in_mirror_bank_rejects_runner_up_hidden_by_same_anchor(self):
        # Nearly mirror-symmetric artwork: both template variants prefer A,
        # but the mirrored variant also has a strong, distinct candidate B.
        # Ranking only each variant's first location misses B's ambiguity.
        noise=np.random.default_rng(563).normal(size=(24,24,3))
        symmetric=(noise+noise[:,::-1])/2
        symmetric-=symmetric.mean(axis=(0,1));symmetric/=np.linalg.norm(symmetric)
        antisymmetric=(noise-noise[:,::-1])/2;antisymmetric/=np.linalg.norm(antisymmetric)
        u,v=np.sqrt(.995),np.sqrt(.005)
        template=u*symmetric+v*antisymmetric
        mirror=u*symmetric-v*antisymmetric
        orthogonal=v*symmetric+u*antisymmetric
        competing=.98*mirror-np.sqrt(1-.98**2)*orthogonal
        template=np.rint(110+600*template).clip(0,255).astype(np.uint8)
        competing=np.rint(110+600*competing).clip(0,255).astype(np.uint8)
        first=self.image([(20,40)],template)
        second=self.image([(130,30)],template)
        second[30:54,270:294]=competing
        package=self.package([first,second])
        selection=self.selection(roi=[20,40,24,24],anchor=[11.5,11.5],horizontal_mirror=True)
        frame=self.frames(package,selection)[1]
        self.assertEqual(frame['template_search'],'full_image_reacquisition')
        self.assertGreater(frame['score'],.99)
        self.assertLess(frame['margin'],.03)
        self.assertFalse(frame['valid'])
        self.assertIsNone(frame['x'])

        # This policy also applies the complete bank check to a local window;
        # the opt-out retains historical scoring and emits no new provenance.
        selection['objects'][0]['search_radius']=400
        local=self.frames(package,selection)[1]
        self.assertFalse(local['valid'])
        selection['objects'][0]['reacquisition_policy']='local_only_v1'
        historical=self.frames(package,selection)[1]
        self.assertTrue(historical['valid'])
        self.assertNotIn('template_search',historical)

    def test_unknown_policy_and_flow_opt_in_are_rejected(self):
        package=self.package([self.image([(20,40)])]*2)
        for selection in (self.selection(policy='unsafe'),self.selection(method='flow')):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(VisualError,'reacquisition policy'):
                    self.frames(package,selection)

    def test_automatic_web_selection_explicitly_opts_in_without_threshold_changes(self):
        image=np.zeros((120,220,3),np.uint8)
        for x,rgb in [(20,(191,165,255)),(80,(77,208,225)),(140,(255,209,102))]:
            cv2.rectangle(image,(x,30),(x+12,46),rgb[::-1],-1)
        path=self.root/'artwork.png'
        path.write_bytes(cv2.imencode('.png',image)[1].tobytes())
        result=select_from_pixels(path,'panel')
        self.assertEqual({obj['id'] for obj in result['objects']},{'handle','bookmark','star'})
        for obj in result['objects']:
            self.assertEqual(obj['reacquisition_policy'],'full_image_unique_v1')
            self.assertEqual((obj['min_score'],obj['min_margin'],obj['max_color_rmse'],obj['search_radius']),(.75,.015,12,90))


if __name__=='__main__':unittest.main()
