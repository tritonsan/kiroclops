"""Real files and packages: legacy and realistic suites bind different policies."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from kiroclops.local_engine import media_suite
from kiroclops.local_engine.execution_policy import resolve_policy,REVISED_ID
from kiroclops.local_engine.realistic_policy import build_policy
from kiroclops.visual.package import create_package,load_package
from kiroclops.visual.schema import digest,read_json,write_json,VisualError


class SuiteV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('original','parent'):
            (self.root/name).mkdir();(self.root/name/'index.html').write_text(name)
        images=[]
        for i in range(3):
            path=self.root/f'{i}.png';cv2.imwrite(str(path),np.full((16,24,3),i,np.uint8));images.append(path)
        create_package(self.root/'pixels',images,[0.,.05,.1],{'schema_version':1,'kind':'InputTimeline',
            'provenance':'recorder_verified','events':[{'time_s':.05,'action':'click','selector':'#open-panel'}]})
        package=load_package(self.root/'pixels')
        self.record={'scenario':{'id':'open','case_id':'open','domain':'panel'},'package':str(self.root/'pixels'),
            'measurement_status':'ok','selection':{'objects':[{'id':'handle'}]},
            'observations':{'package_sha256':package['package_sha256'],'objects':[]},'events':{'kind':'EventSet','events':[]}}
        points=[{'object_id':'handle','frame_sha256':digest(i),'error_px':1.,'role':'reference','split':'calibration','independent':True} for i in range(20)]
        self.policy=build_policy('panel',{'status':'pass','basis':'independent_raw_pixels','points':points})

    def create(self,**kwargs):
        return media_suite.create_suite(self.root/'suite',domain='web_panel',stage='panel',
            records=[{'case_id':'open','role':'calibration','record':self.record}],required_cases=[{'case_id':'open','role':'calibration'}],
            original=self.root/'original',authoring_parent=self.root/'parent',goal='Match panel',**kwargs)

    def test_v1_preserves_old_thresholds_and_version(self):
        value=self.create();self.assertEqual(1,value['schema_version']);self.assertEqual(.5,value['limits']['required_improvement_fraction'])
        self.assertEqual(value,media_suite.load_suite(self.root/'suite'))

    def test_v2_binds_quality_policy_execution_and_sources(self):
        value=self.create(acceptance_policy=self.policy,execution_policy=resolve_policy(REVISED_ID,'panel'))
        self.assertEqual(2,value['schema_version']);self.assertIsNone(value['limits']['required_improvement_fraction'])
        self.assertEqual(.05,value['limits']['web_curve_error_fraction']);self.assertEqual('realistic-behavior-v1',value['acceptance_profile'])
        self.assertEqual(digest(value['analysis_source_hashes']),value['source_epoch'])
        self.assertEqual(value,media_suite.load_suite(self.root/'suite'))

    def test_v2_requires_the_matching_execution_stage(self):
        with self.assertRaises(VisualError):self.create(acceptance_policy=self.policy,execution_policy=resolve_policy(REVISED_ID,'ground'))

    def test_browser_v2_cannot_omit_the_independent_floor_policy(self):
        with self.assertRaises(VisualError):self.create(execution_policy=resolve_policy(REVISED_ID,'panel'))

    def test_rehashed_policy_tamper_does_not_change_fixed_rules(self):
        self.create(acceptance_policy=self.policy,execution_policy=resolve_policy(REVISED_ID,'panel'))
        path=self.root/'suite/suite.json';value=read_json(path);value['acceptance_policy']['rules']['curve_fraction']=.9
        payload={k:v for k,v in value['acceptance_policy'].items() if k!='policy_sha256'}
        value['acceptance_policy']['policy_sha256']=digest(payload);write_json(path,value)
        write_json(self.root/'suite/suite-binding.json',{'suite_sha256':digest(value)})
        with self.assertRaises(VisualError):media_suite.load_suite(self.root/'suite')


if __name__=='__main__':unittest.main()
