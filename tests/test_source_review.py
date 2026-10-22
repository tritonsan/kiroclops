"""Explicit static-review receipts; only synthetic temporary source files."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration import source_review as review, storage


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = self.root / "sessions/ground-01"
        self.baseline = self.session / "baseline"
        self.project = self.session / "candidates/candidate-01/project"
        self.changed = "source/characters/player/player.gd"
        for folder, code in ((self.baseline, b"ground_before\nair_unchanged\n"),
                             (self.project, b"ground_after\nair_unchanged\n")):
            (folder / self.changed).parent.mkdir(parents=True)
            (folder / self.changed).write_bytes(code)
            (folder / "project.godot").write_text("synthetic manifest", encoding="utf-8")
            (folder / "preserved.txt").write_text("unchanged resource", encoding="utf-8")
        self.candidate = {"candidate_id": "candidate-01", "status": "finalized",
                          "source_hashes": storage.project_hashes(self.project)}
        storage.write_json(self.project.parent / "candidate.json", self.candidate)
        self.state = {"schema_version": 3, "experiment_id": review.GROUND_ID, "session_id": "ground-01",
                      "status": "finished", "sealed_at": "2026-09-19T10:00:00+00:00", "active_job_id": None,
                      "baseline": str(self.baseline), "baseline_hashes": storage.project_hashes(self.baseline),
                      "candidates": [self.candidate], "selected_candidate_id": "candidate-01",
                      "final": {"candidate_id": "candidate-01"}}
        storage.write_json(self.session / "session.json", self.state)
        self.checks = {name: {"status": "pass", "reasoning": "Synthetic explicit static inspection for " + name}
                       for name in review.CHECKS}
        self.files = [{"path": self.changed, "reasoning": "Only the synthetic grounded branch differs."}]
        self.kwargs = {"candidate_id": "candidate-01", "source": "assistant_review", "reviewer": "test reviewer",
                       "checks": self.checks, "files": self.files}
        mocked_root = patch.object(storage, "ROOT", self.root)
        mocked_root.start()
        self.addCleanup(mocked_root.stop)

    def write(self, **overrides):
        return review.write_source_review(self.session, **{**self.kwargs, **overrides})

    def test_absent_review_is_pending(self):
        self.assertEqual(review.verify_source_review(self.session)["status"], "pending")
        self.assertFalse((self.session / review.RECEIPT).exists())

    def test_explicit_review_binds_full_maps_and_does_not_change_state_or_sources(self):
        state_bytes = (self.session / "session.json").read_bytes()
        path = self.write()
        raw = path.read_bytes()
        record = storage.read_json(path)
        self.assertEqual(record["baseline_source_hashes"], self.state["baseline_hashes"])
        self.assertEqual(record["candidate_source_hashes"], self.candidate["source_hashes"])
        self.assertEqual(record["changed_files"], [self.changed])
        self.assertIn("preserved.txt", record["candidate_source_hashes"])
        checked = review.verify_source_review(self.session)
        self.assertEqual(checked["status"], "pass", checked)
        self.assertEqual(checked["receipt_sha256"], storage.sha256(path))
        self.assertEqual((self.session / "session.json").read_bytes(), state_bytes)
        self.assertEqual(storage.project_hashes(self.project), self.candidate["source_hashes"])
        self.assertEqual(path.read_bytes(), raw)
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(path.read_bytes(), raw)

    def test_wrong_candidate_and_unapproved_source_cannot_write_receipt(self):
        for overrides in ({"candidate_id": "candidate-02"}, {"source": "tracker"}, {"source": "regex"}):
            with self.subTest(overrides=overrides), self.assertRaises(storage.IntegrationError):
                self.write(**overrides)
        self.assertFalse((self.session / review.RECEIPT).exists())

    def test_complete_explicit_decisions_and_exact_changed_file_coverage_required(self):
        missing_check = deepcopy(self.checks)
        missing_check.pop("airborne_horizontal_preserved")
        blank = deepcopy(self.checks)
        blank["ground_only"]["reasoning"] = " "
        for overrides in ({"checks": missing_check}, {"checks": blank}, {"files": []},
                          {"files": self.files * 2}, {"files": [{"path": "preserved.txt", "reasoning": "wrong file"}]}):
            with self.subTest(overrides=overrides), self.assertRaises(storage.IntegrationError):
                self.write(**overrides)
        self.assertFalse((self.session / review.RECEIPT).exists())

    def test_explicit_failed_scope_is_recorded_as_failed(self):
        self.checks["airborne_horizontal_preserved"] = {"status": "fail", "reasoning": "Reviewer found a changed air branch."}
        self.write(source="human")
        self.assertEqual(review.verify_source_review(self.session)["status"], "fail")

    def test_changed_baseline_candidate_unmodified_asset_and_metadata_invalidate_receipt(self):
        self.write()
        for path in (self.baseline / self.changed, self.project / self.changed,
                     self.project / "preserved.txt", self.project.parent / "candidate.json"):
            original = path.read_bytes()
            try:
                with self.subTest(path=str(path)):
                    path.write_bytes(original + b" ")
                    self.assertEqual(review.verify_source_review(self.session)["status"], "fail")
            finally:
                path.write_bytes(original)
        self.assertEqual(review.verify_source_review(self.session)["status"], "pass")

    def test_receipt_wrong_session_candidate_source_hash_or_verdict_fails(self):
        path = self.write()
        record = storage.read_json(path)
        for field, value in (("session_id", "ground-02"), ("candidate_id", "candidate-02"),
                             ("source", "tracking"), ("candidate_source_hashes", {}),
                             ("baseline_source_hashes", {}), ("status", "fail")):
            with self.subTest(field=field):
                storage.write_json(path, {**record, field: value})
                self.assertEqual(review.verify_source_review(self.session)["status"], "fail")
        storage.write_json(path, record)
        self.assertEqual(review.verify_source_review(self.session)["status"], "pass")

    def test_unsealed_or_active_session_cannot_be_reviewed(self):
        for field, value in (("sealed_at", None), ("active_job_id", "job-running"), ("status", "running")):
            storage.write_json(self.session / "session.json", {**self.state, field: value})
            with self.subTest(field=field), self.assertRaises(storage.IntegrationError):
                self.write()
        self.assertFalse((self.session / review.RECEIPT).exists())

    def test_context_change_before_exclusive_write_is_rejected(self):
        context = review._context(self.session)
        with patch.object(review, "_context", side_effect=[context, {**context, "candidate_metadata_sha256": "changed"}]):
            with self.assertRaisesRegex(storage.IntegrationError, "changed before"):
                self.write()
        self.assertFalse((self.session / review.RECEIPT).exists())


if __name__ == "__main__":
    unittest.main()
