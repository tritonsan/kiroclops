"""Immutable reference reuse, with tiny JSON/pixel-byte doubles and no CV/native.

The fake loader/copier checks real file bytes but deliberately does not decode
images. Comparator outputs are explicit protocol doubles, not visual evidence.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import reference_cache as cache
from kiroclops.local_engine.backend import LocalAnalysisBackend
from kiroclops.local_engine.evaluation import _reference_web_matrix, _run_web, _measure_record as _REAL_MEASURE
from kiroclops.local_engine.qualification import analyzer_sources
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.web_local.specs import scenarios


# Keep the real submission implementation before each test forbids worker use.
# The dedicated compatibility test calls it with process launch stubbed out.
_REAL_SUBMIT = LocalAnalysisBackend.submit


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='reference cache naïve ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.code=self.root/'code';self.code.mkdir()
        self.source_names=['kiroclops/local_engine/worker.py','kiroclops/local_engine/reference_cache.py']
        for name in self.source_names:
            path=self.code/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixed fixture '+name)
        self.app_hashes={'src/app.js':digest('reference app')}
        self.raw={};self.rows=[];raw_rows=[]
        for domain in ('panel','svg'):
            for split in ('calibration','validation'):
                self.raw[domain,split]=[]
                for scenario in scenarios(domain,split):
                    directory=self.root/'raw'/domain/split/scenario['id'];package=directory/'package'
                    package.mkdir(parents=True)
                    frames=[]
                    for i,timestamp in enumerate((0.,.02)):
                        path=package/f'frames/{i}.png';path.parent.mkdir(exist_ok=True)
                        path.write_bytes(b'protocol-double-not-an-image-'+bytes([i]))
                        frames.append({'index':i,'path':f'frames/{i}.png','time_s':timestamp,'sha256':file_sha256(path)})
                    inputs={'provenance':'recorder_verified','events':[{'time_s':.005,'action':'click','selector':'#open-panel'}]}
                    manifest={'frames':frames,'inputs':inputs,'metadata':{'scenario':scenario}}
                    manifest['package_sha256']=digest(manifest);write_json(package/'manifest.json',manifest)
                    record={'scenario':scenario,'package':str(package),'timing':{'status':'pass'},'functional':{'status':'pass'},
                        'source_sha256':digest(self.app_hashes),'reference_package_sha256':manifest['package_sha256']}
                    self.raw[domain,split].append(record)
                    raw_rows.append({'domain':domain,'split':split,'scenario':scenario,'package_sha256':manifest['package_sha256']})
                    selection={'kind':'ObjectSelection','package_sha256':manifest['package_sha256'],'objects':[{'id':'card','roi':[0,0,2,2],'anchor':[1,1]}]}
                    observations={'package_sha256':manifest['package_sha256'],'selection':selection,'inputs':inputs,
                        'objects':[{'id':'card','frames':[{'index':i,'time_s':f['time_s'],'x':1.,'y':1.,'valid':True} for i,f in enumerate(frames)]}]}
                    events={'package_sha256':manifest['package_sha256'],'observation_sha256':digest(observations),'events':[]}
                    semantic={'operation':'analyze','package_sha256':manifest['package_sha256'],'selection':selection,
                        'contract':cache.MEASUREMENT_CONTRACT,'candidate_sha256':record['source_sha256']}
                    analysis={'kind':'AnalysisResult','schema_version':1,'status':'ok','measurement_status':'ok',
                        'package_sha256':manifest['package_sha256'],'candidate_sha256':record['source_sha256'],
                        'request_sha256':digest(semantic),'observations':observations,'events':events}
                    measured=self.root/'gate-measurements'/domain/split/scenario['id']
                    write_json(measured/'analysis.json',analysis);write_json(measured/'selection.json',selection)
                    write_json(measured/'self-comparison.json',self.comparison({**record,'observations':observations}))
                    row={'domain':domain,'split':split,'scenario_id':scenario['id'],'scenario_sha256':digest(scenario),
                        'status':'pass','camera_status':'pass','temporal_support_status':'pass','comparator_self_consistency_status':'pass','errors':[],
                        'reference_package_sha256':manifest['package_sha256'],'analysis_path':str(measured/'analysis.json'),
                        'analysis_sha256':file_sha256(measured/'analysis.json'),'observations_sha256':digest(observations),'events_sha256':digest(events),
                        'self_comparison_path':str(measured/'self-comparison.json'),'self_comparison_sha256':file_sha256(measured/'self-comparison.json'),
                        'evidence_hashes':{str(p):file_sha256(p) for p in (measured/'analysis.json',measured/'selection.json',measured/'self-comparison.json',package/'manifest.json')}}
                    self.rows.append(row)
        self.manifest=self.root/'reference-manifest.json';write_json(self.manifest,{'records':raw_rows})
        self.manifest_sha=file_sha256(self.manifest)
        for records in self.raw.values():
            for record in records:record['reference_pack_sha256']=self.manifest_sha
        for row in self.rows:row['reference_pack_sha256']=self.manifest_sha
        self.gate=self.root/'gate.json';self.proof={'kind':'ReferenceObservabilityGateV2','status':'pass','fatal_error':None,
            'count':32,'passed':32,'actual_comparator_self_consistency_passed':32,'required_count':32,
            'manifest_sha256':self.manifest_sha,'source_hashes':self.sources(),'completed_source_hashes':self.sources(),
            'source_binding_status':'pass','records':self.rows}
        self.state={'mode':'tools','domain':'web_panel','baseline':'initial-project','reference':'private-project',
            'reference_hashes':self.app_hashes,'sealed_at':None,'session_id':'fixture-session',
            'experiment':{'web_reference_bundle':str(self.manifest),'web_reference_bundle_sha256':self.manifest_sha,
                'web_reference_cache':{'path':str(self.gate),'sha256':None,'records':32,'status':'pass','manifest_sha256':self.manifest_sha}}}
        self.save_gate()
        bindings=[patch.object(cache,'ROOT',self.code),patch.object(cache,'_sources',side_effect=self.sources),
            patch('kiroclops.local_engine.web_reference.load_reference_split',side_effect=self.load_split),
            patch.object(cache,'copy_package',side_effect=self.copy_package),patch.object(cache,'load_package',side_effect=self.load_package),
            patch('kiroclops.local_engine.web_reference.check_reference_comparison_coverage',side_effect=self.comparison),
            patch('kiroclops.local_engine.evaluation._measure_record',side_effect=AssertionError('New measurement forbidden')),
            patch('kiroclops.local_engine.backend.LocalAnalysisBackend.submit',side_effect=AssertionError('Worker forbidden')),
            patch('kiroclops.web_local.capture.capture_run',side_effect=AssertionError('Browser forbidden'))]
        self.mocks=[p.start() for p in bindings]
        for p in bindings:self.addCleanup(p.stop)
        self.progress=[]

    def sources(self):return {name:file_sha256(self.code/name) for name in self.source_names}

    @staticmethod
    def comparison(record):
        return {'kind':'ReferenceComparatorSelfConsistency','status':'pass','scenario_id':record['scenario']['id'],
            'reference_package_sha256':record['observations']['package_sha256'],
            'reference_observations_sha256':digest(record['observations'])}

    def save_gate(self):
        write_json(self.gate,self.proof);self.state['experiment']['web_reference_cache']['sha256']=file_sha256(self.gate)

    @staticmethod
    def load_package(package):
        package=Path(package);manifest=read_json(package/'manifest.json')
        if manifest['package_sha256']!=digest({k:v for k,v in manifest.items() if k!='package_sha256'}):
            raise VisualError('package_integrity_failure','Fake package identity changed')
        for frame in manifest['frames']:
            path=package/frame['path']
            if not path.is_file() or file_sha256(path)!=frame['sha256']:
                raise VisualError('package_integrity_failure','Fake raw pixels changed')
        return manifest

    def copy_package(self,source,destination):
        original=self.load_package(source);shutil.copytree(source,destination)
        self.assertEqual(original,self.load_package(destination));return original

    def load_split(self,manifest,domain,split,*,expected_source_hashes,expected_scenarios):
        self.assertEqual(self.manifest,Path(manifest));self.assertEqual(self.app_hashes,expected_source_hashes)
        self.assertEqual(scenarios(domain,split),expected_scenarios)
        for record in self.raw[domain,split]:self.load_package(record['package'])
        return deepcopy(self.raw[domain,split])

    def run_matrix(self,*,state=None,split='calibration',analyze=True,name='output',progress=None,cancel=None):
        return _reference_web_matrix(state or self.state,self.root/name,'panel',split,lambda:None,
            progress or (lambda *args:self.progress.append(args)),cancel or (lambda:False),analyze=analyze)

    def first_row(self):return next(r for r in self.rows if (r['domain'],r['split'])==('panel','calibration'))

    def rebind_analysis(self,change):
        row=self.first_row();path=Path(row['analysis_path']);analysis=read_json(path);change(analysis)
        write_json(path,analysis);row['analysis_sha256']=row['evidence_hashes'][str(path)]=file_sha256(path)
        row['observations_sha256']=digest(analysis['observations']);row['events_sha256']=digest(analysis['events']);self.save_gate()

    def test_tools_copy_exact_measurements_once_per_matrix_without_worker(self):
        records=self.run_matrix();self.assertEqual(4,len(records));self.assertEqual(1,self.mocks[2].call_count)
        self.assertEqual(2,self.mocks[1].call_count) # Source snapshot before/after, no global memoization.
        self.assertEqual(4,self.mocks[5].call_count)
        for record in records:
            row=next(r for r in self.rows if r['scenario_id']==record['scenario']['id'] and r['domain']=='panel')
            self.assertEqual(file_sha256(row['analysis_path']),file_sha256(record['analysis']))
            self.assertEqual(row['observations_sha256'],digest(record['observations']))
            self.assertFalse(record['measurement_origin']['measurement_executed'])
            self.assertFalse(record['measurement_origin']['worker_started'])
            self.assertEqual(record['measurement_origin'],read_json(Path(record['analysis']).parent/'reuse-origin.json'))
        for forbidden in self.mocks[6:]:forbidden.assert_not_called()

    def test_baseline_calibration_opens_no_analysis_and_exports_only_raw_records(self):
        state={**self.state,'mode':'baseline'}
        opened=[];original_open=Path.open
        def raw_only_open(path,*args,**kwargs):
            resolved=path.resolve();opened.append(resolved)
            if resolved.is_relative_to(self.root/'gate-measurements'):
                raise AssertionError('Baseline opened analysis, selection or self-comparison evidence')
            return original_open(path,*args,**kwargs)
        with patch.object(cache,'_measurement',side_effect=AssertionError('Baseline measurement cache forbidden')), \
             patch.object(Path,'open',raw_only_open):
            records=self.run_matrix(state=state,analyze=False)
        self.assertEqual(4,len(records));self.mocks[5].assert_not_called()
        self.assertIn(self.gate,opened) # Bound whole-gate metadata is permitted.
        for record in records:
            for key in ('analysis','observations','events','selection','measurement_origin'):
                self.assertNotIn(key,record)
        self.assertFalse(any((self.root/'output').rglob('analysis.json')))
        self.assertFalse(any((self.root/'output').rglob('selection.json')))
        self.assertFalse(any((self.root/'output').rglob('self-comparison.json')))

    def test_cached_request_hash_matches_real_backend_submission_and_measure_contract(self):
        # The request digest is produced by the real durable backend, not a
        # second copy of the cache's semantic-digest calculation.
        from kiroclops.local_engine import evaluation
        record=self.raw['panel','calibration'][0];row=self.first_row()
        analysis=read_json(row['analysis_path']);selection=analysis['observations']['selection']
        with patch('kiroclops.visual.package.load_package',side_effect=lambda path,**kw:self.load_package(path)), \
             patch.object(LocalAnalysisBackend,'_spawn') as spawn:
            backend=LocalAnalysisBackend(self.root/'real-backend-store')
            submitted=_REAL_SUBMIT(backend,{'operation':'analyze','package_path':record['package'],
                'selection':selection,'contract':cache.MEASUREMENT_CONTRACT,'candidate_sha256':record['source_sha256']})
        state=read_json(backend.root/'jobs'/submitted['job_id']/'state.json')
        request=read_json(backend.root/'jobs'/submitted['job_id']/'request.json')
        self.assertEqual(analysis['request_sha256'],state['request_sha256'])
        self.assertEqual(digest(request),state['request_sha256']);self.assertIsNone(state['worker'])
        spawn.assert_called_once()
        # Bind the currently invoked measurement contract as well; if the
        # evaluation request changes, copied evidence cannot silently keep the old one.
        # The real implementation is captured independently of the safety mock.
        with patch.object(evaluation,'load_package',side_effect=self.load_package), \
             patch('kiroclops.web_local.selection.select_package_from_pixels',return_value=deepcopy(selection)), \
             patch.object(evaluation,'_analysis',return_value=analysis) as worker_request:
            _REAL_MEASURE(record,self.root/'contract-probe',lambda:None,lambda *args:None)
        self.assertEqual(cache.MEASUREMENT_CONTRACT,worker_request.call_args.kwargs['contract'])
        self.assertEqual(record['source_sha256'],worker_request.call_args.kwargs['candidate_sha256'])

    def test_baseline_analysis_request_rejected_before_any_cache_io(self):
        with patch.object(cache,'_gate',side_effect=AssertionError('No cache IO')) as gate:
            with self.assertRaises(VisualError) as caught:self.run_matrix(state={**self.state,'mode':'baseline'})
        self.assertEqual('reference_cache_mode_violation',caught.exception.code);gate.assert_not_called()

    def test_unsealed_validation_rejected_before_any_cache_io_even_raw_only(self):
        for analyze in (False,True):
            with patch.object(cache,'_gate',side_effect=AssertionError('No heldout IO')) as gate:
                with self.assertRaises(VisualError) as caught:self.run_matrix(split='validation',analyze=analyze)
            self.assertEqual('not_sealed',caught.exception.code);gate.assert_not_called()

    def test_both_arms_reuse_twelve_validation_references_only_after_seal(self):
        for mode in ('tools','baseline'):
            records=self.run_matrix(state={**self.state,'mode':mode,'sealed_at':1.},split='validation',name=mode)
            self.assertEqual(12,len(records));self.assertTrue(all('observations' in r for r in records))
        self.assertEqual([('panel','validation')]*2,[call.args[1:3] for call in self.mocks[2].call_args_list])

    def test_failed_one_of_thirty_two_cannot_be_flattened_into_cache_success(self):
        self.rows[-1].update(status='fail',errors=[{'code':'insufficient_observability'}]);self.save_gate()
        with self.assertRaises(VisualError) as caught:self.run_matrix()
        self.assertEqual('reference_not_qualified',caught.exception.code);self.mocks[2].assert_not_called()

    def test_duplicate_row_cannot_replace_missing_reference(self):
        self.proof['records'][-1]=deepcopy(self.proof['records'][0]);self.save_gate()
        with self.assertRaises(VisualError):self.run_matrix()
        self.mocks[2].assert_not_called()

    def test_stale_gate_sha_and_configured_null_never_fall_back(self):
        for binding in (None,{**self.state['experiment']['web_reference_cache'],'sha256':'0'*64}):
            state=deepcopy(self.state);state['experiment']['web_reference_cache']=binding
            with self.assertRaises(VisualError):self.run_matrix(state=state)
        self.mocks[6].assert_not_called();self.mocks[2].assert_not_called()

    def test_worker_change_before_matrix_rejects_old_cache(self):
        (self.code/self.source_names[0]).write_text('changed worker')
        with self.assertRaises(VisualError):self.run_matrix()
        self.mocks[2].assert_not_called()

    def test_source_change_during_copy_rejects_entire_matrix(self):
        def progress(*args):
            if args[1]==0:(self.code/self.source_names[0]).write_text('changed during matrix')
        with self.assertRaises(VisualError):self.run_matrix(progress=progress)

    def test_extra_producer_source_is_rechecked_after_matrix(self):
        name='extra/producer.py';path=self.code/name;path.parent.mkdir();path.write_text('extra producer')
        self.proof['source_hashes'][name]=file_sha256(path)
        self.proof['completed_source_hashes']=deepcopy(self.proof['source_hashes']);self.save_gate()
        def progress(*args):
            if args[1]==0:path.write_text('changed extra producer')
        with self.assertRaises(VisualError):self.run_matrix(progress=progress)

    def test_copied_origin_is_rechecked_after_matrix(self):
        def progress(*args):
            if args[1]==1:
                origin=next((self.root/'output').rglob('reuse-origin.json'))
                value=read_json(origin);value['worker_started']=True;write_json(origin,value)
        with self.assertRaises(VisualError):self.run_matrix(progress=progress)

    def test_selected_raw_pixel_change_rejected(self):
        package=Path(self.raw['panel','calibration'][0]['package']);(package/'frames/0.png').write_bytes(b'changed')
        with self.assertRaises(VisualError):self.run_matrix()

    def test_unselected_raw_not_loaded_in_calibration_but_rejected_when_its_split_is_opened(self):
        # Cohort's full gate validation detects this before every stage. The
        # matrix deliberately does not open unselected heldout pixels itself.
        package=Path(self.raw['panel','validation'][0]['package']);(package/'frames/0.png').write_bytes(b'changed')
        self.assertEqual(4,len(self.run_matrix()))
        with self.assertRaises(VisualError):self.run_matrix(state={**self.state,'sealed_at':1.},split='validation',name='validation')

    def test_analysis_bytes_change_rejected_without_fallback(self):
        path=Path(self.first_row()['analysis_path']);value=read_json(path);value['status']='different';write_json(path,value)
        with self.assertRaises(VisualError):self.run_matrix()
        self.mocks[6].assert_not_called()

    def test_wrong_request_contract_binding_rejected_even_after_receipt_rehash(self):
        self.rebind_analysis(lambda a:a.update(request_sha256='0'*64))
        with self.assertRaises(VisualError):self.run_matrix()

    def test_observation_actual_timeline_mismatch_rejected_even_after_receipt_rehash(self):
        def change(analysis):
            analysis['observations']['objects'][0]['frames'][1]['time_s']=.03
            analysis['events']['observation_sha256']=digest(analysis['observations'])
        self.rebind_analysis(change)
        with self.assertRaises(VisualError):self.run_matrix()

    def test_current_comparator_failure_cannot_reuse_saved_pass(self):
        self.mocks[5].side_effect=lambda record:{**self.comparison(record),'status':'fail'}
        with self.assertRaises(VisualError) as caught:self.run_matrix()
        self.assertEqual('reference_not_qualified',caught.exception.code)

    def test_cancelled_copy_rejected_before_any_reference_io(self):
        with self.assertRaises(VisualError) as caught:self.run_matrix(cancel=lambda:True)
        self.assertEqual('job_cancelled',caught.exception.code);self.mocks[2].assert_not_called()

    def test_local_copies_fit_existing_prepared_evidence_and_tampering_rejects(self):
        from kiroclops.local_engine.prepared_evidence import store_prepared,load_prepared
        session=self.root/'session';job={'job_id':'prepare-id'};state={**self.state,'prepare_job_id':job['job_id']}
        output=session/'measurements'/job['job_id'];records=self.run_matrix(name=str(output/'reference'))
        index=output/'reference.json';write_json(index,records)
        with patch('kiroclops.local_engine.prepared_evidence.load_package',side_effect=self.load_package):
            sha=store_prepared(session,job,state,{'reference':str(index)},{'reference':index})
            result={'prepared_evidence_sha256':sha}
            write_json(session/'jobs'/job['job_id']/'result.json',result)
            write_json(session/'jobs'/job['job_id']/'job.json',{'job_id':job['job_id'],'operation':'prepare','status':'succeeded','result_sha256':digest(result)})
            self.assertEqual({'reference':str(index)},load_prepared(session,state))
            changed=read_json(records[0]['analysis']);changed['request_sha256']='changed';write_json(records[0]['analysis'],changed)
            with self.assertRaises(VisualError):load_prepared(session,state)

    def test_cohort_keeps_cache_binding_private_and_identical_for_both_arms(self):
        from kiroclops.local_engine.cohort import _prepare_stage
        binding=self.state['experiment']['web_reference_cache'];captured=[]
        registry={'web_reference_bundle':{'manifest':str(self.manifest),'manifest_sha256':self.manifest_sha,'observability':binding},
                  'goals':{'panel':'fixture-goal'}}
        with patch('kiroclops.local_engine.cohort.create_session',side_effect=lambda *a,**kw:captured.append(kw)), \
             patch('kiroclops.local_engine.cohort.configure'):
            for mode in ('tools','baseline'):
                _prepare_stage(registry,{'domain':'panel','mode':mode},{'stage':'panel','session':str(self.root/mode)})
        self.assertEqual(captured[0]['experiment'],captured[1]['experiment'])
        self.assertEqual(binding,captured[0]['experiment']['web_reference_cache'])

    def test_actual_source_inventory_includes_cache(self):
        self.assertIn('kiroclops/local_engine/reference_cache.py',analyzer_sources())


if __name__=='__main__':unittest.main()
