"""V6 plan WP2.3: a finalization that failed only in recording is measured again for the same sealed candidate."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine.backend import _atomic, _read
from kiroclops.local_engine.session import MAX_REMEASURES, Session, create_session, run_session_job
from kiroclops.visual.schema import VisualError


def failing(code):
    def runner(directory, job, check, progress, cancelled):
        raise VisualError(code, 'Native screenshot exceeded 120ms' if code == 'capture_timing_failure' else 'other failure')
    return runner


class RemeasureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="v6 remeasure café ")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        baseline = root/"original"
        (baseline/"src").mkdir(parents=True)
        (baseline/"src/panel-motion.js").write_text("export const speed=1;", encoding="utf-8")
        reference = root/"reference"
        reference.mkdir()
        (reference/"secret.js").write_text("private", encoding="utf-8")
        self.directory = root/"session"
        create_session(self.directory, "web_panel", baseline, reference, "Match opening behavior",
                       experiment={"experiment_id": "test", "capture_lock": str(root/"capture.lock"), "calibration": ["open"]})
        self.session = Session(self.directory)

    def submit(self, operation, runner=None, run=True):
        with patch.object(Session, "_spawn"):
            submitted = self.session.submit(operation)
        if not run:
            # get_job would reconcile the queued job by spawning a real worker.
            return submitted
        if submitted["status"] == "queued":
            run_session_job(self.directory, submitted["job_id"], runner=runner or (lambda *args: {}))
        return self.session.get_job(submitted["job_id"], allow_final_result=True)

    def sealed_and_failed(self, code='capture_timing_failure'):
        self.submit("prepare", lambda *args: {"reference": {}, "before": {}})
        self.submit("evaluate", lambda *args: {"eligible": True, "score": .2})
        final = self.submit("finalize", failing(code))
        self.assertEqual(("failed", code), (final["status"], final["error"]["code"]))
        self.assertEqual("finalization_failed", self.session._load()["status"])
        return final

    def test_recording_failure_is_measured_again_with_the_same_sealed_source(self):
        first = self.sealed_and_failed()
        seen = {}

        def runner(directory, job, check, progress, cancelled):
            seen.update(job)
            return {"pass": True, "independent_review_status": "pending"}
        again = self.submit("remeasure", runner)
        self.assertEqual(("finalize", "succeeded"), (again["operation"], again["status"]))
        self.assertEqual((first["candidate_id"], first["source_sha256"]), (again["candidate_id"], again["source_sha256"]))
        self.assertEqual(first["job_id"], seen["retry_after_capture_failure"])
        job = _read(self.directory/"jobs"/again["job_id"]/"job.json")
        self.assertEqual(first["job_id"], job["retry_after_capture_failure"])
        state = self.session._load()
        self.assertEqual(("finalized", again["job_id"]), (state["status"], state["final_job_id"]))
        self.assertTrue(state["final"]["pass"])
        remeasures = self.session.inspect()["remeasures"]
        self.assertEqual([(again["job_id"], first["job_id"], "capture_timing_failure")],
                         [(r["job_id"], r["after_job_id"], r["error_code"]) for r in remeasures])
        # A succeeded finalization is never measured again.
        with self.assertRaises(VisualError) as caught:
            self.session.submit("remeasure")
        self.assertEqual("remeasure_rejected", caught.exception.code)

    def test_other_failures_and_unsealed_sessions_are_rejected(self):
        with self.assertRaises(VisualError) as caught:
            self.session.submit("remeasure")
        self.assertEqual("remeasure_rejected", caught.exception.code)
        self.sealed_and_failed('invalid_result')
        with self.assertRaises(VisualError) as caught:
            self.session.submit("remeasure")
        self.assertEqual("remeasure_rejected", caught.exception.code)
        with self.assertRaises(VisualError) as caught:
            self.session.submit("remeasure", candidate_id="candidate-01")
        self.assertEqual("invalid_request", caught.exception.code)

    def test_at_most_two_remeasurements_and_the_author_still_sees_the_current_final_job(self):
        first = self.sealed_and_failed('machine_not_quiet')
        jobs = [self.submit("remeasure", failing('capture_timing_failure')) for _ in range(MAX_REMEASURES)]
        self.assertEqual([first["job_id"], jobs[0]["job_id"]], [_read(self.directory/"jobs"/j["job_id"]/"job.json")["retry_after_capture_failure"] for j in jobs])
        with self.assertRaises(VisualError) as caught:
            self.session.submit("remeasure")
        self.assertEqual("budget_exhausted", caught.exception.code)
        with patch.object(Session, "_native_authoring_open", return_value=False):
            self.assertEqual(jobs[-1]["job_id"], self.session.submit("finalize")["job_id"])

    def test_authoring_deadline_does_not_block_and_lineage_is_bound(self):
        first = self.sealed_and_failed()
        state = self.session._load()
        state["deadline_at"] = time.time()-60
        self.session._save(state)
        queued = self.submit("remeasure", run=False)
        job_path = self.directory/"jobs"/queued["job_id"]/"job.json"
        job = _read(job_path)
        self.assertEqual(first["job_id"], job["retry_after_capture_failure"])
        job["retry_after_capture_failure"] = "0"*32
        _atomic(job_path, job)
        run_session_job(self.directory, queued["job_id"], runner=lambda *args: {"pass": True})
        self.assertEqual("job_binding_changed", self.session.get_job(queued["job_id"], allow_final_result=True)["error"]["code"])


if __name__ == '__main__':
    unittest.main()
