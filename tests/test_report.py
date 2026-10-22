"""Artifact packaging and untrusted-input checks for the offline report."""

import json
from pathlib import Path
import re
import tempfile
import unittest

from kiroclops.report import generate_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_run(self, name="run", variant="reference", status="valid", **scenario):
        directory = self.root / name
        directory.mkdir()
        manifest = {
            "schema_version": 1, "game": "tiny" if variant == "reference" else "slay",
            "variant": variant, "fps": 60, "viewport": [640, 360], "frame_count": 240,
            "scenario": {"hold_ticks": 6, "start_tick": 30, "split": "calibration",
                         "repeat": 1, "spawn_offset": 0, **scenario},
            "provenance": {"opencv": "test-version", "capture": "test"},
        }
        analysis = {
            "status": status, "game": manifest["game"], "variant": variant,
            "scenario": manifest["scenario"], "fps": 60, "body_height_px": 20,
            "valid_ratio": 1, "events": {"takeoff_frame": 31, "apex_frame": 32, "landing_frame": 33},
            "metrics": {"height_body": 2, "rise_s": 1/60, "fall_s": 1/60, "input_latency_s": 1/60},
            "frames": [{"frame_index": index, "time_s": index/60, "x": 20,
                        "y": y, "score": .95, "valid": True, "height_body": height}
                       for index, y, height in [(31, 100, 0), (32, 60, 2), (33, 100, 0)]],
            "errors": [],
            "provenance": {"opencv_version": "5.0.0-test", "measurement": "rendered-pixels"},
            "annotation_validation": {"status": "pending", "control_point_count": 0},
        }
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        # Packaging tests do not pretend these bytes are an actual captured video.
        (directory / "video.mp4").write_bytes(b"report-packaging-test")
        return directory

    def embedded_data(self, page):
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_report_copies_portable_artifacts_and_preserves_comparison(self):
        runs = [self.make_run(variant, variant) for variant in ("reference", "before", "after")]
        profile = self.root / "fit.json"
        profile.write_text(json.dumps({"family": "hold_then_ballistic", "v0_body_s": 8,
                                       "gravity_body_s2": 20, "hold_ticks": 6,
                                       "fit_diagnostics": {"status": "candidate"}}), encoding="utf-8")
        comparison = self.root / "compare.json"
        comparison.write_text(json.dumps({"status": "failed", "passed": False}), encoding="utf-8")
        output = self.root / "report"
        result = generate_report(runs, output, profile, [comparison])
        self.assertEqual(result, output / "index.html")
        page = result.read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertEqual(data["comparisons"][0]["data"], {"status": "failed", "passed": False})
        self.assertEqual(data["profile"]["data"]["fit_diagnostics"]["status"], "candidate")
        for index, run in enumerate(runs):
            for name in ("video.mp4", "manifest.json", "analysis.json"):
                link = data["runs"][index]["links"][name]
                self.assertFalse(Path(link).is_absolute())
                self.assertEqual((output / link).read_bytes(), (run / name).read_bytes())
        self.assertNotIn("<script src=", page)
        self.assertNotIn("fetch(", page)
        self.assertNotIn(str(self.root), page)
        self.assertIn("Recordings: 3 · Measurement ready: 3", page)
        self.assertIn("5.0.0-test", page)
        self.assertIn("independent_validation", page)

    def test_untrusted_text_cannot_close_script_or_create_markup(self):
        run = self.make_run()
        attack = '</script><img src=x onerror="alert(1)">&\u2028'
        manifest = json.loads((run / "manifest.json").read_text())
        manifest["game"] = attack
        manifest["provenance"]["tool"] = attack
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"family": attack}), encoding="utf-8")
        page = generate_report([run], self.root / "report", profile).read_text(encoding="utf-8")
        self.assertNotIn(attack, page)
        self.assertNotIn('<img src=x', page)
        self.assertIn("&lt;/script&gt;&lt;img", page)
        self.assertIn("\\u003c/script\\u003e", page)
        data = self.embedded_data(page)
        self.assertEqual(data["runs"][0]["game"], attack)
        self.assertEqual(data["profile"]["data"]["family"], attack)

    def test_missing_artifacts_remain_pending_and_invalid_measurement_stays_invalid(self):
        pending = self.root / "pending"
        pending.mkdir()
        invalid = self.make_run("invalid", "before", "invalid")
        analysis = json.loads((invalid / "analysis.json").read_text())
        analysis["errors"] = ["tracking_lost"]
        analysis["metrics"]["height_body"] = float("nan")
        (invalid / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        page = generate_report([pending, invalid], self.root / "report").read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertEqual([run["status"] for run in data["runs"]], ["pending", "invalid"])
        self.assertIsNone(data["runs"][1]["analysis"]["metrics"]["height_body"])
        self.assertEqual(data["runs"][0]["links"], {})
        self.assertIn("Missing file: video.mp4", page)
        self.assertIn("tracking_lost", page)
        self.assertIn("there is no final pass/fail decision", page)

    def test_malformed_json_is_retained_with_visible_issue(self):
        run = self.make_run()
        (run / "analysis.json").write_text('{"status":', encoding="utf-8")
        page = generate_report(run, self.root / "report").read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertEqual(data["runs"][0]["status"], "invalid")
        self.assertIsNone(data["runs"][0]["analysis"])
        self.assertIn("Unreadable JSON: analysis.json", page)
        self.assertEqual((self.root / "report/assets/run-000/analysis.json").read_text(), '{"status":')

    def test_empty_report_and_missing_auxiliary_files_are_explicit(self):
        page = generate_report([], self.root / "empty", self.root / "absent-profile.json",
                               [self.root / "absent-comparison.json"]).read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertEqual(data["runs"], [])
        self.assertIn("No recordings have been added yet", page)
        self.assertIsNone(data["profile"]["href"])
        self.assertIn("Missing file: absent-comparison.json", page)

    def test_group_dimensions_and_normal_run_metadata_survive_packaging(self):
        runs = [self.make_run("calibration", hold_ticks=6),
                self.make_run("validation", "after", hold_ticks=6, split="validation", repeat=2, spawn_offset=32),
                self.make_run("normal", "after", normal_level=True)]
        page = generate_report(runs, self.root / "report").read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertFalse(data["runs"][0]["normal_level"])
        self.assertTrue(data["runs"][2]["normal_level"])
        self.assertEqual(data["runs"][1]["scenario"]["spawn_offset"], 32)
        self.assertEqual(data["runs"][1]["scenario"]["split"], "validation")
        self.assertIn("Normal-level recordings", page)

    def test_generic_comparison_and_template_markers_are_preserved(self):
        run = self.make_run()
        manifest = json.loads((run / "manifest.json").read_text())
        manifest["provenance"]["note"] = "__REPORT_DATA__"
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        comparison = self.root / "comparison.json"
        comparison.write_text(json.dumps([{"status": "pending"}, {"error": "__SUMMARY__"}]), encoding="utf-8")
        page = generate_report([run], self.root / "report", comparison_paths=comparison).read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertEqual(data["runs"][0]["manifest"]["provenance"]["note"], "__REPORT_DATA__")
        self.assertEqual(data["comparisons"][0]["data"], [{"status": "pending"}, {"error": "__SUMMARY__"}])
        self.assertIn('&quot;note&quot;: &quot;__REPORT_DATA__&quot;', page)
        self.assertIn("Held-out validation result pending", page)
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"validation_status": "pending"}), encoding="utf-8")
        for status, title in (("pass", "Held-out validation passed"),
                              ("fail", "Held-out validation did not pass"),
                              ("pending", "Held-out validation result pending")):
            with self.subTest(status=status):
                comparison.write_text(json.dumps({
                    "status": status,
                    "coverage": {"required_triplets": 12, "complete_triplets": 12},
                    "validation_scenarios": [
                        {"improvement_fraction": .9075, "after_rmse_body": .0743},
                        {"improvement_fraction": .9124, "after_rmse_body": .0697},
                    ],
                }), encoding="utf-8")
                page = generate_report([run], self.root / "report", profile,
                                       comparison_paths=comparison).read_text(encoding="utf-8")
                summary = re.search(r'<section class="result-card.*?</section>', page, re.S).group(0)
                self.assertIn(title, summary)
                self.assertIn("12/12 conditions", summary)
                self.assertIn("90.75–91.24%", summary)
                self.assertIn("0.0697–0.0743 body heights", summary)
                if status != "pass":
                    self.assertNotIn("Held-out validation passed", summary)

    def test_unmeasured_normal_video_is_not_a_pending_controlled_measurement(self):
        run = self.make_run("normal", "after", normal_level=True)
        (run / "analysis.json").unlink()
        page = generate_report([run], self.root / "report").read_text(encoding="utf-8")
        data = self.embedded_data(page)
        self.assertTrue(data["runs"][0]["normal_level"])
        self.assertNotIn("Missing file: analysis.json", page)
        self.assertIn("Measurement not applied", page)
        self.assertIn("Pending: 0 · Invalid: 0 · Normal-level recordings: 1", page)


if __name__ == "__main__":
    unittest.main()
