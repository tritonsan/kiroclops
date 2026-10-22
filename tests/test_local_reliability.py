"""Runtime readiness must preserve inputs, failure evidence and timing gates."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import reliability
from kiroclops.visual.schema import VisualError, file_sha256


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "installed/driver/node.exe"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"installed-playwright-node\x00\xff\x01")
        self.runtime = self.root / "r"

    def test_doctor_is_read_only_and_does_not_start_native_runtime(self):
        result = reliability.runtime_doctor(self.runtime, packaged_node=self.source)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(result["status"], "not_prepared")
        self.assertFalse(result["model_started"])
        self.assertFalse(result["cloud_used"])
        self.assertFalse(result["native_capture_started"])
        self.assertEqual(result["capture_lock"], str(reliability.COMMON_CAPTURE_LOCK.resolve()))

    def test_provisioning_verifies_both_copies_and_only_returns_child_environment(self):
        browser = self.root / "browser"
        browser.mkdir()
        before = dict(os.environ)
        result = reliability.prepare_runtime(self.runtime, packaged_node=self.source, browser_path=browser)
        self.assertEqual(dict(os.environ), before)
        destination = Path(result["driver"]["path"])
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(file_sha256(destination), result["driver"]["source_sha256"])
        self.assertEqual(result["environment"], {
            "PLAYWRIGHT_NODEJS_PATH": str(destination), "PLAYWRIGHT_BROWSERS_PATH": str(browser.resolve())})
        self.assertTrue(Path(result["driver"]["receipt_path"]).is_file())
        self.assertTrue(Path(result["session_root"]).is_dir())
        self.assertEqual(reliability.runtime_doctor(self.runtime, packaged_node=self.source)["status"], "ready")
        self.assertEqual(reliability.prepare_runtime(self.runtime, packaged_node=self.source)["driver"], result["driver"])
        json.dumps(result, allow_nan=False)

    def test_existing_changed_driver_is_rejected_without_repairing_its_bytes(self):
        result = reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        destination = Path(result["driver"]["path"])
        destination.write_bytes(b"changed-copy")
        with self.assertRaises(VisualError) as caught:
            reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        self.assertEqual(caught.exception.code, "environment_not_ready")
        self.assertEqual(destination.read_bytes(), b"changed-copy")
        self.assertEqual(self.source.read_bytes(), b"installed-playwright-node\x00\xff\x01")

    def test_source_changing_during_copy_cannot_publish_ready_receipt(self):
        original_copy = reliability.shutil.copyfileobj
        def changed(original, copied):
            original_copy(original, copied)
            self.source.write_bytes(b"new-installed-version")
        with patch.object(reliability.shutil, "copyfileobj", side_effect=changed):
            with self.assertRaises(VisualError) as caught:
                reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        self.assertEqual(caught.exception.code, "environment_not_ready")
        self.assertEqual(list(self.runtime.glob("runtime/driver/*/receipt.json")), [])

    def test_driver_versions_are_separate_and_old_provenance_is_retained(self):
        first = reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        self.source.write_bytes(b"next-installed-node")
        second = reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        self.assertNotEqual(first["driver"]["path"], second["driver"]["path"])
        self.assertTrue(Path(first["driver"]["receipt_path"]).is_file())
        self.assertEqual(Path(first["driver"]["path"]).read_bytes(), b"installed-playwright-node\x00\xff\x01")

    def test_invalid_browser_selection_does_not_write_runtime(self):
        with self.assertRaises(VisualError) as caught:
            reliability.prepare_runtime(self.runtime, packaged_node=self.source, browser_path=self.root / "missing")
        self.assertEqual(caught.exception.code, "environment_not_ready")
        self.assertFalse(self.runtime.exists())

    def test_corrupt_provenance_is_rejected(self):
        result = reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        Path(result["driver"]["receipt_path"]).write_text("broken", encoding="utf8")
        with self.assertRaises(VisualError) as caught:
            reliability.prepare_runtime(self.runtime, packaged_node=self.source)
        self.assertEqual(caught.exception.code, "environment_not_ready")


class PathBudgetTests(unittest.TestCase):
    def test_boundary_is_resolved_before_any_directory_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            units = len(str(base).encode("utf-16-le")) // 2
            exact = base / ("a" * (reliability.MAX_TMP_CHARS - units - 1))
            self.assertEqual(reliability.check_path_budget([exact])["deepest_characters"], 240)
            with self.assertRaises(VisualError) as caught:
                reliability.check_path_budget([exact.with_name(exact.name + "a")])
            self.assertEqual(caught.exception.code, "environment_not_ready")
            self.assertEqual(caught.exception.details["path_budget"]["deepest_characters"], 241)
            self.assertFalse(exact.exists())

    def test_windows_utf16_length_counts_supplementary_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            value = Path(temp) / ("\U0001f680" * 4)
            maximum = len(str(value.resolve()))
            with self.assertRaises(VisualError):
                reliability.check_path_budget([value], maximum_chars=maximum)
            self.assertEqual(reliability.check_path_budget([value])["deepest_characters"], maximum + 4)

    def test_planning_includes_deep_preservation_clean_retry_and_custom_scenario(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = reliability.planned_tmp_paths(Path(temp) / "session", scenario_name="new-recipe")
            names = [path.as_posix() for path in paths]
            self.assertTrue(any("preservation/wall-coyote/candidate/import/clean-retry/" in path for path in names))
            self.assertTrue(any("jump/candidate/raw/new-recipe/" in path for path in names))
            self.assertTrue(all(path.as_posix().endswith("environment/tmp") for path in paths))
            self.assertEqual(len(paths), len(set(paths)))

    def test_budget_cannot_be_relaxed_and_path_components_cannot_escape(self):
        with self.assertRaises(ValueError):
            reliability.check_path_budget([Path("tmp")], maximum_chars=241)
        with self.assertRaises(ValueError):
            reliability.check_path_budget([])
        for value in ("../escape", "..", "nested/name", "bad:name"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reliability.planned_tmp_paths("session", job_id=value)


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "warmups"
        self.calls = []

    def capture_sequence(self, results):
        def capture(directory):
            self.assertFalse(directory.exists())
            directory.mkdir()
            (directory / "raw-capture.txt").write_text(str(len(self.calls)), encoding="utf8")
            self.calls.append(directory)
            result = results[len(self.calls) - 1]
            if isinstance(result, BaseException):
                raise result
            return result
        return capture

    @staticmethod
    def clean():
        return {"status": "ok", "timing": {"status": "pass", "errors": [], "clock_uncertainty_s": .01}}

    def test_requires_consecutive_success_and_preserves_all_failed_captures(self):
        failure = {"status": "failed", "timing": {"status": "fail", "errors": ["clock_mapping_uncertainty_exceeded"]}}
        result = reliability.qualify_warmups(self.capture_sequence([self.clean(), failure, self.clean(), self.clean()]), self.output)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([item["status"] for item in result["attempts"]], ["pass", "fail", "pass", "pass"])
        self.assertEqual(result["consecutive_clean"], 2)
        self.assertEqual((self.calls[1] / "raw-capture.txt").read_text(), "1")
        self.assertTrue((self.output / "attempt-02-result.json").is_file())
        self.assertFalse(result["evaluated"])
        self.assertFalse(result["policy"]["capture_timing_thresholds_changed"])

    def test_six_failures_stop_and_leave_finite_error_manifest(self):
        capture = self.capture_sequence([VisualError("capture_timing_failure", "timing gate failed")] * 6)
        with self.assertRaises(VisualError) as caught:
            reliability.qualify_warmups(capture, self.output)
        self.assertEqual(caught.exception.code, "environment_not_ready")
        self.assertEqual(len(self.calls), 6)
        saved = json.loads(Path(caught.exception.receipt_path).read_text(encoding="utf8"))
        self.assertEqual(saved["status"], "fail")
        self.assertEqual(len(saved["attempts"]), 6)
        self.assertTrue(all((path / "raw-capture.txt").is_file() for path in self.calls))
        json.dumps(saved, allow_nan=False)

    def test_nonfinite_capture_metadata_cannot_be_accepted_or_published(self):
        invalid = self.clean()
        invalid["timing"]["clock_uncertainty_s"] = float("nan")
        with self.assertRaises(VisualError):
            reliability.qualify_warmups(self.capture_sequence([invalid]), self.output, max_attempts=1)
        saved = json.loads((self.output / "result.json").read_text())
        self.assertEqual(saved["attempts"][0]["error"]["code"], "ValueError")
        self.assertNotIn("capture_result", saved["attempts"][0])

    def test_failed_capture_with_pass_timing_is_still_rejected(self):
        result = self.clean()
        result["status"] = "failed"
        with self.assertRaises(VisualError):
            reliability.qualify_warmups(self.capture_sequence([result]), self.output, max_attempts=1)

    def test_cancellation_is_preserved_and_never_retried(self):
        error = VisualError("cancelled", "caller cancelled")
        with self.assertRaises(VisualError) as caught:
            reliability.qualify_warmups(self.capture_sequence([error, self.clean()]), self.output)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads((self.output / "result.json").read_text())["status"], "cancelled")

    def test_existing_evidence_is_not_overwritten_and_limit_cannot_expand(self):
        self.output.mkdir()
        original = self.output / "original.txt"
        original.write_text("keep", encoding="utf8")
        with self.assertRaises(VisualError) as caught:
            reliability.qualify_warmups(lambda output: self.clean(), self.output)
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(original.read_text(), "keep")
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            reliability.qualify_warmups(lambda output: self.clean(), self.output, max_attempts=7)


if __name__ == "__main__":
    unittest.main()
