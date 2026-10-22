"""Portable role/matrix/input contracts; no game or model process is launched."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.integration.storage import IntegrationError
from kiroclops.portable import measurement, specs, runtime


def jump_matrix(split="validation"):
    groups = []
    for role, peak in (("reference", 1.), ("before", 1.8), ("after", 1.)):
        group = []
        for scenario in specs.scenarios("jump", split):
            frames = []
            for index in range(240):
                phase = (index - 29) / 30
                height = peak * max(0., 4 * phase * (1 - phase)) if 29 <= index <= 59 else 0.
                frames.append({"frame_index": index, "physics_tick": 60 + index, "time_s": index / 60,
                               "valid": True, "height_body": height})
            group.append({"schema_version": 1, "status": "ok", "run_id": role + "-" + specs.run_name(scenario),
                "game": "tiny" if role == "reference" else "heart", "variant": role,
                "scenario": scenario, "fps": 60, "body_height_px": 20., "camera_valid": True,
                "valid_ratio": 1., "frames": frames,
                "metrics": {"height_body": peak, "rise_s": .25, "fall_s": .25, "input_latency_s": 0.},
                "events": {"takeoff_frame": 29, "apex_frame": 44, "landing_frame": 59},
                "errors": [], "annotation_validation": {"status": "pending", "points": []}})
        groups.append(group)
    return groups


class PortableMeasurementTests(unittest.TestCase):
    def test_planned_matrix_sizes_offsets_repeats_and_immutable_returns(self):
        for stage, expected in (("jump", 12), ("ground", 18)):
            with self.subTest(stage=stage):
                calibration = specs.scenarios(stage, "calibration")
                validation = specs.scenarios(stage, "validation")
                self.assertEqual(len(calibration), 6)
                self.assertEqual(len(validation), expected)
                self.assertEqual({s["spawn_offset"] for s in calibration}, {0})
                self.assertEqual({s["spawn_offset"] for s in validation}, {0, 24})
                self.assertEqual({s["repeat"] for s in validation}, {1, 2, 3})
                self.assertEqual(len({specs.run_name(s) for s in validation}), expected)
                calibration[0]["events"][0]["tick"] = 999
                self.assertEqual(specs.scenarios(stage, "calibration")[0]["events"][0]["tick"], 30)

    def test_target_record_does_not_alias_frozen_definition(self):
        target = specs.get_target()
        value = target.record()
        value["viewport"][0] = 640
        value["actions"]["jump"] = "ui_up"
        self.assertEqual(target.record()["viewport"], [320, 180])
        self.assertEqual(target.record()["actions"]["jump"], "jump")

    def test_jump_complete_matrix_passes_numerics_with_manual_still_pending(self):
        groups = jump_matrix()
        original = deepcopy(groups)
        result = measurement.compare_motion("jump", *groups, "validation")
        self.assertEqual(result["numerical_status"], "pass")
        self.assertEqual(result["required"], 12)
        self.assertEqual(result["measurement_status"], "pending")
        self.assertEqual(groups, original)

    def test_missing_and_duplicate_rows_are_rejected(self):
        for duplicate in (False, True):
            groups = jump_matrix()
            if duplicate:
                groups[2][-1] = deepcopy(groups[2][0])
            else:
                groups[2].pop()
            with self.subTest(duplicate=duplicate), self.assertRaises(IntegrationError):
                measurement.compare_motion("jump", *groups, "validation")

    def test_wrong_split_is_rejected(self):
        groups = jump_matrix()
        groups[2][0]["scenario"]["split"] = "calibration"
        with self.assertRaises(IntegrationError):
            measurement.compare_motion("jump", *groups, "validation")

    def test_wrong_game_or_variant_is_rejected(self):
        for role, key, value in ((0, "game", "heart"), (1, "game", "slay"),
                                 (2, "game", "tiny"), (2, "variant", "before")):
            groups = jump_matrix()
            groups[role][0][key] = value
            with self.subTest(role=role, key=key, value=value), self.assertRaises(IntegrationError):
                measurement.compare_motion("jump", *groups, "validation")

    def test_input_timing_cannot_hide_under_the_same_slot_id(self):
        for field, value in (("start_tick", 31), ("hold_ticks", 9)):
            groups = jump_matrix()
            groups[2][0]["scenario"][field] = value
            with self.subTest(field=field), self.assertRaises(IntegrationError):
                measurement.compare_motion("jump", *groups, "validation")
        groups = jump_matrix()
        groups[2][0]["scenario"]["events"][0]["tick"] = 31
        with self.assertRaises(IntegrationError):
            measurement.compare_motion("jump", *groups, "validation")

    def test_actual_physics_tick_offset_is_required(self):
        groups = jump_matrix()
        groups[2][0]["frames"][0]["physics_tick"] = 0
        with self.assertRaises(IntegrationError):
            measurement.compare_motion("jump", *groups, "validation")

    def test_bad_tracking_camera_frame_order_and_nonfinite_time_rejected(self):
        for mutate in (lambda r: r.update(valid_ratio=.94), lambda r: r.update(camera_valid=False),
                       lambda r: r["frames"][2].update(frame_index=1),
                       lambda r: r["frames"][0].update(time_s=float("nan"))):
            groups = jump_matrix()
            mutate(groups[2][0])
            with self.subTest(mutate=mutate), self.assertRaises((IntegrationError, ValueError)):
                measurement.compare_motion("jump", *groups, "validation")

    def test_zero_before_error_never_invents_validation_improvement(self):
        refs, _, after = jump_matrix()
        before = deepcopy(after)
        for report in before:
            report["variant"] = "before"
        result = measurement.compare_motion("jump", refs, before, after, "validation")
        self.assertFalse(result["pass"])
        self.assertTrue(all(row["improvement_fraction"] is None for row in result["scenario_checks"]))

    def test_calibration_has_no_improvement_requirement(self):
        refs, _, after = jump_matrix("calibration")
        before = deepcopy(after)
        for report in before:
            report["variant"] = "before"
        result = measurement.compare_motion("jump", refs, before, after, "calibration")
        self.assertTrue(result["pass"])
        self.assertEqual(result["required"], 6)

    def test_ground_delegates_registered_long_hold_speed_policy_and_keeps_18_slots(self):
        from tests.test_ground_measurement import matrix
        groups = matrix()
        for group in groups[1:]:
            for report in group:
                report["game"] = "heart"
                for index, frame in enumerate(report["frames"]):
                    frame["physics_tick"] = 60 + index
        for report in groups[0]:
            for index, frame in enumerate(report["frames"]):
                frame["physics_tick"] = 60 + index
        result = measurement.compare_motion("ground", *groups, "validation")
        self.assertEqual(result["required"], 18)
        self.assertTrue(all(row["speed_policy"] == "ground-speed-long-hold-v2" for row in result["scenario_checks"]))

    def test_reference_reanalysis_writes_only_new_output_paths(self):
        output = Path("new-results-only")
        with patch.object(measurement, "analyze", return_value={"status": "ok"}) as analyze:
            paths = measurement.reference_runs("jump", "calibration", output)
        self.assertEqual(len(paths), 6)
        for call in analyze.call_args_list:
            source, stage, target, annotation = call.args
            self.assertEqual(stage, "jump")
            self.assertTrue(str(source).endswith(str(Path("artifacts/runs") / ("tiny-reference-" + target.stem))))
            self.assertEqual(target.parent, output)
            self.assertNotEqual(source, target.parent)

    def test_historical_frozen_sources_are_still_current(self):
        registry = specs.ROOT / "artifacts/model-trials/autonomous-v4-cohort.json"
        if not registry.is_file():
            self.skipTest("Historical workspace evidence is not bundled into clean installs")
        self.assertEqual(len(specs.verify_legacy()), 61)

    def test_native_heart_manifest_matches_runtime_and_analysis_metadata_contract(self):
        run = specs.ARTIFACTS / "setup/runtime-native-03/h03-x0-r1"
        if not run.is_dir():
            self.skipTest("Optional native qualification evidence is not bundled")
        manifest = measurement.read_json(run / "manifest.json")
        recorded = measurement.read_json(run / "recording.json")
        self.assertEqual((manifest["schema_version"], manifest["game"], manifest["variant"]), (1, "heart", "before"))
        runtime.validate_recording(recorded, manifest["scenario"])
        self.assertEqual(manifest["scenario"]["hold_ticks"], 3)
        self.assertEqual(manifest["scenario"]["start_tick"], 30)
        self.assertEqual(manifest["frames"][30]["actions"], ["jump"])


if __name__ == "__main__":
    unittest.main()
