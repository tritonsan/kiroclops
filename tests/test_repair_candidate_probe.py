"""Operator probe boundaries; no actual model, engine or desktop launch."""
import importlib.util
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("operator_probe", Path(__file__).resolve().parents[1] / "scripts/run_repair_candidate_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = self.root / "sessions/test"
        self.source = self.session / "candidates/candidate-01/project"
        self.source.mkdir(parents=True)
        (self.source / "project.godot").write_text("config_version=5\n")
        (self.source / "player.gd").write_text("extends Node\n")
        self.expected = probe.project_hashes(self.source)
        self.state = {"selected_candidate_id":"candidate-01", "sealed_at":"2026-09-19T14:00:00+00:00", "active_job_id":None, "status":"finished"}
        self.candidate = {"candidate_id":"candidate-01", "status":"evaluated", "source_hashes":self.expected}
        self.protocol = {"protocol_id":"ground-cli-repair-v2", "asset_cache":{"cache_dir":"cache", "manifest_sha256":"fake"}}
        self.output = self.root / "artifacts/model-trials/reviews/probe"
        self.write(self.session / "session.json", self.state)
        self.write(self.session / "candidates/candidate-01/candidate.json", self.candidate)
        self.write(self.session / "kiro/launch.json", {"status":"completed"})
        self.write(self.session / "execution-protocol.json", self.protocol)
        self.write(self.session / "jobs/job.json", {"status":"succeeded"})
        for name, value in [("ROOT", self.root), ("verify_protocol", lambda s:self.protocol), ("_code_hashes", lambda:{"script":"exact"})]:
            item = patch.object(probe, name, value)
            item.start(); self.addCleanup(item.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_prepare_copies_source_without_engine_or_session_write(self):
        old_state = (self.session / "session.json").read_bytes()
        with patch.object(probe.movement_probe, "run_probe") as engine, patch.object(probe, "seed_import_cache") as seed:
            result = probe.prepare(self.session, self.output)
        self.assertEqual(result["status"], "prepared_no_engine")
        self.assertEqual(probe.project_hashes(self.output / "project"), self.expected)
        self.assertEqual((self.session / "session.json").read_bytes(), old_state)
        self.assertFalse(result["canonical_experiment_acceptance"])
        engine.assert_not_called(); seed.assert_not_called()

    def test_active_model_rejects_run_before_any_attempt_mutation(self):
        probe.prepare(self.session, self.output)
        self.write(self.session / "kiro/launch.json", {"status":"running"})
        with patch.object(probe.movement_probe, "run_probe") as engine, patch.object(probe, "seed_import_cache") as seed:
            with self.assertRaisesRegex(ValueError, "model run is still active"):
                probe.run_prepared(self.output)
        self.assertFalse((self.output / "run-start.json").exists())
        self.assertFalse((self.output / "failure.json").exists())
        engine.assert_not_called(); seed.assert_not_called()

    def test_active_job_rejects_terminal_model_before_engine(self):
        probe.prepare(self.session, self.output)
        self.write(self.session / "jobs/job.json", {"status":"running"})
        with patch.object(probe.movement_probe, "run_probe") as engine:
            with self.assertRaisesRegex(ValueError, "job is not terminal"):
                probe.run_prepared(self.output)
        self.assertFalse((self.output / "run-start.json").exists())
        engine.assert_not_called()

    def test_code_or_copy_tampering_rejects_before_engine(self):
        probe.prepare(self.session, self.output)
        with patch.object(probe, "_code_hashes", return_value={"script":"changed"}), self.assertRaises(ValueError):
            probe.run_prepared(self.output)
        (self.output / "project/player.gd").write_text("changed")
        with self.assertRaisesRegex(ValueError, "untouched"):
            probe.run_prepared(self.output)
        self.assertFalse((self.output / "run-start.json").exists())

    def test_wrong_selected_candidate_rejects_preparation(self):
        self.state["selected_candidate_id"] = "candidate-02"
        self.write(self.session / "session.json", self.state)
        with self.assertRaisesRegex(ValueError, "selected candidate"):
            probe.prepare(self.session, self.output)
        self.assertFalse(self.output.exists())

    def test_finalized_candidate_with_terminal_launch_is_valid_source_context(self):
        self.candidate["status"] = "finalized"
        self.write(self.session / "candidates/candidate-01/candidate.json", self.candidate)
        source, expected, _, _, launch = probe._context(self.session, "candidate-01", require_stopped=True)
        self.assertEqual(source, self.source)
        self.assertEqual(expected, self.expected)
        self.assertEqual(launch["status"], "completed")

    def test_failed_private_proof_cannot_become_supplemental_pass(self):
        probe.prepare(self.session, self.output)
        def captured(project, output, **kwargs):
            self.assertEqual(kwargs["probe"], "run_jump_land_brake")
            self.assertTrue(kwargs["normal_level"])
            self.assertTrue(kwargs["video"])
            output.mkdir()
            self.write(output / "probe.json", {"full_source_hashes_after":self.expected})
            return output
        with patch.object(probe, "seed_import_cache", return_value={"status":"seeded"}), patch.object(probe, "isolated_runtime", return_value=nullcontext()), patch.object(probe.movement_probe, "run_probe", side_effect=captured), patch.object(probe.movement_probe, "verify_probe", return_value={"status":"pass"}), patch.object(probe, "verify_runtime_receipt", return_value={"status":"fail"}):
            with self.assertRaisesRegex(ValueError, "Private desktop proof failed"):
                probe.run_prepared(self.output)
        self.assertFalse((self.output / "result.json").exists())
        failure = probe._read(self.output / "failure.json")
        self.assertEqual(failure["status"], "error")
        self.assertFalse(failure["canonical_experiment_acceptance"])
        self.assertEqual(probe.project_hashes(self.source), self.expected)


if __name__ == "__main__":
    unittest.main()
