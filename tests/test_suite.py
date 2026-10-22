"""Evidence reuse and experiment-freeze boundaries; no engine is launched."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_suite import (ROLES, SUPPORTED_ACCEPTANCE, SUPPORTED_PLAN,
                               Suite, SuiteError, sha256, write_json)


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite = Suite(self.root)
        self.capture_patch = patch("scripts.run_suite.capture_run")
        self.capture = self.capture_patch.start()
        self.addCleanup(self.capture_patch.stop)
        self.addCleanup(self.temp.cleanup)
        write_json(self.root / "config/experiment.json", {**SUPPORTED_PLAN, "acceptance": SUPPORTED_ACCEPTANCE})
        sources = {"tiny": {"controller": "src/actor/Player.gd", "commit": "tiny-commit"},
                   "slay": {"controller": "source/characters/player/player.gd", "commit": "slay-commit"}}
        write_json(self.root / "config/sources.json", sources)
        for game, variant in ROLES:
            project = self.suite.project(game, variant)
            controller = project / sources[game]["controller"]
            self.write_file(controller, "# test controller")
            if game == "tiny":
                self.write_file(project / "src/actor/Actor.gd", "# test actor")
            else:
                self.write_file(project / "source/characters/player/state_machine/states/jump.gd", "# test jump")
            for path in self.suite.fixture_files(game, variant).values():
                self.write_file(path, "# test fixture")
        for name in ("analysis.py", "annotation.py", "comparison.py", "fitting.py", "capture.py"):
            self.write_file(self.root / "kiroclops" / name, "# test source fingerprint")

    @staticmethod
    def write_file(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def annotations(self):
        for game in ("tiny", "slay"):
            template = self.root / "templates" / game
            self.write_file(template / "frames/000000.png", "test hash fixture, not decoded by suite")
            write_json(self.root / "config" / f"{game}-annotation.json", {
                "schema_version": 1, "template_run": str(template), "template_frame": 0,
                "roi": [0, 0, 4, 4], "anchor_offset": [1, 1], "body_height_px": 12,
                "background_rois": [[10, 10, 4, 4]], "control_points": [], "control_events": [],
                "control_point_source": None, "control_event_source": None,
                "validation_status": "pending",
            })

    def make_run(self, scenario, *, media=True):
        game, variant, hold, offset, repeat = scenario
        run = self.suite.run_path(scenario)
        split = "calibration" if hold in (3, 24) else "validation"
        manifest = {
            "schema_version": 1, "status": "ok", "game": game, "variant": variant,
            "fps": 60, "frame_count": 240, "normal_level": False,
            "scenario": {"hold_ticks": hold, "start_tick": 30, "split": split,
                         "repeat": repeat, "spawn_offset": offset},
            "frames": [{"frame_index": i, "time_s": i / 60} for i in range(240)],
            "provenance": {
                "source_commit": f"{game}-commit",
                "controller_hashes": {key: sha256(path) for key, path in self.suite.controller_files(game, variant).items()},
                "capture_script_sha256": sha256(self.suite.fixture_files(game, variant)["kiroclops_capture/capture.gd"]),
                "fixture_hashes": {key: sha256(path) for key, path in self.suite.fixture_files(game, variant).items()},
            },
        }
        write_json(run / "manifest.json", manifest)
        if media:
            self.write_file(run / "video.mp4", "test packaging fixture")
            for index in range(240):
                self.write_file(run / "frames" / f"{index:06d}.png", "test frame presence fixture")
        annotation = self.root / "config" / f"{game}-annotation.json"
        if annotation.exists():
            analysis = {
                "status": "ok", "game": game, "variant": variant, "scenario": manifest["scenario"],
                "fps": 60, "body_height_px": 12, "valid_ratio": 1, "camera_valid": True,
                "events": {"takeoff_frame": 30, "apex_frame": 45, "landing_frame": 60},
                "metrics": {"height_body": 2, "rise_s": .25, "fall_s": .25}, "frames": [],
                "annotation_validation": {"status": "pending"},
                "provenance": {"manifest_sha256": sha256(run / "manifest.json"),
                               "annotation_sha256": sha256(annotation)},
            }
            write_json(run / "analysis.json", analysis)
        return run

    def profile(self):
        sources = [self.suite.run_path(("tiny", "reference", hold, 0, 1)) / "analysis.json" for hold in (3, 24)]
        write_json(self.suite.profile, {
            "family": "held_velocity", "v0_body_s": 8, "gravity_body_s2": 30, "hold_ticks": 10,
            "provenance": {"analysis_files": [{"path": str(path), "sha256": sha256(path)} for path in sources]},
        })

    def test_stage_matrices_are_complete_and_disjoint(self):
        calibration = self.suite.scenarios("calibration")
        validation = self.suite.scenarios("validation")
        self.assertEqual(len(calibration), 18)
        self.assertEqual(len(validation), 36)
        self.assertEqual(len(set(validation)), 36)
        self.assertEqual({s[2] for s in calibration}, {3, 24})
        self.assertEqual({s[2] for s in validation}, {8, 14})
        self.assertEqual({s[3] for s in validation}, {0, 24})
        self.capture.assert_not_called()

    def test_missing_annotation_explains_visual_setup_without_inventing_values(self):
        with self.assertRaisesRegex(SuiteError, "Visual annotation required") as caught:
            self.suite.annotation("tiny")
        self.assertIn("--anchor AX AY --body-height PX", str(caught.exception))
        self.assertIn("Independent control points are not generated automatically", str(caught.exception))
        self.assertFalse((self.root / "config/tiny-annotation.json").exists())

    def test_current_capture_can_be_reused_but_changed_fixture_cannot(self):
        scenario = ("tiny", "reference", 3, 0, 1)
        run = self.make_run(scenario)
        self.assertEqual(self.suite.verify_capture(scenario)["status"], "ok")
        fixture = self.suite.fixture_files("tiny", "reference")["project.godot"]
        self.write_file(fixture, "changed")
        with self.assertRaisesRegex(SuiteError, "Fixture has changed"):
            self.suite.verify_capture(scenario)
        self.assertTrue((run / "video.mp4").exists())
        self.capture.assert_not_called()

    def test_legacy_manifest_without_fixture_hashes_is_not_silently_reused(self):
        scenario = ("tiny", "reference", 3, 0, 1)
        run = self.make_run(scenario)
        manifest = json.loads((run / "manifest.json").read_text())
        del manifest["provenance"]["fixture_hashes"]
        write_json(run / "manifest.json", manifest)
        with self.assertRaisesRegex(SuiteError, "Fixture hash evidence is missing"):
            self.suite.verify_capture(scenario)

    def test_partial_capture_stops_stage_without_deletion_or_new_capture(self):
        run = self.suite.run_path(("tiny", "reference", 3, 0, 1))
        self.write_file(run / "engine.log", "original failure")
        with self.assertRaisesRegex(SuiteError, "will not be overwritten"):
            self.suite.calibration()
        self.assertEqual((run / "engine.log").read_text(), "original failure")
        self.capture.assert_not_called()

    def test_validation_never_starts_without_freeze(self):
        with self.assertRaisesRegex(SuiteError, "Run freeze first"):
            self.suite.validation()
        self.capture.assert_not_called()

    def test_fit_provenance_rejects_validation_even_with_matching_hash(self):
        self.annotations()
        for hold in (3, 24):
            self.make_run(("tiny", "reference", hold, 0, 1), media=False)
        path = self.suite.run_path(("tiny", "reference", 24, 0, 1)) / "analysis.json"
        report = json.loads(path.read_text())
        report["scenario"]["split"] = "validation"
        write_json(path, report)
        self.profile()
        with self.assertRaisesRegex(SuiteError, "reference calibration"):
            self.suite._profile_evidence()

    def test_freeze_allows_only_control_extensions_and_preserves_original_snapshot(self):
        self.annotations()
        for scenario in self.suite.scenarios("calibration"):
            self.make_run(scenario, media=False)
        self.profile()
        # Capture hashing/presence is tested above; this test isolates the freeze boundary.
        with patch.object(self.suite, "verify_capture", return_value={}):
            self.suite.freeze()
        original = self.suite.frozen.read_bytes()
        self.suite.verify_frozen()
        annotation_path = self.root / "config/tiny-annotation.json"
        annotation = json.loads(annotation_path.read_text())
        annotation["control_points"].append({"run_id": "independent-check", "frame_index": 45,
                                               "x": 12, "y": 20, "source": "assistant_visual"})
        annotation["control_events"].append({"run_id": "independent-check", "takeoff_frame": 30,
                                               "apex_frame": 45, "landing_frame": 60, "source": "human"})
        write_json(annotation_path, annotation)
        # Independent validation status can change without changing the measured trajectory.
        analysis_path = self.suite.run_path(("tiny", "reference", 3, 0, 1)) / "analysis.json"
        analysis = json.loads(analysis_path.read_text())
        analysis["annotation_validation"]["status"] = "pass"
        analysis["provenance"]["annotation_sha256"] = sha256(annotation_path)
        write_json(analysis_path, analysis)
        self.suite.verify_frozen()
        annotation["body_height_px"] = 13
        write_json(annotation_path, annotation)
        with self.assertRaisesRegex(SuiteError, "measurement settings"):
            self.suite.verify_frozen()
        with self.assertRaises(SuiteError):
            self.suite.freeze()
        self.assertEqual(self.suite.frozen.read_bytes(), original)
        self.capture.assert_not_called()

    def test_frozen_profile_change_blocks_validation_before_capture(self):
        self.annotations()
        for scenario in self.suite.scenarios("calibration"):
            self.make_run(scenario, media=False)
        self.profile()
        with patch.object(self.suite, "verify_capture", return_value={}):
            self.suite.freeze()
        profile = json.loads(self.suite.profile.read_text())
        profile["v0_body_s"] = 99
        write_json(self.suite.profile, profile)
        with self.assertRaisesRegex(SuiteError, "Frozen profile"):
            self.suite.validation()
        self.capture.assert_not_called()

    def test_empty_report_remains_pending(self):
        with patch("scripts.run_suite.generate_report", return_value=self.root / "artifacts/report/index.html"):
            self.suite.report()
        comparison = json.loads((self.root / "artifacts/comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(comparison["status"], "pending")
        self.assertFalse(comparison["success"])
        self.assertFalse(comparison["experiment_frozen"])
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
