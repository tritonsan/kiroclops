"""Invocation-local reuse never substitutes for raw evidence or review replay."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine import cohort,review
from kiroclops.local_engine.qualification import bind_evidence
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import VisualError,digest,file_sha256,read_json,write_json


class ReviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        frames=[]
        for index in range(20):
            path=self.root/f'{index}.png'
            path.write_bytes(cv2.imencode('.png',np.full((4,4,3),index,np.uint8))[1].tobytes());frames.append(path)
        self.package=self.root/'package';manifest=create_package(self.package,frames,[i*.02 for i in range(20)])
        self.obs={'kind':'ObservationSet','package_sha256':manifest['package_sha256'],'objects':[{'id':'a',
            'frames':[{'index':i,'time_s':i*.02,'x':1.,'y':1.,'valid':True} for i in range(20)]}]}
        self.aggregate=self.root/'review.json'
        aggregate={'kind':'IndependentVisualReview','status':'pass',
            'required_observations':[[manifest['package_sha256'],digest(self.obs)]]}
        position=self.root/'position';review.prepare_review(self.package,self.obs,position,tolerance_px=1)
        request=read_json(position/'request.json')
        marks={'request_sha256':digest(request),'reviewer':'synthetic fixture',
            'method':'independent_raw_image_marking','points':[
                {k:t[k] for k in ('record_id','object_id','index','image_sha256')}|{'x':1.,'y':1.} for t in request['tasks']]}
        review.evaluate_review(position,marks)
        events={'kind':'EventSet','package_sha256':manifest['package_sha256'],
            'observation_sha256':digest(self.obs),'events':[],'relations':[]}
        for scope in ('position','events','relationships'):
            directory=self.root/scope
            if scope!='position':
                review.prepare_scope_review(self.package,self.obs,events,directory,scope=scope)
                request=read_json(directory/'request.json')
                annotation={'request_sha256':digest(request),'reviewer':'synthetic fixture',
                    'method':'independent_raw_sequence_review','checks':[
                        {'id':t['id'],'verdict':'pass','observed_absence':True,'evidence':'Known constant fixture anchor',
                         'frame_sha256s':[f['sha256'] for f in t['frames']]} for t in request['tasks']]}
                review.evaluate_scope_review(directory,annotation)
            aggregate[scope+'_reviews']=[{'directory':str(directory),
                'request_sha256':digest(read_json(directory/'request.json')),
                'result_sha256':file_sha256(directory/'review-result.json')}]
        write_json(self.aggregate,aggregate)
        self.contract=self.root/'contract.json';write_json(self.contract,{'fixture':'constant'})
        self.selection=self.root/'selection.json';write_json(self.selection,{'objects':[{'id':'a'}]})
        self.analysis=self.root/'analysis.json';write_json(self.analysis,{'observations':self.obs})
        self.rows=[]
        for domain in ('game','panel','svg'):
            for index in range(6):
                positive=index<2;result=self.root/f'{domain}-{index}.json';write_json(result,{'pass':positive})
                self.rows.append(bind_evidence({'id':f'{domain}-{index}','domain':domain,
                    'expected_class':'correct' if positive else 'behavioral_deviation',
                    'observed_class':'correct' if positive else 'behavioral_deviation','classification_pass':True,
                    'package_dir':str(self.package),'reference_package_dir':str(self.package),
                    'selection_path':str(self.selection),'contract_path':str(self.contract),'analysis_path':str(self.analysis),
                    'result_path':str(result),'settings_sha256':digest(read_json(self.contract)),
                    'independent_review_status':'pass','independent_review_path':str(self.aggregate),
                    'independent_review_sha256':file_sha256(self.aggregate)}))
        failure_analysis=self.root/'failure-analysis.json';write_json(failure_analysis,{'measurement_status':'fail','errors':['fixture rejection']})
        for index in range(6):
            result=self.root/f'fault-{index}.json';write_json(result,{'observed_class':'tracking_failure'})
            fault=self.root/f'fault-review-{index}.json'
            write_json(fault,{'kind':'QualificationFaultReview','sample_id':f'fault-{index}','status':'pass',
                'reviewer':'synthetic fixture','method':'independent_mutation_and_failure_audit',
                'position_review_status':'not_applicable_invalid_measurement',
                'source_manifest_sha256':file_sha256(self.package/'manifest.json'),
                'package_manifest_sha256':file_sha256(self.package/'manifest.json'),
                'selection_sha256':file_sha256(self.selection),'result_sha256':file_sha256(result)})
            self.rows.append(bind_evidence({'id':f'fault-{index}','domain':'fault','kind':'explicit_pixel_or_record_corruption',
                'expected_class':'tracking_failure','observed_class':'tracking_failure','classification_pass':True,
                'package_dir':str(self.package),'reference_package_dir':str(self.package),
                'selection_path':str(self.selection),'contract_path':str(self.contract),'analysis_path':str(failure_analysis),
                'result_path':str(result),'settings_sha256':digest(read_json(self.contract)),
                'independent_review_status':'pass','independent_review_path':str(fault),'independent_review_sha256':file_sha256(fault)}))
        source=self.root/'source.py';source.write_text('# fixture source',encoding='utf-8')
        self.sources={str(source):file_sha256(source)}
        self.component=self.root/'component.json';self.qualification=self.root/'qualification.json';self.save()

    def save(self):
        data={'status':'pass','independent_review_status':'pass','samples':self.rows,'source_hashes':self.sources}
        write_json(self.component,data)
        write_json(self.qualification,{**data,'component_manifests':{str(self.component):file_sha256(self.component)}})

    def test_same_aggregate_replayed_once_per_call_and_again_on_new_call(self):
        with patch.object(review,'validate_independent_review',wraps=review.validate_independent_review) as replay:
            cohort.validate_qualification(self.qualification);self.assertEqual(1,replay.call_count)
            cohort.validate_qualification(self.qualification);self.assertEqual(2,replay.call_count)

    def test_standalone_sample_always_fully_replays(self):
        with patch.object(review,'validate_independent_review',wraps=review.validate_independent_review) as replay:
            cohort._validate_sample(self.rows[0],self.root);cohort._validate_sample(self.rows[1],self.root)
            self.assertEqual(2,replay.call_count)

    def test_new_invocation_detects_changed_linked_annotation_with_same_aggregate(self):
        with patch.object(review,'validate_independent_review',wraps=review.validate_independent_review) as replay:
            cohort.validate_qualification(self.qualification)
            path=self.root/'position/annotation.json';marks=read_json(path);marks['points'][0]['x']=3.;write_json(path,marks)
            with self.assertRaises(VisualError):cohort.validate_qualification(self.qualification)
            self.assertEqual(2,replay.call_count)

    def test_changed_aggregate_is_rejected_even_in_same_cache_scope(self):
        cache={};cohort._validate_sample(self.rows[0],self.root,_review_cache=cache)
        value=read_json(self.aggregate);value['status']='fail';write_json(self.aggregate,value)
        with self.assertRaisesRegex(VisualError,'Qualification review changed'):
            cohort._validate_sample(self.rows[1],self.root,_review_cache=cache)

    def test_wrong_observation_member_rejected_after_cache_hit(self):
        changed=deepcopy(self.obs);changed['objects'][0]['frames'][0]['x']=2.
        other=self.root/'unreviewed-analysis.json';write_json(other,{'observations':changed})
        self.rows[1]=bind_evidence({**self.rows[1],'analysis_path':str(other)})
        self.save()
        with patch.object(review,'validate_independent_review',wraps=review.validate_independent_review) as replay:
            with self.assertRaisesRegex(VisualError,'omits the evaluated recording'):
                cohort.validate_qualification(self.qualification)
            self.assertEqual(1,replay.call_count)

    def test_raw_package_is_revalidated_on_cache_hit(self):
        cache={};cohort._validate_sample(self.rows[0],self.root,_review_cache=cache)
        (self.package/'frames/000000.png').write_bytes(b'changed raw pixels')
        with self.assertRaises(VisualError):cohort._validate_sample(self.rows[1],self.root,_review_cache=cache)


if __name__=='__main__':unittest.main()
