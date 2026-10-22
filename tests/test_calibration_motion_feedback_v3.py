"""Synthetic pixel-only calibration diagnostics; no games or source patches."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from kiroclops.ground.analysis import SCENARIOS, scenario_phases
from kiroclops.ground.fitting import RETENTION, LINEAR, simulate_profile
from kiroclops.integration import calibration_motion_feedback_v3 as feedback
from kiroclops.integration import storage


def profile():
    return {"family": RETENTION, "max_speed_body_s": 6., "accel_body_s2": 360., "retention_per_tick": 0.,
            "fps": 60, "mechanic": "ground", "status": "ok",
            "fit_diagnostics": {"rmse_body": 0., "ambiguous": False, "model_mismatch": False}}


def reports(variant, *, height=120, missing_first_tick=False, uniform_delay=0):
    result = []
    for sid in ("r06", "r48"):
        for repeat in (1, 2, 3):
            events = [{"tick": t, "action": action, "pressed": pressed} for t, action, pressed in SCENARIOS[sid]]
            scenario = {"id": sid, "split": "calibration", "repeat": repeat, "spawn_offset": 0,
                        "events": events, "frame_count": 240, "warmup_ticks": 60}
            # Closed-form constant speed, not the feedback hypothesis simulator.
            start = 30 + int(missing_first_tick)
            release = events[-1]["tick"]
            values = np.clip(np.arange(240) - uniform_delay - start + 1, 0, release - start) * .1
            raw = np.rint(values * height) + 80.5
            frames = [{"frame_index": i, "time_s": i / 60, "valid": True,
                       "x": float(raw[i]), "x_body": float((raw[i] - 80.5) / height)} for i in range(240)]
            result.append({"status": "ok", "game": "reference-game" if variant == "reference" else "target-game",
                           "variant": variant, "scenario": scenario, "fps": 60, "camera_valid": True,
                           "valid_ratio": 1., "body_height_px": height, "frames": frames})
    return result


class DiagnosticsTests(unittest.TestCase):
    def test_equal_motion_at_distinct_scales_does_not_advise_refinement(self):
        refs, target, model = reports("reference", height=60), reports("after", height=120), profile()
        saved = deepcopy((refs, target, model))
        result = feedback.calibration_diagnostics(refs, target, model)
        self.assertEqual(result["summary"]["refinement_advisory"], "within_reference_fit_plus_target_rounding")
        self.assertLess(result["summary"]["target_profile_rmse_body"], 1e-12)
        self.assertEqual(result["summary"]["target_rounding_margin_body"], 1 / 120)
        self.assertFalse(result["acceptance_modified"])
        self.assertEqual((refs, target, model), saved)

    def test_missing_first_integration_is_distinct_from_uniform_display_delay(self):
        result = feedback.calibration_diagnostics(reports("reference"), reports("after", missing_first_tick=True), profile())
        self.assertEqual(result["summary"]["refinement_advisory"], "refinement_recommended")
        winners = [r for r in result["lag_hypotheses"] if r["nominal_minimum"]]
        self.assertEqual([(r["hypothesis"], r["ticks"]) for r in winners], [("press_delay_release_fixed", 1)])
        self.assertLess(winners[0]["rmse_body"], 1e-12)
        self.assertGreater(result["summary"]["input_aligned_target_reference_rmse_body"], .09)
        self.assertIn("unidentified", result["lag_interpretation"]["causal_conclusion"])
        self.assertGreater(result["runs"][0]["target_vs_profile"]["phases"][1]["rmse_body"], .09)
        text = json.dumps(result)
        for forbidden in ("Idle", "player.gd", ".gd", "state_process", "move_and_slide"):
            self.assertNotIn(forbidden, text)

    def test_uniform_curve_delay_is_diagnosed_without_realigning_primary_error(self):
        result = feedback.calibration_diagnostics(reports("reference"), reports("after", uniform_delay=2), profile())
        winners = [r for r in result["lag_hypotheses"] if r["nominal_minimum"]]
        self.assertEqual([(r["hypothesis"], r["ticks"]) for r in winners], [("uniform_output_lag", 2)])
        self.assertGreater(result["summary"]["target_profile_rmse_body"], .1)
        self.assertFalse(result["acceptance_modified"])

    def test_negative_uniform_lag_and_quantization_ambiguity_are_retained(self):
        advanced = reports("after", uniform_delay=-1)
        # Preserve the required pre-input rest; only commanded motion is ahead.
        for report in advanced:
            for frame in report["frames"][:30]:
                frame["x"], frame["x_body"] = 80.5, 0.
        result = feedback.calibration_diagnostics(reports("reference"), advanced, profile())
        self.assertTrue(any(r["hypothesis"] == "uniform_output_lag" and r["ticks"] == -1 and r["nominal_minimum"]
                            for r in result["lag_hypotheses"]))
        coarse = feedback.calibration_diagnostics(reports("reference"), reports("after", height=3), profile())
        self.assertTrue(coarse["lag_interpretation"]["ambiguous"])
        self.assertGreater(sum(r["rounding_compatible_with_minimum"] for r in coarse["lag_hypotheses"]), 1)

    def test_idle_tail_does_not_dilute_phase_error(self):
        target = reports("after", missing_first_tick=True)
        before = feedback.calibration_diagnostics(reports("reference"), target, profile())
        changed = deepcopy(target)
        for report in changed:
            end = scenario_phases(report["scenario"])[-1]["end"]
            for frame in report["frames"][end:]:
                frame["x"] += 10000
                frame["x_body"] += 10000 / report["body_height_px"]
        after = feedback.calibration_diagnostics(reports("reference"), changed, profile())
        self.assertEqual(before["summary"], after["summary"])
        self.assertEqual(before["lag_hypotheses"], after["lag_hypotheses"])

    def test_mismatch_ambiguous_or_stale_reference_profile_never_claims_fit_confidence(self):
        for key in ("model_mismatch", "ambiguous", "rmse_body"):
            model = profile()
            model["fit_diagnostics"][key] = .5 if key == "rmse_body" else True
            result = feedback.calibration_diagnostics(reports("reference"), reports("after", missing_first_tick=True), model)
            self.assertEqual(result["summary"]["refinement_advisory"], "indeterminate")
            self.assertTrue(result["summary"]["confidence_limits"])

    def test_validation_incomplete_duplicate_wrong_scale_or_bad_tracking_are_rejected(self):
        for case in ("heldout", "missing", "duplicate", "scale", "bad_phase", "bad_origin"):
            target = reports("after")
            if case == "heldout": target[0]["scenario"]["split"] = "validation"
            if case == "missing": target.pop()
            if case == "duplicate": target[-1] = deepcopy(target[-2])
            if case == "scale": target[0]["body_height_px"] = 60
            if case == "bad_phase": target[0]["frames"][30]["valid"] = False
            if case == "bad_origin": target[0]["frames"][20]["valid"] = False
            with self.subTest(case=case), self.assertRaises(ValueError):
                feedback.calibration_diagnostics(reports("reference"), target, profile())

    def test_hypothesis_preserves_parameter_integration_and_bidirectional_release_semantics(self):
        scenario = reports("reference")[0]["scenario"]
        for family in (RETENTION, LINEAR):
            model = profile()
            model["family"] = family
            model["brake_body_s2"] = 20
            model["retention_per_tick"] = .8
            np.testing.assert_allclose(feedback._press_delay_prediction(model, scenario["events"], 0),
                                       simulate_profile(model, scenario), atol=1e-12)
        right = [{"tick": 3, "action": "right", "pressed": True}, {"tick": 9, "action": "right", "pressed": False}]
        left = [{**event, "action": "left"} for event in right]
        np.testing.assert_allclose(feedback._press_delay_prediction(profile(), right, 1),
                                   -feedback._press_delay_prediction(profile(), left, 1), atol=1e-12)
        cancelled = [{"tick": 3, "action": "left", "pressed": True}, {"tick": 4, "action": "left", "pressed": False}]
        np.testing.assert_allclose(feedback._press_delay_prediction(profile(), cancelled, 2), 0.)
        reversal = [right[0], {"tick": 9, "action": "left", "pressed": True}, right[1],
                    {"tick": 12, "action": "left", "pressed": False}]
        curve = feedback._press_delay_prediction(profile(), reversal, 1)
        self.assertAlmostEqual(curve[9], curve[8])
        self.assertLess(curve[10], curve[9])


class ContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "sessions" / "café-test"
        self.groups = {}
        for role, base in (("reference", "reference"), ("before", "baseline-runs"),
                           ("after", "candidates/candidate-01/runs")):
            self.groups[role] = []
            for report in reports(role, missing_first_tick=role == "after"):
                s = report["scenario"]
                run = self.directory / base / "calibration" / f'{s["id"]}-x0-r{s["repeat"]}'
                manifest = {"status": "ok", "fps": 60, "frame_count": 240, "variant": role, "scenario": s}
                storage.write_json(run / "manifest.json", manifest)
                report["provenance"] = {"manifest_sha256": storage.sha256(run / "manifest.json")}
                storage.write_json(run / "analysis.json", report)
                self.groups[role].append(run / "analysis.json")
        storage.write_json(self.directory / "session.json", {"reference": {"profile": profile()}})

    def test_prepare_and_evaluate_append_only_and_restore(self):
        original = {"eligible": True, "gates": {"speed": .1}, "status": "ok", "diagnostic_gates_pass": True}
        with patch.object(feedback.ground_worker, "_prepare_feedback", return_value=original) as prepare, \
             patch.object(feedback.comparison, "calibration_feedback", return_value=original) as evaluate:
            with feedback.enhanced_calibration_feedback(self.directory):
                prepared = feedback.ground_worker._prepare_feedback(SimpleNamespace(directory=self.directory),
                    {"runs": [p.parent for p in self.groups["reference"]], "profile": profile()},
                    [p.parent for p in self.groups["before"]], lambda: None)
                result = feedback.comparison.calibration_feedback(*(self.groups[r] for r in ("reference", "before", "after")))
                self.assertEqual({k: v for k, v in result.items() if k != "motion_diagnostics"}, original)
                self.assertEqual({k: v for k, v in prepared.items() if k != "motion_diagnostics"}, original)
                self.assertEqual(result["motion_diagnostics"]["summary"]["refinement_advisory"], "refinement_recommended")
                self.assertEqual(len(result["motion_diagnostics"]["input_file_sha256"]), 24)
                self.assertTrue(result["eligible"])
                self.assertNotIn("motion_diagnostics", original)
            self.assertIs(feedback.ground_worker._prepare_feedback, prepare)
            self.assertIs(feedback.comparison.calibration_feedback, evaluate)

    def test_bad_path_rejected_before_any_heldout_analysis_read(self):
        reads = []
        original = feedback.read_json
        bad = deepcopy(self.groups["after"])
        bad[0] = self.directory / "candidates/candidate-01/runs/validation/l30-x0-r1/analysis.json"
        def track(path):
            reads.append(str(path))
            return original(path)
        with patch.object(feedback, "read_json", side_effect=track), \
             patch.object(feedback.comparison, "calibration_feedback") as legacy:
            with feedback.enhanced_calibration_feedback(self.directory), self.assertRaises(ValueError):
                feedback.comparison.calibration_feedback(self.groups["reference"], self.groups["before"], bad)
            legacy.assert_not_called()
        self.assertFalse(any("validation" in path for path in reads))

    def test_context_restores_on_exception_and_rejects_overlap(self):
        original = feedback.comparison.calibration_feedback
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with feedback.enhanced_calibration_feedback(self.directory):
                with self.assertRaises(storage.IntegrationError):
                    with feedback.enhanced_calibration_feedback(self.directory):
                        self.fail("Nested")
                raise RuntimeError("failure")
        self.assertIs(feedback.comparison.calibration_feedback, original)

    def test_manifest_or_full_analysis_hash_mismatch_is_rejected(self):
        path = self.groups["after"][0]
        data = storage.read_json(path)
        data["provenance"]["manifest_sha256"] = "stale"
        storage.write_json(path, data)
        with self.assertRaisesRegex(ValueError, "native capture"):
            feedback._supplement(self.directory, self.groups["reference"], self.groups["after"], profile(), "after")


if __name__ == "__main__":
    unittest.main()
