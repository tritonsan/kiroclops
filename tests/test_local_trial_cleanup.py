"""Terminal-turn barriers with real durable records and no engine/model process."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiroclops.local_engine import launcher, cohort, trial_cleanup as cleanup
from kiroclops.local_engine.backend import _atomic, _read, _lock
from kiroclops.local_engine.session import Session, create_session, run_session_job
from kiroclops.visual.schema import VisualError, file_sha256, write_json, read_json


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='terminal café ');self.root=Path(self.temp.name)
        baseline=self.root/'original';(baseline/'src').mkdir(parents=True)
        (baseline/'src/panel-motion.js').write_text('export const speed=1')
        reference=self.root/'reference';reference.mkdir();(reference/'data').write_text('pixels')
        self.directory=self.root/'session'
        create_session(self.directory,'web_panel',baseline,reference,'Match motion',
            experiment={'capture_lock':str(self.root/'capture.lock')})
        self.session=Session(self.directory)
        self.worker={'pid':987654,'identity':'owned-creation'}

    def tearDown(self):self.temp.cleanup()

    def submit(self,operation,result=None):
        with patch.object(Session,'_spawn') as spawn:
            value=self.session.submit(operation)
        if result is not None:
            run_session_job(self.directory,value['job_id'],runner=lambda *args:result)
            self.session.get_job(value['job_id'])
        return value['job_id']

    def pending(self,operation='prepare'):
        if operation in ('evaluate','finalize'):
            self.submit('prepare',{'ready':True})
        if operation=='finalize':self.submit('evaluate',{'eligible':True,'score':.1})
        identifier=self.submit(operation)
        path=self.directory/'jobs'/identifier/'job.json'
        job=_read(path);job.update(status='running',worker=dict(self.worker));_atomic(path,job)
        return identifier

    def test_queued_cancel_never_respawns_and_clock_never_resets(self):
        identifier=self.submit('prepare');before=self.session._load()
        with patch.object(Session,'_spawn') as spawn:
            request=cleanup.begin_terminal_cleanup(self.session)
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.1)
            self.assertEqual('cancelled',self.session.get_job(identifier)['status'])
        spawn.assert_not_called();self.assertEqual('pass',result['status'])
        self.assertIsNone(self.session._load()['active_job_id'])
        self.assertEqual(before['deadline_at'],self.session._load()['deadline_at'])

    def test_pending_operations_cancel_only_the_exact_owned_worker(self):
        identifier=self.pending();request=cleanup.begin_terminal_cleanup(self.session)
        alive=[True]
        def terminate(worker):
            self.assertEqual(self.worker,worker);alive[0]=False;return True
        with patch.object(cleanup,'_alive',side_effect=lambda w:bool(w) and alive[0]),patch.object(cleanup,'_terminate_owned',side_effect=terminate) as killed:
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.08)
        self.assertEqual('pass',result['status']);killed.assert_called_once_with(self.worker)
        self.assertEqual('cancelled',self.session.get_job(identifier)['status'])

    def test_pid_reuse_is_not_killed(self):
        self.pending();request=cleanup.begin_terminal_cleanup(self.session)
        with patch.object(cleanup,'_alive',return_value=False),patch.object(cleanup,'_terminate_owned') as killed:
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.05)
        self.assertEqual('pass',result['status']);killed.assert_not_called()

    def test_still_live_worker_blocks_even_if_record_is_terminal(self):
        identifier=self.pending();path=self.directory/'jobs'/identifier/'job.json'
        job=_read(path);job.update(status='failed',finished_at=1);_atomic(path,job)
        original=path.read_bytes();request=cleanup.begin_terminal_cleanup(self.session)
        with patch.object(cleanup,'_alive',side_effect=lambda w:bool(w)),patch.object(cleanup,'_terminate_owned',return_value=True):
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.04)
        self.assertEqual('unconfirmed',result['status']);self.assertFalse(result['worker_exit_verified'])
        self.assertEqual(original,path.read_bytes())

    def test_finished_result_and_job_bytes_are_preserved(self):
        identifier=self.submit('prepare')
        run_session_job(self.directory,identifier,runner=lambda *args:{'ready':True,'evidence':'bound'})
        path=self.directory/'jobs'/identifier;before={n:(path/n).read_bytes() for n in ('job.json','result.json')}
        request=cleanup.begin_terminal_cleanup(self.session)
        result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.1)
        self.assertEqual('pass',result['status']);self.assertFalse(request['had_pending_job'])
        self.assertFalse((path/'cancel.json').exists())
        self.assertEqual(before,{n:(path/n).read_bytes() for n in before})
        self.assertEqual('succeeded',self.session.get_job(identifier)['status'])

    def test_live_client_and_capture_lease_each_block_success(self):
        request=cleanup.begin_terminal_cleanup(self.session)
        request['client_process']=self.worker
        with patch.object(cleanup,'_alive',side_effect=lambda w:bool(w)),patch.object(cleanup,'_terminate_owned') as kill:
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.03)
        self.assertEqual('unconfirmed',result['status']);kill.assert_not_called()
        request.pop('client_process')
        @contextmanager
        def busy(path,timeout=10):
            if Path(path)==self.root/'capture.lock':raise TimeoutError('held')
            with _lock(path,timeout=timeout):yield
        with patch.object(cleanup,'_lock',busy):
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.03)
        self.assertEqual('unconfirmed',result['status']);self.assertTrue(result['worker_exit_verified'])
        self.assertFalse(result['capture_lease_released'])

    def test_worker_identity_change_is_rejected_without_kill(self):
        identifier=self.pending();request=cleanup.begin_terminal_cleanup(self.session)
        path=self.directory/'jobs'/identifier/'job.json';job=_read(path)
        job['worker']['identity']='different';_atomic(path,job)
        with patch.object(cleanup,'_terminate_owned') as killed:
            result=cleanup.finish_terminal_cleanup(self.session,request,timeout=.05)
        self.assertEqual('cleanup_binding_changed',result['error']['code']);killed.assert_not_called()

    def native_client(self,operation,raises=None,expect_final_result=False):
        client=SimpleNamespace(guard=SimpleNamespace(generation_started=True,summary=lambda:{}),
            kas_version='test',prompt_sent=True,process=SimpleNamespace(poll=lambda:0))
        client.handshake=lambda:{'session_id':'native'}
        identifiers=[]
        def prompt(text):
            identifiers.append(self.pending(operation))
            if raises:raise raises
            return {'stopReason':'end_turn'}
        def close():
            marker=self.directory/'jobs'/identifiers[0]/'cancel.json'
            if not expect_final_result:
                self.assertTrue(marker.is_file())
            else:
                self.assertFalse(marker.exists())
                self.assertTrue((marker.parent/'result.json').is_file(),
                                'Closing the Windows Job before the final worker commits kills it')
            return []
        client.prompt=prompt;client.close=Mock(side_effect=close)
        return client

    def run_native(self,client):
        prompt=self.root/'prompt.txt';prompt.write_text('one task')
        agent=self.root/'agent.json';write_json(agent,{'mcpServers':{'kiroclops':{'fixture':True}}})
        ready={'config':{'mode':'tools','prompt_path':str(prompt),'agent_path':str(agent),
                         'target_dir':str(self.directory/'target'),'launcher_sha256':file_sha256(launcher.__file__)},'cli_version':'fake'}
        # This fixture covers durable job handoff/cleanup, not native startup.
        # Supply the same immutable startup identity shape as the real client;
        # no subprocess, network or model is started by a cleanup test.
        client.process=SimpleNamespace(pid=987655,poll=lambda:0)
        identity={'pid':987655,'identity':'fixture-native-creation'}
        client_receipt=self.root/'client-startup.json'
        write_json(client_receipt,{'kind':'OwnedNativeClientStartupIdentity','status':'bound','native_client_process':identity})
        client.owned_process_receipt={'path':str(client_receipt),'sha256':file_sha256(client_receipt)}
        with patch.object(launcher,'preflight',return_value=ready), \
             patch('kiroclops.local_engine.startup.connect_native',return_value=(client,{'session_id':'native'},[{'status':'pass'}])), \
             patch('kiroclops.local_engine.startup.probe_stdio',return_value={'status':'pass'}), \
             patch.object(launcher,'_verify_saved_client_process',side_effect=lambda *args,**kwargs:dict(identity)), \
             patch.object(cleanup,'_alive',return_value=False):
            return launcher._run_native(self.directory,{'run_kind':'test'})

    def test_end_turn_waits_for_exact_finalize_before_client_close(self):
        client=self.native_client('finalize',expect_final_result=True)
        def finish_before_close(session,request):
            client.close.assert_not_called()
            run_session_job(self.directory,request['job_id'],runner=lambda *args:{'pass':True})
            terminal=session.get_job(request['job_id'],allow_final_result=True)
            return {'status':'terminal','job_id':request['job_id'],'job_status':terminal['status'],
                    'candidate_id':terminal['candidate_id'],'source_sha256':terminal['source_sha256'],
                    'finished_at':terminal['finished_at'],
                    'validation_deadline_at':request['validation_deadline_at']}
        with patch.object(launcher,'_await_preserved_finalization',side_effect=finish_before_close):
            result=self.run_native(client)
        self.assertEqual('completed',result['status']);self.assertEqual('completed',result['native_turn_status'])
        self.assertEqual('pass',result['terminal_cleanup']['status']);client.close.assert_called_once()
        self.assertEqual('terminal',result['postturn_finalization']['status'])
        self.assertEqual('succeeded',result['postturn_finalization']['job_status'])
        self.assertTrue(result['postturn_finalization']['worker_exit_verified_before_client_close'])
        self.assertTrue(result['postturn_finalization']['capture_lease_released_before_client_close'])
        self.assertEqual('succeeded',self.session.inspect()['final']['status'])
        self.assertFalse((self.directory/'jobs'/result['postturn_finalization']['job_id']/'cancel.json').exists())

    def test_bounded_finalize_wait_cancels_only_bound_job_on_timeout(self):
        client=self.native_client('finalize',expect_final_result=True)
        # Simulate a worker that remains pending after ACP teardown.
        client.close=Mock(return_value=[])
        with patch.object(launcher,'_await_preserved_finalization',side_effect=VisualError('finalization_wait_timeout','deadline')):
            result=self.run_native(client)
        self.assertEqual('finalization_wait_failed',result['status'])
        self.assertEqual('finalization_wait_timeout',result['postturn_finalization']['code'])
        self.assertEqual('pass',result['terminal_cleanup']['status'])
        self.assertEqual('cancelled',self.session.inspect()['final']['status'])
        with patch('kiroclops.local_engine.reporting.build_report') as report:
            result=launcher._with_postturn_report(self.directory,result)
        self.assertEqual({'status':'not_run','reason':'native_turn_incomplete'},result['postturn_report'])
        report.assert_not_called()

    def test_invalid_preserve_binding_falls_back_to_cancellation_and_fails_trial(self):
        client=self.native_client('finalize')
        original_prompt=client.prompt
        def corrupt_selection(text):
            response=original_prompt(text)
            state=_read(self.directory/'session.json')
            state['selected_candidate_id']='candidate-99'
            _atomic(self.directory/'session.json',state)
            return response
        client.prompt=corrupt_selection
        result=self.run_native(client)
        client.close.assert_called_once()
        self.assertEqual('terminal_cleanup_unconfirmed',result['status'])
        self.assertEqual('cleanup_binding_changed',result['terminal_cleanup']['error']['code'])
        final_job=self.session._load()['final_job_id']
        self.assertTrue((self.directory/'jobs'/final_job/'cancel.json').is_file())
        self.assertEqual('cancelled',self.session.get_job(final_job)['status'])
        with patch('kiroclops.local_engine.reporting.build_report') as report:
            wrapped=launcher._with_postturn_report(self.directory,result)
        self.assertEqual('not_run',wrapped['postturn_report']['status'])
        report.assert_not_called()

    def test_native_error_cancels_pending_evaluate_and_preserves_error(self):
        client=self.native_client('evaluate',VisualError('tool_denied','test denial'));result=self.run_native(client)
        self.assertEqual('tool_denied',result['status']);self.assertEqual('pass',result['terminal_cleanup']['status'])
        self.assertEqual('cancelled',self.session.inspect()['candidates'][0]['status'])

    def test_marker_failure_still_closes_client_and_blocks(self):
        client=SimpleNamespace(handshake=lambda:{},prompt=lambda text:{'stopReason':'end_turn'},
            guard=SimpleNamespace(generation_started=True,summary=lambda:{}),prompt_sent=True,kas_version='test',close=Mock(return_value=[]))
        with patch.object(cleanup,'begin_terminal_cleanup',side_effect=PermissionError('write denied')):
            result=self.run_native(client)
        client.close.assert_called_once();self.assertEqual('terminal_cleanup_unconfirmed',result['status'])


class CohortBarrierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.registration=self.root/'registration.json';write_json(self.registration,{'registered':True})
        self.rows=[{'id':f'trial-{i}','domain':'panel','mode':'tools','repeat':i,'status':'registered',
                    'stages':[{'stage':'panel','session':str(self.root/f'session-{i}'),'status':'registered'}]} for i in (1,2)]
        self.registry={'rows':self.rows,'sessions':[r['stages'][0]['session'] for r in self.rows]}
        write_json(self.root/'progress.json',{'registration_sha256':file_sha256(self.registration),'rows':self.rows})

    def tearDown(self):self.temp.cleanup()

    def test_cleanup_failure_blocks_next_trial_and_keeps_denominator(self):
        calls=[]
        def launch(directory,*args):
            calls.append(directory);Path(directory).mkdir()
            result={'status':'terminal_cleanup_unconfirmed','terminal_cleanup':{'status':'unconfirmed','request':{'job_id':'old'}}}
            write_json(Path(directory)/'kiro/launch.json',result);return result
        with patch.object(cohort,'verify_registration',return_value=self.registry),patch.object(cohort,'_prepare_stage'),patch.object(cohort,'run',side_effect=launch),patch.object(cohort,'summarize') as summary:
            with self.assertRaises(VisualError) as error:cohort.run_cohort(self.registration)
        self.assertEqual('terminal_cleanup_unconfirmed',error.exception.code);self.assertEqual(1,len(calls));summary.assert_not_called()
        saved=read_json(self.root/'progress.json');self.assertEqual(2,len(saved['rows']))
        self.assertEqual('failed',saved['rows'][0]['status']);self.assertEqual('registered',saved['rows'][1]['status'])
        self.assertIn('continuation_blocked',saved)
        with patch.object(cohort,'verify_registration',return_value=self.registry),patch.object(cohort,'run') as run:
            with self.assertRaises(VisualError):cohort.run_cohort(self.registration)
        run.assert_not_called()

    def test_explicit_resume_rechecks_only_old_cleanup_then_skips_failed_trial(self):
        saved=read_json(self.root/'progress.json');saved['rows'][0]['status']='failed'
        directory=saved['rows'][0]['stages'][0]['session'];path=Path(directory)/'kiro/launch.json'
        request={'job_id':'old','worker':{'pid':123,'identity':'original'}}
        write_json(path,{'terminal_cleanup':{'status':'unconfirmed','request':request}})
        saved['continuation_blocked']={'session':directory,'launch_sha256':file_sha256(path)};write_json(self.root/'progress.json',saved)
        with patch.object(cleanup,'finish_terminal_cleanup',return_value={'status':'pass'}) as finish,patch.object(cohort,'Session'):
            cohort._resume_cleanup(self.registry,saved,self.root/'progress.json',True)
        self.assertEqual(request,finish.call_args.args[1]);self.assertFalse(finish.call_args.kwargs['terminate'])
        self.assertNotIn('continuation_blocked',saved);self.assertEqual('failed',saved['rows'][0]['status'])
        self.assertEqual('registered',saved['rows'][1]['status']);self.assertEqual(1,len(saved['cleanup_rechecks']))

    def test_resume_rejects_changed_original_record(self):
        saved=read_json(self.root/'progress.json');directory=saved['rows'][0]['stages'][0]['session']
        path=Path(directory)/'kiro/launch.json';write_json(path,{'terminal_cleanup':{'request':{}}})
        saved['continuation_blocked']={'session':directory,'launch_sha256':'wrong'}
        with patch.object(cleanup,'finish_terminal_cleanup') as finish:
            with self.assertRaises(VisualError):cohort._resume_cleanup(self.registry,saved,self.root/'progress.json',True)
        finish.assert_not_called()


if __name__=='__main__':unittest.main()
