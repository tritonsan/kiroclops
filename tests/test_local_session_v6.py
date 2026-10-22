"""V6 session additions: partial probes, budget refusal, worker tracebacks, inspection guards."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kiroclops.local_engine.backend import _atomic, _read
from kiroclops.local_engine.session import Session, create_session, run_session_job
from kiroclops.visual.schema import VisualError


class SessionV6Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="v6 session ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root/"original"
        (self.baseline/"src").mkdir(parents=True)
        (self.baseline/"src/panel-motion.js").write_text("export const speed=1;", encoding="utf-8")
        self.reference = self.root/"reference"
        self.reference.mkdir()
        (self.reference/"secret.js").write_text("private", encoding="utf-8")
        self.directory = self.root/"session"
        create_session(self.directory, "web_panel", self.baseline, self.reference, "Match opening behavior",
                       experiment={"experiment_id": "test", "capture_lock": str(self.root/"capture.lock"),
                                   "calibration": ["open", {"id": "close", "case_id": "close-r1"}]})
        self.session = Session(self.directory)

    def submit(self, operation, result=None, candidate_id=None, case_ids=None, runner=None):
        with patch.object(Session, "_spawn"):
            submitted = self.session.submit(operation, candidate_id, case_ids)
        if submitted["status"] == "queued":
            run_session_job(self.directory, submitted["job_id"], runner=runner or (lambda *args: result or {}))
        return self.session.get_job(submitted["job_id"])

    def prepare(self):
        self.assertEqual("succeeded", self.submit("prepare", {"reference": {}, "before": {}})["status"])

    def test_partial_probe_runs_named_cases_without_consuming_a_candidate(self):
        self.prepare()
        seen = {}

        def runner(directory, job, check, progress, cancelled):
            seen.update(job)
            return {"eligible": False, "partial": True, "partial_cases": job["case_ids"], "score": .5}
        job = self.submit("evaluate", case_ids=["close-r1"], runner=runner)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(["close-r1"], seen["case_ids"])
        self.assertTrue(seen["partial"])
        self.assertEqual("probe-01", job["candidate_id"])
        self.assertTrue((self.directory/"probes/probe-01/source/src/panel-motion.js").is_file())
        view = self.session.inspect()
        self.assertEqual([], view["candidates"])
        self.assertEqual(1, view["budget"]["partial_evaluations_used"])
        self.assertEqual(3, view["budget"]["remaining_candidates"])
        # A probe is never a finalizable candidate: the finalize job fails at submission.
        with patch.object(Session, "_spawn"):
            finalize = self.session.submit("finalize")
        self.assertEqual("failed", finalize["status"])
        self.assertEqual("no_eligible_candidate", self.session.get_job(finalize["job_id"])["error"]["code"])

    def test_probe_validation_and_budget(self):
        self.prepare()
        for bad, code in (([], "invalid_request"), (["open", "open"], "invalid_request"), (["ghost"], "invalid_request"), ("open", "invalid_request")):
            with self.subTest(bad=bad), self.assertRaises(VisualError) as caught:
                self.session.submit("evaluate", None, bad)
            self.assertEqual(code, caught.exception.code)
        with self.assertRaises(VisualError) as caught:
            self.session.submit("prepare", None, ["open"])
        self.assertEqual("invalid_request", caught.exception.code)
        for _ in range(3):
            self.submit("evaluate", {"eligible": False, "partial": True}, case_ids=["open"])
        with self.assertRaises(VisualError) as caught:
            self.session.submit("evaluate", None, ["open"])
        self.assertEqual("budget_exhausted", caught.exception.code)
        # Full candidates remain available after the probes are spent.
        self.assertEqual("succeeded", self.submit("evaluate", {"eligible": True, "score": .2})["status"])

    def test_evaluation_refused_when_measured_time_does_not_fit(self):
        self.prepare()
        first = self.submit("evaluate", {"eligible": True, "score": .3})
        job_path = self.directory/"jobs"/first["job_id"]/"job.json"
        job = _read(job_path)
        job.update(started_at=job["started_at"], finished_at=job["started_at"]+600.)
        _atomic(job_path, job)
        state = self.session._load()
        state["deadline_at"] = time.time()+200.
        self.session._save(state)
        self.assertEqual(600., round(self.session.inspect()["budget"]["measured_evaluation_seconds"]))
        with self.assertRaises(VisualError) as caught:
            self.session.submit("evaluate")
        self.assertEqual("budget_insufficient", caught.exception.code)
        # A probe over one of two calibration cases needs about half the time and still does not fit.
        with self.assertRaises(VisualError) as caught:
            self.session.submit("evaluate", None, ["open"])
        self.assertEqual("budget_insufficient", caught.exception.code)
        state["deadline_at"] = time.time()+900.
        self.session._save(state)
        self.assertEqual("succeeded", self.submit("evaluate", {"eligible": True, "score": .25})["status"])

    def test_worker_failure_writes_a_traceback(self):
        self.prepare()

        def runner(*args):
            raise KeyError("frames")
        job = self.submit("evaluate", runner=runner)
        self.assertEqual("failed", job["status"])
        self.assertEqual("execution_error", job["error"]["code"])
        self.assertEqual("KeyError", job["error"]["type"])
        self.assertTrue(job["error"]["traceback_logged"])
        log = (self.directory/"jobs"/job["job_id"]/"worker.log").read_text(encoding="utf-8")
        self.assertIn("KeyError: 'frames'", log)
        self.assertIn("Traceback", log)

    def test_inspect_window_refuses_non_evaluate_and_bad_identifiers(self):
        self.prepare()
        prepared = self.session._load()["prepare_job_id"]
        with self.assertRaises(VisualError) as caught:
            self.session.inspect_window(prepared, "open", "card")
        self.assertEqual("not_inspectable", caught.exception.code)
        with self.assertRaises(VisualError) as caught:
            self.session.inspect_window(prepared, "../open", "card")
        self.assertEqual("invalid_request", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
