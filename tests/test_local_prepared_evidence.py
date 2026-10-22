"""Actual portable pixels and durable job receipts, without app/model execution."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.prepared_evidence import store_prepared,load_prepared
from kiroclops.local_engine.session import Session,create_session,run_session_job
from kiroclops.local_engine.evaluation import _run_web
from kiroclops.local_engine.game import run_game_job
from kiroclops.visual.package import create_package,load_package
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.schema import VisualError,read_json,write_json,digest,file_sha256


class PreparedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'evidence café';self.root.mkdir()
        self.initial=self.root/'initial';self.initial.mkdir();(self.initial/'player.gd').write_text('extends KinematicBody2D\n')
        (self.initial/'src').mkdir();(self.initial/'src/panel-motion.js').write_text('export const test = 1;')
        self.raw=self.root/'pixels';self.raw.mkdir();self.frames=[]
        image=np.zeros((64,96,3),np.uint8);rng=np.random.default_rng(9)
        image[20:30,25:35]=rng.integers(20,245,(10,10,3),dtype=np.uint8)
        image[44:56,8:20]=rng.integers(20,245,(12,12,3),dtype=np.uint8)
        for i in range(3):
            path=self.raw/f'{i:06}.png';path.write_bytes(cv2.imencode('.png',image)[1].tobytes());self.frames.append(path)

    def prepared(self,domain='web_panel',mode='tools'):
        folder=self.root/f'{domain}-{mode}';session_dir=folder/'session'
        state=create_session(session_dir,domain,self.initial,self.initial,'recorded fixture',mode,
            {'stage':'jump','capture_lock':str(folder/'unit.lock')})
        session=Session(session_dir)
        with patch.object(Session,'_spawn'):submitted=session.submit('prepare')
        def runner(root,job,check,progress,cancel):
            root=Path(root);output=root/'measurements'/job['job_id'];indexes={}
            for role in ('reference','before'):
                destination=output/role;package=destination/'package'
                create_package(package,self.frames,[0.,1/60,2/60],{'provenance':'unknown','events':[]},{'role':role})
                row={'scenario':{'id':'fixture'},'package':str(package),'functional':{'status':'pass'}}
                if mode=='tools':
                    selection={'schema_version':1,'objects':[{'id':'character','roi':[25,20,10,10],'anchor':[5,5],'method':'template'}],
                        'background_rois':[[8,44,12,12]]}
                    observed=analyze_package(package,selection,{'require_static_camera':True})
                    self.assertEqual('ok',observed['status'])
                    events={'schema_version':1,'kind':'EventSet','events':[]}
                    write_json(destination/'selection.json',selection)
                    row.update(analysis=str(destination/'analysis.json'),measurement_status='ok')
                    if domain=='game':
                        write_json(destination/'observations.json',observed)
                        write_json(destination/'analysis.json',{'metrics':{'height_body':1.},'provenance':{
                            'package_sha256':load_package(package)['package_sha256'],'observation_sha256':digest(observed)}})
                    else:
                        write_json(destination/'analysis.json',{'observations':observed,'events':events})
                        row.update(observations=observed,events=events,selection=selection)
                indexes[role]=output/(role+'.json');write_json(indexes[role],[row])
            prepared={role:str(path) for role,path in indexes.items()};prepared.update(stage='jump',source_bindings={})
            extras={}
            if domain=='game' and mode=='tools':
                extras['profile']=output/'profile.json';write_json(extras['profile'],{'height_body':1.})
            sha=store_prepared(root,job,read_json(root/'session.json'),prepared,indexes,extras)
            return {'status':'ready','prepared_evidence_sha256':sha}
        run_session_job(session_dir,submitted['job_id'],runner=runner)
        finished=session.get_job(submitted['job_id']);self.assertEqual('succeeded',finished['status'],finished.get('error'))
        state=read_json(session_dir/'session.json');self.assertIn('reference',load_prepared(session_dir,state))
        return session_dir,state,session_dir/'measurements'/submitted['job_id']

    def test_current_web_and_game_cache_can_be_reconnected_and_neutral_cache_has_no_cv(self):
        for domain in ('web_panel','game'):
            for mode in ('tools','baseline'):
                with self.subTest(domain=domain,mode=mode):
                    directory,state,output=self.prepared(domain,mode)
                    value=load_prepared(directory,state)
                    row=read_json(value['reference'])[0]
                    self.assertEqual(mode=='tools','analysis' in row)
                    self.assertEqual(state['prepare_job_id'],read_json(directory/'prepared-evidence.json')['prepare_job_id'])

    def test_cached_index_mutation_blocks_real_dispatch_before_candidate_capture(self):
        for domain in ('web_panel','game'):
            with self.subTest(domain=domain):
                directory,state,output=self.prepared(domain)
                rows=read_json(output/'reference.json');rows[0]['scenario']['id']='other-scenario';write_json(output/'reference.json',rows)
                target='kiroclops.local_engine.evaluation._web_matrix' if domain=='web_panel' else 'kiroclops.local_engine.game._capture'
                dispatch=_run_web if domain=='web_panel' else run_game_job
                with patch(target) as capture:
                    with self.assertRaises(VisualError):dispatch(directory,{'job_id':'next','operation':'evaluate','source_path':str(directory/'target')},state,lambda:None,lambda *a:None,lambda:False)
                capture.assert_not_called()

    def test_game_metric_and_common_observation_tampering_are_both_rejected(self):
        directory,state,output=self.prepared('game')
        path=output/'reference/analysis.json';original=read_json(path);mutated=deepcopy(original);mutated['metrics']['height_body']=99;write_json(path,mutated)
        with self.assertRaises(VisualError):load_prepared(directory,state)
        write_json(path,original)
        path=output/'reference/observations.json';observed=read_json(path);observed['objects'][0]['frames'][1]['x']+=20;write_json(path,observed)
        with self.assertRaises(VisualError):load_prepared(directory,state)

    def test_actual_pixel_mutation_is_not_hidden_by_unchanged_json(self):
        directory,state,output=self.prepared()
        frame=output/'reference/package/frames/000001.png';frame.write_bytes(frame.read_bytes()+b'changed')
        with self.assertRaises(VisualError):load_prepared(directory,state)

    def test_self_rehashed_private_manifest_still_fails_completed_job_binding(self):
        directory,state,output=self.prepared()
        private=read_json(directory/'prepared-private.json');private['reference']=private['before'];write_json(directory/'prepared-private.json',private)
        receipt=read_json(directory/'prepared-evidence.json');receipt['prepared_private_sha256']=file_sha256(directory/'prepared-private.json');write_json(directory/'prepared-evidence.json',receipt)
        with self.assertRaisesRegex(VisualError,'completed prepare result'):load_prepared(directory,state)

    def test_other_session_or_wrong_prepare_job_and_changed_profile_are_rejected(self):
        directory,state,output=self.prepared('game')
        for key in ('session_id','prepare_job_id'):
            changed=deepcopy(state);changed[key]='another'
            with self.assertRaises(VisualError):load_prepared(directory,changed)
        profile=output/'profile.json';write_json(profile,{'height_body':99.})
        with self.assertRaisesRegex(VisualError,'profile changed'):load_prepared(directory,state)

    def test_cannot_register_observations_from_a_different_pixel_package(self):
        directory,state,output=self.prepared()
        refs=read_json(output/'reference.json');refs[0]['observations']['package_sha256']='wrong'
        write_json(output/'reference.json',refs)
        analysis=read_json(output/'reference/analysis.json');analysis['observations']=refs[0]['observations']
        write_json(output/'reference/analysis.json',analysis)
        with self.assertRaisesRegex(VisualError,'another recording'):
            store_prepared(directory,{'job_id':state['prepare_job_id']},state,read_json(directory/'prepared-private.json'),
                {'reference':output/'reference.json','before':output/'before.json'})


if __name__=='__main__':unittest.main()
