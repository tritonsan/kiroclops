"""Cost/provenance presentation only; no pixel analysis, recorder or worker."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import report_costs as costs, reporting
from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json


class ReportCostTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='report costs naïve ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.manifest=self.root/'raw/manifest.json';write_json(self.manifest,{'kind':'FrozenWebReferencePack'})
        self.manifest_sha=file_sha256(self.manifest)
        self.gate=self.root/'gate/result.json';write_json(self.gate,{
            'kind':'ReferenceObservabilityGateV2','manifest_sha256':self.manifest_sha,
            'status':'pass','count':32,'started_at':100.,'finished_at':142.5,'elapsed_s':42.50001,
            'records':[{'analysis_path':str(self.root/'never-open/analysis.json')}]*32})
        self.binding={'path':str(self.gate),'sha256':file_sha256(self.gate),'manifest_sha256':self.manifest_sha}

    def shared(self):return costs.shared_reference_costs(self.manifest,self.manifest_sha,self.binding)

    def test_bound_original_cost_is_separate_and_missing_capture_cost_is_not_zero(self):
        value=self.shared()
        self.assertEqual(42.5,value['reference_analysis_and_self_check_wall_seconds'])
        for name in ('reference_capture_wall_seconds','qualification_24_wall_seconds',
                     'total_first_time_engineering_wall_seconds','first_time_user_video_latency_seconds'):
            self.assertIsNone(value[name])
        self.assertEqual(self.binding['sha256'],value['reference_gate_sha256'])
        self.assertNotIn(str(self.root),str(value))

    def test_read_only_metadata_never_opens_private_measured_records(self):
        opened=[];original=Path.open
        def checked(path,*args,**kwargs):
            self.assertIn(path.resolve(),(self.manifest,self.gate));opened.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',checked):self.shared()
        self.assertTrue(opened)

    def test_changed_gate_or_foreign_raw_cost_is_rejected(self):
        write_json(self.gate,{'manifest_sha256':'other','started_at':0,'finished_at':1})
        with self.assertRaises(VisualError):self.shared()
        self.binding['sha256']=file_sha256(self.gate)
        with self.assertRaises(VisualError):self.shared()

    def test_missing_or_invalid_duration_is_unknown_not_zero(self):
        for record in ({},{'started_at':True,'finished_at':3},
                       {'started_at':10,'finished_at':9},{'started_at':0,'finished_at':float('nan')}):
            self.assertIsNone(costs._duration(record))
        self.assertEqual(0,costs._duration({'started_at':10,'finished_at':10}))

    def test_cohort_counts_precompute_once_without_model_only_claim(self):
        registry={'web_reference_bundle':{'manifest':str(self.manifest),'manifest_sha256':self.manifest_sha,
                    'observability':self.binding},'qualification_sha256':'qualified-hash',
                  'budget':{'max_seconds':1800,'max_candidates':3,'per':'stage'},'rows':[{}]*18}
        value=costs.cohort_costs(registry)
        self.assertEqual(42.5,value['shared_prerequisites']['reference_analysis_and_self_check_wall_seconds'])
        self.assertTrue(value['shared_prerequisites_counted_once'])
        self.assertFalse(value['group_elapsed_is_model_only']);self.assertFalse(value['first_time_video_latency_claimed'])
        self.assertEqual(registry['budget'],value['budget'])

    def test_session_prepare_scope_is_wall_time_and_has_no_first_video_claim(self):
        write_json(self.root/'jobs/p/job.json',{'job_id':'p','operation':'prepare','started_at':150.,'finished_at':155.})
        state={'prepare_job_id':'p','budget':{'max_seconds':1800},'experiment':{
            'web_reference_bundle':str(self.manifest),'web_reference_bundle_sha256':self.manifest_sha,
            'web_reference_cache':self.binding}}
        value=costs.session_costs(self.root,state)
        self.assertEqual(5,value['prepare_job_wall_seconds']);self.assertEqual(1800,value['model_stage_budget_seconds'])
        self.assertEqual('preregistered_reuse',value['reference_measurement_policy'])
        self.assertFalse(value['unknown_is_zero'])

    def test_media_retains_only_portable_bound_reuse_provenance(self):
        record={'package':'synthetic','measurement_origin':{'kind':'PreregisteredReferenceMeasurementReuse',
            'gate_sha256':'g'*64,'worker_started':False,'measurement_executed':False,
            'original_analysis_sha256':'a'*64,'analysis_path':str(self.root/'private/analysis.json'),
            'private_heldout_results':{'score':100}}}
        manifest={'package_sha256':'p','frames':[{'path':'frames/0.png','time_s':0}],
                  'image_size':[10,10],'inputs':{'events':[]}}
        with patch.object(reporting,'copy_package',return_value=manifest), \
             patch.object(reporting,'_observations',return_value=None),patch.object(reporting,'_events',return_value=None):
            media=reporting._media(record,self.root,'media/reference',False)
        origin=media['measurement_origin'];self.assertFalse(origin['worker_started'])
        self.assertEqual('a'*64,origin['original_analysis_sha256'])
        self.assertNotIn('analysis_path',origin);self.assertNotIn('private_heldout_results',origin)
        self.assertNotIn(str(self.root),str(media))

    def test_absent_reuse_receipt_never_claims_fresh_worker(self):
        value=costs.media_measurement_origin({})
        self.assertEqual('not_recorded',value['status']);self.assertNotIn('worker_started',value)

    def test_report_has_explicit_conditional_operator_control_and_cost_sections(self):
        self.assertIn('id="engineering-control" hidden',reporting.HTML)
        self.assertIn('if(D.engineering_control)',reporting.HTML)
        self.assertIn("c.model_started===false&&c.kiro_success_claim===false",reporting.HTML)
        self.assertIn('not autonomous Kiro success evidence',reporting.HTML)
        self.assertIn('Cost and measurement provenance',reporting.HTML)
        self.assertIn('m.measurement_origin.worker_started===false',reporting.HTML)

    def test_report_build_keeps_unknown_cost_and_does_not_invent_operator_authorship(self):
        state={'domain':'web_panel','mode':'tools','goal':'fixture','status':'new','budget':{'max_seconds':1800},
               'experiment':{},'candidates':[]}
        with patch.object(reporting.Session,'_load',return_value=state), \
             patch.object(reporting,'measurement_groups',return_value=[]):
            result=reporting.build_report(self.root,encode=False)
        data=read_json(result['data'])
        self.assertNotIn('engineering_control',data)
        self.assertIsNone(data['cost_scopes']['prepare_job_wall_seconds'])
        self.assertIsNone(data['cost_scopes']['shared_prerequisites']['first_time_user_video_latency_seconds'])


if __name__=='__main__':unittest.main()
