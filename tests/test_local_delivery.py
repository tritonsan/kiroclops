from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.session import Session, create_session, run_session_job, source_hashes
from kiroclops.local_engine.reporting import build_report, report_tree_receipt
from kiroclops.local_engine.review import prepare_review, evaluate_review
from kiroclops.local_engine.launcher import configure, guard, load_config
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='delivery café ');self.root=Path(self.temp.name)
        baseline=self.root/'original';(baseline/'src').mkdir(parents=True)
        (baseline/'src/panel-motion.js').write_text('export const speed=1;')
        (baseline/'index.html').write_text('<h1>protected</h1>')
        ref=self.root/'reference';ref.mkdir();(ref/'private.js').write_text('secret reference')
        self.session=self.root/'session'
        create_session(self.session,'web_panel',baseline,ref,'Match </script><script>unsafe</script>',experiment={'capture_lock':str(self.root/'capture.lock')})
        paths=[]
        for i in range(24):
            p=self.root/f'{i}.png';cv2.imencode('.png',np.full((32,32,3),i,np.uint8))[1].tofile(p);paths.append(p)
        self.package=self.root/'package';manifest=create_package(self.package,paths,[i*.02 for i in range(24)])
        self.observations={'kind':'ObservationSet','package_sha256':manifest['package_sha256'],'objects':[
            {'id':'box','valid_ratio':1.,'frames':[{'index':i,'time_s':i*.02,'x':10.,'y':12.,'valid':True,'width':3,'height':4} for i in range(24)]}]}

    def tearDown(self):self.temp.cleanup()

    def complete_job(self,operation,result):
        s=Session(self.session)
        with patch.object(Session,'_spawn'):job=s.submit(operation)
        run_session_job(self.session,job['job_id'],runner=lambda *a:result)
        s.get_job(job['job_id'])
        return job

    def test_portable_report_uses_original_times_and_escapes_script(self):
        output=self.session/'measurements/test';output.mkdir(parents=True)
        write_json(output/'reference.json',[{'scenario':{'id':'open'},'package':str(self.package),'observations':self.observations}])
        report=build_report(self.session,encode=False)
        root=Path(report['report']).parent
        data=read_json(root/'report.json');self.assertEqual('pending',data['overall_acceptance'])
        self.assertNotIn(str(self.root),json.dumps(data))
        self.assertEqual(.46,data['groups'][0]['roles']['reference']['frames'][-1]['time_s'])
        html=(root/'index.html').read_text('utf-8')
        self.assertIn('<\\/script>',html);self.assertNotIn('Match </script><script>',html)
        destination=self.root/'moved report';shutil.copytree(root,destination)
        media=data['groups'][0]['roles']['reference']['package']
        self.assertEqual(load_package(self.package)['package_sha256'],load_package((destination/media).parent)['package_sha256'])

    def test_report_opens_on_complete_comparison_before_candidate_only_group(self):
        record={'scenario':{'id':'open'},'package':str(self.package),'observations':self.observations}
        candidate=self.session/'measurements/a-candidate';candidate.mkdir(parents=True)
        write_json(candidate/'candidate.json',[record])
        complete=self.session/'measurements/z-final';complete.mkdir(parents=True)
        for role in ('reference','before','final'):
            write_json(complete/(role+'.json'),[record])
        report=build_report(self.session,encode=False)
        groups=read_json(Path(report['data']))['groups']
        self.assertEqual({'reference','before','final'},set(groups[0]['roles']))
        self.assertEqual({'candidate'},set(groups[1]['roles']))

    def test_report_never_uses_later_workspace_edit_as_selected_code(self):
        self.complete_job('prepare',{})
        target=self.session/'target/src/panel-motion.js';target.write_text('export const speed=2;')
        self.complete_job('evaluate',{'eligible':True,'score':.01})
        self.complete_job('finalize',{'pass':True})
        target.write_text('export const speed=999;')
        report=build_report(self.session,encode=False);diff=(Path(report['report']).parent/'candidate.diff').read_text()
        self.assertIn('speed=2',diff);self.assertNotIn('999',diff)
        self.assertEqual('pending',report['overall_acceptance'])

    def test_report_keeps_pixel_events_geometry_and_missing_measurements(self):
        output=self.session/'measurements/test';output.mkdir(parents=True)
        self.observations['objects'][0]['frames'][2].update(valid=False,reason='occlusion')
        self.observations['objects'][0]['frames'][3].update(left=8,top=10)
        events={'kind':'EventSet','events':[{'type':'motion_start','object_ids':['box'],'time_interval_s':[.02,.04]}],
                'relations':[{'type':'boundary_distance','object_id':'box','time_s':.04,'distance_px':8}],
                'unobservable':[{'reason':'occlusion'}]}
        analysis=output/'analysis.json';write_json(analysis,{'observations':self.observations,'events':events})
        write_json(output/'reference.json',[{'scenario':{'id':'open'},'package':str(self.package),'analysis':str(analysis)}])
        report=build_report(self.session,encode=False);root=Path(report['report']).parent
        data=read_json(root/'report.json')['groups'][0]['roles']['reference']
        self.assertEqual(events['events'],data['events']);self.assertEqual(events['relations'],data['relations'])
        self.assertFalse(data['objects'][0]['frames'][2]['valid'])
        self.assertEqual(8,data['objects'][0]['frames'][3]['left'])
        self.assertEqual(events,read_json(root/data['event_file']))

    def test_changed_selected_source_rejects_report(self):
        self.complete_job('prepare',{});self.complete_job('evaluate',{'eligible':True,'score':.01});self.complete_job('finalize',{'pass':True})
        (self.session/'candidates/candidate-01/source/src/panel-motion.js').write_text('changed')
        with self.assertRaises(VisualError) as c:build_report(self.session,encode=False)
        self.assertEqual('source_changed',c.exception.code)

    def test_single_clip_partial_is_not_reported_as_full_pass(self):
        self.complete_job('prepare',{});self.complete_job('evaluate',{'eligible':True,'score':.01})
        self.complete_job('finalize',{'pass':False,'calibration_pass':True,'acceptance_status':'partial_unobserved_holdout'})
        report=build_report(self.session,encode=False)
        self.assertEqual('partial',report['overall_acceptance'])
        data=read_json(Path(report['data']));self.assertFalse(data['numerical']['pass'])

    def prepare_marks(self):
        output=self.root/'review';prepare_review(self.package,self.observations,output,tolerance_px=1.5)
        request=read_json(output/'request.json')
        marks={'reviewer':'unit fixture','method':'independent_raw_image_marking','package_sha256':request['package_sha256'],'request_sha256':digest(request),
               'points':[{k:t[k] for k in ('record_id','object_id','index','image_sha256')}|{'x':10.,'y':12.} for t in request['tasks']]}
        return output,marks

    def test_review_does_not_expose_prediction_and_requires_twenty_points(self):
        output,marks=self.prepare_marks();request=read_json(output/'request.json')
        self.assertEqual(20,len(request['tasks']));self.assertNotIn('x',request['tasks'][0])
        result=evaluate_review(output,marks);self.assertEqual('pass',result['status']);self.assertEqual('pending',result['event_review_status'])
        marks['points'].pop();self.assertEqual('fail',evaluate_review(output,marks)['status'])

    def test_review_rejects_duplicate_and_wrong_frame_hash(self):
        output,marks=self.prepare_marks();marks['points'].append(marks['points'][0])
        with self.assertRaises(VisualError):evaluate_review(output,marks)
        marks['points'].pop();marks['points'][0]['image_sha256']='bad'
        with self.assertRaises(VisualError) as c:evaluate_review(output,marks)
        self.assertEqual('stale_review',c.exception.code)

    def test_review_large_error_fails_and_no_interpolation_of_invalid(self):
        output,marks=self.prepare_marks()
        for p in marks['points']:p['x']=14
        result=evaluate_review(output,marks);self.assertEqual('fail',result['status']);self.assertEqual(4.,result['objects'][0]['median_error_px'])

    def test_launcher_symmetric_standard_tools_and_guarded_editable_scope(self):
        config=configure(self.session);target=Path(config['target_dir'])
        agent=read_json(config['agent_path']);self.assertTrue({'read','write','shell'}<=set(agent['tools']))
        self.assertEqual('claude-opus-5.5',agent['model']);self.assertEqual('max',agent['effortLevel'])
        self.assertTrue(guard(self.session,{'tool_name':'fs_read','tool_input':{'path':str(target/'index.html')}})['allow'])
        self.assertTrue(guard(self.session,{'tool_name':'fs_write','tool_input':{'path':str(target/'src/panel-motion.js')}})['allow'])
        self.assertFalse(guard(self.session,{'tool_name':'fs_write','tool_input':{'path':str(target/'index.html')}})['allow'])
        self.assertFalse(guard(self.session,{'tool_name':'fs_read','tool_input':{'path':str(self.root/'reference/private.js')}})['allow'])
        self.assertTrue(guard(self.session,{'tool_name':'shell','tool_input':{'command':'python .kiroclops-scratch/inspect.py'}})['allow'])
        self.assertFalse(guard(self.session,{'tool_name':'shell','tool_input':{'command':'cat ../reference/private.js'}})['allow'])

    def test_launcher_control_change_rejected(self):
        config=configure(self.session);Path(config['prompt_path']).write_text('changed')
        with self.assertRaises(VisualError) as c:load_config(self.session)
        self.assertEqual('config_changed',c.exception.code)

    def test_baseline_has_no_mcp_but_keeps_neutral_commands_and_shell(self):
        state=read_json(self.session/'session.json');other=self.root/'baseline session'
        create_session(other,'web_panel',state['baseline'],state['reference'],'same goal',mode='baseline')
        config=configure(other);agent=read_json(config['agent_path'])
        self.assertEqual(['read','write','shell'],agent['tools']);self.assertEqual({},agent['mcpServers'])
        self.assertTrue(guard(other,{'tool_name':'shell','tool_input':{'command':config['neutral_commands']['evaluate']}})['allow'])
        self.assertFalse(guard(other,{'tool_name':'kiroclops::evaluate_candidate','tool_input':{}})['allow'])

    def test_neutral_jobs_are_async_and_only_accept_bound_polling_arguments(self):
        config=configure(self.session);commands=config['neutral_commands'];job_id='a'*32
        allowed=lambda command:guard(self.session,{'tool_name':'shell','tool_input':{'command':command}})['allow']
        for operation in ('prepare','evaluate','finalize','report'):
            self.assertTrue(commands[operation].endswith('--async'))
            self.assertTrue(allowed(commands[operation]))
        self.assertTrue(allowed(commands['inspect']))
        self.assertTrue(allowed(commands['finalize']+' --candidate candidate-03'))
        self.assertTrue(allowed(commands['job']+' --job-id '+job_id+' --wait-seconds 60'))
        self.assertTrue(allowed(commands['cancel']+' --job-id '+job_id))
        for command in (commands['job'],commands['job']+' --job-id ../other',
                         commands['job']+' --job-id '+job_id+' --wait-seconds 61',
                         commands['job']+' --job-id '+job_id+'; echo injection',
                         commands['cancel']+' --job-id '+job_id+' --wait-seconds 1',
                         commands['inspect']+' --session elsewhere',
                         commands['finalize']+' --candidate candidate-04'):
            self.assertFalse(allowed(command),command)


class ReportTreeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'report';self.root.mkdir()
        (self.root/'report.json').write_text('{}',encoding='utf-8')
        (self.root/'index.html').write_text('<html></html>',encoding='utf-8')
        (self.root/'candidate.diff').write_text('original diff',encoding='utf-8')
        media=self.root/'media'/'test';media.mkdir(parents=True)
        (media/'frame-000.png').write_bytes(b'original frame')

    def tearDown(self):self.temp.cleanup()

    def test_receipt_changes_for_diff_and_nested_media_bytes(self):
        original=report_tree_receipt(self.root)
        self.assertEqual(original,report_tree_receipt(self.root))
        self.assertEqual(4,original['file_count'])
        diff=self.root/'candidate.diff';diff.write_text('changed diff',encoding='utf-8')
        self.assertNotEqual(original['sha256'],report_tree_receipt(self.root)['sha256'])
        diff.write_text('original diff',encoding='utf-8')
        frame=self.root/'media/test/frame-000.png';frame.write_bytes(b'changed frame')
        self.assertNotEqual(original['sha256'],report_tree_receipt(self.root)['sha256'])

    def test_receipt_binds_relative_names_and_rejects_symlinks(self):
        original=report_tree_receipt(self.root)
        (self.root/'candidate.diff').rename(self.root/'renamed.diff')
        self.assertNotEqual(original['sha256'],report_tree_receipt(self.root)['sha256'])
        link=self.root/'linked.diff'
        try:
            link.symlink_to(self.root/'renamed.diff')
        except (OSError,NotImplementedError):
            self.skipTest('Creating symlinks is unavailable on this host')
        with self.assertRaises(VisualError) as error:
            report_tree_receipt(self.root)
        self.assertEqual('invalid_report_tree',error.exception.code)


if __name__=='__main__':unittest.main()
