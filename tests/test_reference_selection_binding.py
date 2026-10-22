"""Actual auto-selector identity shape through gate/cache protocol fixtures.

The tiny package is decoded by the real selector. Gate and cache fixtures keep
their existing no-worker/no-browser doubles; this tests provenance, not motion.
"""
from copy import deepcopy
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import reference_acceptance as acceptance, reference_cache as cache
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.web_local.selection import select_package_from_pixels, COLORS


def _fixture_module(name):
    spec=importlib.util.spec_from_file_location('_binding_'+name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


_acceptance_cases=_fixture_module('test_reference_acceptance')
_cache_cases=_fixture_module('test_reference_cache')


class ReferenceSelectionBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='actual selection naïve ')
        directory=Path(cls.temporary.name)
        image=np.full((480,640,3),(26,20,15),np.uint8)
        for number,name in enumerate(('a','b','c')):
            x,y=300,120+100*number
            cv2.rectangle(image,(x,y),(x+139,y+59),(79,57,38),-1)
            cv2.rectangle(image,(x+15,y+17),(x+31,y+37),COLORS['svg'][name][::-1],-1)
            cv2.rectangle(image,(x+18,y+20),(x+21,y+23),(79,57,38),-1)
        png=directory/'actual.png';png.write_bytes(cv2.imencode('.png',image)[1].tobytes())
        package=directory/'package'
        cls.manifest=create_package(package,[png,png],[0.,.02])
        cls.actual=select_package_from_pixels(package,'svg')

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def fixture(self,kind):
        fixture=(_acceptance_cases.ReferenceAcceptanceTests('test_missing_gate_rejected_without_comparator_or_pixel_work')
                 if kind=='gate' else _cache_cases.ReferenceCacheTests('test_tools_copy_exact_measurements_once_per_matrix_without_worker'))
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    @contextmanager
    def case(self,kind):
        fixture=self.fixture(kind)
        try:yield fixture
        finally:fixture.doCleanups()

    def install_auto_shape(self,fixture,kind,mutate=None):
        # Preserve real selector objects, graph, centroid options and field names.
        # Only the package identity is rebound to each explicit protocol fixture.
        for row in fixture.rows:
            path=Path(row['analysis_path']);analysis=read_json(path)
            selection=deepcopy(self.actual)
            selection['reference_package_sha256']=row['reference_package_sha256']
            if mutate:mutate(selection,row['reference_package_sha256'])
            analysis['observations']['selection']=selection
            analysis['events']['observation_sha256']=digest(analysis['observations'])
            semantic={'operation':'analyze','package_sha256':row['reference_package_sha256'],
                'selection':selection,'contract':cache.MEASUREMENT_CONTRACT,
                'candidate_sha256':analysis['candidate_sha256']}
            analysis['request_sha256']=digest(semantic)
            write_json(path,analysis);write_json(path.parent/'selection.json',selection)
            row['analysis_sha256']=file_sha256(path)
            row['observations_sha256']=digest(analysis['observations']);row['events_sha256']=digest(analysis['events'])
            comparison=read_json(path.parent/'self-comparison.json')
            comparison['reference_observations_sha256']=digest(analysis['observations'])
            write_json(path.parent/'self-comparison.json',comparison)
            row['self_comparison_sha256']=file_sha256(path.parent/'self-comparison.json')
            for evidence in (path,path.parent/'selection.json',path.parent/'self-comparison.json'):
                row['evidence_hashes'][str(evidence)]=file_sha256(evidence)
        fixture.save() if kind=='gate' else fixture.save_gate()

    def execute(self,fixture,kind):
        return acceptance.validate_reference_gate(fixture.manifest,fixture.gate) if kind=='gate' else fixture.run_matrix()

    def test_actual_auto_selector_uses_bound_reference_identity(self):
        self.assertEqual(self.actual['reference_package_sha256'],self.manifest['package_sha256'])
        self.assertNotIn('package_sha256',self.actual)
        self.assertEqual([o['id'] for o in self.actual['objects']],['a','b','c'])
        self.assertTrue(all(o['pixel_anchor']['method']=='foreground_color_mass_v1' for o in self.actual['objects']))

    def test_real_auto_selection_shape_passes_gate_unchanged(self):
        fixture=self.fixture('gate');self.install_auto_shape(fixture,'gate')
        self.assertEqual(self.execute(fixture,'gate')['status'],'pass')

    def test_real_auto_selection_shape_passes_cache_exact_copy(self):
        fixture=self.fixture('cache');self.install_auto_shape(fixture,'cache')
        records=self.execute(fixture,'cache')
        self.assertEqual(len(records),4)
        for record in records:
            self.assertNotIn('package_sha256',record['selection'])
            self.assertEqual(record['selection'],record['observations']['selection'])
            self.assertFalse(record['measurement_origin']['worker_started'])

    def test_missing_wrong_and_conflicting_identities_fail_both_consumers(self):
        mutations={
            'missing':lambda s,h:s.pop('reference_package_sha256'),
            'wrong_reference':lambda s,h:s.update(reference_package_sha256='0'*64),
            'conflicting_generic':lambda s,h:s.update(package_sha256='0'*64),
            'conflicting_reference':lambda s,h:s.update(package_sha256=h,reference_package_sha256='0'*64),
            'null_reference':lambda s,h:s.update(package_sha256=h,reference_package_sha256=None)}
        for kind in ('gate','cache'):
            for name,mutate in mutations.items():
                with self.subTest(kind=kind,mutation=name),self.case(kind) as fixture:
                    self.install_auto_shape(fixture,kind,mutate)
                    with self.assertRaises(VisualError):self.execute(fixture,kind)

    def test_consistent_dual_identities_and_intake_shape_remain_supported(self):
        for kind in ('gate','cache'):
            for shape in ('dual','intake'):
                with self.subTest(kind=kind,shape=shape),self.case(kind) as fixture:
                    def mutate(selection,sha):
                        selection['package_sha256']=sha
                        if shape=='intake':selection.pop('reference_package_sha256')
                    self.install_auto_shape(fixture,kind,mutate)
                    self.execute(fixture,kind)

    def test_rehashed_roi_centroid_and_graph_tampering_still_fails_exact_selection(self):
        changes={
            'roi':lambda s:s['objects'][0]['roi'].__setitem__(0,s['objects'][0]['roi'][0]+1),
            'centroid':lambda s:s['objects'][0]['pixel_anchor']['foreground_rgb'].__setitem__(0,100),
            'graph':lambda s:s['connections'].pop()}
        for kind in ('gate','cache'):
            for name,mutate in changes.items():
                with self.subTest(kind=kind,mutation=name),self.case(kind) as fixture:
                    self.install_auto_shape(fixture,kind)
                    row=fixture.rows[0];path=Path(row['analysis_path']).parent/'selection.json'
                    selection=read_json(path);mutate(selection);write_json(path,selection)
                    row['evidence_hashes'][str(path)]=file_sha256(path)
                    fixture.save() if kind=='gate' else fixture.save_gate()
                    with self.assertRaises(VisualError):self.execute(fixture,kind)

    def test_rebound_observations_cannot_replace_original_cache_worker_request(self):
        fixture=self.fixture('cache');self.install_auto_shape(fixture,'cache')
        row=fixture.rows[0];path=Path(row['analysis_path']);analysis=read_json(path)
        selection=deepcopy(analysis['observations']['selection']);selection['objects'][0]['roi'][0]+=1
        analysis['observations']['selection']=selection
        analysis['events']['observation_sha256']=digest(analysis['observations'])
        # Rebind every outer receipt, but never forge the original worker request.
        write_json(path,analysis);write_json(path.parent/'selection.json',selection)
        row['analysis_sha256']=file_sha256(path);row['observations_sha256']=digest(analysis['observations'])
        row['events_sha256']=digest(analysis['events'])
        comparison=read_json(path.parent/'self-comparison.json');comparison['reference_observations_sha256']=digest(analysis['observations'])
        write_json(path.parent/'self-comparison.json',comparison);row['self_comparison_sha256']=file_sha256(path.parent/'self-comparison.json')
        for evidence in (path,path.parent/'selection.json',path.parent/'self-comparison.json'):
            row['evidence_hashes'][str(evidence)]=file_sha256(evidence)
        fixture.save_gate()
        with self.assertRaisesRegex(VisualError,'worker request'):
            self.execute(fixture,'cache')


if __name__=='__main__':unittest.main()
