"""No native capture/model is performed by these supplemental seam tests."""
import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase3d_v2_game_supplemental as mod
from kiroclops.visual.schema import VisualError, digest, file_sha256, write_json, read_json


class SupplementalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.session=self.root/'old-session'
        self.session.mkdir()
        self.source=self.session/'candidates/candidate-02/source'
        self.source.mkdir(parents=True)
        (self.source/'player.gd').write_text('extends CharacterBody2D\n',encoding='utf-8')
        self.hashes=mod.source_hashes(self.source)
        self.sha=digest(self.hashes)
        self.job=self.session/'jobs/final'
        self.job.mkdir(parents=True)
        self.suite=self.root/'frozen-suite'
        self.suite.mkdir()
        self.suite_value={'source_epoch':'historical-epoch'}
        write_json(self.suite/'suite.json',self.suite_value)
        write_json(self.suite/'suite-binding.json',{'suite_sha256':digest(self.suite_value)})
        self.state={'session_id':'old','final_job_id':'final','reference':str(self.suite),
            'experiment':{'suite_sha256':digest(self.suite_value)},'status':'finalization_failed'}
        write_json(self.session/'session.json',self.state)
        self.state_bytes=(self.session/'session.json').read_bytes()
        write_json(self.job/'job.json',{'source_hashes':self.hashes,'job_id':'final'})
        write_json(self.job/'binding.json',{})
        trace=self.session/'trace.jsonl'
        trace.write_text('actual unchanged native\n',encoding='utf-8')
        self.native=self.root/'native/result.json'
        write_json(self.native,{'kind':'AppendOnlyNativeReaudit','status':'pass',
            'native':{'status':'pass','model':'claude-opus-5.5','effort':'max','prompt_count':1,
                'evidence_hashes':{'trace.jsonl':file_sha256(trace)}},
            'source_files':{str(self.session/'session.json'):file_sha256(self.session/'session.json')},
            'original_failed_job_unchanged':True,'new_model_generation':False,'new_capture':False})
        self.diag=self.root/'diagnostic'
        self.diag.mkdir()
        write_json(self.diag/'diagnostic.json',{'kind':'FailedFinalNumericalDiagnostic','session_id':'old',
            'original_job_id':'final','candidate_id':'candidate-02','source_sha256':self.sha,
            'numerical_status':'pass','passed':24,'required':24,'retained_parent_behavior':True,
            'full_behavior_acceptance':False,'suite_sha256':digest(self.suite_value)})
        write_json(self.diag/'comparisons.json',[{'case_id':str(i),'pass':True} for i in range(24)])
        write_json(self.diag/'receipt.json',{'diagnostic_sha256':file_sha256(self.diag/'diagnostic.json'),
            'output_file_hashes':{n:file_sha256(self.diag/n) for n in ('diagnostic.json','comparisons.json')}})
        self.output=self.root/'supplemental'
        chosen={'candidate_id':'candidate-02','source_sha256':self.sha}
        policy={'policy_id':mod.REVISED_ID,'max_validation_seconds':3600}
        self.patches=[patch.object(mod,'_terminal',return_value=(chosen,policy)),
            patch.object(mod,'policy_from_state',return_value=policy),
            patch('kiroclops.local_engine.media_suite.load_suite',return_value=self.suite_value)]
        self.active=[p.start() for p in self.patches]
        self.addCleanup(lambda:[p.stop() for p in self.patches])
        self.addCleanup(self.temp.cleanup)

    def preflight(self):
        return mod._preflight(self.session,self.native,self.diag,self.output)

    def test_exact_source_historical_epoch_is_not_relabelled(self):
        plan=self.preflight()
        self.assertEqual(plan['source_sha256'],self.sha)
        self.assertEqual(plan['historical_source_epoch'],'historical-epoch')
        self.active[2].assert_called_once_with(self.suite,verify_analysis_code=False)
        self.assertFalse(plan['full_behavior_acceptance'])
        self.assertFalse(self.output.exists())

    def test_changed_candidate_is_rejected(self):
        (self.source/'player.gd').write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(VisualError,'source changed'):
            self.preflight()

    def test_changed_native_trace_is_rejected(self):
        (self.session/'trace.jsonl').write_text('different',encoding='utf-8')
        with self.assertRaisesRegex(VisualError,'native trace changed'):
            self.preflight()

    def test_wrong_numerical_candidate_is_rejected(self):
        value=read_json(self.diag/'diagnostic.json')
        value['candidate_id']='candidate-01'
        write_json(self.diag/'diagnostic.json',value)
        with self.assertRaisesRegex(VisualError,'exact retained'):
            self.preflight()

    def test_failed_numerical_window_cannot_hide_in_aggregate(self):
        write_json(self.diag/'comparisons.json',[{'case_id':str(i),'pass':i!=3} for i in range(24)])
        receipt=read_json(self.diag/'receipt.json')
        receipt['output_file_hashes']['comparisons.json']=file_sha256(self.diag/'comparisons.json')
        write_json(self.diag/'receipt.json',receipt)
        with self.assertRaisesRegex(VisualError,'matrix is incomplete'):
            self.preflight()

    def test_existing_output_is_not_reused(self):
        self.output.mkdir()
        with self.assertRaises(VisualError) as raised:self.preflight()
        self.assertEqual(raised.exception.code,'output_exists')

    def test_fixed_deadline_and_explicit_cancel(self):
        mod._deadline(5,self.root/'cancel.json',now=3604.999)
        with self.assertRaises(VisualError) as raised:mod._deadline(5,self.root/'cancel.json',now=3605)
        self.assertEqual(raised.exception.code,'deadline_exceeded')
        write_json(self.root/'cancel.json',{})
        with self.assertRaises(VisualError) as raised:mod._deadline(5,self.root/'cancel.json',now=5)
        self.assertEqual(raised.exception.code,'cancelled')

    def run_mocked(self, *, boundary_error=None, source_change=False, warmup_error=False, normal_status='pass'):
        code=self.root/'producer.py'
        code.write_text('frozen producer',encoding='utf-8')
        code_sources={str(code):file_sha256(code)}
        lock_calls=[]
        @contextlib.contextmanager
        def lock(path,timeout):
            lock_calls.append((Path(path),timeout))
            yield
        def target(project,scenario,out,check,progress,cancel,**kwargs):
            self.assertFalse(cancel.is_set())
            check()
            Path(out).mkdir(parents=True)
            if warmup_error:raise VisualError('capture_timing_failure','Warmup timing failed')
            return {'analysis':{},'scenario':scenario}
        def boundary(project,out,candidate,check,progress,cancel):
            self.assertEqual(candidate,'candidate-02')
            self.assertEqual(mod.source_hashes(project),self.hashes)
            self.assertFalse(cancel.is_set())
            if source_change:(Path(project)/'player.gd').write_text('tamper',encoding='utf-8')
            check()
            if boundary_error:raise boundary_error
            value={'status':'pass'}
            write_json(out,value)
            return value
        def normal(project,out,check,cancel):
            self.assertFalse(cancel.is_set())
            check()
            value={'status':normal_status,'slope_diagnostic_status':'diagnostic_fail'}
            write_json(Path(out)/'result.json',value)
            return value
        with patch.object(mod,'_code_sources',return_value=code_sources), \
             patch.object(mod,'_contain_windows_children',return_value=None), \
             patch.object(mod,'_lock',side_effect=lock), \
             patch.object(mod.suite_game,'capture_target',side_effect=target) as capture, \
             patch.object(mod.suite_game,'_report',return_value={}), \
             patch.object(mod.suite_game,'_reliable'), \
             patch.object(mod.suite_boundary,'capture',side_effect=boundary) as boundary_call, \
             patch.object(mod.suite_boundary,'bindings',return_value={('package','observation')}), \
             patch.object(mod.suite_game_demo,'capture',side_effect=normal) as normal_call, \
             patch.object(mod.suite_game_demo,'verify'):
            try:return mod.run(self.session,self.native,self.diag,self.output),lock_calls,capture,boundary_call,normal_call
            finally:self.assertEqual((self.session/'session.json').read_bytes(),self.state_bytes)

    def test_two_warmups_global_lock_and_pending_full_acceptance(self):
        result,locks,capture,boundary,normal=self.run_mocked()
        self.assertEqual(capture.call_count,2)
        self.assertEqual(len(locks),1)
        self.assertEqual(locks[0][0],mod.reliability.COMMON_CAPTURE_LOCK.resolve())
        self.assertEqual(boundary.call_count,1)
        self.assertEqual(normal.call_count,1)
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['retained_numerical_source_epoch'],'historical-epoch')
        self.assertFalse(result['full_behavior_acceptance'])
        self.assertFalse(result['new_model_generation'])
        self.assertEqual(mod.source_hashes(self.output/'source'),self.hashes)

    def test_exact_callback_failure_persists_without_replacement(self):
        with self.assertRaisesRegex(AttributeError,'is_set'):
            self.run_mocked(boundary_error=AttributeError("'function' object has no attribute 'is_set'"))
        failure=read_json(self.output/'failure.json')
        self.assertEqual(failure['status'],'failed')
        self.assertFalse((self.output/'result.json').exists())
        self.assertFalse((self.output/'normal-level').exists())

    def test_source_change_during_boundary_stops_normal(self):
        with self.assertRaises(VisualError) as raised:self.run_mocked(source_change=True)
        self.assertEqual(raised.exception.code,'source_changed')
        self.assertFalse((self.output/'normal-level').exists())

    def test_failed_warmups_do_not_begin_active_boundary(self):
        with self.assertRaises(VisualError) as raised:self.run_mocked(warmup_error=True)
        self.assertEqual(raised.exception.code,'environment_not_ready')
        self.assertEqual(len(read_json(self.output/'warmups/result.json')['attempts']),6)
        self.assertFalse((self.output/'visual-boundary.json').exists())

    def test_normal_route_failure_is_not_full_success(self):
        result,*_=self.run_mocked(normal_status='fail')
        self.assertEqual(result['status'],'fail')
        self.assertFalse(result['full_behavior_acceptance'])


if __name__=='__main__':unittest.main()
