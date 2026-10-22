"""Append-only preparation revision over saved pixels; no application starts."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import suite_revision, suite_web_v2
from kiroclops.local_engine.reliability import qualify_warmups
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from scripts import phase3d_v2_prepare as preparation
from tests.test_suite_revision import Fixture


class CalibrationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='kc-cal-revision-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.fixture=Fixture(self.root)
        self.fixture.scenario=suite_web_v2.scenarios('panel','calibration')[0]
        self.previous=self.root/'p'/'old'
        self.previous.mkdir(parents=True)
        self.reference_source=self.fixture.project(self.root/'reference-source','reference')
        self.case={'case_id':self.fixture.scenario['case_id'],'role':'calibration','stage':'panel',
            'purpose':'primary','scenario':deepcopy(self.fixture.scenario)}
        rows=[]
        # Store targets first to prove the revision orders references itself.
        for role,project in (('before',self.fixture.parent),('original',self.fixture.original),('reference',self.reference_source)):
            record=self.fixture.record(self.previous/role/self.case['case_id'],project=project)
            rows.append({**deepcopy(self.case),'source_role':role,'record':record})
        def recorded_warmup(path):
            record=self.fixture.record(path/'capture',project=self.fixture.parent)
            write_json(path/'record.json',record)
            return {'status':'ok','timing':{'status':'pass','errors':[]},
                'measurement_status':'ok','record_sha256':digest(record)}
        warmups=qualify_warmups(recorded_warmup,self.previous/'warmups')
        write_json(self.previous/'recipes.json',[self.case])
        write_json(self.previous/'start-binding.json',{'stage':'panel','parent':str(self.fixture.parent),
            'original':str(self.fixture.original),'parent_source_sha256':digest(source_hashes(self.fixture.parent)),
            'original_source_sha256':digest(source_hashes(self.fixture.original)),
            'recipes_sha256':digest([self.case]),'historical_candidate':{'candidate_id':'candidate-01'},'model_started':False})
        write_json(self.previous/'calibration.json',{'stage':'panel','output':str(self.previous),
            'status':'pending_independent_calibration_review','records':rows,'warmups':warmups,
            'runtime':{'capture_lock':str(self.root/'shared.lock'),'environment':{},'driver':{}},'model_started':False})
        self.patches=[patch.object(suite_revision,'measurement_source_hashes',side_effect=lambda *a,**k:deepcopy(self.fixture.sources)),
            patch.object(preparation,'measurement_source_hashes',side_effect=lambda *a,**k:deepcopy(self.fixture.sources)),
            patch.object(suite_revision,'service_sources',return_value=self.fixture.sources),
            patch.object(suite_revision,'engine_identity',return_value=self.fixture.engine),
            patch('kiroclops.cloud.identity.engine_identity',return_value=self.fixture.engine),
            patch('kiroclops.web_local.selection.select_package_from_pixels',side_effect=lambda *a:deepcopy(self.fixture.selection)),
            patch('kiroclops.local_engine.evaluation._analysis',side_effect=self.fixture.analyze),
            patch.object(preparation,'Capture',side_effect=AssertionError('No recorder or runtime preparation permitted')),
            patch.object(preparation,'stamp_new_measurement',side_effect=AssertionError('Historical pixels cannot be stamped as fresh'))]
        self.mocks=[p.start() for p in self.patches]
        for p in reversed(self.patches):self.addCleanup(p.stop)

    def revise(self, identifier='new'):
        return preparation.revise_calibration(self.previous,identifier,self.root)

    def test_actual_revision_preserves_historical_evidence_and_remaps_sources(self):
        old_files=source_hashes(self.previous)
        before=read_json(self.previous/'calibration.json')
        result=self.revise()
        self.assertEqual(old_files,source_hashes(self.previous))
        self.assertEqual('pending_independent_calibration_review',result['status'])
        self.assertFalse(result['native_capture_started'])
        self.assertFalse(result['model_started'])
        self.assertEqual(3,result['calibration_revision']['reanalyzed_records'])
        self.assertEqual(before['runtime'],result['runtime'])
        self.assertEqual(before['warmups'],result['warmups'])
        self.assertEqual((self.previous/'recipes.json').read_bytes(),(self.root/'p/new/recipes.json').read_bytes())
        binding=read_json(self.root/'p/new/start-binding.json')
        self.assertEqual(str(self.root/'p/new/parent'),binding['parent'])
        self.assertEqual(str(self.root/'p/new/original-source'),binding['original'])
        self.assertEqual(source_hashes(self.fixture.parent),source_hashes(binding['parent']))
        self.assertEqual(source_hashes(self.fixture.original),source_hashes(binding['original']))
        for old,new in zip(before['records'],result['records']):
            self.assertEqual(old['source_role'],new['source_role'])
            self.assertEqual(load_package(old['record']['package']),load_package(new['record']['package']))
            self.assertEqual(old['record']['source_sha256'],new['record']['source_sha256'])
            self.assertEqual(old['record']['scenario'],new['record']['scenario'])
            self.assertEqual(old['record']['observations']['inputs'],new['record']['observations']['inputs'])
            suite_revision.verify_measurement_record(new['record'],'panel',expected_sources=self.fixture.sources)
        wrapper=read_json(self.root/'p/new/warmups/inherited.json')
        self.assertFalse(wrapper['environment_requalified'])
        self.assertEqual(before['warmups']['receipt_path'],wrapper['receipt_path'])
        self.assertEqual(file_sha256(wrapper['receipt_path']),wrapper['receipt_sha256'])
        reviewed=preparation.review_records(self.root/'p/new')
        self.assertEqual({'reference','original','before'},{r['role'] for r in reviewed})
        for row in reviewed:
            if row['role']!='reference':
                self.assertTrue(Path(row['source_project']).is_relative_to(self.root/'p/new'))

    def test_references_are_first_and_targets_use_the_new_reference(self):
        actual=suite_revision.revise_record
        calls=[]
        def tracked(record,output,stage,**kwargs):
            value=actual(record,output,stage,**kwargs)
            calls.append((Path(output).parent.name,value,kwargs))
            return value
        with patch.object(preparation,'revise_record',side_effect=tracked):
            self.revise()
        self.assertEqual(['reference','before','original'],[c[0] for c in calls])
        for _,_,kwargs in calls[1:]:
            self.assertIs(calls[0][1],kwargs['reference_record'])
            self.assertIs(calls[0][1]['selection'],kwargs['reference_selection'])

    def test_local_backend_scope_is_restored_after_revision(self):
        def analyze(*args,**kwargs):
            self.assertEqual('local',os.environ['KIROCLOPS_ANALYSIS_BACKEND'])
            return self.fixture.analyze(*args,**kwargs)
        self.mocks[6].side_effect=analyze
        with patch.dict(os.environ,{'KIROCLOPS_ANALYSIS_BACKEND':'remote'}):
            self.revise()
            self.assertEqual('remote',os.environ['KIROCLOPS_ANALYSIS_BACKEND'])

    def test_invalid_source_or_missing_role_stops_before_worker(self):
        value=read_json(self.previous/'calibration.json')
        value['records'][0]['record']['source_sha256']=digest('foreign source')
        write_json(self.previous/'calibration.json',value)
        with self.assertRaises(VisualError) as caught:self.revise()
        self.assertEqual('wrong_result',caught.exception.code)
        self.assertFalse(self.mocks[6].called)
        self.assertFalse((self.root/'p/new').exists())
        value['records'].pop(0)
        write_json(self.previous/'calibration.json',value)
        with self.assertRaises(VisualError) as caught:self.revise()
        self.assertEqual('invalid_suite',caught.exception.code)

    def test_failed_reanalysis_is_preserved_without_publishing_calibration(self):
        old_files=source_hashes(self.previous)
        self.mocks[6].side_effect=VisualError('tracking_failure','saved pixels did not qualify')
        with self.assertRaises(VisualError):self.revise()
        self.assertEqual(old_files,source_hashes(self.previous))
        self.assertFalse((self.root/'p/new/calibration.json').exists())
        failure=read_json(self.root/'p/new/failure.json')
        self.assertEqual('tracking_failure',failure['code'])
        self.assertFalse(failure['native_capture_started'])
        self.assertFalse(failure['model_started'])

    def test_capture_identity_mutation_cannot_be_published(self):
        actual=suite_revision.revise_record
        def changed(record,*args,**kwargs):
            result=actual(record,*args,**kwargs)
            result['source_sha256']=digest('changed producer')
            return result
        with patch.object(preparation,'revise_record',side_effect=changed):
            with self.assertRaises(VisualError) as caught:self.revise()
        self.assertEqual('measurement_provenance_changed',caught.exception.code)
        self.assertFalse((self.root/'p/new/calibration.json').exists())

    def test_unsafe_nested_output_and_existing_preparation_remain_unchanged(self):
        old_files=source_hashes(self.previous)
        with self.assertRaises(VisualError) as caught:
            preparation.revise_calibration(self.previous,'child',self.previous)
        self.assertEqual('unsafe_path',caught.exception.code)
        self.assertEqual(old_files,source_hashes(self.previous))
        argv=['phase3d_v2_prepare.py','--root',str(self.root),'revise-calibration','--previous',str(self.previous),'--id','old']
        with patch('sys.argv',argv),patch('builtins.print'):
            self.assertEqual(1,preparation.main())
        self.assertEqual(old_files,source_hashes(self.previous))
        self.assertFalse((self.previous/'failure.json').exists())


if __name__=='__main__':unittest.main()
