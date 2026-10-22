"""Validation cannot quietly become a new edit/retry or a post-hoc protocol."""
from pathlib import Path
import tempfile
import unittest

from kiroclops.visual.schema import write_json,file_sha256
from kiroclops.local_engine.media_session import LIMITS
from scripts.validate_sealed_panel import run
from kiroclops.web_local.specs import scenarios


class SealedPanelValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.session=self.root/'session';self.output=self.root/'validation'
        self.protocol=self.root/'protocol.json'
        self.state={'status':'finalized','selected_candidate_id':'candidate-01','active_job_id':None,'started_at':200}
        self.plan={'created_at':100,'limits':dict(LIMITS),'heldout':[]}
        self.save()

    def save(self):
        write_json(self.session/'session.json',self.state);write_json(self.protocol,self.plan)
        write_json(self.session/'operator-preparation.json',{'protocol_sha256':file_sha256(self.protocol)})

    def reject(self,pattern):
        with self.assertRaisesRegex(ValueError,pattern):run(self.session,self.protocol,self.root/'reference',self.output)
        self.assertFalse(self.output.exists())

    def test_active_candidate_is_never_validated(self):
        self.state['active_job_id']='running';self.save();self.reject('idle sealed')

    def test_unsealed_candidate_is_never_validated(self):
        self.state['status']='prepared';self.save();self.reject('idle sealed')

    def test_changed_protocol_does_not_get_new_approval(self):
        self.plan['created_at']=110;write_json(self.protocol,self.plan)
        self.reject('not bound')

    def test_posthoc_protocol_cannot_claim_preregistration(self):
        self.plan['created_at']=201;self.save();self.reject('not bound')

    def test_relaxed_limits_rejected_even_when_hash_bound(self):
        self.plan['limits']['position_rmse_fraction']=.5;self.save();self.reject('limits changed')

    def test_missing_scenarios_do_not_vacuously_pass(self):
        self.reject('twelve distinct')

    def test_scenario_cannot_escape_output_directory(self):
        self.plan['heldout']=scenarios('panel','validation')
        self.plan['heldout'][0]['id']='../../outside';self.save();self.reject('path-free')

    def test_session_evidence_cannot_be_used_as_output(self):
        self.output=self.session/'nested-validation';self.reject('outside the original')

    def test_existing_validation_is_preserved(self):
        self.output.mkdir();sentinel=self.output/'old.json';sentinel.write_text('original')
        with self.assertRaisesRegex(ValueError,'fresh output'):run(self.session,self.protocol,self.root/'reference',self.output)
        self.assertEqual(sentinel.read_text(),'original')


if __name__=='__main__':unittest.main()
