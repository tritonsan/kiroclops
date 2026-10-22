"""Post-turn validation must replay the model-sealed job and bounded result."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine import review
from kiroclops.local_engine.session import JOB_BINDING_FIELDS
from kiroclops.visual.schema import VisualError, digest, write_json


class PostturnNativeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.identifier='a'*32
        self.source_hashes={'player.gd':'source-bytes'}
        self.source_sha256=digest(self.source_hashes)
        self.job={'schema_version':1,'job_id':self.identifier,'operation':'finalize',
                  'created_at':110.,'candidate_id':'candidate-01',
                  'source_path':str(self.directory/'candidates/candidate-01/source'),
                  'source_hashes':self.source_hashes,'source_sha256':self.source_sha256,
                  'status':'succeeded','finished_at':130.,'result_applied':True}
        self.result={'pass':True,'independent_review_status':'pending'}
        self.job['result_sha256']=digest(self.result)
        self.binding=digest({key:self.job[key] for key in JOB_BINDING_FIELDS})
        self.state={'session_id':'session-01','budget':{'max_validation_seconds':1800},
                    'selected_candidate_id':'candidate-01','final_job_id':self.identifier,
                    'active_job_id':None,'sealed_at':111.,'deadline_at':1900.,
                    'candidates':[{'candidate_id':'candidate-01','source_sha256':self.source_sha256}],
                    'final':{'job_id':self.identifier,'status':'succeeded','pass':True}}
        request={'preserved_finalization':True,'job_id':self.identifier,
                 'job_binding_sha256':self.binding,'candidate_id':'candidate-01',
                 'source_sha256':self.source_sha256,'session_id':'session-01',
                 'validation_deadline_at':1910.,'started_at':121.}
        self.launch={'status':'completed','response':{'stopReason':'end_turn'},
                     'prompt_started_at':100.,'response_received_at':120.,'finished_at':140.,
                     'terminal_cleanup':{'status':'pass','request':request,
                                         'terminal_job_status':'succeeded'},
                     'postturn_finalization':{'status':'terminal','job_id':self.identifier,
                         'job_status':'succeeded','candidate_id':'candidate-01',
                         'source_sha256':self.source_sha256,'finished_at':130.,
                         'validation_deadline_at':1910.}}
        self.save()

    def save(self):
        job_dir=self.directory/'jobs'/self.identifier
        write_json(self.directory/'session.json',self.state)
        write_json(job_dir/'job.json',self.job)
        write_json(job_dir/'binding.json',{'sha256':self.binding})
        write_json(job_dir/'result.json',self.result)

    def audit(self):
        self.save()
        return review._audit_postturn_finalization(self.directory,self.launch)

    def test_timely_sealed_terminal_result_passes(self):
        self.assertEqual('pass',self.audit()['status'])

    def test_legacy_native_turn_without_handoff_is_compatible(self):
        self.launch.pop('postturn_finalization')
        self.assertEqual('not_applicable',self.audit()['status'])

    def test_submission_after_end_turn_fails(self):
        self.launch['response_received_at']=109.
        with self.assertRaises(VisualError):self.audit()

    def test_changed_candidate_source_or_deadline_fails(self):
        original=deepcopy(self.launch)
        for key,value in (('source_sha256','wrong-source'),('validation_deadline_at',1911.),
                          ('job_status','failed')):
            with self.subTest(key=key):
                self.launch=deepcopy(original)
                self.launch['postturn_finalization'][key]=value
                with self.assertRaises(VisualError):self.audit()

    def test_changed_result_bytes_or_binding_fails(self):
        self.result['pass']=False
        with self.assertRaises(VisualError):self.audit()
        self.result['pass']=True
        self.binding='0'*64
        with self.assertRaises(VisualError):self.audit()

    def test_terminal_result_beyond_validation_deadline_fails(self):
        self.job['finished_at']=1916.
        self.launch['postturn_finalization']['finished_at']=1916.
        self.launch['finished_at']=1917.
        with self.assertRaises(VisualError):self.audit()


if __name__=='__main__':unittest.main()
