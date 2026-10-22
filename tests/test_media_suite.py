import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from kiroclops.local_engine import media_suite as suite


class ReferenceSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / 'original'; self.original.mkdir()
        (self.original / 'index.html').write_text('original')
        self.parent = self.root / 'parent'; self.parent.mkdir()
        (self.parent / 'index.html').write_text('parent')
        frames=[]
        for index in range(3):
            path=self.root / f'{index}.png'; cv2.imwrite(str(path), np.full((16, 24, 3), index, np.uint8)); frames.append(path)
        create_package(self.root/'pixels',frames,[0.,.05,.1],
            {'kind':'InputTimeline','schema_version':1,'provenance':'recorder_verified',
             'events':[{'time_s':.05,'action':'click','selector':'#open-panel','source':'test_recorder'}]})
        manifest=load_package(self.root/'pixels')
        self.record={'scenario':{'id':'open-1','case_id':'open-1','split':'calibration','domain':'panel'},
            'package':str(self.root/'pixels'),'measurement_status':'ok','source_sha256':'source',
            'selection':{'objects':[{'id':'handle'}]},'observations':{'package_sha256':manifest['package_sha256'],'objects':[]},
            'events':{'kind':'EventSet','events':[]}}
        self.required=[{'case_id':'open-1','role':'calibration'}]

    def tearDown(self):self.temp.cleanup()

    def create(self, records=None, required=None):
        return suite.create_suite(self.root/'suite',domain='web_panel',stage='panel',
            records=records or [{'case_id':'open-1','role':'calibration','record':self.record}],
            required_cases=required or self.required,original=self.original,authoring_parent=self.parent,goal='Match real panel')

    def test_original_and_parent_are_distinct(self):
        value=self.create(); loaded=suite.load_suite(self.root/'suite')
        self.assertEqual(value,loaded)
        self.assertNotEqual(value['original_source_hashes'],value['authoring_parent_source_hashes'])
        self.assertEqual((self.root/'suite/comparison-original/index.html').read_text(),'original')
        self.assertFalse((self.root/'suite/clips/open-1/index.html').exists())
        self.assertEqual(value['heldout_status'],'not_provided')

    def test_portable_suite_survives_directory_change(self):
        import shutil
        self.create(); shutil.copytree(self.root/'suite',self.root/'café naïve')
        value=suite.load_suite(self.root/'café naïve')
        record=suite.record_from_suite(self.root/'café naïve',value['cases'][0])
        self.assertEqual(load_package(record['package'])['package_sha256'],load_package(self.root/'pixels')['package_sha256'])

    def test_changed_frame_rejected(self):
        self.create(); (self.root/'suite/clips/open-1/package/frames/000001.png').write_bytes(b'bad')
        with self.assertRaisesRegex(VisualError,'changed'):suite.load_suite(self.root/'suite')

    def test_changed_original_rejected(self):
        self.create(); (self.root/'suite/comparison-original/index.html').write_text('changed')
        with self.assertRaises(VisualError):suite.load_suite(self.root/'suite')

    def test_missing_long_clip_reported_without_start(self):
        required=self.required+[{'case_id':'long-1','role':'calibration','request':'Add a long press recording.'}]
        with self.assertRaises(VisualError) as error:self.create(required=required)
        self.assertEqual(error.exception.code,'needs_reference')
        pending=read_json(self.root/'suite/incomplete.json')
        self.assertEqual(pending['missing_references'][0]['request'],'Add a long press recording.')
        self.assertFalse((self.root/'suite/suite.json').exists())

    def test_duplicate_case_rejected(self):
        rows=[{'case_id':'open-1','role':'calibration','record':self.record}]*2
        with self.assertRaises(VisualError):self.create(records=rows)

    def test_case_recipe_identity_cannot_be_relabelled(self):
        self.record['scenario']['case_id']='other'
        with self.assertRaises(VisualError):self.create()

    def test_threshold_change_rejected_even_with_rehashed_binding(self):
        self.create(); value=read_json(self.root/'suite/suite.json'); value['limits']['minimum_valid_ratio']=.5
        write_json(self.root/'suite/suite.json',value);write_json(self.root/'suite/suite-binding.json',{'suite_sha256':digest(value)})
        with self.assertRaises(VisualError):suite.load_suite(self.root/'suite')

    def test_suite_session_rejects_wrong_parent(self):
        self.create()
        with self.assertRaises(VisualError) as error:
            suite.create_suite_session(self.root/'session',self.root/'suite',self.original,runtime={})
        self.assertEqual(error.exception.code,'wrong_authoring_parent')

    def test_unknown_inputs_are_not_inferred(self):
        result=suite.coverage([{'case_id':'a','role':'calibration','input_provenance':'unknown'}],[{'case_id':'a','role':'calibration'}])
        self.assertEqual(result['status'],'needs_reference');self.assertFalse(result['inferred_inputs'])


if __name__=='__main__':unittest.main()
