"""Review consumers bind actual producer receipts, not merely self-consistent pixels."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from kiroclops.local_engine import evidence as e
from kiroclops.visual.package import create_package,load_package
from kiroclops.visual.schema import digest,read_json,write_json,file_sha256,VisualError


class ProducerSourcesTests(unittest.TestCase):
    def setUp(self):
        import cv2,numpy as np
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.image=self.root/'frame.png'
        cv2.imencode('.png',np.zeros((4,4,3),np.uint8))[1].tofile(self.image)
        self.sources={'index.html':'a'*64,'src/motion.js':'b'*64}
        self.scenario={'id':'case','split':'validation','events':[]}

    def web(self,name='record',sources=None):
        sources=self.sources if sources is None else sources;folder=self.root/name
        m=create_package(folder/'package',[self.image]*3,[0,.1,.2],
            {'provenance':'recorder_verified','events':[]},{'scenario':self.scenario,'source_hashes':sources})
        selection={'objects':[{'id':'natural-icon','roi':[0,0,2,2]}]}
        obs={'package_sha256':m['package_sha256'],'image_size':[4,4],'inputs':m['inputs'],'selection':selection,
            'objects':[{'id':'natural-icon','frames':[{'index':i,'time_s':i/10} for i in range(3)]}]}
        analysis=folder/'analysis.json'
        write_json(analysis,{'observations':obs,'package_sha256':m['package_sha256'],'candidate_sha256':digest(sources)})
        return {'scenario':self.scenario,'package':str(folder/'package'),'analysis':str(analysis),
                'observations':obs,'selection':selection,'source_sha256':digest(sources)},m

    def test_foreign_web_source_swap_rejected_with_valid_package_and_observation_hashes(self):
        row,m=self.web(sources={**self.sources,'src/motion.js':'c'*64})
        self.assertEqual(m['package_sha256'],load_package(row['package'])['package_sha256'])
        self.assertIsNotNone(e._record_observation(row,self.scenario,'web',measured=True))
        with self.assertRaisesRegex(VisualError,'different browser source'):
            e._record_observation(row,self.scenario,'web',measured=True,expected_source={'kind':'web','sources':self.sources})

    def test_matching_web_source_and_analysis_pass_but_analysis_other_candidate_fails(self):
        row,_=self.web();expected={'kind':'web','sources':self.sources}
        self.assertIsNotNone(e._record_observation(row,self.scenario,'web',measured=True,expected_source=expected))
        value=read_json(row['analysis']);value['candidate_sha256']='f'*64;write_json(row['analysis'],value)
        with self.assertRaisesRegex(VisualError,'Analysis belongs'):
            e._record_observation(row,self.scenario,'web',measured=True,expected_source=expected)
        value.pop('candidate_sha256');write_json(row['analysis'],value)
        with self.assertRaisesRegex(VisualError,'Analysis belongs'):
            e._record_observation(row,self.scenario,'web',measured=True,expected_source=expected)

    def test_raw_baseline_source_checked_without_opening_analysis(self):
        row,_=self.web();row.pop('analysis');row.pop('observations');row.pop('selection')
        self.assertIsNone(e._record_observation(row,self.scenario,'web',measured=False,expected_source={'kind':'web','sources':self.sources}))
        with self.assertRaisesRegex(VisualError,'different browser source'):
            e._record_observation(row,self.scenario,'web',measured=False,expected_source={'kind':'web','sources':{'other':'a'}})

    def test_frozen_reference_package_identity_not_replaced_by_same_source_capture(self):
        row,_=self.web()
        with self.assertRaisesRegex(VisualError,'shared frozen'):
            e._record_observation(row,self.scenario,'web',measured=True,
                expected_source={'kind':'web','sources':self.sources,'package_sha256':'wrong'})

    def native(self,name='native',sources=None,boundary=False,game='heart'):
        sources=sources or {'player.gd':'b'*64};raw=self.root/name/'raw';(raw/'frames').mkdir(parents=True)
        frames=[{'frame_index':i,'physics_tick':60+i,'time_s':i/60,'actions':[]} for i in range(240)]
        for i in range(240):(raw/'frames'/f'{i:06d}.png').write_bytes(self.image.read_bytes())
        hashes={f'{i:06d}.png':file_sha256(self.image) for i in range(240)}
        write_json(raw/'capture_config.json',{'id':'case'})
        write_json(raw/'recording.json',{'status':'ok','game':game,'fps':60,'frame_count':240,'frames':frames})
        if boundary:
            write_json(raw/'private-process/process.json',{'exit_code':0})
            prov={'source_hashes':sources,'process_sha256':file_sha256(raw/'private-process/process.json'),
                  'config_sha256':file_sha256(raw/'capture_config.json')}
        else:
            receipt=raw/'engine/bridge.json';write_json(receipt,{'status':'completed','receipt_path':str(receipt),'source_hashes_before':sources,'source_hashes_after':sources})
            prov={'full_source_hashes_before':sources,'full_source_hashes_after':sources,
                'source_digest_before':digest(sources),'source_digest_after':digest(sources),
                'runtime_receipt':str(receipt),'runtime_receipt_sha256':file_sha256(receipt),
                'recording_sha256':file_sha256(raw/'recording.json'),'capture_config_sha256':file_sha256(raw/'capture_config.json')}
        write_json(raw/'manifest.json',{'status':'ok','game':game,'fps':60,'frame_count':240,'scenario':self.scenario,
            'frames':frames,'raw_frame_hashes':hashes,'provenance':prov})
        m=create_package(self.root/name/'package',[raw/'frames'/f'{i:06d}.png' for i in range(240)],
            [frame['time_s'] for frame in frames],{'provenance':'recorder_verified','events':[]},
            {'adapter':'legacy-godot-v1','scenario':self.scenario,'source_manifest_sha256':file_sha256(raw/'manifest.json')})
        if boundary:
            write_json(raw/'runtime-receipt.json',{'status':'pass','source_hashes':sources,'manifest_sha256':file_sha256(raw/'manifest.json'),
                'raw_frame_hashes':hashes,'process_sha256':prov['process_sha256']})
        return {'raw_run':str(raw),'package':str(self.root/name/'package')},m,sources

    def test_native_sources_and_png_bridge(self):
        row,m,sources=self.native();e._record_source(row,m,{'kind':'native','sources':sources})
        with self.assertRaisesRegex(VisualError,'different controller'):
            e._record_source(row,m,{'kind':'native','sources':{'player.gd':'a'*64}})
        (Path(row['raw_run'])/'frames/000000.png').write_bytes(b'changed')
        with self.assertRaisesRegex(VisualError,'Package pixels'):
            e._record_source(row,m,{'kind':'native','sources':sources})

    def test_other_native_receipt_cannot_be_rebound_to_valid_package(self):
        row,m,sources=self.native();raw=Path(row['raw_run']);p=raw/'engine/bridge.json'
        receipt=read_json(p);receipt['source_hashes_after']={'player.gd':'f'*64};write_json(p,receipt)
        with self.assertRaisesRegex(VisualError,'producer receipt'):
            e._record_source(row,m,{'kind':'native','sources':sources})

    def test_boundary_requires_selected_base_and_exact_overlay(self):
        base={'player.gd':'a'*64};rendered={**base,'kiroclops_visual_boundary_v4/boundary.gd':'b'*64}
        row,m,_=self.native(sources=rendered,boundary=True);row.update(source_hashes=base,candidate_id='c1')
        expected={'kind':'boundary','sources':base,'rendered_sources':rendered,'candidate_id':'c1'}
        e._record_source(row,m,expected)
        for changed in ({**expected,'sources':{'player.gd':'wrong'}},{**expected,'candidate_id':'c2'},
                        {**expected,'rendered_sources':{**rendered,'hidden.gd':'x'}}):
            with self.assertRaisesRegex(VisualError,'selected controller'):
                e._record_source(row,m,changed)

    def test_frozen_tiny_reference_rejects_other_run_even_with_valid_pixels(self):
        row,m,_=self.native(game='tiny');e._record_source(row,m,{'kind':'frozen_reference','raw_run':row['raw_run']})
        with self.assertRaisesRegex(VisualError,'frozen Tiny'):
            e._record_source(row,m,{'kind':'frozen_reference','raw_run':str(self.root/'other')})

    def test_same_code_foreign_job_and_wrong_nested_role_still_fail(self):
        state={'experiment':{},'prepare_job_id':'prepare'}
        own=self.root/'measurements/final/jump/candidate/run'
        e._record_origin(self.root,state,'final','jump','candidate',{'package':str(own/'package'),'raw_run':str(own/'raw')})
        for wrong in ('old/jump/candidate','final/jump/before','final/candidate'):
            with self.assertRaisesRegex(VisualError,'job/context/role'):
                e._record_origin(self.root,state,'final','jump','candidate',{'package':str(self.root/'measurements'/wrong/'package')})

    def test_explicit_media_reference_and_prepared_before_reuse(self):
        state={'experiment':{'reference_mode':'media_bundle'},'prepare_job_id':'prepare'}
        e._record_origin(self.root,state,'final','','reference',{'package':str(self.root/'bundle/package')})
        e._record_origin(self.root,state,'final','','before',{'package':str(self.root/'measurements/prepare/before-capture/package')})
        with self.assertRaises(VisualError):
            e._record_origin(self.root,state,'final','','candidate',{'package':str(self.root/'measurements/prepare/before-capture/package')})

    def test_ground_jump_denominator_uses_bound_original_not_inherited_baseline(self):
        from kiroclops.portable_v2.runtime import source_hashes
        original=self.root/'original';original.mkdir();(original/'player.gd').write_text('original')
        original_hashes=source_hashes(original)
        private={'source_bindings':{str(original.resolve()):original_hashes}}
        write_json(self.root/'prepared-private.json',private)
        write_json(self.root/'prepared-evidence.json',{'prepared_private_sha256':file_sha256(self.root/'prepared-private.json')})
        write_json(self.root/'jobs/prepare/result.json',{'prepared_evidence_sha256':file_sha256(self.root/'prepared-evidence.json')})
        state={'domain':'game','experiment':{'original_baseline':str(original)},'prepare_job_id':'prepare'}
        value=e._producer_expectation(self.root,state,{},'before','jump','jump',{},{} )
        self.assertEqual(original_hashes,value['sources'])
        (original/'player.gd').write_text('inherited change')
        with self.assertRaisesRegex(VisualError,'denominator source changed'):
            e._producer_expectation(self.root,state,{},'before','jump','jump',{},{} )

    def test_native_policy_is_derived_from_verified_full_session_snapshot(self):
        from kiroclops.local_engine.session import source_hashes
        project=self.root/'candidates/c1/source';project.mkdir(parents=True)
        (project/'player.gd').write_text('code');(project/'player.gd.uid').write_text('sidecar')
        full=source_hashes(project);write_json(self.root/'jobs/chosen/job.json',{'source_hashes':full})
        state={'domain':'game','experiment':{}};candidate={'candidate_id':'c1','job_id':'chosen'}
        value=e._producer_expectation(self.root,state,candidate,'candidate','','jump',{},{} )
        self.assertEqual(full,value['session_sources']);self.assertNotIn('player.gd.uid',value['sources'])
        (project/'player.gd.uid').write_text('tampered original')
        with self.assertRaisesRegex(VisualError,'producer source changed'):
            e._producer_expectation(self.root,state,candidate,'candidate','','jump',{},{} )


if __name__=='__main__':unittest.main()
