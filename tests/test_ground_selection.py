"""Ground-only completion receipts must precede immutable candidate selection."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiroclops.integration import session, storage


class GroundSelectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.directory=self.root/"sessions/ground-test"
        self.directory.mkdir(parents=True)
        self.candidate={"candidate_id":"candidate-01","status":"evaluated",
                        "feedback":{"eligible":True,"mean_rmse_body":.07},"smoke":{"status":"passed"}}
        self.state={"session_id":"ground-test","experiment_id":"ground-motion-v1","status":"ready",
                    "candidates":[self.candidate],"selected_candidate_id":None,"active_job_id":None,
                    "started_at":None,"deadline_at":None,"reference":{"calibration":[]}}
        storage.write_json(self.directory/"session.json",self.state)
        for module in (session,storage):
            p=patch.object(module,"ROOT",self.root);p.start();self.addCleanup(p.stop)
        p=patch.object(session,"process_identity",side_effect=lambda pid:f"process-{pid}");p.start();self.addCleanup(p.stop)
        process=Mock(pid=99999);process.poll.return_value=None
        p=patch.object(session.subprocess,"Popen",return_value=process);self.launch=p.start();self.addCleanup(p.stop)
        self.session=session.Session(self.directory)

    def job(self,**changes):
        record={"job_id":"job-evaluated","session_id":"ground-test","action":"evaluate",
                "candidate_id":"candidate-01","status":"succeeded"}
        record.update(changes)
        storage.write_json(self.directory/"jobs/job-evaluated.json",record)

    def rejected(self):
        before=self.session.path.read_bytes()
        with self.assertRaises(storage.IntegrationError) as caught:self.session.submit("finalize")
        self.assertEqual(caught.exception.code,"no_eligible_candidate")
        self.assertEqual(before,self.session.path.read_bytes())
        self.launch.assert_not_called()

    def test_failed_candidate_cannot_reuse_successful_feedback(self):
        self.job()
        for status in ("failed","interrupted","cancelled","queued"):
            self.session.update(lambda s:s["candidates"][0].update(status=status))
            with self.subTest(status=status):self.rejected()

    def test_matching_succeeded_evaluate_job_is_required(self):
        self.rejected()
        for changes in ({"status":"interrupted"},{"status":"cancelled"},{"status":"failed"},{"status":"running"},
                        {"candidate_id":"candidate-02"},{"session_id":"other-session"},{"action":"report"},
                        {"job_id":"job-other"}):
            self.job(**changes)
            with self.subTest(changes=changes):self.rejected()

    def test_successful_ground_job_selects_lowest_completed_eligible_candidate(self):
        self.job()
        unfinished=deepcopy(self.candidate)
        unfinished.update(candidate_id="candidate-02",feedback={"eligible":True,"mean_rmse_body":.01})
        self.session.update(lambda s:s["candidates"].append(unfinished))
        result=self.session.submit("finalize")
        self.assertEqual(result["candidate_id"],"candidate-01")
        self.assertEqual(self.session.read()["selected_candidate_id"],"candidate-01")
        self.assertTrue(self.session.read()["sealed_at"])

    def test_legacy_sessions_keep_existing_selection_contract_without_job_receipt(self):
        self.session.update(lambda s:s.update(experiment_id="legacy-jump-v1"))
        result=self.session.submit("finalize")
        self.assertEqual(result["candidate_id"],"candidate-01")
        self.assertEqual(self.launch.call_count,1)


if __name__=="__main__":unittest.main()
