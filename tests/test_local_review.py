"""Exact pixels, frozen review criteria, and separate independent evidence gates."""
from pathlib import Path
import tempfile
import unittest
import cv2
import numpy as np
from kiroclops.visual.schema import VisualError,digest,read_json,write_json,file_sha256
from kiroclops.visual.package import create_package
from kiroclops.local_engine.review import (prepare_review,prepare_review_group,evaluate_review,
    prepare_scope_review,evaluate_scope_review,validate_independent_review)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def recording(self,name,count=24,start=0):
        images=[]
        for index in range(count):
            p=self.root/f'{name}-{index}.png';p.write_bytes(cv2.imencode('.png',np.full((32,32,3),index+start,np.uint8))[1].tobytes());images.append(p)
        package=self.root/name;manifest=create_package(package,images,[index*.02 for index in range(count)])
        obs={'kind':'ObservationSet','package_sha256':manifest['package_sha256'],'objects':[
            {'id':'box','frames':[{'index':i,'time_s':i*.02,'x':10.,'y':12.,'valid':True} for i in range(count)]}]}
        return package,obs

    def marks(self,directory):
        request=read_json(directory/'request.json')
        return {'request_sha256':digest(request),'reviewer':'test reviewer','method':'independent_raw_image_marking',
            'points':[{k:t[k] for k in ('record_id','object_id','index','image_sha256')}|{'x':10.,'y':12.} for t in request['tasks']]}

    def test_twenty_images_aggregate_short_clips_by_object_and_role(self):
        a,oa=self.recording('a',12);b,ob=self.recording('b',12,12)
        with self.assertRaises(VisualError):prepare_review(a,oa,self.root/'single',tolerance_px=1.5)
        directory=self.root/'group';prepare_review_group([{'package':a,'observations':oa,'role':'reference'},
            {'package':b,'observations':ob,'role':'reference'}],directory,tolerance_px=1.5)
        result=evaluate_review(directory,self.marks(directory));self.assertEqual('pass',result['status']);self.assertEqual(20,result['objects'][0]['unique_images'])

    def test_roles_cannot_borrow_each_others_twenty_images(self):
        a,oa=self.recording('a',12);b,ob=self.recording('b',12,12)
        with self.assertRaises(VisualError):prepare_review_group([{'package':a,'observations':oa,'role':'reference'},
            {'package':b,'observations':ob,'role':'candidate'}],self.root/'group',tolerance_px=1.5)

    def test_threshold_or_task_edit_cannot_heal_bad_marks(self):
        package,obs=self.recording('a');directory=self.root/'review';prepare_review(package,obs,directory,tolerance_px=1.5)
        marks=self.marks(directory);request=read_json(directory/'request.json');request['tolerance_px']=100;write_json(directory/'request.json',request)
        with self.assertRaises(VisualError) as c:evaluate_review(directory,marks)
        self.assertEqual('stale_review',c.exception.code)

    def test_foreign_request_and_changed_measurement_reject(self):
        package,obs=self.recording('a');directory=self.root/'review';prepare_review(package,obs,directory,tolerance_px=1.5)
        marks=self.marks(directory);marks['request_sha256']='other'
        with self.assertRaises(VisualError):evaluate_review(directory,marks)
        marks=self.marks(directory);value=read_json(directory/'observations/record-000.json');value['objects'][0]['frames'][0]['x']=11;write_json(directory/'observations/record-000.json',value)
        with self.assertRaises(VisualError):evaluate_review(directory,marks)

    def scope(self,package,obs,scope):
        events={'kind':'EventSet','package_sha256':obs['package_sha256'],'observation_sha256':digest(obs),
            'events':[{'type':'motion_start','object_ids':['box'],'time_interval_s':[.1,.12],'frame_indices':[5,6]}],
            'relations':[{'type':'connection_attachment','connection_id':'edge','index':5,'time_s':.1,
                'endpoint_distances_px':[0.,1.],'visually_attached':True}]}
        directory=self.root/scope;prepare_scope_review(package,obs,events,directory,scope=scope,candidate_sha256='candidate')
        request=read_json(directory/'request.json');checks=[]
        for task in request['tasks']:
            checks.append({'id':task['id'],'verdict':'pass','evidence':'Independent synthetic fixture audit','frame_sha256s':[f['sha256'] for f in task['frames']],
                **({'observed_interval_s':[.1,.12]} if scope=='events' else {'observed':{'endpoint_distances_px':[0.,1.],'visually_attached':True}})})
        annotation={'request_sha256':digest(request),'reviewer':'test reviewer','method':'independent_raw_sequence_review','checks':checks}
        return directory,annotation

    def test_event_timing_requires_actual_independent_interval(self):
        package,obs=self.recording('a');directory,annotation=self.scope(package,obs,'events')
        self.assertEqual('pass',evaluate_scope_review(directory,annotation)['status'])
        annotation['checks'][0]['observed_interval_s']=[.3,.32]
        self.assertEqual('fail',evaluate_scope_review(directory,annotation)['status'])

    def test_aggregate_replays_all_scopes_and_frozen_annotations(self):
        package,obs=self.recording('a');position=self.root/'position';prepare_review(package,obs,position,tolerance_px=1.5,candidate_sha256='candidate')
        evaluate_review(position,self.marks(position))
        aggregate={'kind':'IndependentVisualReview','status':'pass','candidate_sha256':'candidate',
            'required_observations':[[obs['package_sha256'],digest(obs)]]}
        for scope in ('position','events','relationships'):
            if scope=='position':directory=position
            else:
                directory,annotation=self.scope(package,obs,scope);evaluate_scope_review(directory,annotation)
            aggregate[scope+'_reviews']=[{'directory':str(directory),'request_sha256':digest(read_json(directory/'request.json')),
                'result_sha256':file_sha256(directory/'review-result.json')}]
        path=self.root/'aggregate.json';write_json(path,aggregate)
        self.assertEqual('pass',validate_independent_review(path,candidate_sha256='candidate')['status'])
        aggregate['relationships_reviews']=[];write_json(path,aggregate)
        with self.assertRaises(VisualError):validate_independent_review(path,candidate_sha256='candidate')

    def test_minimal_pass_receipt_never_accepts(self):
        path=self.root/'review.json';write_json(path,{'status':'pass','candidate_sha256':'candidate'})
        with self.assertRaises(VisualError):validate_independent_review(path,candidate_sha256='candidate')

    def test_unresolved_event_cannot_pass_by_copying_broad_interval(self):
        package,obs=self.recording('a');directory=self.root/'broad'
        events={'kind':'EventSet','observation_sha256':digest(obs),'events':[{'type':'settled','object_ids':['box'],
            'time_interval_s':[.1,.4],'frame_indices':[5,20],'timing_status':'unresolved'}]}
        prepare_scope_review(package,obs,events,directory,scope='events')
        request=read_json(directory/'request.json');task=request['tasks'][0]
        annotation={'request_sha256':digest(request),'reviewer':'test','method':'independent_raw_sequence_review',
            'checks':[{'id':task['id'],'verdict':'pass','evidence':'Too broad to establish onset',
                'frame_sha256s':[f['sha256'] for f in task['frames']],'observed_interval_s':[.1,.4]}]}
        self.assertEqual('fail',evaluate_scope_review(directory,annotation)['status'])

    def test_pair_relations_keep_each_identity_and_require_measured_values(self):
        package,obs=self.recording('a');directory=self.root/'pairs'
        relations=[{'type':'relative_anchor','object_ids':['a','parent'],'index':i,'time_s':i*.02,'dx_px':10.,'dy_px':-5.}
            for i in range(24)]+[{'type':'relative_anchor','object_ids':['b','parent'],'index':i,'time_s':i*.02,'dx_px':20.,'dy_px':-3.}
            for i in range(24)]
        relations.append({'type':'relative_event_timing','object_ids':['a','b'],'event_type':'motion_start','occurrence':0,
            'lag_interval_s':[.06,.10],'order':'before','timing_status':'resolved',
            'reference_frame_indices':[5,6],'candidate_frame_indices':[9,10]})
        events={'kind':'EventSet','observation_sha256':digest(obs),'relations':relations}
        prepare_scope_review(package,obs,events,directory,scope='relationships')
        request=read_json(directory/'request.json');self.assertEqual(41,len(request['tasks']))
        checks=[]
        for task in request['tasks']:
            observed=({'lag_interval_s':[.06,.10],'order':'before'} if task['measurement']['type']=='relative_event_timing'
                else {'dx_px':10.,'dy_px':-5.} if task['measurement']['object_ids'][0]=='a' else {'dx_px':20.,'dy_px':-3.})
            checks.append({'id':task['id'],'verdict':'pass','evidence':'Independent fixture geometry',
                'frame_sha256s':[f['sha256'] for f in task['frames']],'observed':observed})
        annotation={'request_sha256':digest(request),'reviewer':'test','method':'independent_raw_sequence_review','checks':checks}
        self.assertEqual('pass',evaluate_scope_review(directory,annotation)['status'])
        checks[-1]['observed']['lag_interval_s']=[.2,.24]
        self.assertEqual('fail',evaluate_scope_review(directory,annotation)['status'])


if __name__=='__main__':unittest.main()
