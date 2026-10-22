"""Reports must not promote incomplete or failed acceptance to successful status."""
from pathlib import Path
import tempfile
import unittest

from kiroclops.integration.storage import read_json, write_json
from kiroclops.portable_v2.reporting import build_session_report, encode_video


class ReportAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root/"session.json", {"schema_version": 5, "session_id": "heart-test-ground",
            "experiment_id": "heart-ground-v2", "status": "finished", "candidates": [], "selected_candidate_id": None})
        self.final = {"numerical_status": "pass", "automatic_integrity_status": "pass", "measurement_status": "pending",
                      "normal_gameplay_status": "pass", "runs": {}, "normal_capture": str(self.root/"normal")}

    def render(self, *, audit=None):
        write_json(self.root/"final/comparison.json", self.final)
        if audit is not None:
            write_json(self.root/"final/independent-review.json", audit)
        path = build_session_report(self.root)
        self.assertTrue(path.is_file())
        return read_json(self.root/"report/summary.json")

    def test_numeric_success_stays_pending_until_independent_review(self):
        self.assertEqual(self.render()["status"], "pending")

    def test_known_numeric_failure_does_not_become_pending(self):
        self.final["numerical_status"] = "fail"
        self.assertEqual(self.render()["status"], "fail")

    def test_integrity_failure_cannot_be_overridden_by_review_pass(self):
        self.final["automatic_integrity_status"] = "fail"
        self.assertEqual(self.render(audit={"status": "pass"})["status"], "fail")

    def test_review_pass_without_candidate_binding_cannot_grant_acceptance(self):
        self.assertNotEqual(self.render(audit={"status": "pass"})["status"], "pass")

    def test_independent_failure_remains_failure(self):
        self.assertEqual(self.render(audit={"status": "fail"})["status"], "fail")

    def test_video_rejects_incomplete_manifest_before_encoding(self):
        run = self.root/"run"
        write_json(run/"manifest.json", {"frame_count": 240, "frames": [{}]*239})
        with self.assertRaises(ValueError):
            encode_video(run, self.root/"output.mp4")


if __name__ == "__main__":
    unittest.main()
