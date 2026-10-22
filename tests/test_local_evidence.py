from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from kiroclops.local_engine import evidence as e
from kiroclops.local_engine.session import JOB_BINDING_FIELDS
from kiroclops.visual.schema import read_json,VisualError
from kiroclops.visual.package import create_package

from kiroclops.local_engine.evidence import measurement_groups,required_observation_bindings,event_set
from kiroclops.visual.schema import write_json,digest


class EvidenceTests(unittest.TestCase):
    def test_nested_preserved_jump_and_boundary_are_required_but_unselected_candidate_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);obs=[]
            for context in ('prepare','chosen','sealed','sealed/jump','sealed/boundary','unselected'):
                value={'package_sha256':context,'objects':[]};obs.append(value)
                write_json(root/'measurements'/context/'candidate.json',[{'scenario':{'id':'case'},'package':'raw','observations':value}])
            state={'prepare_job_id':'prepare','final_job_id':'sealed','selected_candidate_id':'c1',
                   'candidates':[{'candidate_id':'c1','job_id':'chosen'},{'candidate_id':'c2','job_id':'unselected'}]}
            expected={(o['package_sha256'],digest(o)) for o in obs[:-1]}
            self.assertEqual(expected,required_observation_bindings(root,state,require_complete=False))
            self.assertEqual(6,len(list(measurement_groups(root))))

    def test_non_record_json_is_not_a_measurement_group(self):
        with tempfile.TemporaryDirectory() as folder:
            write_json(Path(folder)/'measurements/job/private/candidate.json',[{'x':1}])
            self.assertEqual([],list(measurement_groups(folder)))

    def test_external_analysis_events_sibling_remains_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'analysis.json';write_json(path,{'status':'ok'})
            write_json(path.parent/'events.json',{'events':[{'type':'approach'}]})
            self.assertEqual('approach',event_set({'analysis':str(path)})['events'][0]['type'])


class MatrixFixture:
    """Real job/index serialization; the pixel reader is tested separately."""
    def __init__(self,root,domain='game',stage='ground',mode='tools',media=False):
        self.root=root;self.media=media;self.source={'player.gd':'abc'}
        self.state={'domain':domain,'mode':mode,'baseline':str(root/'baseline'),'baseline_hashes':self.source,
            'experiment':{'stage':stage},'prepare_job_id':'prepare','final_job_id':'sealed','sealed_at':1,
            'selected_candidate_id':'c1','candidates':[{'candidate_id':'c1','job_id':'chosen','status':'succeeded','source_sha256':digest(self.source)},
                {'candidate_id':'c2','job_id':'unselected'}],
            'final':{'job_id':'sealed','status':'succeeded','pass':not media}}
        if media:
            cal=val=[{'id':'upload','split':'calibration','events':[{'action':'click','time_s':.4}]}]
            self.bundle={'record':{'capture_binding':'capture.json','package_sha256':'media-source'}}
            self.state['reference']=str(root/'bundle');self.state['experiment'].update(reference_mode='media_bundle',bundle_sha256=digest(self.bundle))
            write_json(root/'bundle/capture.json',{'scenario':cal[0]})
        elif domain=='game':
            from kiroclops.portable_v2.specs import scenarios
            cal,val=scenarios(stage,'calibration'),scenarios(stage,'validation')
        else:
            from kiroclops.web_local.specs import scenarios
            cal,val=scenarios('svg' if domain=='svg' else 'panel','calibration'),scenarios('svg' if domain=='svg' else 'panel','validation')
            self.state['experiment'].update(calibration=cal,validation=val)
        self.add('prepare','',('reference','before'),cal)
        neutral_game=domain=='game' and mode=='baseline' and not media
        if not neutral_game:self.add('chosen','',('candidate',),cal)
        self.add('sealed','',('reference','before','candidate' if domain=='game' and not media else 'final'),val)
        if domain=='game' and not media:
            from kiroclops.local_engine.visual_boundary_v4 import cases
            self.add('sealed','boundary',('candidate',),cases())
            if stage=='ground':
                from kiroclops.portable_v2.specs import scenarios
                if mode=='tools':
                    self.add('prepare','jump',('reference','before'),scenarios('jump','calibration'))
                    self.add('chosen','jump',('candidate',),scenarios('jump','calibration'))
                self.add('sealed','jump',('reference','before','candidate'),scenarios('jump','validation'))
        for name,op in [('prepare','prepare'),('chosen','evaluate'),('sealed','finalize')]:
            result={'pass':not media}
            if name=='sealed' and media:result.update(heldout_status='not_provided')
            if neutral_game and name=='chosen':
                from kiroclops.portable_v2.specs import run_name
                result['candidate_media']=[{'scenario':s,'package':str(root/'target/.kiroclops-evidence/candidates/c1'/run_name(s))} for s in cal]
            job={'schema_version':1,'job_id':name,'operation':op,'created_at':0,'status':'succeeded',
                'candidate_id':None if name=='prepare' else 'c1','source_sha256':digest(self.source),'source_hashes':self.source,
                'source_path':str(root/'baseline' if name=='prepare' else root/'candidates/c1/source'),'result_sha256':digest(result)}
            write_json(root/'jobs'/name/'job.json',job);write_json(root/'jobs'/name/'result.json',result)
            write_json(root/'jobs'/name/'binding.json',{'sha256':digest({k:job[k] for k in JOB_BINDING_FIELDS})})
    def add(self,job,context,roles,cases):
        for role in roles:
            p=self.root/'measurements'/job/context/(role+'.json')
            folder='packages' if context=='boundary' else 'candidate' if role=='final' else role
            write_json(p,[{'scenario':deepcopy(s),'package':str(p.parent/folder/str(i))} for i,s in enumerate(cases)])
    def check(self):
        calls=[]
        def reader(record,scenario,kind,**kwargs):
            calls.append((record,scenario,kind,kwargs))
            return (record['package'],digest(record)) if kwargs['measured'] else None
        with patch.object(e,'_record_observation',side_effect=reader), patch.object(e,'_producer_expectation',return_value={'kind':'matrix_fixture'}):
            if self.media:
                with patch('kiroclops.local_engine.media_session.load_bundle',return_value=self.bundle):
                    result=e.required_observation_bindings(self.root,self.state)
            else:result=e.required_observation_bindings(self.root,self.state)
        return result,calls


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def test_tools_ground_all_nested_roles_and_unselected_exclusion(self):
        f=MatrixFixture(self.root);write_json(self.root/'measurements/unselected/candidate.json',[{'garbage':True}])
        result,calls=f.check();self.assertEqual(134,len(result))
        self.assertTrue(all(c[3]['measured'] for c in calls));self.assertEqual(8,sum(c[2]=='boundary' for c in calls))
    def test_baseline_ground_raw_only_calibration(self):
        f=MatrixFixture(self.root,mode='baseline');result,calls=f.check()
        self.assertEqual(98,len(result));self.assertEqual(18,sum(not c[3]['measured'] for c in calls))
        self.assertFalse((self.root/'measurements/chosen/candidate.json').exists())
    def test_web_and_svg_complete_matrices(self):
        for domain in ('web_panel','svg'):
            with self.subTest(domain=domain):
                f=MatrixFixture(self.root/domain,domain=domain);result,calls=f.check()
                self.assertEqual(48,len(result));self.assertEqual(48,len(calls))
    def test_media_one_case_not_synthetic_heldout(self):
        for domain in ('game','web_panel'):
            for mode in ('tools','baseline'):
                with self.subTest(domain=domain,mode=mode):
                    f=MatrixFixture(self.root/(domain+mode),domain=domain,mode=mode,media=True)
                    result,calls=f.check();self.assertEqual(6 if mode=='tools' else 3,len(result))
                    self.assertTrue(all(c[2]=='media' for c in calls))
                    self.assertEqual(2,sum(c[3]['reference_package']=='media-source' for c in calls))
    def test_missing_nested_or_calibration_index_cannot_shrink_review(self):
        for relative in ('sealed/boundary/candidate.json','sealed/jump/before.json','prepare/jump/reference.json',
                         'chosen/jump/candidate.json','chosen/candidate.json','sealed/reference.json'):
            with self.subTest(relative=relative):
                f=MatrixFixture(self.root/relative.replace('/','-'));(f.root/'measurements'/relative).unlink()
                with self.assertRaisesRegex(VisualError,'group/role'):f.check()
    def test_duplicate_wrong_and_config_mutated_scenario_rejected(self):
        for mutation in ('duplicate','id','events'):
            with self.subTest(mutation=mutation):
                f=MatrixFixture(self.root/mutation);p=f.root/'measurements/sealed/candidate.json';rows=read_json(p)
                if mutation=='duplicate':rows[1]=deepcopy(rows[0])
                elif mutation=='id':rows[0]['scenario']['id']='unregistered'
                else:rows[0]['scenario']['events'][0]['tick']+=1
                write_json(p,rows)
                with self.assertRaisesRegex(VisualError,'scenarios'):f.check()
    def test_extra_role_and_malformed_index_rejected(self):
        f=MatrixFixture(self.root);write_json(self.root/'measurements/sealed/final.json',[])
        with self.assertRaisesRegex(VisualError,'group/role'):f.check()
        (self.root/'measurements/sealed/final.json').unlink();write_json(self.root/'measurements/sealed/candidate.json',[{'x':1}])
        with self.assertRaises(VisualError):f.check()
    def test_missing_result_source_or_result_tamper_rejected(self):
        for mutation in ('missing','source','result'):
            with self.subTest(mutation=mutation):
                f=MatrixFixture(self.root/mutation);folder=f.root/'jobs/chosen'
                if mutation=='missing':(folder/'result.json').unlink()
                elif mutation=='result':write_json(folder/'result.json',{'pass':False})
                else:
                    job=read_json(folder/'job.json');job['source_sha256']='wrong';write_json(folder/'job.json',job)
                with self.assertRaises(VisualError):f.check()
    def test_missing_config_and_unfinished_acceptance_rejected_partial_allowed(self):
        f=MatrixFixture(self.root);del f.state['domain']
        with self.assertRaises(VisualError):f.check()
        f.state['domain']='game';f.state['final']=None
        with self.assertRaisesRegex(VisualError,'completed sealed'):f.check()
        self.assertIsInstance(e.required_observation_bindings(self.root,f.state,require_complete=False),set)
    def test_changed_browser_matrix_and_neutral_export_rejected(self):
        f=MatrixFixture(self.root/'web',domain='web_panel');f.state['experiment']['validation'].pop()
        with self.assertRaisesRegex(VisualError,'scenario matrix'):f.check()
        f=MatrixFixture(self.root/'game',mode='baseline');p=f.root/'jobs/chosen/result.json';result=read_json(p)
        result['candidate_media'][0]['scenario']['id']='wrong';write_json(p,result)
        job=read_json(p.parent/'job.json');job['result_sha256']=digest(result);write_json(p.parent/'job.json',job)
        with self.assertRaisesRegex(VisualError,'Neutral game scenarios'):f.check()
    def test_legacy_jump_missing_id_is_explicit_timing_bound_projection(self):
        from kiroclops.portable_v2.specs import scenarios
        expected=scenarios('jump','validation')[0];legacy={k:expected[k] for k in ('hold_ticks','start_tick','split','repeat','spawn_offset')}
        self.assertEqual(e._scenario(expected,'jump'),e._scenario(legacy,'jump'))
        legacy['events']=[{'tick':99,'action':'jump','pressed':True}]
        with self.assertRaises(VisualError):e._scenario(legacy,'jump')

    def test_native_simultaneous_edges_are_order_independent_but_tick_bound(self):
        from kiroclops.portable_v2.specs import scenarios
        scenario=next(s for s in scenarios('ground','validation') if s['id']=='reverse24')
        events=[{'time_s':x['tick']/60,'action':x['action'],'pressed':x['pressed']} for x in scenario['events']]
        manifest={'metadata':{'scenario':scenario},'frames':[{'index':i,'time_s':i/60} for i in range(240)],
                  'inputs':{'events':list(reversed(events))}}
        row={'scenario':scenario,'package':'native-receipt'}
        with patch.object(e,'load_package',return_value=manifest):
            self.assertIsNone(e._record_observation(row,scenario,'ground',measured=False))
            manifest['inputs']['events'][0]['time_s']+=1/60
            with self.assertRaisesRegex(VisualError,'recorded inputs'):
                e._record_observation(row,scenario,'ground',measured=False)

    def test_media_variant_is_role_metadata_not_changed_input_schedule(self):
        scenario={'id':'upload','events':[{'tick':30,'action':'jump','pressed':True}]}
        self.assertEqual(e._scenario(scenario,'media'),e._scenario(dict(scenario,variant='after'),'media'))
        changed=deepcopy(scenario);changed['events'][0]['tick']=31
        self.assertNotEqual(e._scenario(scenario,'media'),e._scenario(changed,'media'))


class PixelBindingTests(unittest.TestCase):
    def setUp(self):
        import cv2,numpy as np
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        image=self.root/'image.png';cv2.imencode('.png',np.zeros((4,4,3),np.uint8))[1].tofile(image)
        self.scenario={'id':'case','events':[{'action':'click','time_s':.1}],'split':'validation'}
        self.manifest=create_package(self.root/'package',[image]*3,[0.,.1,.2],{'provenance':'recorder_verified','events':[]},{'scenario':self.scenario})
        self.selection={'objects':[{'id':'a','roi':[0,0,2,2]}]}
        self.obs={'package_sha256':self.manifest['package_sha256'],'image_size':[4,4],'inputs':self.manifest['inputs'],'selection':self.selection,
            'objects':[{'id':'a','frames':[{'index':i,'time_s':t} for i,t in enumerate((0.,.1,.2))]}]}
        self.analysis=self.root/'analysis.json';self.record={'scenario':self.scenario,'package':str(self.root/'package'),
            'analysis':str(self.analysis),'observations':self.obs,'selection':self.selection};self.save()
    def save(self):write_json(self.analysis,{'observations':self.obs,'package_sha256':self.manifest['package_sha256']})
    def check(self):return e._record_observation(self.record,self.scenario,'web',measured=True)
    def test_actual_package_and_analysis_binding(self):self.assertEqual((self.manifest['package_sha256'],digest(self.obs)),self.check())
    def test_missing_analysis_not_healed_by_inline_observations(self):
        self.analysis.unlink()
        with self.assertRaises(OSError):self.check()
    def test_missing_observations_rejected(self):
        self.record.pop('observations');write_json(self.analysis,{'status':'ok'})
        with self.assertRaisesRegex(VisualError,'observations'):self.check()
    def test_raw_pixel_tamper_rejected(self):
        (self.root/'package/frames/000001.png').write_bytes(b'changed')
        with self.assertRaises(VisualError):self.check()
    def test_selection_same_id_changed_roi_rejected(self):
        self.record['selection']={'objects':[{'id':'a','roi':[2,2,2,2]}]}
        with self.assertRaisesRegex(VisualError,'configuration'):self.check()
    def test_missing_selection_rejected(self):
        self.record.pop('selection')
        with self.assertRaises(OSError):self.check()
    def test_inline_saved_disagreement_rejected(self):
        self.record['observations']=deepcopy(self.obs);self.record['observations']['package_sha256']='other'
        with self.assertRaisesRegex(VisualError,'Inline'):self.check()
    def test_duplicate_frame_and_missing_object_rejected(self):
        self.obs['objects'][0]['frames'][1]['index']=0;self.save()
        with self.assertRaisesRegex(VisualError,'frame identities'):self.check()
        self.obs['objects']=[];self.save()
        with self.assertRaisesRegex(VisualError,'objects'):self.check()
    def test_baseline_raw_only_and_media_source_binding(self):
        row={'scenario':self.scenario,'package':self.record['package']}
        self.assertIsNone(e._record_observation(row,self.scenario,'web',measured=False))
        row['observations']=self.obs
        with self.assertRaisesRegex(VisualError,'raw-only'):e._record_observation(row,self.scenario,'web',measured=False)
        scenario={'id':'renamed-upload'};self.record['scenario']=scenario
        self.assertIsNotNone(e._record_observation(self.record,scenario,'media',measured=True,reference_package=self.manifest['package_sha256']))
        with self.assertRaises(VisualError):e._record_observation(self.record,scenario,'media',measured=True,reference_package='wrong')


if __name__=='__main__':unittest.main()
