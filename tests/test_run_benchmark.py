"""Batch orchestration with all preparations/model launches mocked."""
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.integration.kiro import KiroError
from kiroclops.integration.storage import IntegrationError, read_json, write_json
from scripts import run_benchmark as batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Experiment Café"
        self.root.mkdir()
        root = patch.object(batch, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        self.stdout = patch("sys.stdout", new_callable=StringIO)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def prepare(self, name, *, mode):
        path = self.root / "sessions" / name
        path.mkdir(parents=True)
        write_json(path / "session.json", {"status": "new", "mode": mode})
        return path

    def test_default_dry_run_never_prepares_launches_or_writes_artifacts(self):
        with patch.object(batch, "prepare_session") as prepare, patch.object(batch, "run_kiro") as launch:
            self.assertEqual(batch.main(["--tools", "T01", "--baseline", "B01"]), 0)
        prepare.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_last_slot_and_duplicates_stop_entire_batch_before_any_run(self):
        (self.root / "sessions/B03").mkdir(parents=True)
        with patch.object(batch, "prepare_session") as prepare, patch.object(batch, "run_kiro") as launch:
            with self.assertRaises(IntegrationError) as ctx:
                batch.run_batch(["T03", "T04"], ["B01", "B02", "B03"], execute=True)
            self.assertEqual(ctx.exception.code, "session_exists")
            with self.assertRaises(IntegrationError) as ctx:
                batch.run_batch(["Duplicate"], ["duplicate"], execute=True)
            self.assertEqual(ctx.exception.code, "duplicate_session")
        prepare.assert_not_called()
        launch.assert_not_called()
        self.assertFalse((self.root / "artifacts").exists())

    def test_bounds_invalid_ids_and_empty_batch_rejected(self):
        for tools, baseline in (([], []), (["1", "2", "3", "4"], []), (["../escape"], [])):
            with self.subTest(tools=tools), self.assertRaises(IntegrationError):
                batch.plan_batch(tools, baseline)

    def test_runs_are_sequential_and_ordinary_failure_does_not_skip_named_runs(self):
        order = []
        def prepare(name, *, mode):
            order.append(("prepare", name, mode))
            return self.prepare(name, mode=mode)
        def launch(path, **kwargs):
            order.append(("launch", path.name, kwargs["mode"]))
            result = {"status": "tool_denied" if path.name == "T01" else "completed", "prompt_sent": True}
            write_json(path / "kiro/launch.json", result)
            write_json(path / "session.json", {"status": "closed_failed" if path.name == "T01" else "finished", "final": {"numerical_status": "fail"}})
            return result
        with patch.object(batch, "prepare_session", side_effect=prepare), patch.object(batch, "run_kiro", side_effect=launch):
            result = batch.run_batch(["T01", "T02"], ["B01"], execute=True)
        self.assertEqual(order, [(phase, name, mode) for name, mode in (("T01", "tools"), ("T02", "tools"), ("B01", "baseline")) for phase in ("prepare", "launch")])
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["attempted_count"], 3)
        self.assertEqual(result["sessions"][0]["status"], "tool_denied")
        self.assertEqual(result["sessions"][1]["experiment"]["final"]["numerical_status"], "fail")
        self.assertEqual(read_json(result["summary_path"]), result)

    def test_prepare_failure_is_not_retried_and_next_session_is_attempted(self):
        def prepare(name, *, mode):
            if name == "T01":
                raise ValueError("one session setup failed")
            return self.prepare(name, mode=mode)
        with patch.object(batch, "prepare_session", side_effect=prepare) as prepared, \
                patch.object(batch, "run_kiro", return_value={"status": "completed"}) as launch:
            result = batch.run_batch(["T01"], ["B01"], execute=True)
        self.assertEqual(prepared.call_count, 2)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["sessions"][0]["status"], "failed")

    def test_environment_failure_stops_without_launching_or_preparing_remainder(self):
        with patch.object(batch, "prepare_session", side_effect=self.prepare) as prepare, \
                patch.object(batch, "run_kiro", side_effect=KiroError("model_unavailable", "exact model missing")) as launch:
            result = batch.run_batch(["T01", "T02"], ["B01"], execute=True)
        prepare.assert_called_once()
        launch.assert_called_once()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "model_unavailable")
        self.assertEqual([item["status"] for item in result["sessions"]], ["failed", "not_run", "not_run"])

    def test_keyboard_interrupt_sets_only_active_event_and_retains_partial_launch(self):
        observed = []
        def launch(path, *, cancel, **kwargs):
            observed.append(cancel)
            write_json(path / "kiro/launch.json", {"status": "cancelled", "prompt_sent": True})
            raise KeyboardInterrupt
        with patch.object(batch, "prepare_session", side_effect=self.prepare) as prepare, \
                patch.object(batch, "run_kiro", side_effect=launch):
            result = batch.run_batch(["T01", "T02"], ["B01"], execute=True)
        prepare.assert_called_once()
        self.assertTrue(observed[0].is_set())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["sessions"][0]["launch"]["status"], "cancelled")
        self.assertEqual(result["sessions"][1]["status"], "not_run")


if __name__ == "__main__":
    unittest.main()
