from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts import audit_capture_costs_v1 as audit
from kiroclops.integration.storage import project_hashes


class CostAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.session=self.root/'sessions/trial'
        (self.session/'baseline').mkdir(parents=True)
        (self.session/'baseline/project.godot').write_text('[application]')
        self.patch=patch.object(audit,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.state={'baseline':str(self.session/'baseline'),'baseline_hashes':project_hashes(self.session/'baseline'),
                    'active_job_id':None,'candidates':[]}
        for n in range(1,4):
            cid=f'candidate-{n:02d}';candidate={'candidate_id':cid,'status':'evaluated',
                'feedback':{'eligible':n>=2,'mean_rmse_body':.1/n}}
            self.state['candidates'].append(candidate)
            self.write(self.session/'candidates'/cid/'candidate.json',candidate)
            self.write(self.session/'jobs'/f'job-{n}.json',{'action':'evaluate','candidate_id':cid,'status':'succeeded',
                'started_at':f'2026-09-19T12:0{n}:00+00:00','finished_at':f'2026-09-19T12:0{n}:30+00:00'})
        self.write(self.session/'session.json',self.state)
        self.write(self.session/'kiro/launch.json',{'status':'timeout'})
        self.write(self.root/'artifacts/model-trials/reviews/trial/timing-diagnostic.json',
            {'totals':{'completed_capture_count':78},'filesystem_interval_summaries':{},
             'candidate_startup_observations':[{'import_log_activity_seconds':47}]})
        for name in ['kiroclops/integration/runner.py','kiroclops/integration/ground_runner.py',
                     'kiroclops/integration/ground_worker.py','kiroclops/integration/storage.py',
                     'kiroclops/integration/import_cache_v1.py','scripts/audit_capture_costs_v1.py']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture\n')
        self.output=self.root/'artifacts/new/diagnosis.json'

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')

    def test_completed_eligible_candidate_is_distinct_from_later_cost(self):
        before=(self.session/'session.json').read_bytes()
        result=audit.audit(self.session,self.output,source_timing_repeats=1)
        self.assertEqual(result['calibration_decisions']['first_eligible_candidate'],'candidate-02')
        self.assertEqual(result['calibration_decisions']['later_completed_evaluation_seconds'],30)
        self.assertFalse(result['acceptance_credit'])
        self.assertEqual((self.session/'session.json').read_bytes(),before)

    def test_failed_evaluation_cannot_be_first_eligible(self):
        p=self.session/'jobs/job-2.json';job=json.loads(p.read_text());job['status']='failed';self.write(p,job)
        result=audit.audit(self.session,self.output,source_timing_repeats=1)
        self.assertEqual(result['calibration_decisions']['first_eligible_candidate'],'candidate-03')
        self.assertEqual(result['calibration_decisions']['later_completed_evaluation_seconds'],0)

    def test_active_run_is_not_profiled(self):
        self.state['active_job_id']='job-active';self.write(self.session/'session.json',self.state)
        with self.assertRaisesRegex(ValueError,'ended runs'):
            audit.audit(self.session,self.output)
        self.assertFalse(self.output.exists())

    def test_output_cannot_replace_history(self):
        with self.assertRaisesRegex(ValueError,'new JSON'):
            audit.audit(self.session,self.session/'session.json')


if __name__=='__main__':unittest.main()
